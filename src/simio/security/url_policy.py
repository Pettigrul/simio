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

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_EXTRA_BLOCKED_NETS = [
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

BLOCKED_HOSTNAMES = frozenset(
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
BLOCKED_SUFFIXES = (
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


class PolicyViolation(ValueError):
    """Raised when a URL is not allowed to be fetched."""


def ip_is_public(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip_is_public(ip.ipv4_mapped)
        if ip.sixtofour is not None and not ip_is_public(ip.sixtofour):
            return False
        if ip.teredo is not None and not all(ip_is_public(x) for x in ip.teredo):
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
    return not any(ip in net for net in _EXTRA_BLOCKED_NETS if net.version == ip.version)


@dataclass
class UrlPolicy:
    allowed_ports: frozenset[int] = frozenset({80, 443, 8080, 8443})
    # Hosts explicitly allowed to bypass the private-address check. Tests only.
    unsafe_allow_hosts: frozenset[str] = frozenset()
    dns_cache_ttl_s: float = 60.0
    _cache: dict[str, tuple[float, list[str]]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_settings(cls, allowed_ports: Iterable[int], unsafe_allow_hosts: Iterable[str]) -> UrlPolicy:
        return cls(
            allowed_ports=frozenset(int(p) for p in allowed_ports),
            unsafe_allow_hosts=frozenset(h.lower() for h in unsafe_allow_hosts),
        )

    # ------------------------------------------------------------------ static checks
    def check_static(self, url: str) -> tuple[str, int]:
        """Validate syntax-level rules. Returns (hostname, port)."""
        if not url or len(url) > 4096:
            raise PolicyViolation("empty or oversized URL")
        if any(ch in url for ch in ("\x00", "\r", "\n", "\t", "\\")):
            raise PolicyViolation("control characters or backslash in URL")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise PolicyViolation(f"malformed URL: {exc}") from exc
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise PolicyViolation(f"scheme not allowed: {scheme or '(none)'}")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise PolicyViolation("credentials in URL are not allowed")
        host = (parts.hostname or "").rstrip(".").lower()
        if not host:
            raise PolicyViolation("missing host")
        if port is None:
            port = 443 if scheme == "https" else 80
        if host in self.unsafe_allow_hosts:
            return host, port
        if port not in self.allowed_ports:
            raise PolicyViolation(f"port {port} not allowed")
        if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
            raise PolicyViolation(f"internal hostname blocked: {host}")
        if "." not in host and ":" not in host:
            # Single-label names resolve via local search domains (intranet).
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise PolicyViolation(f"single-label hostname blocked: {host}") from None
        literal = _parse_ip_literal(host)
        if literal is not None and not ip_is_public(literal):
            raise PolicyViolation(f"non-public IP literal blocked: {host}")
        return host, port

    # ------------------------------------------------------------------ resolution
    def _resolve_sync(self, host: str, port: int) -> list[str]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return sorted({str(info[4][0]) for info in infos})

    def _check_addresses(self, host: str, addresses: list[str]) -> None:
        if not addresses:
            raise PolicyViolation(f"{host} did not resolve")
        for addr in addresses:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
            if not ip_is_public(ip):
                raise PolicyViolation(f"{host} resolves to non-public address {ip}")

    def check(self, url: str) -> str:
        """Full synchronous check (static + DNS). Returns hostname."""
        host, port = self.check_static(url)
        if host in self.unsafe_allow_hosts:
            return host
        addresses = self._cached(host)
        if addresses is None:
            try:
                addresses = self._resolve_sync(host, port)
            except OSError as exc:
                raise PolicyViolation(f"DNS resolution failed for {host}: {exc}") from exc
            self._store(host, addresses)
        self._check_addresses(host, addresses)
        return host

    async def acheck(self, url: str) -> str:
        """Async check, resolution runs in the default executor."""
        host, port = self.check_static(url)
        if host in self.unsafe_allow_hosts:
            return host
        addresses = self._cached(host)
        if addresses is None:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise PolicyViolation(f"DNS resolution failed for {host}: {exc}") from exc
            addresses = sorted({str(i[4][0]) for i in infos})
            self._store(host, addresses)
        self._check_addresses(host, addresses)
        return host

    async def aresolve_checked(self, url: str) -> list[str]:
        """Validate ``url`` and return the resolved addresses that were validated.

        Used by the egress proxy, which then connects to one of *these* addresses (DNS pinning).
        """
        host, port = self.check_static(url)
        addresses = self._cached(host)
        if addresses is None:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise PolicyViolation(f"DNS resolution failed for {host}: {exc}") from exc
            addresses = sorted({str(i[4][0]) for i in infos})
            self._store(host, addresses)
        if host not in self.unsafe_allow_hosts:
            self._check_addresses(host, addresses)
        if not addresses:
            raise PolicyViolation(f"{host} did not resolve")
        return addresses

    def check_server_address(self, host: str, ip: str | None) -> None:
        """Post-connection verification of the address actually used by the browser."""
        if not ip or host in self.unsafe_allow_hosts:
            return
        try:
            addr = ipaddress.ip_address(ip.strip("[]").split("%", 1)[0])
        except ValueError:
            return
        if not ip_is_public(addr):
            raise PolicyViolation(f"browser connected to non-public address {addr} for {host}")

    def _cached(self, host: str) -> list[str] | None:
        hit = self._cache.get(host)
        if hit and time.monotonic() - hit[0] < self.dns_cache_ttl_s:
            return hit[1]
        return None

    def _store(self, host: str, addresses: list[str]) -> None:
        self._cache[host] = (time.monotonic(), addresses)


def _parse_ip_literal(host: str) -> IPAddress | None:
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
        parts = h.split(".")
        if (
            len(parts) == 4
            and all(p and p.isdigit() for p in parts)
            and any(p.startswith("0") and len(p) > 1 for p in parts)
        ):
            return ipaddress.IPv4Address(".".join(str(int(p, 8)) for p in parts))
    except (ValueError, ipaddress.AddressValueError):
        return None
    return None
