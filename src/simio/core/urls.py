"""URL normalisation, registrable-domain extraction and URL extraction from free text."""

from __future__ import annotations

import contextlib
import re
from functools import lru_cache
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import tldextract

TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "gbraid",
        "wbraid",
        "dclid",
        "fbclid",
        "msclkid",
        "yclid",
        "ttclid",
        "twclid",
        "igshid",
        "igsh",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "ref",
        "ref_src",
        "referrer",
        "srsltid",
        "spm",
        "si",
        "trk",
        "cmpid",
        "s_kwcid",
        "mibextid",
        "rdid",
        "share_url",
    }
)
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_", "vero_", "_hs")
SOCIAL_UI_LOCALE_HOSTS = (
    "instagram.com",
    "facebook.com",
    "tiktok.com",
    "threads.com",
    "wa.me",
    "whatsapp.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "play.google.com",
    "youtube.com",
)
SOCIAL_UI_LOCALE_PARAMS = frozenset({"hl", "locale", "lang"})
DEFAULT_PORTS = {"http": 80, "https": 443}

_URL_RE = re.compile(
    r"""(?xi)
    \b(
      https?://[^\s<>"'`{}|\\^\[\]]+
      |
      www\.[a-z0-9-]+(?:\.[a-z0-9-]+)+[^\s<>"'`{}|\\^\[\]]*
    )
    """
)
_TRAILING_PUNCT = ".,;:!?)]}'\"»”"


@lru_cache(maxsize=1)
def _extractor() -> tldextract.TLDExtract:
    # suffix_list_urls=() -> use the bundled Public Suffix List snapshot; never hits the network.
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def registrable_domain(hostname: str) -> str:
    host = hostname.lower().rstrip(".")
    ext = _extractor()(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return host


def normalize_url(url: str) -> str:
    """Canonical form used for deduplication.

    Lower-cases scheme/host, strips ``www.``, default ports, fragments, tracking parameters,
    sorts the query string and removes trailing slashes. ``http`` and ``https`` collapse to
    the same key (the same page served over both schemes is one candidate).
    """
    raw = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    with contextlib.suppress(UnicodeError):
        host = host.encode("idna").decode("ascii") if host else host
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port in (None, DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"
    raw_path = re.sub(r";jsessionid=[^/?#]+", "", parts.path or "/", flags=re.IGNORECASE)
    path = quote(unquote(raw_path), safe="/:@!$&'()*+,;=-._~%")
    path = re.sub(r"/{2,}", "/", path)
    for suffix in ("/index.html", "/index.htm", "/index.php"):
        if path.endswith(suffix):
            path = path[: -len(suffix) + 1]
    if len(path) > 1:
        path = path.rstrip("/")
    strip_locale = domain_matches(host, SOCIAL_UI_LOCALE_HOSTS)
    query_items = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
        and not k.lower().startswith(TRACKING_PREFIXES)
        and not (strip_locale and k.lower() in SOCIAL_UI_LOCALE_PARAMS)
    ]
    query = urlencode(sorted(query_items))
    return urlunsplit(("https" if scheme in ("http", "https") else scheme, netloc, path, query, ""))


def hostname_of(url: str) -> str:
    """Lower-case host of a URL; "" for malformed input (e.g. a page link like ``http://[broken``)."""
    try:
        return (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def extract_urls(text: str) -> list[str]:
    """Extract http(s) URLs from free text, preserving order and removing duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        candidate = match.group(1).rstrip(_TRAILING_PUNCT)
        # Balance a trailing ")" that belongs to the URL (e.g. wikipedia style)
        if candidate.lower().startswith("www."):
            candidate = "https://" + candidate
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def domain_matches(hostname: str, domains: list[str] | tuple[str, ...] | set[str]) -> bool:
    host = hostname.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in (x.lower() for x in domains))
