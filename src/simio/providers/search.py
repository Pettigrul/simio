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
# Serper's ``displayedLink`` ("https://www.loja.com.br › financiamento"): the destination host of an
# opaque result, without the path.
_DISPLAYED_HOST_RE = re.compile(r"^(https?)://([a-z0-9.-]+\.[a-z]{2,})(?=$|[\s›/])", re.IGNORECASE)
# Hosts whose home page says nothing about the matched post/profile: not recovered.
_UNRECOVERABLE_HOSTS = (
    "instagram.com", "facebook.com", "tiktok.com", "youtube.com", "x.com", "twitter.com", "linkedin.com",
    "kwai.com", "threads.net", "pinterest.com", "t.me", "telegram.me", "google.com", "reddit.com",
)


# Search operators that make Google hide results behind opaque links (measured: 6-9 of 10 results).
_OPERATOR_RE = re.compile(r"(?i)(?:^|[\s(])-?(?:site|inurl|intitle|intext|allintitle|allinurl):|\sOR\s|[()]")
_QUERY_TOKEN_RE = re.compile(r'[()]|-?[A-Za-z]+:"[^"]*"|-?"[^"]*"|[^\s()]+')
# site: targets rewritten as a plain word (Google still ranks the platform's pages first).
_SITE_WORDS = {
    "instagram.com": "instagram", "facebook.com": "facebook", "tiktok.com": "tiktok", "t.me": "telegram",
    "youtube.com": "youtube", "linkedin.com": "linkedin", "kwai.com": "kwai", "x.com": "twitter",
    "threads.net": "threads", "olx.com.br": "olx", "mercadolivre.com.br": "mercado livre",
}


def _plain_term(token: str) -> str | None:
    """One query token without operators; None drops it (``-site:`` exclusions are applied client-side)."""
    low = token.lower()
    if low.startswith("-") and ":" in low.split('"', 1)[0]:
        return None
    op, sep, value = token.partition(":")
    if sep and op.lower() in ("site", "inurl", "intitle", "intext", "allintitle", "allinurl") and '"' not in op:
        value = value.strip()
        if op.lower() == "site":
            dom = value.lower().removeprefix("www.").strip("/")
            return _SITE_WORDS.get(dom, dom)
        return value
    return token


