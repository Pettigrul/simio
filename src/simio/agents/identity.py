"""Identity Agent: who controls / represents the page?

Deterministic and fully traceable: every fact points to an evidence-catalog reference.
Facts (observed) and inferences (association reasoning) are kept separate.

``CONFIRMED_COBAN`` requires a match by CNPJ in an *authoritative* registry supplied by the
operator (e.g. the contracting institution's open-data list of correspondents published under
Res. CMN 4.935/2021 art. 21, or an internal base). Advertising the brand's products is never
enough to infer a correspondent relationship.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from simio.agents.evidence import (
    CNPJ_RE,
    EMAIL_RE,
    LABELLED_NAME_RE,
    LEGAL_NAME_RE,
    PageSignals,
    cnpj_is_valid,
    format_cnpj,
    normalize_cnpj,
)
from simio.brands import BrandProfile, fold
from simio.core.models import (
    Evidence,
    EvidenceItem,
    IdentityAttribution,
    IdentityClass,
    IdentityFact,
    Level,
    RegistryMatch,
)
from simio.core.urls import hostname_of, registrable_domain

SOCIAL_RE = re.compile(r"(?i)https?://(?:www\.)?(instagram|facebook|linkedin|tiktok|youtube)\.com/[^\s<>\"]+")


# --------------------------------------------------------------------------------- registry
@dataclass
class CobanRegistry:
    """Operator-supplied list of authorised correspondents (CSV).

    Required column: ``cnpj``. Optional: ``nome``/``name``, ``contratante``, ``fonte``,
    ``data_referencia``. ``authoritative`` must only be True for an official/approved source.
    """

    records: dict[str, dict[str, str]] = field(default_factory=dict)
    source: str = "none"
    authoritative: bool = False

    @classmethod
    def load(cls, path: Path | None, authoritative: bool = True) -> CobanRegistry:
        if path is None:
            return cls()
        if not path.is_file():
            raise FileNotFoundError(f"COBAN registry not found: {path}")
        recs: dict[str, dict[str, str]] = {}
        text = path.read_text(encoding="utf-8-sig")
        first = text.splitlines()[0] if text else ""
        delimiter = ";" if first.count(";") > first.count(",") else ","
        for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
            norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            cnpj = normalize_cnpj(norm.get("cnpj", ""))
            if len(cnpj) == 14:
                recs[cnpj] = norm
        return cls(records=recs, source=path.name, authoritative=authoritative)

    @property
    def loaded(self) -> bool:
        return bool(self.records)

    def match_cnpj(self, cnpj: str) -> dict[str, str] | None:
        return self.records.get(normalize_cnpj(cnpj))

    def match_name(self, name: str) -> dict[str, str] | None:
        target = fold(name).strip()
        if len(target) < 6:
            return None
        for rec in self.records.values():
            n = fold(rec.get("nome") or rec.get("name") or rec.get("razao_social") or "")
            if n and (n == target):
                return rec
        return None


# --------------------------------------------------------------------------------- agent
class IdentityAgent:
    def __init__(self, brand: BrandProfile, registry: CobanRegistry) -> None:
        self.brand = brand
        self.registry = registry
        self._institution_cnpjs = {normalize_cnpj(c) for c in brand.institution_cnpjs}

    def _is_institution_context(self, context: str) -> bool:
        ctx = fold(context)
        return any(fold(n) in ctx for n in self.brand.institution_names)

    def attribute(
        self, candidate_id: str, ev: Evidence, catalog: list[EvidenceItem], signals: PageSignals
    ) -> IdentityAttribution:
        url = ev.final_url or ev.original_url
        host = hostname_of(url)
        reg_domain = registrable_domain(host)
        facts: list[IdentityFact] = [IdentityFact(kind="domain", value=reg_domain, evidence_ref="E-URL")]
        inferences: list[str] = []
        limitations: list[str] = []

        if not ev.usable:
            limitations.append("coleta sem conteúdo utilizável; identidade não pôde ser examinada")
            return IdentityAttribution(
                candidate_id=candidate_id,
                classification=IdentityClass.UNKNOWN_IDENTITY,
                entity_key=f"domain:{reg_domain}",
                display_name=None,
                facts=facts,
                inferences=["sem evidência de identidade; associação limitada ao domínio"],
                attribution_confidence=Level.NONE,
                limitations=limitations,
                rationale="Conteúdo não coletado; nenhuma atribuição possível além do domínio.",
            )

        # ---- CNPJs: owner vs. referenced institution ---------------------------------
        owner_cnpjs: list[str] = []
        institution_refs: list[str] = []
        invalid: list[str] = []
        for item in catalog:
            prev_end = 0
            for m in CNPJ_RE.finditer(item.content.upper()):
                raw = m.group(1)
                if sum(ch.isdigit() for ch in raw) < 8:
                    continue
                cnpj = format_cnpj(raw)
                valid = cnpj_is_valid(raw)
                # Context = text since the previous CNPJ (max 120 chars) so labels don't leak across.
                ctx = item.content[max(prev_end, m.start() - 120) : m.end()]
                prev_end = m.end()
                facts.append(IdentityFact(kind="cnpj", value=cnpj, evidence_ref=item.ref, context=ctx, valid=valid))
                if not valid:
                    invalid.append(cnpj)
                    continue
                if normalize_cnpj(cnpj) in self._institution_cnpjs or (
                    self._is_institution_context(ctx) and not self.registry.match_cnpj(cnpj)
                ):
                    if cnpj not in institution_refs:
                        institution_refs.append(cnpj)
                elif cnpj not in owner_cnpjs:
                    owner_cnpjs.append(cnpj)
        if invalid:
            inferences.append(f"CNPJ(s) com dígito verificador inválido ignorados: {', '.join(invalid)}")
        if institution_refs:
            inferences.append(
                "CNPJ(s) citados em contexto da própria instituição foram tratados como referência "
                f"à contratante, não como titular da página: {', '.join(institution_refs)}"
            )

        # ---- names, contacts, declarations -------------------------------------------
        legal_names: list[str] = []
        for item in catalog:
            if item.kind in ("screenshot", "network"):
                continue
            for m in LABELLED_NAME_RE.finditer(item.content):
                kind = "legal_name" if "raz" in m.group(1).lower() else "trade_name"
                val = m.group(2).strip(" .,-")
                facts.append(IdentityFact(kind=kind, value=val, evidence_ref=item.ref))
                if kind == "legal_name":
                    legal_names.append(val)
            if item.location in ("footer", "page") or item.kind == "text":
                for m in LEGAL_NAME_RE.finditer(item.content):
                    val = m.group(1).strip(" .,-")
                    if not self._is_institution_context(val) and val not in legal_names:
                        legal_names.append(val)
                        facts.append(IdentityFact(kind="legal_name", value=val, evidence_ref=item.ref))
            for m in EMAIL_RE.finditer(item.content):
                facts.append(IdentityFact(kind="email", value=m.group(0), evidence_ref=item.ref))
            for m in SOCIAL_RE.finditer(item.content):
                facts.append(IdentityFact(kind="social_profile", value=m.group(0)[:200], evidence_ref=item.ref))
        site_name = ev.page.meta.get("og:site_name") if ev.page else None
        if site_name:
            facts.append(
                IdentityFact(
                    kind="trade_name",
                    value=site_name,
                    evidence_ref="E-META-2" if any(i.ref == "E-META-2" for i in catalog) else "E-TITLE",
                )
            )
        for h in signals.phones[:10]:
            facts.append(IdentityFact(kind="phone", value=h.value, evidence_ref=h.ref))
        for h in signals.whatsapp_contacts[:5]:
            facts.append(IdentityFact(kind="whatsapp", value=h.value, evidence_ref=h.ref))
        for h in signals.disclosures[:5]:
            facts.append(
                IdentityFact(kind="correspondent_declaration", value=h.value, evidence_ref=h.ref, context=h.context)
            )
        for h in signals.partnership[:5]:
            facts.append(
                IdentityFact(kind="partnership_declaration", value=h.value, evidence_ref=h.ref, context=h.context)
            )
        facts = _dedupe_facts(facts)

        # ---- registry lookup -----------------------------------------------------------
        registry_match: RegistryMatch | None = None
        for c in owner_cnpjs:
            rec = self.registry.match_cnpj(c)
            if rec:
                registry_match = RegistryMatch(
                    source=self.registry.source,
                    matched_on="cnpj",
                    record=rec,
                    authoritative=self.registry.authoritative,
                )
                break
        if registry_match is None:
            for name in legal_names:
                rec = self.registry.match_name(name)
                if rec:
                    registry_match = RegistryMatch(
                        source=self.registry.source, matched_on="name", record=rec, authoritative=False
                    )
                    break
        if not self.registry.loaded:
            limitations.append(
                "nenhum cadastro autorizado de correspondentes carregado; CONFIRMED_COBAN indisponível nesta execução"
            )

        # ---- classification -----------------------------------------------------------
        declares_correspondent = bool(signals.disclosures)
        mentions_partner = bool(signals.partnership)
        identified = bool(owner_cnpjs or legal_names)
        if len(owner_cnpjs) > 1:
            inferences.append(
                f"{len(owner_cnpjs)} CNPJs distintos na página: pode ser diretório, marketplace "
                "ou grupo de empresas; associação ao responsável é incerta"
            )

        if registry_match and registry_match.matched_on == "cnpj" and registry_match.authoritative:
            cls = IdentityClass.CONFIRMED_COBAN
            conf = Level.HIGH if len(owner_cnpjs) == 1 else Level.MEDIUM
            rationale = (
                f"CNPJ {owner_cnpjs[0] if owner_cnpjs else ''} observado na página consta do "
                f"cadastro autorizado '{registry_match.source}'."
            )
            inferences.append("associação página→CNPJ baseada na publicação do CNPJ na própria página")
        elif registry_match:
            cls = IdentityClass.POSSIBLE_COBAN
            conf = Level.LOW
            rationale = (
                "Correspondência apenas por nome (ou fonte não autoritativa) no cadastro; requer confirmação por CNPJ."
            )
        elif declares_correspondent and identified:
            cls = IdentityClass.POSSIBLE_COBAN
            conf = Level.MEDIUM
            rationale = (
                "A página declara atuação como correspondente/prestador e identifica a empresa, "
                "mas não houve confirmação em cadastro autorizado."
            )
        elif declares_correspondent or (mentions_partner and identified):
            cls = IdentityClass.POSSIBLE_COBAN
            conf = Level.LOW
            rationale = (
                "Há declaração de correspondente ou de parceria, sem identificação suficiente "
                "ou sem confirmação cadastral."
            )
        elif identified:
            cls = IdentityClass.OTHER_THIRD_PARTY
            conf = Level.MEDIUM if len(owner_cnpjs) == 1 else Level.LOW
            rationale = (
                "Empresa identificada na página sem declaração de relação de correspondente; "
                "anunciar produtos da marca não comprova vínculo."
            )
        else:
            cls = IdentityClass.UNKNOWN_IDENTITY
            conf = Level.NONE if not facts[1:] else Level.LOW
            rationale = "Nenhum CNPJ ou razão social identificáveis no conteúdo coletado."

        if owner_cnpjs:
            entity_key = f"cnpj:{normalize_cnpj(owner_cnpjs[0])}"
            basis = "cnpj publicado na página"
        else:
            entity_key = f"domain:{reg_domain}"
            basis = "domínio (sem CNPJ)"
            inferences.append(
                "entidade agrupada pelo domínio registrável; um mesmo estabelecimento "
                "pode ter outros domínios não associados"
            )
        display = (legal_names[0] if legal_names else None) or site_name
        if registry_match and not display:
            display = registry_match.record.get("nome") or registry_match.record.get("name")
        inferences.append(f"base da associação: {basis}")
        return IdentityAttribution(
            candidate_id=candidate_id,
            classification=cls,
            entity_key=entity_key,
            display_name=display,
            owner_cnpjs=owner_cnpjs,
            referenced_institution_cnpjs=institution_refs,
            facts=facts,
            inferences=inferences,
            registry_match=registry_match,
            attribution_confidence=conf,
            limitations=limitations,
            rationale=rationale,
        )


def _dedupe_facts(facts: list[IdentityFact]) -> list[IdentityFact]:
    seen: set[tuple[str, str]] = set()
    out = []
    for f in facts:
        key = (f.kind, f.value.lower())
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
