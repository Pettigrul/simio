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

from simio.marcas import carregar_marca
from simio.nucleo.modelos import DecisaoFinal, InteligenciaDominio, InteligenciaIp, InteligenciaRdap
from simio.nucleo.traducao_legado import traduzir_modelo
from simio.nucleo.urls import dominio_corresponde, dominio_registravel, hostname_de

if TYPE_CHECKING:
    from simio.configuracoes import Configuracoes
    from simio.nucleo.armazenamento import Armazenamento

log = logging.getLogger(__name__)

_TIPOS_DNS = {"A": 1, "AAAA": 28, "CNAME": 5, "NS": 2, "MX": 15}
# Platforms where the page owner never shows up in RDAP/DNS (besides the Public Suffix List private part).
_HOSTS_PLATAFORMA = (
    "weebly.com", "wixsite.com", "site123.me", "lovable.app", "vercel.app", "netlify.app", "pages.dev",
    "github.io", "web.app", "firebaseapp.com", "blogspot.com", "wordpress.com", "godaddysites.com",
    "hostingersite.com", "webnode.page", "canva.site", "framer.website", "carrd.co", "glitch.me", "replit.app",
    "onrender.com", "herokuapp.com", "softonic.com",
)
_HOSTS_SOCIAIS = (
    "instagram.com", "facebook.com", "tiktok.com", "linkedin.com", "youtube.com", "x.com", "twitter.com",
    "threads.net", "kwai.com", "t.me", "wa.me", "whatsapp.com", "pinterest.com",
)


@lru_cache(maxsize=1)
def _extrator_privado() -> tldextract.TLDExtract:
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)


def plataforma_hospedagem(hostname: str) -> str | None:
    """The shared platform a host is a tenant of (``x.weebly.com`` -> ``weebly.com``), if any."""
    host = hostname.lower().rstrip(".")
    reg = dominio_registravel(host)
    if host != reg and dominio_corresponde(host, _HOSTS_PLATAFORMA):
        return reg
    ext = _extrator_privado()(host)
    if ext.is_private and ext.suffix and host != ext.suffix:
        return ext.suffix
    return None


def _vcard(entidade: dict[str, Any]) -> dict[str, str]:
    saida: dict[str, str] = {}
    try:
        for item in entidade.get("vcardArray", [None, []])[1]:
            if item[0] in ("fn", "kind", "email", "org") and item[3] and item[0] not in saida:
                saida[item[0]] = str(item[3] if not isinstance(item[3], list) else " ".join(item[3])).strip()
    except (IndexError, TypeError):
        pass
    return saida


