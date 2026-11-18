"""Discovery Agent: formulates investigative hypotheses, runs the available connectors within
the query budget, deduplicates and prioritises candidate pages."""

from __future__ import annotations

import itertools
import logging
from collections import OrderedDict
from dataclasses import dataclass, field

from simio.brands import BrandProfile
from simio.core.models import DiscoveryHit, DiscoveryOutcome, QueryRecord, SearchHypothesis
from simio.core.urls import hostname_of, normalize_url, registrable_domain
from simio.providers.llm import BudgetExceeded
from simio.providers.search import Connector
from simio.security.url_policy import PolicyViolation, UrlPolicy

log = logging.getLogger(__name__)

# A connector is disabled for the rest of the run after this many consecutive errors.
MAX_CONSECUTIVE_ERRORS = 3


def build_hypotheses(brand: BrandProfile) -> list[SearchHypothesis]:
    """Investigative hypotheses (sec. 4.2). Each yields a natural-language grounding prompt
    and keyword queries for classic search APIs."""
    name = brand.display_name
    excl = " ".join(f"-site:{d}" for d in brand.official_domains)
    hs: list[SearchHypothesis] = []

    hs.append(
        SearchHypothesis(
            id="H1-identity",
            description="Terceiros que usam a marca como identidade comercial principal",
            grounding_prompt=(
                f"sites de empresas independentes no Brasil que usam a marca {name} como "
                f"nome ou identidade principal do próprio site, sem serem o {name}"
            ),
            keyword_queries=[f'"{name}" financiamento loja {excl}', f'intitle:"{name}" {excl}'],
        )
    )
    for product in brand.products[:4]:
        hs.append(
            SearchHypothesis(
                id=f"H2-product:{product}",
                description="Páginas de produto com aparência de canal institucional",
                grounding_prompt=(
                    f"páginas de {product} {name} mantidas por lojas, promotoras ou "
                    f"correspondentes que se parecem com o site do banco"
                ),
                keyword_queries=[f'"{product}" "{name}" {excl}'],
            )
        )
    hs.append(
        SearchHypothesis(
            id="H3-service",
            description="Atendimento ou simulação apresentados como do banco",
            grounding_prompt=(
                f"sites que oferecem simulação ou atendimento de financiamento {name} por "
                f"WhatsApp ou telefone apresentando-se como atendimento do {name}"
            ),
            keyword_queries=[f'"simulação" "{name}" whatsapp {excl}', f'"atendimento {name}" whatsapp {excl}'],
        )
    )
    hs.append(
        SearchHypothesis(
            id="H4-official-language",
            description="Uso de expressões de oficialidade",
            grounding_prompt=(
                f"páginas que se descrevem como 'canal oficial', 'site oficial' ou "
                f"'central de atendimento' do {name} mas não pertencem ao banco"
            ),
            keyword_queries=[
                f'"{name}" "canal oficial" {excl}',
                f'"{name}" "site oficial" {excl}',
                f'"central de atendimento" "{name}" {excl}',
            ],
        )
    )
    for etype, region in itertools.islice(itertools.product(brand.establishment_types, brand.regions), 0, None, 7):
        hs.append(
            SearchHypothesis(
                id=f"H5-establishment:{etype}:{region}",
                description="Estabelecimentos cuja identidade não está evidente",
                grounding_prompt=(
                    f"{etype} em {region} que divulga financiamento {name} em seu site; "
                    f"identifique páginas onde não fica claro quem é a empresa"
                ),
                keyword_queries=[f'{etype} {region} "{name}" financiamento {excl}'],
            )
        )
    for platform in brand.platforms:
        hs.append(
            SearchHypothesis(
                id=f"H6-platform:{platform}",
                description="Canais de contato/plataformas usando a marca",
                grounding_prompt=(
                    f"perfis ou páginas no {platform} que oferecem financiamento ou "
                    f"crédito usando a marca {name} como se fossem do banco"
                ),
                keyword_queries=[f'"{name}" {platform} financiamento atendimento {excl}'],
            )
        )
    return hs


def urlscan_queries(brand: BrandProfile) -> list[str]:
    titles = " OR ".join(f'page.title:"{t}"' for t in brand.strong_terms[:3])
    not_official = " AND ".join(f"NOT page.domain:{d}" for d in brand.official_domains) or "*"
    return [f"({titles}) AND {not_official}"]


@dataclass
class _ConnectorState:
    connector: Connector
    errors_in_row: int = 0
    disabled: str | None = None
    executed: int = 0
    succeeded: int = 0


@dataclass
class PlannedQuery:
    connector: str
    hypothesis: SearchHypothesis
    query: str


