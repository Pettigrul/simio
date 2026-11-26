from __future__ import annotations

import contextlib
import re
from functools import lru_cache
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import tldextract

PARAMS_RASTREAMENTO = frozenset(
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
PREFIXOS_RASTREAMENTO = ("utm_", "pk_", "mtm_", "hsa_", "vero_", "_hs")
HOSTS_SOCIAIS_COM_LOCALIDADE = (
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
PARAMS_LOCALIDADE_SOCIAL = frozenset({"hl", "locale", "lang"})
PORTAS_PADRAO = {"http": 80, "https": 443}

_URL_RE = re.compile(
    r"""(?xi)
    \b(
      https?://[^\s<>"'`{}|\\^\[\]]+
      |
      www\.[a-z0-9-]+(?:\.[a-z0-9-]+)+[^\s<>"'`{}|\\^\[\]]*
    )
    """
)
_PONTUACAO_FINAL = ".,;:!?)]}'\"»”"


@lru_cache(maxsize=1)
def _extrator() -> tldextract.TLDExtract:
    # suffix_list_urls=() usa a cópia da Public Suffix List que vem no pacote, sem acessar a rede.
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def dominio_registravel(hostname: str) -> str:
    host = hostname.lower().rstrip(".")
    ext = _extrator()(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return host


def normalizar_url(url: str) -> str:
    # Chave de deduplicação: http e https caem na mesma URL (a mesma página nos dois esquemas é
    # uma candidata só).
    bruto = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", bruto):
        bruto = "https://" + bruto
    partes = urlsplit(bruto)
    esquema_url = partes.scheme.lower()
    host = (partes.hostname or "").lower().rstrip(".")
    with contextlib.suppress(UnicodeError):
        host = host.encode("idna").decode("ascii") if host else host
    if host.startswith("www."):
        host = host[4:]
    porta = partes.port
    netloc = host if porta in (None, PORTAS_PADRAO.get(esquema_url)) else f"{host}:{porta}"
    caminho_bruto = re.sub(r";jsessionid=[^/?#]+", "", partes.path or "/", flags=re.IGNORECASE)
    caminho = quote(unquote(caminho_bruto), safe="/:@!$&'()*+,;=-._~%")
    caminho = re.sub(r"/{2,}", "/", caminho)
    for sufixo in ("/index.html", "/index.htm", "/index.php"):
        if caminho.endswith(sufixo):
            caminho = caminho[: -len(sufixo) + 1]
    if len(caminho) > 1:
        caminho = caminho.rstrip("/")
    remover_localidade = dominio_corresponde(host, HOSTS_SOCIAIS_COM_LOCALIDADE)
    itens_consulta = [
        (k, v)
        for k, v in parse_qsl(partes.query, keep_blank_values=True)
        if k.lower() not in PARAMS_RASTREAMENTO
        and not k.lower().startswith(PREFIXOS_RASTREAMENTO)
        and not (remover_localidade and k.lower() in PARAMS_LOCALIDADE_SOCIAL)
    ]
    consulta = urlencode(sorted(itens_consulta))
    return urlunsplit(("https" if esquema_url in ("http", "https") else esquema_url, netloc, caminho, consulta, ""))


def hostname_de(url: str) -> str:
    # "" para URL malformada, comum em links de página como ``http://[quebrado``.
    try:
        return (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def extrair_urls(texto: str) -> list[str]:
    vistos: set[str] = set()
    saida: list[str] = []
    for correspondencia in _URL_RE.finditer(texto or ""):
        candidata = correspondencia.group(1).rstrip(_PONTUACAO_FINAL)
        # TODO: o rstrip corta o ")" final que faz parte da URL (ex.: wikipedia.org/wiki/Foo_(bar)).
        if candidata.lower().startswith("www."):
            candidata = "https://" + candidata
        if candidata not in vistos:
            vistos.add(candidata)
            saida.append(candidata)
    return saida


def dominio_corresponde(hostname: str, dominios: list[str] | tuple[str, ...] | set[str]) -> bool:
    host = hostname.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in (x.lower() for x in dominios))