def _percorrer(entidades: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    saida: list[dict[str, Any]] = []
    for e in entidades or []:
        saida.append(e)
        saida.extend(_percorrer(e.get("entities")))
    return saida


def _emails_abuso(entidades: list[dict[str, Any]] | None) -> list[str]:
    emails = []
    for e in _percorrer(entidades):
        if "abuse" in (e.get("roles") or []):
            email = _vcard(e).get("email")
            if email and email not in emails:
                emails.append(email)
    return emails


def interpretar_rdap_dominio(dados: dict[str, Any], agora: datetime | None = None) -> InteligenciaRdap:
    eventos = {e.get("eventAction"): e.get("eventDate") for e in dados.get("events") or []}
    criado = eventos.get("registration")
    idade = None
    if criado:
        try:
            idade = ((agora or datetime.now(UTC)) - datetime.fromisoformat(criado.replace("Z", "+00:00"))).days
        except ValueError:
            idade = None
    registrador = titular = doc_titular = None
    tipo_titular = "não publicado"
    for e in _percorrer(dados.get("entities")):
        papeis = e.get("roles") or []
        vc = _vcard(e)
        if "registrar" in papeis and not registrador:
            registrador = vc.get("fn") or vc.get("org")
        if "registrant" in papeis:
            ids = {str(p.get("type", "")).lower(): p.get("identifier") for p in e.get("publicIds") or []}
            if "cpf" in ids or vc.get("kind") == "individual":
                tipo_titular = "pessoa física (dados pessoais omitidos — LGPD)"
            elif "cnpj" in ids or vc.get("kind") == "org" or vc.get("org"):
                nome = vc.get("fn") or vc.get("org") or ""
                if nome and "redacted" not in nome.lower() and "privacy" not in nome.lower():
                    titular, doc_titular, tipo_titular = nome, ids.get("cnpj"), "pessoa jurídica"
                else:
                    tipo_titular = "oculto (serviço de privacidade)"
    return InteligenciaRdap(
        registrador=registrador,
        criado=criado,
        expira=eventos.get("expiration"),
        atualizado=eventos.get("last changed"),
        idade_dias=idade,
        status=[str(s) for s in dados.get("status") or []][:6],
        servidores_nomes=[str(n.get("ldhName", "")).lower() for n in dados.get("nameservers") or []][:6],
        titular=titular,
        documento_titular=doc_titular,
        tipo_titular=tipo_titular,
        emails_abuso=_emails_abuso(dados.get("entities"))[:3],
    )


def interpretar_rdap_ip(ip: str, dados: dict[str, Any]) -> InteligenciaIp:
    org = None
    for e in _percorrer(dados.get("entities")):
        if "registrant" in (e.get("roles") or []):
            vc = _vcard(e)
            if vc.get("kind") != "individual":
                org = vc.get("fn") or vc.get("org")
                break
    return InteligenciaIp(
        ip=ip,
        rede=dados.get("name"),
        organizacao=org,
        pais=dados.get("country"),
        emails_abuso=_emails_abuso(dados.get("entities"))[:3],
    )


def _erro(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


class ColetorInteligenciaDominio:
    def __init__(
        self,
        base_rdap: str = "https://rdap.org",
        url_doh: str = "https://cloudflare-dns.com/dns-query",
        timeout: float = 20.0,
        transporte: httpx.AsyncBaseTransport | None = None,
        concorrencia: int = 4,
        retentativas: int = 4,
        espera_s: float = 5.0,
        intervalo_rdap_s: float = 1.2,
    ) -> None:
        self.retentativas = retentativas
        self.espera_s = espera_s
        # Registries (Registro.br, ARIN, RIPE...) throttle bursts: RDAP calls are spaced out globally.
        self.intervalo_rdap_s = intervalo_rdap_s
        self._trava_rdap = asyncio.Lock()
        self._ultimo_rdap = 0.0
        self.base_rdap = base_rdap.rstrip("/")
        self.url_doh = url_doh
        self.timeout = timeout
        self.transporte = transporte
        self._semaforo = asyncio.Semaphore(concorrencia)
        self._cache_rdap: dict[str, InteligenciaRdap | str] = {}
        self._cache_ip: dict[str, InteligenciaIp | str] = {}

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transporte,
            trust_env=self.transporte is None,
            follow_redirects=True,
            headers={"User-Agent": "simio/0.1", "Accept": "application/rdap+json, application/json"},
        )

    async def _json(self, c: httpx.AsyncClient, url: str, **kw: Any) -> dict[str, Any]:
        """GET with retries: RDAP redirectors and registries rate-limit bursts (429) and flap (5xx)."""
        for tentativa in range(self.retentativas + 1):
            if url.startswith(self.base_rdap):
                async with self._trava_rdap:
                    loop = asyncio.get_running_loop()
                    espera = self._ultimo_rdap + self.intervalo_rdap_s - loop.time()
                    if espera > 0:
                        await asyncio.sleep(espera)
                    self._ultimo_rdap = loop.time()
            async with self._semaforo:
                r = await c.get(url, **kw)
            if (r.status_code == 429 or r.status_code >= 500) and tentativa < self.retentativas:
                await asyncio.sleep(self.espera_s * 2**tentativa)
                continue
            r.raise_for_status()
            return r.json()
        raise AssertionError("unreachable")  # pragma: no cover

    async def _dns(self, c: httpx.AsyncClient, nome: str, tipo_registro: str) -> list[str]:
        dados = await self._json(
            c, self.url_doh, params={"name": nome, "type": tipo_registro}, headers={"accept": "application/dns-json"}
        )
        desejado = _TIPOS_DNS[tipo_registro]
        saida = []
        for a in dados.get("Answer") or []:
            if a.get("type") == desejado:
                valor = str(a.get("data", "")).rstrip(".")
                saida.append(valor.split(" ", 1)[1].rstrip(".") if tipo_registro == "MX" and " " in valor else valor)
        return saida[:8]

    async def _rdap_dominio(self, c: httpx.AsyncClient, dominio: str) -> InteligenciaRdap | str:
        if dominio not in self._cache_rdap:
            try:
                self._cache_rdap[dominio] = interpretar_rdap_dominio(
                    await self._json(c, f"{self.base_rdap}/domain/{dominio}")
                )
            except httpx.HTTPStatusError as exc:
                self._cache_rdap[dominio] = (
                    f"O registro de .{dominio.rsplit('.', 1)[-1]} não publica RDAP para este domínio (consultar WHOIS)"
                    if exc.response.status_code == 404
                    else f"RDAP do domínio indisponível: {_erro(exc)}"
                )
            except (httpx.HTTPError, ValueError) as exc:
                self._cache_rdap[dominio] = f"RDAP do domínio indisponível: {_erro(exc)}"
        return self._cache_rdap[dominio]

    async def _rdap_ip(self, c: httpx.AsyncClient, ip: str) -> InteligenciaIp | str:
        if ip not in self._cache_ip:
            try:
                self._cache_ip[ip] = interpretar_rdap_ip(ip, await self._json(c, f"{self.base_rdap}/ip/{ip}"))
            except (httpx.HTTPError, ValueError) as exc:
                self._cache_ip[ip] = f"RDAP do IP {ip} indisponível: {_erro(exc)}"
        return self._cache_ip[ip]

    async def coletar(self, hostname: str, ip_servidor: str | None = None) -> InteligenciaDominio:
        host = hostname.lower().rstrip(".")
        dominio = dominio_registravel(host)
        plataforma = plataforma_hospedagem(host)
        intel = InteligenciaDominio(hostname=host, dominio_registrado=dominio, plataforma=plataforma,
                            coletada_em=datetime.now(UTC).isoformat(timespec="seconds"))
        if plataforma:
            intel.notas.append(
                f"Página hospedada na plataforma {plataforma}: registro e DNS são da plataforma; o responsável pela "
                f"página só é identificável pela própria plataforma (denúncia de abuso ou ordem judicial)."
            )
        async with self._http() as c:
            async def dns(nome: str, tipo_registro: str) -> list[str]:
                try:
                    return await self._dns(c, nome, tipo_registro)
                except (httpx.HTTPError, ValueError) as exc:
                    intel.erros.append(f"DNS {tipo_registro} {nome}: {_erro(exc)}")
                    return []

            a, aaaa, cname, ns, mx = await asyncio.gather(
                dns(host, "A"), dns(host, "AAAA"), dns(host, "CNAME"), dns(dominio, "NS"), dns(dominio, "MX")
            )
            intel.a, intel.aaaa, intel.cname, intel.ns, intel.mx = a, aaaa, cname, ns, mx
            if not (a or aaaa or cname):
                intel.notas.append("O nome não resolve hoje (domínio fora do ar, suspenso ou expirado).")
            if not plataforma:  # a platform's registration says nothing about the page owner
                rdap = await self._rdap_dominio(c, dominio)
                if isinstance(rdap, str):
                    (intel.notas if "não publica RDAP" in rdap else intel.erros).append(rdap)
                else:
                    intel.rdap = rdap
            for ip in list(dict.fromkeys([*(a[:2]), *([ip_servidor] if ip_servidor else [])]))[:3]:
                info = await self._rdap_ip(c, ip)
                if isinstance(info, str):
                    intel.erros.append(info)
                else:
                    intel.ips.append(info)
        return intel


def resumir(intel: InteligenciaDominio) -> dict[str, str]:
    """Flat, human-readable fields for spreadsheets and dossiers."""
    # On a shared platform the registration data belongs to the platform, not to the page owner.
    r = None if intel.plataforma else intel.rdap
    criado = ""
    if r and r.criado:
        criado = r.criado[:10] + (f" ({r.idade_dias} dias)" if r.idade_dias is not None else "")
    titular = ""
    if r:
        titular = r.titular or r.tipo_titular
        if r.titular and r.documento_titular:
            titular = f"{r.titular} (CNPJ {r.documento_titular})"
    hospedagem = "; ".join(
        f"{i.organizacao or i.rede or '?'} ({i.ip}{', ' + i.pais if i.pais else ''})" for i in intel.ips
    )
    abuso_reg = intel.rdap.emails_abuso if intel.rdap and not intel.plataforma else []
    abuso = list(dict.fromkeys([*(e for i in intel.ips for e in i.emails_abuso), *abuso_reg]))
    return {
        "plataforma": intel.plataforma or "",
        "registrador": (r.registrador or "") if r else "",
        "criado": criado,
        "titular": titular or ("— (dono só identificável pela plataforma)" if intel.plataforma else ""),
        "ns": "" if intel.plataforma else ", ".join(intel.ns or (r.servidores_nomes if r else [])),
        "mx": "" if intel.plataforma else ", ".join(intel.mx),
        "resolve": "sim" if (intel.a or intel.aaaa or intel.cname) else "não",
        "hospedagem": hospedagem,
        "abuso": ", ".join(abuso[:4]),
        "notas": " ".join(intel.notas + intel.erros)[:600],
    }


ARQUIVO_INTELIGENCIA = "domain-intel.json"
SINALIZADAS = ("INDICIO_FORTE", "REQUER_REVISAO")


def caminho_inteligencia(dir_saida: Path, id_campanha: str) -> Path:
    return dir_saida / "campaigns" / id_campanha / ARQUIVO_INTELIGENCIA


def carregar_inteligencia(dir_saida: Path, id_campanha: str) -> dict[str, InteligenciaDominio]:
    """``hostname -> DomainIntel`` of a campaign (empty when it was never enriched)."""
    caminho = caminho_inteligencia(dir_saida, id_campanha)
    if not caminho.exists():
        return {}
    bruto = json.loads(caminho.read_text(encoding="utf-8"))
    # Files written by the English-named builds are read through the legacy translation (never rewritten here).
    return {h: InteligenciaDominio.model_validate(traduzir_modelo(InteligenciaDominio, v)) for h, v in bruto.items()}


async def enriquecer_campanha(
    configuracoes: Configuracoes, armazenamento: Armazenamento, id_campanha: str, atualizar: bool = False
) -> dict[str, InteligenciaDominio]:
    """Collect domain intelligence for every flagged page of a campaign (STRONG/REVIEW, asking for
    credentials, a lookalike or a domain carrying the brand), one lookup per host. Social profiles are
    skipped (the owner is the profile). Results already collected are kept unless ``atualizar``."""
    # Hosts whose lookup failed (rate limits, timeouts) are tried again on every run.
    camp = armazenamento.obter_campanha(id_campanha) or {}
    try:
        marca = carregar_marca(camp.get("marca") or "bv", configuracoes.arquivo_marca)
    except Exception:  # an unknown brand only narrows the targets
        marca = None
    conhecidos = (
        {}
        if atualizar
        else {h: v for h, v in carregar_inteligencia(configuracoes.dir_saida, id_campanha).items() if not v.erros}
    )
    alvos: dict[str, str | None] = {}
    for c in armazenamento.listar_candidatas(id_campanha):
        final = armazenamento.obter_avaliacao(c.id, "final", DecisaoFinal)
        if final is None:
            continue
        mais_recente = armazenamento.evidencia_mais_recente_com_id(c.id)
        ev = mais_recente[1] if mais_recente else None
        host = hostname_de((ev.url_final if ev else None) or c.url) or c.hostname
        semelhante = any(h.fonte == "typosquat" for h in armazenamento.resultados_descoberta(c.id))
        dominio_marca = bool(marca and not marca.eh_oficial(host) and marca.correspondencia_marca_dominio(host))
        if final.classificacao.value not in SINALIZADAS and not final.dados_sensiveis_solicitados and not (
            semelhante or dominio_marca
        ):
            continue
        if not host or dominio_corresponde(host, _HOSTS_SOCIAIS) or host in conhecidos:
            continue
        alvos.setdefault(host, ev.ip_servidor if ev else None)
    coletor = ColetorInteligenciaDominio(
        base_rdap=configuracoes.url_base_rdap, url_doh=configuracoes.url_doh, timeout=configuracoes.timeout_http_s
    )

    async def um(host: str, ip: str | None) -> tuple[str, InteligenciaDominio]:
        try:
            return host, await coletor.coletar(host, ip)
        except Exception as exc:  # one host never stops the others
            log.warning("domain intel failed for %s: %s", host, exc)
            return host, InteligenciaDominio(
                hostname=host, dominio_registrado=dominio_registravel(host), erros=[str(exc)[:300]]
            )

    retornos = dict(await asyncio.gather(*(um(h, ip) for h, ip in alvos.items())))
    mesclado = {**conhecidos, **retornos}
    caminho = caminho_inteligencia(configuracoes.dir_saida, id_campanha)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(
        json.dumps({h: v.model_dump(mode="json") for h, v in sorted(mesclado.items())}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return mesclado
