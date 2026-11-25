"""Google Search Grounding connector (Gemini on Vertex AI).

The model is asked an investigative question with the ``google_search`` tool enabled.
URLs are taken **exclusively** from ``grounding_metadata.grounding_chunks`` -- never from the
model's free text -- so no URL is invented. Grounding redirect URIs
(``vertexaisearch.cloud.google.com/grounding-api-redirect/...``) are resolved with a single
non-following request and the target is validated by the URL policy.

Grounding is not an exhaustive web index; results are a sample guided by the hypotheses.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import unquote, urljoin

import httpx

from simio.core.models import DiscoveryHit, QueryRecord, SearchHypothesis
from simio.core.urls import hostname_of
from simio.providers.llm import BudgetExceeded, LLMGateway, LLMUnavailable, TransientLLMError
from simio.security.url_policy import PolicyViolation, UrlPolicy

log = logging.getLogger(__name__)

REDIRECT_HOSTS = frozenset({"vertexaisearch.cloud.google.com"})
SEARCH_RESULT_HOSTS = frozenset({"www.google.com", "google.com"})
_YAHOO_RESULT_RE = re.compile(r'/RU=([^/]+)/RK=\d+/RS=[^"\']+["\'][^>]*>(.*?)</a>', re.DOTALL)
_SOCIAL_DOMAINS = ("instagram.com/", "facebook.com/", "tiktok.com/", "linkedin.com/")

GROUNDING_INSTRUCTIONS = (
    "Você está apoiando uma auditoria de conformidade de correspondentes bancários (Resolução CMN "
    "nº 4.935/2021) e proteção de marca. Use a busca do Google para encontrar páginas públicas na "
    "web brasileira e em redes sociais que correspondam especificamente à hipótese abaixo.\n"
    "Priorize encontrar:\n"
    "- sites e landing pages de correspondentes bancários, promotoras de crédito, lojas/revendas de "
    "veículos e empresas de energia solar que utilizem a marca ou o logotipo do banco em destaque, "
    "ofereçam simuladores próprios, promovam feirões ou omitam no site sua razão social, CNPJ e a "
    "declaração obrigatória de correspondente bancário;\n"
    "- perfis comerciais, páginas e publicações de terceiros em redes sociais que usem a marca como "
    "se fossem canais ou representantes oficiais.\n"
    "Exclua das buscas e não retorne:\n"
    "- domínios e perfis oficiais do próprio grupo (bv.com.br, bancobv.com.br, bvfinanceira.com.br, "
    "bvinvestimentos.com.br, napista.com.br, bvopen.com.br, meufinanciamentosolar.com.br);\n"
    "- notícias de imprensa, reclamações (Reclame Aqui), processos judiciais (Jusbrasil/Tribunais), "
    "órgãos públicos (.gov.br), arquivos PDF avulsos de leilão e guias telefônicos/cadastrais que "
    "apenas listam o CNPJ/endereço da própria BV Financeira S.A.\n"
    "Para cada página encontrada, descreva brevemente como ela utiliza a marca e se exibe ou omite "
    "sua razão social, CNPJ e condição de correspondente.\n\n"
    "Hipótese: "
)


class GroundingConnector:
    name = "grounding"

    def __init__(
        self,
        gateway: LLMGateway | None,
        model: str | None,
        policy: UrlPolicy,
        exclude_domains: list[str],
        http_timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.gateway = gateway
        self.model = model
        self.policy = policy
        self.exclude_domains = exclude_domains
        self.http_timeout = http_timeout
        self.transport = transport
        self.disabled_reason: str | None = None

    def available(self) -> tuple[bool, str]:
        if self.gateway is None:
            return False, "grounding: Vertex AI not configured (SIMIO_GCP_PROJECT / model) or offline"
        if not self.model:
            return False, "grounding: no discovery/analysis model configured"
        if self.disabled_reason:
            return False, f"grounding: disabled after error: {self.disabled_reason}"
        return True, f"grounding: model={self.model}"

    async def resolve(self, uri: str) -> tuple[str | None, str | None]:
        """Resolve grounding redirect URIs. Returns (url, error)."""
        host = hostname_of(uri)
        if host in SEARCH_RESULT_HOSTS:
            return None, "search-results page reference (not a target page)"
        if host not in REDIRECT_HOSTS:
            return uri, None
        try:
            await self.policy.acheck(uri)
            kwargs: dict = {"timeout": self.http_timeout, "follow_redirects": False}
            if self.transport is not None:
                kwargs["transport"] = self.transport
            else:
                kwargs["trust_env"] = True
            async with httpx.AsyncClient(**kwargs) as c, c.stream("GET", uri) as r:
                loc = r.headers.get("location")
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    target = urljoin(uri, loc)
                    self.policy.check_static(target)
                    return target, None
                return None, f"redirect not resolved (HTTP {r.status_code})"
        except PolicyViolation as exc:
            return None, f"policy: {exc}"
        except httpx.HTTPError as exc:
            return None, f"{type(exc).__name__}: {exc}"

    async def run(self, hypothesis: SearchHypothesis, query: str) -> tuple[list[DiscoveryHit], QueryRecord]:
        assert self.gateway is not None and self.model is not None
        prompt = GROUNDING_INSTRUCTIONS + query
        try:
            res = await self.gateway.grounded_search(
                agent="discovery.grounding",
                model=self.model,
                prompt=prompt,
                exclude_domains=self.exclude_domains,
            )
        except BudgetExceeded:
            raise
        except LLMUnavailable as exc:
            self.disabled_reason = str(exc)[:300]
            return [], QueryRecord(
                source=self.name, query=query, hypothesis_id=hypothesis.id, status="ERROR", error=self.disabled_reason
            )
        except (TransientLLMError, Exception) as exc:
            return [], QueryRecord(
                source=self.name,
                query=query,
                hypothesis_id=hypothesis.id,
                status="ERROR",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )

        hits: list[DiscoveryHit] = []
        unresolved: list[dict[str, str]] = []
        for src in res.sources:
            url, err = await self.resolve(src.uri)
            if url is None:
                unresolved.append({"uri": src.uri, "title": src.title or "", "error": err or ""})
                continue
            hits.append(
                DiscoveryHit(
                    source=self.name,
                    original_url=url,
                    query=query,
                    hypothesis_id=hypothesis.id,
                    title=src.title,
                    snippet=" | ".join(src.support_texts)[:1000] or None,
                    rank=src.chunk_index + 1,
                    metadata={
                        "grounding_uri": src.uri,
                        "grounding_domain": src.domain,
                        "web_search_queries": res.web_search_queries,
                        "model": res.model,
                    },
                )
            )
        record = QueryRecord(
            source=self.name,
            query=query,
            hypothesis_id=hypothesis.id,
            status="OK" if hits else "EMPTY",
            result_count=len(hits),
            metadata={
                "web_search_queries": res.web_search_queries,
                "grounding_chunks": len(res.sources),
                "unresolved": unresolved,
                # Kept verbatim (unmodified) as required by the grounding terms of service.
                "search_entry_point_html": res.search_entry_point_html,
                "model_text_excerpt": res.text[:1500],
            },
        )
        # Vertex AI Search Grounding strips instagram.com and facebook.com from grounding_chunks.
        # When executing social platform hypotheses in live mode (no mock transport), supplement with
        # direct web dork queries so public Instagram/Facebook reels, posts and profiles are discovered.
        if self.transport is None and hypothesis.id.startswith("H6-platform:") and hypothesis.keyword_queries:
            social_hits = await self._supplement_social_dorks(hypothesis)
            seen_urls = {h.original_url for h in hits}
            for sh in social_hits:
                if sh.original_url not in seen_urls:
                    seen_urls.add(sh.original_url)
                    hits.append(sh)
            record.result_count = len(hits)
            if hits:
                record.status = "OK"
        return hits, record

    async def _supplement_social_dorks(self, hypothesis: SearchHypothesis) -> list[DiscoveryHit]:
        out: list[DiscoveryHit] = []
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
        }
        try:
            async with httpx.AsyncClient(timeout=self.http_timeout, trust_env=True, follow_redirects=True, headers=headers) as c:
                for dork in hypothesis.keyword_queries:
                    # Strip excess -site: operators for the social search engine query
                    clean_dork = re.sub(r"\s+-site:\S+", "", dork).strip()
                    r = await c.get("https://br.search.yahoo.com/search", params={"p": clean_dork, "n": 20})
                    if r.status_code != 200:
                        continue
                    for idx, m in enumerate(_YAHOO_RESULT_RE.finditer(r.text), 1):
                        raw_url = unquote(m.group(1)).strip()
                        if not raw_url.startswith(("http://", "https://")):
                            continue
                        if not any(d in raw_url.lower() for d in _SOCIAL_DOMAINS):
                            continue
                        try:
                            self.policy.check_static(raw_url)
                        except PolicyViolation:
                            continue
                        title = html.unescape(re.sub(r"<[^>]+>", " ", m.group(2))).strip()
                        out.append(
                            DiscoveryHit(
                                source=self.name,
                                original_url=raw_url,
                                query=clean_dork,
                                hypothesis_id=hypothesis.id,
                                title=title[:300] or None,
                                rank=idx,
                                metadata={"supplement": "social_dork"},
                            )
                        )
        except Exception as exc:
            log.debug("social dork supplement skipped: %s", exc)
        return out
