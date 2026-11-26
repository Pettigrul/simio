"""Outbound URL policy (SSRF protection).

Every URL the system fetches -- top-level navigations, browser sub-resources, redirects,
grounding redirect resolution and connector calls -- goes through :class:`UrlPolicy`.

Rules:
* only ``http``/``https``; no embedded credentials; allowed ports only;
* hostname must resolve, and **every** resolved address must be public
  (no loopback, private, link-local, CGNAT, multicast, reserved, unspecified,
  IPv4-mapped/6to4/Teredo-embedded private addresses, cloud metadata);
* blocked hostnames/suffixes (``localhost``, ``*.internal``, ``metadata.google.internal``...).

Residual risk (documented in docs/security.md): Chromium performs its own DNS resolution after
our check (TOCTOU / DNS rebinding). The collector therefore also verifies the *actual* server
address of every response and discards evidence if a private address was reached.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

EnderecoIP = ipaddress.IPv4Address | ipaddress.IPv6Address

_REDES_BLOQUEADAS_EXTRAS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",  # CGNAT / shared address space
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "64:ff9b::/96",  # NAT64 (could embed private v4)
        "64:ff9b:1::/48",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
        "::/128",
        "::1/128",
    )
]

HOSTNAMES_BLOQUEADOS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
SUFIXOS_BLOQUEADOS = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".corp",
    ".home",
    ".lan",
    ".localdomain",
    ".home.arpa",
    ".in-addr.arpa",
    ".ip6.arpa",
)


class ViolacaoPolitica(ValueError):
    """Raised when a URL is not allowed to be fetched."""


def ip_eh_publico(ip: EnderecoIP) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip_eh_publico(ip.ipv4_mapped)
        if ip.sixtofour is not None and not ip_eh_publico(ip.sixtofour):
            return False
        if ip.teredo is not None and not all(ip_eh_publico(x) for x in ip.teredo):
            return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
    ):
        return False
    return not any(ip in rede for rede in _REDES_BLOQUEADAS_EXTRAS if rede.version == ip.version)


@dataclass
class PoliticaUrl:
    portas_permitidas: frozenset[int] = frozenset({80, 443, 8080, 8443})
    # Hosts explicitly allowed to bypass the private-address check. Tests only.
    hosts_liberados_inseguro: frozenset[str] = frozenset()
    ttl_cache_dns_s: float = 60.0
    _cache: dict[str, tuple[float, list[str]]] = field(default_factory=dict, repr=False)

    @classmethod
    def das_configuracoes(
        cls, portas_permitidas: Iterable[int], hosts_liberados_inseguro: Iterable[str]
    ) -> PoliticaUrl:
        return cls(
            portas_permitidas=frozenset(int(p) for p in portas_permitidas),
            hosts_liberados_inseguro=frozenset(h.lower() for h in hosts_liberados_inseguro),
        )

    # ------------------------------------------------------------------ static checks
    def verificar_estatico(self, url: str) -> tuple[str, int]:
        """Validate syntax-level rules. Returns (hostname, port)."""
        if not url or len(url) > 4096:
            raise ViolacaoPolitica("empty or oversized URL")
        if any(ch in url for ch in ("\x00", "\r", "\n", "\t", "\\")):
            raise ViolacaoPolitica("control characters or backslash in URL")
        try:
            partes = urlsplit(url)
            porta = partes.port
        except ValueError as exc:
            raise ViolacaoPolitica(f"malformed URL: {exc}") from exc
        esquema_url = partes.scheme.lower()
        if esquema_url not in ("http", "https"):
            raise ViolacaoPolitica(f"scheme not allowed: {esquema_url or '(none)'}")
        if partes.username is not None or partes.password is not None or "@" in partes.netloc:
            raise ViolacaoPolitica("credentials in URL are not allowed")
        host = (partes.hostname or "").rstrip(".").lower()
        if not host:
            raise ViolacaoPolitica("missing host")
        if porta is None:
            porta = 443 if esquema_url == "https" else 80
        if host in self.hosts_liberados_inseguro:
            return host, porta
        if porta not in self.portas_permitidas:
            raise ViolacaoPolitica(f"port {porta} not allowed")
        if host in HOSTNAMES_BLOQUEADOS or host.endswith(SUFIXOS_BLOQUEADOS):
            raise ViolacaoPolitica(f"internal hostname blocked: {host}")
        if "." not in host and ":" not in host:
            # Single-label names resolve via local search domains (intranet).
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise ViolacaoPolitica(f"single-label hostname blocked: {host}") from None
        literal = _interpretar_ip_literal(host)
        if literal is not None and not ip_eh_publico(literal):
            raise ViolacaoPolitica(f"non-public IP literal blocked: {host}")
        return host, porta

    # ------------------------------------------------------------------ resolution
    def _resolver_sincrono(self, host: str, porta: int) -> list[str]:
        infos = socket.getaddrinfo(host, porta, type=socket.SOCK_STREAM)
        return sorted({str(info[4][0]) for info in infos})

    def _verificar_enderecos(self, host: str, enderecos: list[str]) -> None:
        if not enderecos:
            raise ViolacaoPolitica(f"{host} did not resolve")
        for addr in enderecos:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
            if not ip_eh_publico(ip):
                raise ViolacaoPolitica(f"{host} resolves to non-public address {ip}")

    def verificar(self, url: str) -> str:
        """Full synchronous check (static + DNS). Returns hostname."""
        host, porta = self.verificar_estatico(url)
        if host in self.hosts_liberados_inseguro:
            return host
        enderecos = self._do_cache(host)
        if enderecos is None:
            try:
                enderecos = self._resolver_sincrono(host, porta)
            except OSError as exc:
                raise ViolacaoPolitica(f"DNS resolution failed for {host}: {exc}") from exc
            self._guardar(host, enderecos)
        self._verificar_enderecos(host, enderecos)
        return host

    async def verificar_async(self, url: str) -> str:
        """Async check, resolution runs in the default executor."""
        host, porta = self.verificar_estatico(url)
        if host in self.hosts_liberados_inseguro:
            return host
        enderecos = self._do_cache(host)
        if enderecos is None:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, porta, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise ViolacaoPolitica(f"DNS resolution failed for {host}: {exc}") from exc
            enderecos = sorted({str(i[4][0]) for i in infos})
            self._guardar(host, enderecos)
        self._verificar_enderecos(host, enderecos)
        return host

    async def resolver_verificado_async(self, url: str) -> list[str]:
        """Validate ``url`` and return the resolved addresses that were validated.

        Used by the egress proxy, which then connects to one of *these* addresses (DNS pinning).
        """
        host, porta = self.verificar_estatico(url)
        enderecos = self._do_cache(host)
        if enderecos is None:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, porta, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise ViolacaoPolitica(f"DNS resolution failed for {host}: {exc}") from exc
            enderecos = sorted({str(i[4][0]) for i in infos})
            self._guardar(host, enderecos)
        if host not in self.hosts_liberados_inseguro:
            self._verificar_enderecos(host, enderecos)
        if not enderecos:
            raise ViolacaoPolitica(f"{host} did not resolve")
        return enderecos

    def verificar_endereco_servidor(self, host: str, ip: str | None) -> None:
        """Post-connection verification of the address actually used by the browser."""
        if not ip or host in self.hosts_liberados_inseguro:
            return
        try:
            addr = ipaddress.ip_address(ip.strip("[]").split("%", 1)[0])
        except ValueError:
            return
        if not ip_eh_publico(addr):
            raise ViolacaoPolitica(f"browser connected to non-public address {addr} for {host}")

    def _do_cache(self, host: str) -> list[str] | None:
        resultado = self._cache.get(host)
        if resultado and time.monotonic() - resultado[0] < self.ttl_cache_dns_s:
            return resultado[1]
        return None

    def _guardar(self, host: str, enderecos: list[str]) -> None:
        self._cache[host] = (time.monotonic(), enderecos)


def _interpretar_ip_literal(host: str) -> EnderecoIP | None:
    h = host.strip("[]")
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    # Legacy numeric forms accepted by some resolvers: 2130706433, 0x7f000001, 0177.0.0.1
    try:
        if h.isdigit():
            return ipaddress.IPv4Address(int(h))
        if h.lower().startswith("0x") and all(c in "0123456789abcdef" for c in h[2:].lower()):
            return ipaddress.IPv4Address(int(h, 16))
        partes = h.split(".")
        if (
            len(partes) == 4
            and all(p and p.isdigit() for p in partes)
            and any(p.startswith("0") and len(p) > 1 for p in partes)
        ):
            return ipaddress.IPv4Address(".".join(str(int(p, 8)) for p in partes))
    except (ValueError, ipaddress.AddressValueError):
        return None
    return None