def plain_variants(query: str, limit: int = 3) -> tuple[list[str], list[str]]:
    """Operator-free versions of a dork, plus the domains it excluded with ``-site:``.

    ``OR`` groups are expanded round-robin (variant *i* takes the *i*-th alternative of every group), so
    ``limit`` variants cover the alternatives instead of AND-ing them all. ``site:instagram.com`` becomes
    the word ``instagram``, ``intitle:"X"`` becomes ``"X"``."""
    excluded = [
        m.group(1).lower().removeprefix("www.")
        for m in re.finditer(r"(?i)(?:^|\s)-site:([^\s()]+)", query)
    ]
    tokens = _QUERY_TOKEN_RE.findall(query)
    slots: list[list[str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "(":
            depth, j, group = 1, i + 1, []
            while j < len(tokens) and depth:
                depth += {"(": 1, ")": -1}.get(tokens[j], 0)
                if depth:
                    group.append(tokens[j])
                j += 1
            alts, cur = [], []
            for t in group:
                if t == "OR":
                    alts.append(" ".join(cur))
                    cur = []
                elif t not in ("(", ")"):
                    cur.append(t)
            alts.append(" ".join(cur))
            slots.append([a for a in alts if a])
            i = j
            continue
        if tok == "OR" and slots and i + 1 < len(tokens) and tokens[i + 1] not in ("(", ")"):
            slots[-1].append(tokens[i + 1])
            i += 2
            continue
        if tok not in (")", "OR"):
            slots.append([tok])
        i += 1
    slots = [s for s in slots if s]
    if not slots:
        return [], excluded
    n = max(1, min(limit, max(len(s) for s in slots)))
    out: list[str] = []
    for v in range(n):
        words = []
        for slot in slots:
            alt = slot[v % len(slot)]
            words.extend(w for w in (_plain_term(t) for t in _QUERY_TOKEN_RE.findall(alt)) if w)
        q = " ".join(dict.fromkeys(words))
        if q and q not in out and q != query:
            out.append(q)
    return out, excluded


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
    is not full, so short result lists do not spend extra credits.

    Google hides most results of operator queries behind opaque links. When that happens, up to
    ``plain_variants`` operator-free versions of the dork run as well (see ``plain_variants``)."""

    name = "serper"
    endpoint = "https://google.serper.dev/search"

    def __init__(
        self,
        api_key: str | None,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        pages: int = 1,
        plain_variants: int = 3,
    ) -> None:
        super().__init__(api_key, timeout, transport)
        self.pages = max(1, pages)
        self.plain_variants = max(0, plain_variants)

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
            (
                r.get("link"),
                r.get("title"),
                r.get("snippet"),
                {"position": r.get("position"), "date": r.get("date"), "displayed_link": r.get("displayedLink")},
            )
            for r in data.get("organic") or []
        ]

    async def run(self, hypothesis: SearchHypothesis, query: str) -> tuple[list[DiscoveryHit], QueryRecord]:
        """Google often returns opaque ``google.com/goto?url=...`` links that only resolve through an
        anti-automation interstitial. They are never followed. When the result names its destination
        host (``displayedLink``), the *home page* of that host becomes the candidate instead (marked
        ``recovered_from_google_redirect``; the matched page itself stays unknown). The rest is kept in
        the query record (title/snippet) for manual follow-up."""
        hits, record = await super().run(hypothesis, query)
        opaque_share = sum(bool(_GOOGLE_REDIRECT_RE.match(h.original_url)) for h in hits) / max(1, len(hits))
        if self.plain_variants and record.status != "ERROR" and _OPERATOR_RE.search(query) and (
            opaque_share >= 0.3 or not hits
        ):
            variants, excluded = plain_variants(query, self.plain_variants)
            extra: list[DiscoveryHit] = []
            for v in variants:
                more, _ = await super().run(hypothesis, v)
                extra.extend(
                    h.model_copy(update={"metadata": {**h.metadata, "plain_variant_of": query}})
                    for h in more
                    if not any(_host_in(h.original_url, d) for d in excluded)
                )
            record.metadata["plain_variants"] = variants
            if record.status == "EMPTY" and extra:
                record.status = "OK"
            hits = hits + extra
        kept: list[DiscoveryHit] = []
        opaque: list[dict[str, str]] = []
        recovered = 0
        seen: set[str] = set()
        for h in hits:
            if not _GOOGLE_REDIRECT_RE.match(h.original_url):
                if h.original_url not in seen:
                    seen.add(h.original_url)
                    kept.append(h)
                continue
            shown = str(h.metadata.get("displayed_link") or "")
            m = _DISPLAYED_HOST_RE.match(shown.strip())
            host = m.group(2).lower() if m else ""
            if host and not any(host == d or host.endswith("." + d) for d in _UNRECOVERABLE_HOSTS):
                url = f"{m.group(1).lower()}://{host}/"  # type: ignore[union-attr]
                if url not in seen:
                    seen.add(url)
                    recovered += 1
                    kept.append(
                        h.model_copy(
                            update={
                                "original_url": url,
                                "metadata": {
                                    **h.metadata,
                                    "recovered_from_google_redirect": True,
                                    "matched_page": shown[:200],
                                },
                            }
                        )
                    )
                continue
            opaque.append({"title": h.title or "", "snippet": (h.snippet or "")[:300], "displayed": shown[:120]})
        if opaque:
            record.metadata["unresolved_google_redirects"] = opaque[:50]
        if recovered:
            record.metadata["recovered_google_redirect_hosts"] = recovered
        record.result_count = len(kept)
        if not kept and record.status == "OK":
            record.status = "EMPTY"
        return kept, record


def _host_in(url: str, domain: str) -> bool:
    host = (httpx.URL(url).host or "").lower()
    return host == domain or host.endswith("." + domain)


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
