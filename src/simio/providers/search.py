"""Optional keyword-search connectors and URL import adapters.

Every connector is optional: a missing API key makes it *unavailable*, never fatal.
Connectors only return URLs present in the provider response (nothing is synthesised).
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Any, Protocol

import httpx

from simio.core.models import DiscoveryHit, QueryRecord, SearchHypothesis
from simio.core.urls import extract_urls

log = logging.getLogger(__name__)


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
    name = "serper"
    endpoint = "https://google.serper.dev/search"

    async def _request(self, query: str) -> Any:
        async with self._http() as c:
            r = await c.post(
                self.endpoint,
                json={"q": query, "gl": "br", "hl": "pt-br", "num": 20},
                headers={"X-API-KEY": self.api_key or "", "Content-Type": "application/json"},
            )
            r.raise_for_status()
            return r.json()

    def _parse(self, data: Any) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
        return [
            (r.get("link"), r.get("title"), r.get("snippet"), {"position": r.get("position")})
            for r in data.get("organic") or []
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
