"""Optional keyword-search connectors and URL import adapters.

Every connector is optional: a missing API key makes it *unavailable*, never fatal.
Connectors only return URLs present in the provider response (nothing is synthesised).
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
import re
from typing import Any, Protocol

import httpx

from simio.core.models import DiscoveryHit, QueryRecord, SearchHypothesis
from simio.core.urls import extract_urls

log = logging.getLogger(__name__)
_GOOGLE_REDIRECT_RE = re.compile(r"(?i)^https?://(?:www\.)?google\.[a-z.]+/(?:goto|url)\?")


class ConnectorError(RuntimeError):
    pass


class Connector(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    async def run(self, hypothesis: SearchHypothesis, query: str) -> tuple[list[DiscoveryHit], QueryRecord]: ...


def _client(timeout: float) -> httpx.AsyncClient:
    # trust_env=True honours corporate HTTP(S)_PROXY / SSL_CERT_FILE settings.
    return httpx.AsyncClient(
        timeout=timeout,
        trust_env=True,
        follow_redirects=False,
        headers={"User-Agent": "simio/0.1"},
    )


class _KeywordConnector:
    name = "base"
    endpoint = ""

    def __init__(
        self, api_key: str | None, timeout: float = 20.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, f"{self.name}: API key not configured (optional)"
        return True, f"{self.name}: configured"

    def _http(self) -> httpx.AsyncClient:
        if self.transport is not None:
            return httpx.AsyncClient(timeout=self.timeout, transport=self.transport)
        return _client(self.timeout)

    async def _request(self, query: str) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _parse(self, data: Any) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
        raise NotImplementedError  # pragma: no cover

    async def run(self, hypothesis: SearchHypothesis, query: str) -> tuple[list[DiscoveryHit], QueryRecord]:
        try:
            data = await self._request(query)
            rows = self._parse(data)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            msg = f"{type(exc).__name__}: {exc}"[:500]
            return [], QueryRecord(
                source=self.name, query=query, hypothesis_id=hypothesis.id, status="ERROR", error=msg
            )
        hits = [
            DiscoveryHit(
                source=self.name,
                original_url=url,
                query=query,
                hypothesis_id=hypothesis.id,
                title=title,
                snippet=snippet,
                rank=i + 1,
                metadata=meta,
            )
            for i, (url, title, snippet, meta) in enumerate(rows)
            if url and url.startswith(("http://", "https://"))
        ]
        return hits, QueryRecord(
            source=self.name,
            query=query,
            hypothesis_id=hypothesis.id,
            status="OK" if hits else "EMPTY",
            result_count=len(hits),
        )


class BraveSearch(_KeywordConnector):
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    async def _request(self, query: str) -> Any:
        async with self._http() as c:
            r = await c.get(
                self.endpoint,
                params={"q": query, "count": 20, "country": "BR", "search_lang": "pt-br"},
                headers={"X-Subscription-Token": self.api_key or "", "Accept": "application/json"},
            )
            r.raise_for_status()
            return r.json()

    def _parse(self, data: Any) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
        results = (data.get("web") or {}).get("results") or []
        return [(r.get("url"), r.get("title"), r.get("description"), {"age": r.get("age")}) for r in results]


class SerperSearch(_KeywordConnector):
    """Google results via serper.dev, dorks executed literally (site:, inurl:, intitle:, OR, -site:).

    Pages of 10 results (1 credit each; free accounts reject ``num`` > 10). Stops early when a page
    is not full, so short result lists do not spend extra credits."""

    name = "serper"
    endpoint = "https://google.serper.dev/search"

    def __init__(
        self,
        api_key: str | None,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        pages: int = 1,
    ) -> None:
        super().__init__(api_key, timeout, transport)
        self.pages = max(1, pages)

    async def _request(self, query: str) -> Any:
        organic: list[Any] = []
        async with self._http() as c:
            for page in range(1, self.pages + 1):
                r = await c.post(
                    self.endpoint,
                    json={"q": query, "gl": "br", "hl": "pt-br", "num": 10, "page": page},
                    headers={"X-API-KEY": self.api_key or "", "Content-Type": "application/json"},
                )
                if r.status_code >= 400:
                    try:
                        detail = r.json().get("message") or r.text[:200]
                    except ValueError:
                        detail = r.text[:200]
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}: {detail}", request=r.request, response=r)
                rows = r.json().get("organic") or []
                for row in rows:
                    row.setdefault("position", len(organic) + 1)
                    organic.append(row)
                if len(rows) < 10:
                    break
        return {"organic": organic}

    def _parse(self, data: Any) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
        return [
            (r.get("link"), r.get("title"), r.get("snippet"), {"position": r.get("position"), "date": r.get("date")})
            for r in data.get("organic") or []
        ]

    async def run(self, hypothesis: SearchHypothesis, query: str) -> tuple[list[DiscoveryHit], QueryRecord]:
        """Google sometimes returns opaque ``google.com/goto?url=...`` links (mostly social posts) that
        only resolve through an anti-automation interstitial. They are not followed: title/snippet are
        kept in the query record for manual follow-up, and they never become candidates."""
        hits, record = await super().run(hypothesis, query)
        kept: list[DiscoveryHit] = []
        opaque: list[dict[str, str]] = []
        for h in hits:
            if _GOOGLE_REDIRECT_RE.match(h.original_url):
                opaque.append({"title": h.title or "", "snippet": (h.snippet or "")[:300]})
            else:
                kept.append(h)
        if opaque:
            record.metadata["unresolved_google_redirects"] = opaque[:50]
            record.result_count = len(kept)
            if not kept and record.status == "OK":
                record.status = "EMPTY"
        return kept, record


class LensSearch(_KeywordConnector):
    """Reverse image search (Google Lens via serper.dev): pages showing images visually similar to a
    public copy of the brand's *legacy* logo. Each query is an image URL (3 credits per search).

    It finds pages that never write the brand name in text. Results are only leads: every page goes
    through the normal pipeline, where the deterministic logo comparison confirms (or not) the logo."""

    name = "lens"
    endpoint = "https://google.serper.dev/lens"

    def __init__(
        self,
        api_key: str | None,
        image_urls: list[str],
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(api_key, max(timeout, 60.0), transport)
        self.image_urls = list(image_urls)

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "lens: Serper API key not configured (optional)"
        if not self.image_urls:
            return False, "lens: no legacy_logo_search_urls in the brand profile"
        return True, f"lens: reverse image search of {len(self.image_urls)} legacy-logo image(s)"

    def queries(self) -> list[str]:
        return list(self.image_urls)

    async def _request(self, query: str) -> Any:
        async with self._http() as c:
            r = await c.post(
                self.endpoint,
                json={"url": query, "gl": "br", "hl": "pt-br"},
                headers={"X-API-KEY": self.api_key or "", "Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                try:
                    detail = r.json().get("message") or r.text[:200]
                except ValueError:
                    detail = r.text[:200]
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}: {detail}", request=r.request, response=r)
            return r.json()

    def _parse(self, data: Any) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
        return [
            (
                r.get("link"),
                r.get("title"),
                f"Imagem visualmente semelhante ao logotipo antigo (fonte: {r.get('source') or '?'})",
                {"visual_match_of_legacy_logo": True, "source_site": r.get("source"), "image_url": r.get("imageUrl")},
            )
            for r in (data.get("organic") or data.get("visual_matches") or [])
        ]


class UrlscanSearch(_KeywordConnector):
    """urlscan.io search API (Elasticsearch query-string syntax)."""

    name = "urlscan"
    endpoint = "https://urlscan.io/api/v1/search/"

    async def _request(self, query: str) -> Any:
        async with self._http() as c:
            r = await c.get(self.endpoint, params={"q": query, "size": 100}, headers={"API-Key": self.api_key or ""})
            r.raise_for_status()
            return r.json()

    def _parse(self, data: Any) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
        out: list[tuple[str, str | None, str | None, dict[str, Any]]] = []
        for r in data.get("results") or []:
            page = r.get("page") or {}
            task = r.get("task") or {}
            out.append(
                (
                    str(page.get("url") or ""),
                    page.get("title"),
                    None,
                    {"scan_time": task.get("time"), "domain": page.get("domain"), "result": r.get("result")},
                )
            )
        return out


class CrtShSearch:
    """Certificate Transparency (crt.sh) search connector.
    
    Zero-configuration OSINT connector querying public certificate logs for lookalike / combosquatting domains.
    """

    name = "crtsh"
    endpoint = "https://crt.sh/"

    def __init__(self, timeout: float = 20.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.timeout = timeout
        self.transport = transport

    def available(self) -> tuple[bool, str]:
        return True, "crtsh: active (public Certificate Transparency log)"

    def _http(self) -> httpx.AsyncClient:
        return _client(self.timeout)

    async def run(self, hypothesis: SearchHypothesis, query: str) -> tuple[list[DiscoveryHit], QueryRecord]:
        hits: list[DiscoveryHit] = []
        # Query crt.sh expecting JSON array: [{name_value: "...", ...}]
        clean_q = re.sub(r"[^a-zA-Z0-9.\-_%]", "", query.strip()).replace(" ", "%")
        if not clean_q.startswith("%") and not clean_q.endswith("%"):
            clean_q = f"%{clean_q}%"
        async with self._http() as c:
            try:
                r = await c.get(self.endpoint, params={"q": clean_q, "output": "json"})
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                log.warning("crt.sh query '%s' failed: %s", query, e)
                return hits, QueryRecord(
                    source=self.name,
                    hypothesis_id=hypothesis.id,
                    query=query,
                    status="ERROR",
                    result_count=0,
                    error=str(e),
                )

        seen: set[str] = set()
        for item in data or []:
            name_val = item.get("name_value") or item.get("common_name") or ""
            for domain in name_val.split("\n"):
                domain = domain.strip().lower()
                if domain.startswith("*."):
                    domain = domain[2:]
                if not domain or domain in seen or " " in domain:
                    continue
                seen.add(domain)
                url = f"https://{domain}"
                hits.append(
                    DiscoveryHit(
                        source=f"{self.name}:{hypothesis.id}",
                        original_url=url,
                        rank=len(hits) + 1,
                        title=f"Certificado CT: {domain}",
                        snippet=f"Issuer: {item.get('issuer_name')}",
                        metadata={"entry_id": item.get("id"), "logged_at": item.get("entry_timestamp")},
                    )
                )
                if len(hits) >= 50:
                    break
            if len(hits) >= 50:
                break

        return hits, QueryRecord(
            source=self.name,
            hypothesis_id=hypothesis.id,
            query=query,
            status="OK" if hits else "EMPTY",
            result_count=len(hits),
        )


# --------------------------------------------------------------------------------------
# Imports
# --------------------------------------------------------------------------------------
def import_text(text: str, source: str = "import:text") -> list[DiscoveryHit]:
    return [DiscoveryHit(source=source, original_url=u, rank=i + 1) for i, u in enumerate(extract_urls(text))]


def import_file(path: Path) -> list[DiscoveryHit]:
    """Import URLs from .txt (any text), .csv (any column containing URLs) or .json."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    source = f"import:file:{path.name}"
    if path.suffix.lower() == ".csv":
        urls: list[str] = []
        for row in csv.reader(io.StringIO(raw)):
            for cell in row:
                urls.extend(extract_urls(cell))
        unique = list(dict.fromkeys(urls))
        return [DiscoveryHit(source=source, original_url=u, rank=i + 1) for i, u in enumerate(unique)]
    return import_text(raw, source=source)
