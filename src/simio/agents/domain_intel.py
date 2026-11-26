"""Domain intelligence for flagged pages: who registered the domain, when, where it is hosted and whom
to contact for a takedown.

Public, unauthenticated sources only:

* RDAP (the structured successor of WHOIS) via the ``rdap.org`` bootstrap redirector: registrar,
  registration/expiry dates, status, name servers, registrant *when it is a company* and the abuse
  contact of the registrar. ``.br`` answers come from Registro.br, which publishes the holder's CNPJ.
* DNS over HTTPS (JSON API): A/AAAA/CNAME of the host, NS/MX of the registered domain.
* RDAP of the server IP: network name, hosting organisation and its abuse contact.

LGPD: when the holder is a natural person (CPF, ``kind: individual``) no name, e-mail or document is
kept, only "pessoa física". Personal contacts (administrative/technical) are never kept; only role
mailboxes of organisations (abuse) are.

Pages on shared platforms (``*.weebly.com``, ``*.lovable.app``...) are flagged as such: RDAP/DNS of the
platform says nothing about the page owner, who is known only to the platform (abuse report).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import tldextract

from simio.brands import load_brand
from simio.core.models import DomainIntel, FinalDecision, IpIntel, RdapIntel
from simio.core.urls import domain_matches, hostname_of, registrable_domain

if TYPE_CHECKING:
    from simio.config import Settings
    from simio.core.storage import Storage

log = logging.getLogger(__name__)

_DNS_TYPES = {"A": 1, "AAAA": 28, "CNAME": 5, "NS": 2, "MX": 15}
# Platforms where the page owner never shows up in RDAP/DNS (besides the Public Suffix List private part).
_PLATFORM_HOSTS = (
    "weebly.com", "wixsite.com", "site123.me", "lovable.app", "vercel.app", "netlify.app", "pages.dev",
    "github.io", "web.app", "firebaseapp.com", "blogspot.com", "wordpress.com", "godaddysites.com",
    "hostingersite.com", "webnode.page", "canva.site", "framer.website", "carrd.co", "glitch.me", "replit.app",
    "onrender.com", "herokuapp.com", "softonic.com",
)
_SOCIAL_HOSTS = (
    "instagram.com", "facebook.com", "tiktok.com", "linkedin.com", "youtube.com", "x.com", "twitter.com",
    "threads.net", "kwai.com", "t.me", "wa.me", "whatsapp.com", "pinterest.com",
)


@lru_cache(maxsize=1)
def _private_extractor() -> tldextract.TLDExtract:
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)


def hosting_platform(hostname: str) -> str | None:
    """The shared platform a host is a tenant of (``x.weebly.com`` -> ``weebly.com``), if any."""
    host = hostname.lower().rstrip(".")
    reg = registrable_domain(host)
    if host != reg and domain_matches(host, _PLATFORM_HOSTS):
        return reg
    ext = _private_extractor()(host)
    if ext.is_private and ext.suffix and host != ext.suffix:
        return ext.suffix
    return None


def _vcard(entity: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for item in entity.get("vcardArray", [None, []])[1]:
            if item[0] in ("fn", "kind", "email", "org") and item[3] and item[0] not in out:
                out[item[0]] = str(item[3] if not isinstance(item[3], list) else " ".join(item[3])).strip()
    except (IndexError, TypeError):
        pass
    return out


def _walk(entities: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in entities or []:
        out.append(e)
        out.extend(_walk(e.get("entities")))
    return out


def _abuse_emails(entities: list[dict[str, Any]] | None) -> list[str]:
    emails = []
    for e in _walk(entities):
        if "abuse" in (e.get("roles") or []):
            email = _vcard(e).get("email")
            if email and email not in emails:
                emails.append(email)
    return emails


def parse_domain_rdap(data: dict[str, Any], now: datetime | None = None) -> RdapIntel:
    events = {e.get("eventAction"): e.get("eventDate") for e in data.get("events") or []}
    created = events.get("registration")
    age = None
    if created:
        try:
            age = ((now or datetime.now(UTC)) - datetime.fromisoformat(created.replace("Z", "+00:00"))).days
        except ValueError:
            age = None
    registrar = holder = holder_doc = None
    holder_kind = "não publicado"
    for e in _walk(data.get("entities")):
        roles = e.get("roles") or []
        vc = _vcard(e)
        if "registrar" in roles and not registrar:
            registrar = vc.get("fn") or vc.get("org")
        if "registrant" in roles:
            ids = {str(p.get("type", "")).lower(): p.get("identifier") for p in e.get("publicIds") or []}
            if "cpf" in ids or vc.get("kind") == "individual":
                holder_kind = "pessoa física (dados pessoais omitidos — LGPD)"
            elif "cnpj" in ids or vc.get("kind") == "org" or vc.get("org"):
                name = vc.get("fn") or vc.get("org") or ""
                if name and "redacted" not in name.lower() and "privacy" not in name.lower():
                    holder, holder_doc, holder_kind = name, ids.get("cnpj"), "pessoa jurídica"
                else:
                    holder_kind = "oculto (serviço de privacidade)"
    return RdapIntel(
        registrar=registrar,
        created=created,
        expires=events.get("expiration"),
        updated=events.get("last changed"),
        age_days=age,
        status=[str(s) for s in data.get("status") or []][:6],
        name_servers=[str(n.get("ldhName", "")).lower() for n in data.get("nameservers") or []][:6],
        holder=holder,
        holder_document=holder_doc,
        holder_kind=holder_kind,
        abuse_emails=_abuse_emails(data.get("entities"))[:3],
    )


def parse_ip_rdap(ip: str, data: dict[str, Any]) -> IpIntel:
    org = None
    for e in _walk(data.get("entities")):
        if "registrant" in (e.get("roles") or []):
            vc = _vcard(e)
            if vc.get("kind") != "individual":
                org = vc.get("fn") or vc.get("org")
                break
    return IpIntel(
        ip=ip,
        network=data.get("name"),
        organisation=org,
        country=data.get("country"),
        abuse_emails=_abuse_emails(data.get("entities"))[:3],
    )


def _err(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


class DomainIntelCollector:
    def __init__(
        self,
        rdap_base: str = "https://rdap.org",
        doh_url: str = "https://cloudflare-dns.com/dns-query",
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = 4,
        retries: int = 4,
        backoff_s: float = 5.0,
        rdap_interval_s: float = 1.2,
    ) -> None:
        self.retries = retries
        self.backoff_s = backoff_s
        # Registries (Registro.br, ARIN, RIPE...) throttle bursts: RDAP calls are spaced out globally.
        self.rdap_interval_s = rdap_interval_s
        self._rdap_lock = asyncio.Lock()
        self._rdap_last = 0.0
        self.rdap_base = rdap_base.rstrip("/")
        self.doh_url = doh_url
        self.timeout = timeout
        self.transport = transport
        self._sem = asyncio.Semaphore(concurrency)
        self._rdap_cache: dict[str, RdapIntel | str] = {}
        self._ip_cache: dict[str, IpIntel | str] = {}

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
            trust_env=self.transport is None,
            follow_redirects=True,
            headers={"User-Agent": "simio/0.1", "Accept": "application/rdap+json, application/json"},
        )

    async def _json(self, c: httpx.AsyncClient, url: str, **kw: Any) -> dict[str, Any]:
        """GET with retries: RDAP redirectors and registries rate-limit bursts (429) and flap (5xx)."""
        for attempt in range(self.retries + 1):
            if url.startswith(self.rdap_base):
                async with self._rdap_lock:
                    loop = asyncio.get_running_loop()
                    wait = self._rdap_last + self.rdap_interval_s - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._rdap_last = loop.time()
            async with self._sem:
                r = await c.get(url, **kw)
            if (r.status_code == 429 or r.status_code >= 500) and attempt < self.retries:
                await asyncio.sleep(self.backoff_s * 2**attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise AssertionError("unreachable")  # pragma: no cover

    async def _dns(self, c: httpx.AsyncClient, name: str, rtype: str) -> list[str]:
        data = await self._json(
            c, self.doh_url, params={"name": name, "type": rtype}, headers={"accept": "application/dns-json"}
        )
        want = _DNS_TYPES[rtype]
        out = []
        for a in data.get("Answer") or []:
            if a.get("type") == want:
                value = str(a.get("data", "")).rstrip(".")
                out.append(value.split(" ", 1)[1].rstrip(".") if rtype == "MX" and " " in value else value)
        return out[:8]

    async def _domain_rdap(self, c: httpx.AsyncClient, domain: str) -> RdapIntel | str:
        if domain not in self._rdap_cache:
            try:
                self._rdap_cache[domain] = parse_domain_rdap(await self._json(c, f"{self.rdap_base}/domain/{domain}"))
            except httpx.HTTPStatusError as exc:
                self._rdap_cache[domain] = (
                    f"O registro de .{domain.rsplit('.', 1)[-1]} não publica RDAP para este domínio (consultar WHOIS)"
                    if exc.response.status_code == 404
                    else f"RDAP do domínio indisponível: {_err(exc)}"
                )
            except (httpx.HTTPError, ValueError) as exc:
                self._rdap_cache[domain] = f"RDAP do domínio indisponível: {_err(exc)}"
        return self._rdap_cache[domain]

    async def _ip_rdap(self, c: httpx.AsyncClient, ip: str) -> IpIntel | str:
        if ip not in self._ip_cache:
            try:
                self._ip_cache[ip] = parse_ip_rdap(ip, await self._json(c, f"{self.rdap_base}/ip/{ip}"))
            except (httpx.HTTPError, ValueError) as exc:
                self._ip_cache[ip] = f"RDAP do IP {ip} indisponível: {_err(exc)}"
        return self._ip_cache[ip]

    async def collect(self, hostname: str, server_ip: str | None = None) -> DomainIntel:
        host = hostname.lower().rstrip(".")
        domain = registrable_domain(host)
        platform = hosting_platform(host)
        intel = DomainIntel(hostname=host, registered_domain=domain, platform=platform,
                            collected_at=datetime.now(UTC).isoformat(timespec="seconds"))
        if platform:
            intel.notes.append(
                f"Página hospedada na plataforma {platform}: registro e DNS são da plataforma; o responsável pela "
                f"página só é identificável pela própria plataforma (denúncia de abuso ou ordem judicial)."
            )
        async with self._http() as c:
            async def dns(name: str, rtype: str) -> list[str]:
                try:
                    return await self._dns(c, name, rtype)
                except (httpx.HTTPError, ValueError) as exc:
                    intel.errors.append(f"DNS {rtype} {name}: {_err(exc)}")
                    return []

            a, aaaa, cname, ns, mx = await asyncio.gather(
                dns(host, "A"), dns(host, "AAAA"), dns(host, "CNAME"), dns(domain, "NS"), dns(domain, "MX")
            )
            intel.a, intel.aaaa, intel.cname, intel.ns, intel.mx = a, aaaa, cname, ns, mx
            if not (a or aaaa or cname):
                intel.notes.append("O nome não resolve hoje (domínio fora do ar, suspenso ou expirado).")
            if not platform:  # a platform's registration says nothing about the page owner
                rdap = await self._domain_rdap(c, domain)
                if isinstance(rdap, str):
                    (intel.notes if "não publica RDAP" in rdap else intel.errors).append(rdap)
                else:
                    intel.rdap = rdap
            for ip in list(dict.fromkeys([*(a[:2]), *([server_ip] if server_ip else [])]))[:3]:
                info = await self._ip_rdap(c, ip)
                if isinstance(info, str):
                    intel.errors.append(info)
                else:
                    intel.ips.append(info)
        return intel


def summarize(intel: DomainIntel) -> dict[str, str]:
    """Flat, human-readable fields for spreadsheets and dossiers."""
    # On a shared platform the registration data belongs to the platform, not to the page owner.
    r = None if intel.platform else intel.rdap
    created = ""
    if r and r.created:
        created = r.created[:10] + (f" ({r.age_days} dias)" if r.age_days is not None else "")
    holder = ""
    if r:
        holder = r.holder or r.holder_kind
        if r.holder and r.holder_document:
            holder = f"{r.holder} (CNPJ {r.holder_document})"
    hosting = "; ".join(
        f"{i.organisation or i.network or '?'} ({i.ip}{', ' + i.country if i.country else ''})" for i in intel.ips
    )
    reg_abuse = intel.rdap.abuse_emails if intel.rdap and not intel.platform else []
    abuse = list(dict.fromkeys([*(e for i in intel.ips for e in i.abuse_emails), *reg_abuse]))
    return {
        "platform": intel.platform or "",
        "registrar": (r.registrar or "") if r else "",
        "created": created,
        "holder": holder or ("— (dono só identificável pela plataforma)" if intel.platform else ""),
        "ns": "" if intel.platform else ", ".join(intel.ns or (r.name_servers if r else [])),
        "mx": "" if intel.platform else ", ".join(intel.mx),
        "resolves": "sim" if (intel.a or intel.aaaa or intel.cname) else "não",
        "hosting": hosting,
        "abuse": ", ".join(abuse[:4]),
        "notes": " ".join(intel.notes + intel.errors)[:600],
    }


INTEL_FILE = "domain-intel.json"
FLAGGED = ("STRONG_INDICATION", "REVIEW_REQUIRED")


def intel_path(output_dir: Path, campaign_id: str) -> Path:
    return output_dir / "campaigns" / campaign_id / INTEL_FILE


def load_intel(output_dir: Path, campaign_id: str) -> dict[str, DomainIntel]:
    """``hostname -> DomainIntel`` of a campaign (empty when it was never enriched)."""
    path = intel_path(output_dir, campaign_id)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {h: DomainIntel.model_validate(v) for h, v in raw.items()}


async def enrich_campaign(
    settings: Settings, storage: Storage, campaign_id: str, refresh: bool = False
) -> dict[str, DomainIntel]:
    """Collect domain intelligence for every flagged page of a campaign (STRONG/REVIEW, asking for
    credentials, a lookalike or a domain carrying the brand), one lookup per host. Social profiles are
    skipped (the owner is the profile). Results already collected are kept unless ``refresh``."""
    # Hosts whose lookup failed (rate limits, timeouts) are tried again on every run.
    camp = storage.get_campaign(campaign_id) or {}
    try:
        brand = load_brand(camp.get("brand") or "bv", settings.brand_file)
    except Exception:  # an unknown brand only narrows the targets
        brand = None
    known = {} if refresh else {h: v for h, v in load_intel(settings.output_dir, campaign_id).items() if not v.errors}
    targets: dict[str, str | None] = {}
    for c in storage.list_candidates(campaign_id):
        final = storage.get_assessment(c.id, "final", FinalDecision)
        if final is None:
            continue
        latest = storage.latest_evidence_with_id(c.id)
        ev = latest[1] if latest else None
        host = hostname_of((ev.final_url if ev else None) or c.url) or c.hostname
        lookalike = any(h.source == "typosquat" for h in storage.discovery_hits(c.id))
        brand_domain = bool(brand and not brand.is_official(host) and brand.domain_brand_match(host))
        if final.classification.value not in FLAGGED and not final.sensitive_data_requested and not (
            lookalike or brand_domain
        ):
            continue
        if not host or domain_matches(host, _SOCIAL_HOSTS) or host in known:
            continue
        targets.setdefault(host, ev.server_ip if ev else None)
    collector = DomainIntelCollector(
        rdap_base=settings.rdap_base_url, doh_url=settings.doh_url, timeout=settings.http_timeout_s
    )

    async def one(host: str, ip: str | None) -> tuple[str, DomainIntel]:
        try:
            return host, await collector.collect(host, ip)
        except Exception as exc:  # one host never stops the others
            log.warning("domain intel failed for %s: %s", host, exc)
            return host, DomainIntel(
                hostname=host, registered_domain=registrable_domain(host), errors=[str(exc)[:300]]
            )

    results = dict(await asyncio.gather(*(one(h, ip) for h, ip in targets.items())))
    merged = {**known, **results}
    path = intel_path(settings.output_dir, campaign_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({h: v.model_dump(mode="json") for h, v in sorted(merged.items())}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return merged
