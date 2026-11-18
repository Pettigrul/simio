"""Evidence Agent: runs the secure collector and turns raw page data into an addressable
evidence catalog plus deterministic observations ("signals") that other agents cite.

The catalog is a pure function of (Evidence, BrandProfile), so it is reproducible from the
stored evidence at any time (reports and dossiers rebuild it instead of trusting summaries).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from simio.brands import BrandProfile, fold
from simio.collectors.browser import BrowserCollector
from simio.core.models import Evidence, EvidenceItem, ImageInfo
from simio.core.urls import domain_matches, hostname_of

# ---------------------------------------------------------------------------------- regexes
CNPJ_RE = re.compile(r"(?<![\w])([0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?\d{2})(?![\w])")
PHONE_RE = re.compile(r"(?<!\d)(0800[\s.-]?\d{3}[\s.-]?\d{4}|\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4})(?!\d)")
CEP_RE = re.compile(r"(?<!\d)\d{5}-\d{3}(?!\d)")
LEGAL_NAME_RE = re.compile(
    r"([A-ZÀ-Ú0-9][A-Za-zÀ-ú0-9&.,'\- ]{2,80}?\s(?:LTDA\.?|Ltda\.?|EIRELI|S\.A\.|S/A|SA|ME|EPP)\b)"
)
LABELLED_NAME_RE = re.compile(r"(?i)(raz[aã]o social|nome fantasia)\s*[:\-]\s*([^\n|;]{3,100})")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
INJECTION_RE = re.compile(
    r"(?i)(ignore (all |the )?(previous|prior|above) instructions|ignore (as|todas as) instru[cç][oõ]es"
    r"|desconsidere (as|todas as) instru[cç][oõ]es|you are now|system prompt|prompt do sistema"
    r"|as an ai|classif(y|ique) (this|esta|como)|NO_INDICATION|STRONG_INDICATION"
    r"|<\|im_start\|>|<\|system\|>|###\s*instruction|\bassistant\s*:)"
)
KEYWORD_RE = re.compile(
    r"(?i)(cnpj|raz[aã]o social|nome fantasia|ltda|eireli|ouvidoria|sac\b|atendimento|whatsapp|"
    r"correspondente|parceir|credenciad|autorizad|oficial|endere[cç]o|cep\b|telefone|"
    r"institui[cç][aã]o financeira|banco)"
)

_CNPJ_WEIGHTS_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
_CNPJ_WEIGHTS_2 = [6, *_CNPJ_WEIGHTS_1]


def normalize_cnpj(raw: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", raw.upper())


def cnpj_is_valid(raw: str) -> bool:
    """Validate numeric and alphanumeric (2026+) CNPJ check digits."""
    c = normalize_cnpj(raw)
    if len(c) != 14 or not c[12:].isdigit() or len(set(c)) == 1:
        return False
    vals = [ord(ch) - 48 for ch in c]

    def dv(base: list[int], weights: list[int]) -> int:
        r = sum(v * w for v, w in zip(base, weights, strict=True)) % 11
        return 0 if r < 2 else 11 - r

    d1 = dv(vals[:12], _CNPJ_WEIGHTS_1)
    d2 = dv([*vals[:12], d1], _CNPJ_WEIGHTS_2)
    return vals[12] == d1 and vals[13] == d2


def format_cnpj(c: str) -> str:
    c = normalize_cnpj(c)
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else c


# --------------------------------------------------------------------------------- catalog
def _lines(text: str) -> list[str]:
    raw = [re.sub(r"\s+", " ", ln).strip() for ln in (text or "").splitlines()]
    out: list[str] = []
    for ln in raw:
        if not ln:
            continue
        while len(ln) > 400:
            cut = ln.rfind(" ", 0, 400)
            cut = cut if cut > 100 else 400
            out.append(ln[:cut])
            ln = ln[cut:].strip()
        out.append(ln)
    return out


def build_catalog(ev: Evidence, brand: BrandProfile, max_text_items: int = 60) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    add = items.append
    url = ev.final_url or ev.original_url
    add(
        EvidenceItem(
            ref="E-URL", kind="domain", location="url", content=f"URL final: {url} (domínio: {hostname_of(url)})"
        )
    )
    if ev.redirect_chain:
        add(
            EvidenceItem(
                ref="E-REDIR",
                kind="network",
                location="url",
                content="Cadeia de redirecionamento: " + " -> ".join([*ev.redirect_chain, url]),
            )
        )
    if ev.status.value not in ("OK", "PARTIAL") or ev.errors:
        add(
            EvidenceItem(
                ref="E-COLLECT",
                kind="network",
                location="collection",
                content=f"Status da coleta: {ev.status.value}; HTTP {ev.http_status}; "
                f"ocorrências: {'; '.join(ev.errors)[:600] or 'nenhuma'}",
            )
        )
    for art, ref in (("screenshot_viewport", "E-SHOT-VIEWPORT"), ("screenshot_full", "E-SHOT-FULL")):
        a = ev.artifact(art)
        if a:
            add(
                EvidenceItem(
                    ref=ref,
                    kind="screenshot",
                    location="viewport" if "VIEW" in ref else "page",
                    content=f"{a.path} (sha256 {a.sha256[:16]}…)",
                )
            )
    p = ev.page
    if p is None:
        return items
    add(EvidenceItem(ref="E-TITLE", kind="title", location="head", content=p.title or "(sem título)"))
    for i, key in enumerate(
        k
        for k in ("description", "og:site_name", "og:title", "og:description", "author", "application-name")
        if k in p.meta
    ):
        add(EvidenceItem(ref=f"E-META-{i + 1}", kind="meta", location="head", content=f"{key}: {p.meta[key]}"))
    if p.canonical:
        add(EvidenceItem(ref="E-CANONICAL", kind="meta", location="head", content=p.canonical))
    for i, h in enumerate(p.headings[:15]):
        add(EvidenceItem(ref=f"E-HEAD-{i + 1}", kind="heading", location="page", content=h[:300]))
    if p.header_text:
        add(EvidenceItem(ref="E-HDR", kind="text", location="header", content=p.header_text[:800]))
    if p.above_fold_text:
        add(
            EvidenceItem(
                ref="E-FOLD", kind="text", location="viewport", above_fold=True, content=p.above_fold_text[:2000]
            )
        )
    if p.footer_text:
        add(EvidenceItem(ref="E-FTR", kind="text", location="footer", content=p.footer_text[:2000]))
    if p.nav_items:
        add(
            EvidenceItem(
                ref="E-NAV",
                kind="nav",
                location="header",
                content=" | ".join(f"{n.text} <{n.href}>" for n in p.nav_items[:30])[:1500],
            )
        )
    for i, c in enumerate(p.ctas[:20]):
        add(
            EvidenceItem(
                ref=f"E-CTA-{i + 1}",
                kind="cta",
                location=c.location,
                content=f"{c.text}" + (f" <{c.href}>" if c.href else ""),
            )
        )
    for i, c in enumerate(p.contact_links[:20]):
        add(EvidenceItem(ref=f"E-CONTACT-{i + 1}", kind="contact", location=c.location, content=f"{c.text} <{c.href}>"))
    brandish = [img for img in p.images if _image_is_brandish(img, brand)]
    for i, img in enumerate(brandish[:20]):
        add(
            EvidenceItem(
                ref=f"E-IMG-{i + 1}",
                kind="image",
                location=img.location,
                above_fold=img.above_fold,
                content=f"imagem alt='{img.alt}' src={img.src[:200]} "
                f"({img.width}x{img.height}, {'acima' if img.above_fold else 'abaixo'} da dobra)",
            )
        )
    official_links = [ln for ln in p.links if domain_matches(hostname_of(ln.href), brand.official_domains)]
    if official_links:
        add(
            EvidenceItem(
                ref="E-OFFICIAL-LINKS",
                kind="link",
                location="page",
                content=" | ".join(f"{ln.text} <{ln.href}>" for ln in official_links[:10]),
            )
        )
    n = 0
    for line in _lines(p.visible_text):
        if n >= max_text_items:
            break
        if (
            brand.mentions(line)
            or brand.official_claims(line)
            or brand.disclosures(line)
            or KEYWORD_RE.search(line)
            or CNPJ_RE.search(line)
            or INJECTION_RE.search(line)
        ):
            n += 1
            add(EvidenceItem(ref=f"E-TXT-{n:03d}", kind="text", location="page", content=line))
    if ev.blocked_requests:
        add(
            EvidenceItem(
                ref="E-NET-BLOCKED",
                kind="network",
                location="collection",
                content=f"{len(ev.blocked_requests)} requisição(ões) bloqueadas pela política: "
                + "; ".join(ev.blocked_requests[:5]),
            )
        )
    return items


def _image_is_brandish(img: ImageInfo, brand: BrandProfile) -> bool:
    blob = f"{img.alt} {img.src}"
    return bool(brand.mentions(blob) or "logo" in fold(blob) or brand.domain_brand_match(img.src))


# --------------------------------------------------------------------------------- signals
@dataclass
class Hit:
    value: str
    ref: str
    context: str = ""


@dataclass
class PageSignals:
    """Deterministic observations with catalog references (facts, not conclusions)."""

    usable: bool
    brand_mentions: int = 0
    brand_in_title: bool = False
    brand_in_domain: str | None = None
    brand_in_header: bool = False
    brand_above_fold: bool = False
    brand_images: list[Hit] = field(default_factory=list)
    brand_images_above_fold: int = 0
    official_claims: list[Hit] = field(default_factory=list)
    brand_linked_claims: list[Hit] = field(default_factory=list)
    disclosures: list[Hit] = field(default_factory=list)
    disclosure_above_fold: bool = False
    disclosure_locations: set[str] = field(default_factory=set)
    partnership: list[Hit] = field(default_factory=list)
    ouvidoria: list[Hit] = field(default_factory=list)
    official_links: bool = False
    whatsapp_contacts: list[Hit] = field(default_factory=list)
    phones: list[Hit] = field(default_factory=list)
    cnpjs: list[Hit] = field(default_factory=list)
    injection_markers: list[Hit] = field(default_factory=list)
    forms_present: int = 0

    def as_observations(self) -> list[str]:
        out = [
            f"menções à marca no texto visível: {self.brand_mentions}",
            f"marca no título: {'sim' if self.brand_in_title else 'não'}",
            f"padrão da marca no domínio: {self.brand_in_domain or 'não'}",
            f"marca no cabeçalho: {'sim' if self.brand_in_header else 'não'}",
            f"marca na área visível inicial: {'sim' if self.brand_above_fold else 'não'}",
            f"imagens possivelmente de logotipo/marca: {len(self.brand_images)} "
            f"({self.brand_images_above_fold} acima da dobra)",
            "expressões de oficialidade: "
            + (", ".join(f"'{h.value}' [{h.ref}]" for h in self.official_claims[:8]) or "nenhuma"),
            "expressões de oficialidade próximas à marca: "
            + (", ".join(f"'{h.value}' [{h.ref}]" for h in self.brand_linked_claims[:8]) or "nenhuma"),
            "declarações de correspondente/independência: "
            + (", ".join(f"'{h.value}' [{h.ref}]" for h in self.disclosures[:8]) or "nenhuma encontrada")
            + f"; locais: {', '.join(sorted(self.disclosure_locations)) or '-'}"
            + f"; acima da dobra: {'sim' if self.disclosure_above_fold else 'não'}",
            "menções a parceria: " + (", ".join(f"'{h.value}' [{h.ref}]" for h in self.partnership[:5]) or "nenhuma"),
            f"ouvidoria mencionada: {'sim' if self.ouvidoria else 'não'}",
            f"links para domínio oficial da instituição: {'sim' if self.official_links else 'não'}",
            f"contatos WhatsApp: {len(self.whatsapp_contacts)}; telefones: {len(self.phones)}",
            "CNPJs: " + (", ".join(f"{h.value} [{h.ref}]" for h in self.cnpjs[:5]) or "nenhum"),
            f"formulários na página: {self.forms_present}",
        ]
        if self.injection_markers:
            out.append(
                "ATENÇÃO: conteúdo com padrões de instrução a modelos de IA (possível prompt "
                "injection): " + ", ".join(f"[{h.ref}]" for h in self.injection_markers[:5])
            )
        return out


def _near(
    text: str, needle_spans: list[tuple[int, int]], other_spans: list[tuple[int, int]], window: int = 120
) -> bool:
    return any(abs(a0 - b0) <= window for a0, _ in needle_spans for b0, _ in other_spans)


def compute_signals(ev: Evidence, brand: BrandProfile, catalog: list[EvidenceItem]) -> PageSignals:
    sig = PageSignals(usable=ev.usable)
    url = ev.final_url or ev.original_url
    sig.brand_in_domain = brand.domain_brand_match(hostname_of(url))
    p = ev.page
    if p is None:
        return sig
    sig.forms_present = p.forms_present
    sig.brand_mentions = len(brand.mentions(p.visible_text))
    sig.brand_in_title = bool(brand.mentions(p.title) or brand.mentions(p.meta.get("og:site_name", "")))
    sig.brand_in_header = bool(brand.mentions(p.header_text) or any(brand.mentions(n.text) for n in p.nav_items))
    sig.brand_above_fold = bool(brand.mentions(p.above_fold_text))
    sig.official_links = any(i.ref == "E-OFFICIAL-LINKS" for i in catalog)
    strong_rx = brand._strong_re  # compiled internally
    for item in catalog:
        text = item.content
        folded = fold(text)
        if item.kind == "image":
            sig.brand_images.append(Hit(text[:120], item.ref))
            if item.above_fold:
                sig.brand_images_above_fold += 1
            continue
        if item.kind in ("screenshot", "network", "domain"):
            continue
        claims = brand.official_claims(text)
        for c in claims:
            sig.official_claims.append(Hit(c, item.ref, text[:200]))
        if claims and strong_rx is not None:
            brand_spans = [m.span() for m in strong_rx.finditer(folded)]
            claim_spans = [m.span() for m in (brand._claim_re.finditer(folded) if brand._claim_re else [])]
            if _near(folded, claim_spans, brand_spans) or any(brand.strong_mentions(c) for c in claims):
                for c in claims:
                    sig.brand_linked_claims.append(Hit(c, item.ref, text[:200]))
        for d in brand.disclosures(text):
            sig.disclosures.append(Hit(d, item.ref, text[:200]))
            sig.disclosure_locations.add(item.location)
            if item.location in ("viewport", "header") or item.above_fold:
                sig.disclosure_above_fold = True
        for pm in brand.partnership_mentions(text):
            sig.partnership.append(Hit(pm, item.ref, text[:200]))
        if "ouvidoria" in folded:
            sig.ouvidoria.append(Hit("ouvidoria", item.ref, text[:200]))
        if item.kind == "contact" and ("wa.me" in folded or "whatsapp" in folded):
            sig.whatsapp_contacts.append(Hit(text[:200], item.ref))
        for m in PHONE_RE.finditer(text):
            sig.phones.append(Hit(m.group(1), item.ref, text[:200]))
        for m in CNPJ_RE.finditer(text.upper()):
            raw = m.group(1)
            if sum(ch.isdigit() for ch in raw) >= 8:  # avoid matching random uppercase words
                sig.cnpjs.append(Hit(format_cnpj(raw), item.ref, text[:300]))
        if INJECTION_RE.search(text):
            sig.injection_markers.append(Hit(INJECTION_RE.search(text).group(0), item.ref))  # type: ignore[union-attr]
    # Disclosure in the above-fold text counts even if only the E-TXT copy matched.
    if brand.disclosures(p.above_fold_text) or brand.disclosures(p.header_text):
        sig.disclosure_above_fold = True
    return sig


# --------------------------------------------------------------------------------- agent
class EvidenceAgent:
    def __init__(self, collector: BrowserCollector, campaign_dir: Path) -> None:
        self.collector = collector
        self.campaign_dir = campaign_dir

    async def collect(self, candidate_id: str, url: str, attempt: int) -> Evidence:
        return await self.collector.collect(candidate_id, url, attempt, self.campaign_dir)
