"""Google Search Grounding connector (Gemini on Vertex AI).

The model is asked an investigative question with the ``google_search`` tool enabled.
URLs are taken **exclusively** from ``grounding_metadata.grounding_chunks`` -- never from the
model's free text -- so no URL is invented. Grounding redirect URIs
(``vertexaisearch.cloud.google.com/grounding-api-redirect/...``) are resolved with a single
non-following request and the target is validated by the URL policy.

Grounding is not an exhaustive web index; results are a sample guided by the hypotheses.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

import httpx

from simio.core.models import DiscoveryHit, QueryRecord, SearchHypothesis
from simio.core.urls import hostname_of
from simio.providers.llm import BudgetExceeded, LLMGateway, LLMUnavailable, TransientLLMError
from simio.security.url_policy import PolicyViolation, UrlPolicy

log = logging.getLogger(__name__)

REDIRECT_HOSTS = frozenset({"vertexaisearch.cloud.google.com"})
SEARCH_RESULT_HOSTS = frozenset({"www.google.com", "google.com"})

GROUNDING_INSTRUCTIONS = (
    "Você está apoiando uma auditoria de conformidade. Use a busca do Google para encontrar "
    "páginas públicas na web brasileira que correspondam à hipótese abaixo. Cite as páginas "
    "específicas encontradas (sites de estabelecimentos, páginas de anúncios ou de atendimento), "
    "não páginas institucionais do próprio banco nem notícias. Seja factual e breve.\n\n"
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
        return hits, record
