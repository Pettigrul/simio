"""Evidence Agent: runs the secure collector and turns raw page data into an addressable
evidence catalog plus deterministic observations ("signals") that other agents cite.

The catalog is a pure function of (Evidence, BrandProfile), so it is reproducible from the
stored evidence at any time (reports and dossiers rebuild it instead of trusting summaries).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from simio.brands import BrandProfile, fold, sentences
from simio.collectors.browser import BrowserCollector
from simio.core.models import DiscoveryHit, Evidence, EvidenceItem, ImageInfo, VisualMatch
from simio.core.urls import domain_matches, hostname_of

# ---------------------------------------------------------------------------------- regexes
CNPJ_RE = re.compile(r"(?<![\w])([0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?\d{2})(?![\w])")
PHONE_RE = re.compile(r"(?<!\d)(0800[\s.-]?\d{3}[\s.-]?\d{4}|\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4})(?!\d)")
CEP_RE = re.compile(r"(?<!\d)\d{5}-\d{3}(?!\d)")
# A legal name = 1..8 capitalised words (connectors allowed) ending in a company-type suffix, on a
# single line. Lower-case words and sentence-ending periods break the chain, so surrounding
# sentences are not captured.
LEGAL_NAME_RE = re.compile(
    r"((?:[A-ZÀ-Ý0-9][\wÀ-ÿ&'\-]*(?:\.[\wÀ-ÿ]+)*[ \t]+(?:(?:de|da|do|dos|das|e|&)[ \t]+)?){1,8}"
    r"(?:LTDA|Ltda|EIRELI|Eireli|S\.[ ]?A\.?|S/A|ME|EPP)\b\.?)"
)
# Third-person / reporting / warning context: a claim *about* the brand, not *by* the page.
REPORTED_RE = re.compile(
    r"\b(segundo|de acordo com|conforme|informou|anunciou|afirmou|divulgou|comunicado|reportagem|"
    r"noticia|golpe|golpistas|alerta|cuidado com|falso|falsa|fraude|denuncia|procurado pela)\b"
)

# UI words that often precede a company name in flattened text (buttons, menus).
NAME_UI_PREFIX = re.compile(
    r"^(?:(?:WhatsApp|Whatsapp|Atendimento|Ligar|Fale|Contato|Contatos|Home|Início|Menu|Simule|Simular|"
    r"Oficial|Agora|Sobre|Quem|Somos|Copyright|Todos|Direitos|Reservados|©)[ \t]+)+"
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
    r"institui[cç][aã]o financeira|banco|2[aª]\s*via|segunda\s*via|boleto|quita[cç][aã]o|"
    r"logo(?:tipo)?\s*(?:antig[oa]|velh[oa]|legad[oa])|bv\s*antig[oa])"
)
LEGACY_LOGO_RE = re.compile(
    r"(?i)(bv-?logo-?antig[oa]|logo-?antiga-?bv|bv-?antig[oa]|bv_antig[oa]|logo-?bv-?antiga|"
    r"bv-?vermelh|b-?vermelho|logotipo\s+antigo|logo\s+antiga|marca\s+antiga)"
)
MULTI_BANK_PHRASE_RE = re.compile(
    r"(?i)\b(bancos\s+parceiros|financeiras\s+parceiras|principais\s+bancos|diversos\s+bancos|"
    r"v[aá]rios\s+bancos|m[uú]ltiplos\s+bancos|todas\s+as\s+financeiras|"
    r"trabalhamos\s+com\s+(?:os\s+)?bancos|financiamos\s+(?:por|pelos))\b"
)
BANKING_CONTEXT_RE = re.compile(
    r"(?i)\b(banco|financeira|financiamento|financie|financiar|cr[eé]dito|empr[eé]stimo|"
    r"consignado|promotora|correspondente|boleto|quita[cç][aã]o|parcela|sac\b|ouvidoria|"
    r"refinanciamento|ve[ií]culos?|seminovos|autom[oó]veis|motos?|solar|feir[aã]o)\b"
)
EDITORIAL_URL_RE = re.compile(
    r"(?i)(?:^https?://(?:ajuda|help|meajuda)\.|/hc/pt-br/articles/|"
    r"/(?:jurisprudencia|processos?|reclame|publieditorial|post-patrocinado)/)"
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


def build_catalog(
    ev: Evidence,
    brand: BrandProfile,
    max_text_items: int = 60,
    discovery_hits: list[DiscoveryHit] | None = None,
    visual_matches: list[VisualMatch] | None = None,
) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    add = items.append
    for i, m in enumerate(visual_matches or [], 1):
        x, y, w, h = m.bbox
        add(
            EvidenceItem(
                ref=f"E-VIS-LEGACY-{i:03d}",
                kind="visual",
                location="viewport" if m.above_fold else "page",
                above_fold=m.above_fold,
                content=(
                    f"Comparação de imagem (determinística, sem IA): logotipo ANTIGO de {brand.display_name} "
                    f"(B vermelho sobre V azul) em {m.screenshot_ref}, posição x={x} y={y}, {w}x{h}px, "
                    f"similaridade {m.score:.2f}" + (f"; imagem: {m.image_src[:200]}" if m.image_src else "")
                ),
            )
        )
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
    if discovery_hits:
        seen_disc: set[str] = set()
        disc_idx = 0
        for h in discovery_hits:
            parts = [x.strip() for x in (h.title or "", h.snippet or "") if x and x.strip()]
            if not parts:
                continue
            combined = " — ".join(parts)[:1000]
            if combined in seen_disc:
                continue
            seen_disc.add(combined)
            disc_idx += 1
            add(
                EvidenceItem(
                    ref=f"E-DISC-{disc_idx:03d}",
                    kind="text",
                    location="search_index",
                    content=f"Índice de busca ({h.source}): {combined}",
                )
            )
            if disc_idx >= 5:
                break
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
    if p is None or not ev.usable:
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
    is_social = domain_matches(hostname_of(url), ("instagram.com", "facebook.com", "tiktok.com"))
    brandish = [
        img
        for img in p.images
        if _image_is_brandish(img, brand) or (is_social and img.above_fold and img.width >= 200 and img.height >= 200)
    ]
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
    return bool(
        brand.mentions(blob)
        or "logo" in fold(blob)
        or LEGACY_LOGO_RE.search(blob)
        or brand.domain_brand_match(img.src)
    )


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
    legacy_logo_markers: list[Hit] = field(default_factory=list)
    legacy_logo_visual: list[Hit] = field(default_factory=list)  # deterministic image matches
    official_claims: list[Hit] = field(default_factory=list)
    brand_linked_claims: list[Hit] = field(default_factory=list)
    disclosures: list[Hit] = field(default_factory=list)
    disclosure_above_fold: bool = False
    disclosure_names_institution: bool = False
    disclosure_locations: set[str] = field(default_factory=set)
    partnership: list[Hit] = field(default_factory=list)
    partnership_with_brand: list[Hit] = field(default_factory=list)
    reported_claims: list[Hit] = field(default_factory=list)  # third-person / news / warnings
    weak_mentions: int = 0
    has_discovery_evidence: bool = False
    official_channel: bool = False
    article_page: bool = False
    editorial_context: bool = False
    homonym_context: bool = False
    competitor_banks: list[str] = field(default_factory=list)
    multi_bank_context: bool = False
    sensitive_service_claims: list[Hit] = field(default_factory=list)
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
            "indícios de logotipo antigo/legado (B vermelho / V azul ou termo legado): "
            + (", ".join(f"'{h.value}' [{h.ref}]" for h in self.legacy_logo_markers[:5]) or "nenhum identificado"),
            "logotipo antigo confirmado por comparação de imagem: "
            + (", ".join(f"[{h.ref}] {h.value}" for h in self.legacy_logo_visual[:3]) or "não"),
            "expressões de oficialidade: "
            + (", ".join(f"'{h.value}' [{h.ref}]" for h in self.official_claims[:8]) or "nenhuma"),
            "expressões de oficialidade próximas à marca: "
            + (", ".join(f"'{h.value}' [{h.ref}]" for h in self.brand_linked_claims[:8]) or "nenhuma"),
            "serviços sensíveis (2ª via de boleto/quitação/acordo) associados à marca: "
            + (", ".join(f"'{h.value}' [{h.ref}]" for h in self.sensitive_service_claims[:6]) or "nenhum"),
            "declarações de correspondente/independência: "
            + (", ".join(f"'{h.value}' [{h.ref}]" for h in self.disclosures[:8]) or "nenhuma encontrada")
            + f"; locais: {', '.join(sorted(self.disclosure_locations)) or '-'}"
            + f"; na área visível inicial/cabeçalho: {'sim' if self.disclosure_above_fold else 'não'}"
            + f"; nomeia a instituição: {'sim' if self.disclosure_names_institution else 'não'}"
            + " (tamanho, contraste e legibilidade não medidos automaticamente)",
            "menções a parceria: "
            + (", ".join(f"'{h.value}' [{h.ref}]" for h in self.partnership[:5]) or "nenhuma")
            + f" (associadas à marca: {len(self.partnership_with_brand)})",
            "contexto multimarcas/multibancos (outras instituições financeiras na página): "
            + (
                f"sim ({', '.join(self.competitor_banks[:6])})"
                if self.multi_bank_context
                else (f"parcial ({', '.join(self.competitor_banks[:4])})" if self.competitor_banks else "não")
            ),
            "expressões de oficialidade em discurso de terceiros (notícia/alerta): "
            + (", ".join(f"'{h.value}' [{h.ref}]" for h in self.reported_claims[:5]) or "nenhuma"),
            f"página do tipo artigo/notícia (og:type): {'sim' if self.article_page else 'não'}; "
            f"contexto editorial/informativo/reclamação: {'sim' if self.editorial_context else 'não'}",
            f"ouvidoria mencionada: {'sim' if self.ouvidoria else 'não'}",
            f"links para domínio oficial da instituição: {'sim' if self.official_links else 'não'}",
            f"contatos WhatsApp: {len(self.whatsapp_contacts)}; telefones: {len(self.phones)}",
            "CNPJs: " + (", ".join(f"{h.value} [{h.ref}]" for h in self.cnpjs[:5]) or "nenhum"),
            f"formulários na página: {self.forms_present}",
        ]
        if self.official_channel:
            out.append("canal/domínio oficial da própria instituição identificado na URL final")
        if self.has_discovery_evidence and not self.usable:
            out.append(
                "acesso direto pelo navegador indisponível/bloqueado; sinais extraídos das evidências "
                "indexadas na descoberta (E-DISC-*)"
            )
        if self.homonym_context and self.brand_mentions == 0:
            out.append(
                "observação: indício de homônimo da sigla 'BV' (ex.: Boa Vista/RR ou agência de "
                "publicidade/modelos) sem menção explícita ao nome completo da instituição"
            )
        if self.injection_markers:
            out.append(
                "ATENÇÃO: conteúdo com padrões de instrução a modelos de IA (possível prompt "
                "injection): " + ", ".join(f"[{h.ref}]" for h in self.injection_markers[:5])
            )
        return out


def compute_signals(ev: Evidence, brand: BrandProfile, catalog: list[EvidenceItem]) -> PageSignals:
    sig = PageSignals(usable=ev.usable)
    url = ev.final_url or ev.original_url
    host = hostname_of(url)
    p = ev.page if ev.usable else None
    disc_items = [i for i in catalog if i.ref.startswith("E-DISC-")]
    disc_text = "\n".join(i.content for i in disc_items)
    sig.has_discovery_evidence = any(len(i.content) >= 80 for i in disc_items)
    sig.official_channel = brand.is_official_url(url, title=p.title if p else disc_text[:200])
    sig.brand_in_domain = None if sig.official_channel else brand.domain_brand_match(host)
    if p is None and not sig.has_discovery_evidence:
        return sig
    if p is not None:
        sig.forms_present = p.forms_present
        full_text = "\n".join(filter(None, (p.title, p.header_text, p.above_fold_text, p.visible_text, p.footer_text)))
        sig.brand_mentions = len(brand.strong_mentions(p.visible_text))
        sig.weak_mentions = len(brand.weak_mentions(p.visible_text))
        sig.article_page = (p.meta.get("og:type") or "").strip().lower() in ("article", "news", "noticia")
    else:
        full_text = disc_text
        has_bank_ctx = bool(BANKING_CONTEXT_RE.search(disc_text))
        sig.brand_mentions = len(brand.strong_mentions(disc_text)) + (
            len(brand.weak_mentions(disc_text)) if has_bank_ctx else 0
        )
        sig.weak_mentions = len(brand.weak_mentions(disc_text))
        sig.article_page = False
    sig.editorial_context = sig.article_page or bool(EDITORIAL_URL_RE.search(url) or brand.is_deprioritized(host))
    folded_full = fold(full_text)
    has_explicit_bank_marker = (
        sig.brand_mentions > 0
        or bool(brand.strong_mentions(host))
        or bool(brand.sensitive_service_mentions(full_text))
        or bool(brand.disclosures(full_text))
        or bool(re.search(r"\b(banco|financeira|correspondente|sac\s+bv|0800)\b", folded_full))
    )
    sig.homonym_context = brand.has_homonym_context(full_text) and not has_explicit_bank_marker
    if sig.homonym_context and sig.brand_mentions == 0 and not brand.strong_mentions(host):
        sig.brand_in_domain = None
    sig.competitor_banks = brand.competitor_mentions(full_text)
    sig.multi_bank_context = len(sig.competitor_banks) >= 2 or (
        len(sig.competitor_banks) >= 1 and bool(MULTI_BANK_PHRASE_RE.search(full_text))
    )

    nd = brand.without_disclosures
    if p is not None:
        title_nd = f"{nd(p.title)} {nd(p.meta.get('og:site_name', ''))}".strip()
        header_nd = " ".join([nd(p.header_text), *(nd(n.text) for n in p.nav_items)]).strip()
        has_banking_context = (
            sig.brand_mentions > 0
            or bool(sig.brand_in_domain)
            or bool(brand.official_claims(full_text))
            or bool(brand.disclosures(full_text))
            or bool(BANKING_CONTEXT_RE.search(full_text))
        )
        allow_weak_identity = has_banking_context and not (sig.homonym_context and sig.brand_mentions == 0)
        title_has_competitors = bool(brand.competitor_mentions(title_nd)) and not sig.brand_in_domain
        header_has_competitors = bool(brand.competitor_mentions(header_nd)) and not sig.brand_in_domain

        sig.brand_in_title = bool(
            (brand.strong_mentions(title_nd) or (allow_weak_identity and brand.weak_mentions(title_nd)))
            and not title_has_competitors
        )
        sig.brand_in_header = bool(
            (brand.strong_mentions(header_nd) or (allow_weak_identity and brand.weak_mentions(header_nd)))
            and not header_has_competitors
        )
        sig.brand_above_fold = bool(brand.strong_mentions(nd(p.above_fold_text)))
        sig.disclosure_names_institution = any(
            _real_disclosures_naming_institution(brand, t) for t in (p.visible_text, p.header_text, p.footer_text)
        )
    else:
        sig.brand_above_fold = sig.brand_mentions > 0
        sig.disclosure_names_institution = bool(_real_disclosures_naming_institution(brand, disc_text))
    sig.official_links = any(i.ref == "E-OFFICIAL-LINKS" for i in catalog)
    for item in catalog:
        text = item.content
        if INJECTION_RE.search(text):  # every kind, including image alt text and metadata
            sig.injection_markers.append(Hit(INJECTION_RE.search(text).group(0), item.ref))  # type: ignore[union-attr]
        if item.kind == "image":
            # Only treat an image as a brand logo indicator when not a non-bank homonym page.
            if not (sig.homonym_context and sig.brand_mentions == 0 and not sig.brand_in_domain):
                sig.brand_images.append(Hit(text[:120], item.ref))
                if item.above_fold:
                    sig.brand_images_above_fold += 1
                if LEGACY_LOGO_RE.search(text):
                    sig.legacy_logo_markers.append(Hit(text[:120], item.ref))
            continue
        if item.kind == "visual":
            sig.legacy_logo_visual.append(Hit(text[text.find("em E-SHOT") :][:120], item.ref))
            sig.legacy_logo_markers.append(Hit("logotipo antigo (comparação de imagem)", item.ref))
            continue
        if item.kind in ("screenshot", "network", "domain"):
            continue
        # Do not count synthetic meta-descriptions inside E-DISC snippets ("sem exibir razão social /
        # correspondente bancário", "semelhante ao logotipo antigo") as facts observed on the page.
        is_disc = item.ref.startswith("E-DISC-")
        if LEGACY_LOGO_RE.search(text) and not is_disc:
            sig.legacy_logo_markers.append(Hit(LEGACY_LOGO_RE.search(text).group(0), item.ref))  # type: ignore[union-attr]
        for sentence in sentences(text):
            claims = brand.official_claims(sentence)
            if claims:
                if sig.homonym_context and sig.brand_mentions == 0 and not brand.strong_mentions(sentence):
                    claims = [c for c in claims if brand.strong_mentions(c)]
            if claims:
                # brand-linked: the claim phrase itself names the brand ("SAC BV") or the sentence does
                linked = any(brand.mentions(c) for c in claims) or bool(brand.mentions(sentence))
                reported = bool(REPORTED_RE.search(fold(sentence)))
                for c in claims:
                    sig.official_claims.append(Hit(c, item.ref, sentence[:200]))
                    if linked and reported:
                        sig.reported_claims.append(Hit(c, item.ref, sentence[:200]))
                    elif linked:
                        sig.brand_linked_claims.append(Hit(c, item.ref, sentence[:200]))
            sens = brand.sensitive_service_mentions(sentence)
            if sens and not REPORTED_RE.search(fold(sentence)):
                if brand.mentions(sentence) or sig.brand_in_title or sig.brand_in_domain or sig.brand_mentions > 0:
                    for s_val in sens:
                        sig.sensitive_service_claims.append(Hit(s_val, item.ref, sentence[:200]))
            for pm in brand.partnership_mentions(sentence):
                sig.partnership.append(Hit(pm, item.ref, sentence[:200]))
                if brand.mentions(sentence):
                    sig.partnership_with_brand.append(Hit(pm, item.ref, sentence[:200]))
        if not is_disc:
            for d in brand.disclosures(text):
                sig.disclosures.append(Hit(d, item.ref, text[:200]))
                sig.disclosure_locations.add(item.location)
                if item.location in ("viewport", "header") or item.above_fold:
                    sig.disclosure_above_fold = True
        folded = fold(text)
        if "ouvidoria" in folded and not is_disc:
            sig.ouvidoria.append(Hit("ouvidoria", item.ref, text[:200]))
        if (item.kind == "contact" or is_disc) and ("wa.me" in folded or "whatsapp" in folded):
            sig.whatsapp_contacts.append(Hit(text[:200], item.ref))
        for m in PHONE_RE.finditer(text):
            sig.phones.append(Hit(m.group(1), item.ref, text[:200]))
        for m in CNPJ_RE.finditer(text.upper()):
            raw = m.group(1)
            if sum(ch.isdigit() for ch in raw) >= 8:  # avoid matching random uppercase words
                sig.cnpjs.append(Hit(format_cnpj(raw), item.ref, text[:300]))
    # Disclosure in the above-fold text counts even if only the E-TXT copy matched.
    if p is not None and (brand.disclosures(p.above_fold_text) or brand.disclosures(p.header_text)):
        sig.disclosure_above_fold = True
    return sig


def _real_disclosures_naming_institution(brand: BrandProfile, text: str) -> list[str]:
    """Disclosure sentences naming the institution that are not negated/contradicted in the same
    sentence (e.g. "Não somos correspondente bancário: somos o Banco BV oficial")."""
    return [s for s in brand.disclosures_naming_institution(text) if not brand.official_claims(s)]


# --------------------------------------------------------------------------------- agent
class EvidenceAgent:
    def __init__(self, collector: BrowserCollector, campaign_dir: Path) -> None:
        self.collector = collector
        self.campaign_dir = campaign_dir

    async def collect(self, candidate_id: str, url: str, attempt: int) -> Evidence:
        return await self.collector.collect(candidate_id, url, attempt, self.campaign_dir)