@dataclass
class DiscoveryAgent:
    brand: BrandProfile
    connectors: list[Connector]
    policy: UrlPolicy
    max_queries: int = 40
    diagnostics: list[str] = field(default_factory=list)

    def plan(self, available: list[Connector]) -> list[PlannedQuery]:
        """Interleave hypotheses across connectors so a small budget still diversifies."""
        hyps = build_hypotheses(self.brand)
        per_connector: list[list[PlannedQuery]] = []
        for conn in available:
            plans: list[PlannedQuery] = []
            if conn.name == "grounding":
                plans = [PlannedQuery(conn.name, h, h.grounding_prompt) for h in hyps]
            elif conn.name == "urlscan":
                h = SearchHypothesis(
                    id="H7-urlscan", description="Páginas escaneadas com a marca no título", grounding_prompt=""
                )
                plans = [PlannedQuery(conn.name, h, q) for q in urlscan_queries(self.brand)]
            else:
                plans = [PlannedQuery(conn.name, h, q) for h in hyps for q in h.keyword_queries]
            per_connector.append(plans)
        out: list[PlannedQuery] = []
        for batch in itertools.zip_longest(*per_connector):
            out.extend(p for p in batch if p is not None)
        return out[: self.max_queries]

    async def run(self, imported: list[DiscoveryHit] | None = None, on_query=None) -> DiscoveryOutcome:  # type: ignore[no-untyped-def]
        outcome = DiscoveryOutcome()
        outcome.hits.extend(imported or [])
        if imported:
            outcome.sources_used.append("import")
            outcome.diagnostics.append(f"{len(imported)} URL(s) imported manually")

        available: list[Connector] = []
        for conn in self.connectors:
            ok, msg = conn.available()
            outcome.diagnostics.append(msg)
            if ok:
                available.append(conn)
        if not available:
            if not imported:
                outcome.diagnostics.append(
                    "NO automatic discovery mechanism is available. Configure Vertex AI "
                    "(SIMIO_GCP_PROJECT + SIMIO_ANALYSIS_MODEL) for Google Search Grounding, or an "
                    "optional connector key (SIMIO_BRAVE_API_KEY / SIMIO_SERPER_API_KEY / "
                    "SIMIO_URLSCAN_API_KEY), or import URLs with --urls-file / --urls."
                )
            return outcome

        states = {c.name: _ConnectorState(c) for c in available}
        for planned in self.plan(available):
            st = states[planned.connector]
            if st.disabled:
                continue
            try:
                hits, record = await st.connector.run(planned.hypothesis, planned.query)
            except BudgetExceeded as exc:
                outcome.diagnostics.append(f"{planned.connector}: stopped, {exc}")
                st.disabled = "budget"
                continue
            st.executed += 1
            outcome.queries.append(record)
            if on_query is not None:
                on_query(record, hits)
            outcome.hits.extend(hits)
            if record.status == "ERROR":
                st.errors_in_row += 1
                ok, msg = st.connector.available()
                if not ok or st.errors_in_row >= MAX_CONSECUTIVE_ERRORS:
                    st.disabled = record.error or msg
                    outcome.diagnostics.append(
                        f"{planned.connector}: disabled for this run -> fallback to remaining sources ({st.disabled})"
                    )
            else:
                st.errors_in_row = 0
                st.succeeded += 1
        for name, st in states.items():
            if st.succeeded:
                outcome.sources_used.append(name)
        return outcome


# --------------------------------------------------------------------------------------
# Deduplication and prioritisation
# --------------------------------------------------------------------------------------
@dataclass
class CandidateGroup:
    normalized_url: str
    url: str  # representative original URL (first seen)
    hostname: str
    registrable_domain: str
    hits: list[DiscoveryHit]
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    excluded_reason: str | None = None


def deduplicate(
    hits: list[DiscoveryHit], policy: UrlPolicy
) -> tuple[list[CandidateGroup], list[tuple[DiscoveryHit, str]]]:
    """Group hits by normalised URL. Returns (groups, rejected[(hit, reason)])."""
    groups: OrderedDict[str, CandidateGroup] = OrderedDict()
    rejected: list[tuple[DiscoveryHit, str]] = []
    for hit in hits:
        try:
            policy.check_static(hit.original_url)
            norm = normalize_url(hit.original_url)
        except (PolicyViolation, ValueError) as exc:
            rejected.append((hit, f"invalid/blocked URL: {exc}"))
            continue
        host = hostname_of(norm)
        if norm not in groups:
            groups[norm] = CandidateGroup(norm, hit.original_url, host, registrable_domain(host), [])
        groups[norm].hits.append(hit)
    return list(groups.values()), rejected


def prioritise(groups: list[CandidateGroup], brand: BrandProfile) -> list[CandidateGroup]:
    """Deterministic pre-collection score. It only orders the queue; it is not a finding."""
    by_domain: dict[str, int] = {}
    for g in groups:
        by_domain[g.registrable_domain] = by_domain.get(g.registrable_domain, 0) + 1
    for g in groups:
        if brand.is_official(g.hostname):
            g.excluded_reason = "official institutional domain"
            continue
        score, reasons = 0.0, []
        if pat := brand.domain_brand_match(g.hostname):
            score += 3
            reasons.append(f"brand pattern in domain ({pat})")
        text = " ".join(filter(None, [*(h.title or "" for h in g.hits), *(h.snippet or "" for h in g.hits)]))
        if brand.strong_mentions(text):
            score += 2
            reasons.append("brand mentioned in title/snippet")
        if brand.official_claims(text):
            score += 3
            reasons.append("official-channel language in title/snippet")
        if brand.disclosures(text):
            score -= 1
            reasons.append("correspondent disclosure in snippet")
        sources = {h.source for h in g.hits}
        hyps = {h.hypothesis_id for h in g.hits if h.hypothesis_id}
        if len(sources) > 1 or len(hyps) > 1:
            score += min(2, len(sources) + len(hyps) - 2) + 0.5
            reasons.append(f"seen by {len(sources)} source(s) / {len(hyps)} hypothesis(es)")
        if any(h.source.startswith("import") for h in g.hits):
            score += 5
            reasons.append("manually imported by operator")
        if brand.is_deprioritized(g.hostname):
            score -= 4
            reasons.append("news/review/regulator domain (de-prioritised)")
        if g.normalized_url.rstrip("/").count("/") <= 2:
            score += 0.5
            reasons.append("home page")
        g.score, g.reasons = score, reasons
    kept = [g for g in groups if g.excluded_reason is None]
    kept.sort(key=lambda g: (-g.score, g.normalized_url))
    return kept + [g for g in groups if g.excluded_reason is not None]


def summarize_queries(records: list[QueryRecord]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in records:
        out[f"{r.source}:{r.status}"] = out.get(f"{r.source}:{r.status}", 0) + 1
    return out
