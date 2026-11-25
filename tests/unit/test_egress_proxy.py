"""Egress proxy: network-layer enforcement of the URL policy (no browser needed)."""

from __future__ import annotations

import asyncio

from simio.security.egress_proxy import EgressProxy, _split_hostport
from simio.security.url_policy import UrlPolicy


async def _raw(port: int, payload: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(payload)
    await w.drain()
    data = await asyncio.wait_for(r.read(65536), 5)
    if b"Content-Length:" in data and data.endswith(b"\r\n\r\n"):
        data += await asyncio.wait_for(r.read(65536), 5)
    w.close()
    return data


async def _origin() -> tuple[asyncio.base_events.Server, int, list[bytes]]:
    seen: list[bytes] = []

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = await r.readuntil(b"\r\n\r\n")
        seen.append(head)
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await w.drain()
        w.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], seen


async def test_connect_to_private_is_refused() -> None:
    async with EgressProxy(UrlPolicy()) as proxy:
        out = await _raw(proxy.port, b"CONNECT 169.254.169.254:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403")
        out = await _raw(proxy.port, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403")
        assert len(proxy.blocked) == 2


async def test_plain_http_to_private_is_refused() -> None:
    async with EgressProxy(UrlPolicy()) as proxy:
        out = await _raw(proxy.port, b"GET http://10.1.1.1/admin HTTP/1.1\r\nHost: 10.1.1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403")
        out = await _raw(proxy.port, b"GET /relative HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 400")


async def test_allowed_http_is_forwarded_with_connection_close_and_pinned_ip() -> None:
    srv, port, seen = await _origin()
    policy = UrlPolicy(unsafe_allow_hosts=frozenset({"127.0.0.1"}))
    async with EgressProxy(policy) as proxy:
        out = await _raw(
            proxy.port,
            f"GET http://127.0.0.1:{port}/p?q=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\n\r\n".encode(),
        )
        assert out.endswith(b"ok")
        head = seen[0].decode()
        assert head.startswith("GET /p?q=1 HTTP/1.1")
        assert "Connection: close" in head and "Proxy-Connection" not in head and "keep-alive" not in head
        assert proxy.ip_for("127.0.0.1") == "127.0.0.1"
    srv.close()


async def test_dns_answer_is_validated_and_pinned(monkeypatch) -> None:
    policy = UrlPolicy()
    loop = asyncio.get_running_loop()

    async def rebinding(host, port, type=0):
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(loop, "getaddrinfo", rebinding)
    async with EgressProxy(policy) as proxy:
        out = await _raw(proxy.port, b"CONNECT rebind.example.com:443 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403") and "non-public" in proxy.blocked[0]


async def test_ipv6_first_answer_falls_back_to_reachable_ipv4(monkeypatch) -> None:
    """Hosts like Meta's answer AAAA first; without an IPv6 route the proxy must still connect
    through a validated IPv4 instead of failing the whole page (ERR_TUNNEL_CONNECTION_FAILED)."""
    srv, port, _ = await _origin()
    loop = asyncio.get_running_loop()

    async def dual_stack(host, port, type=0):
        return [(10, 1, 6, "", ("2001:db8::1", port, 0, 0)), (2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(loop, "getaddrinfo", dual_stack)
    policy = UrlPolicy(unsafe_allow_hosts=frozenset({"dual.example.com"}))
    async with EgressProxy(policy) as proxy:
        out = await _raw(proxy.port, f"CONNECT dual.example.com:{port} HTTP/1.1\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 200"), out
        assert proxy.ip_for("dual.example.com") == "127.0.0.1"
    srv.close()


def test_split_hostport() -> None:
    assert _split_hostport("example.com:443", 1) == ("example.com", 443)
    assert _split_hostport("[::1]:8443", 1) == ("::1", 8443)
    assert _split_hostport("example.com", 443) == ("example.com", 443)


async def _fake_upstream() -> tuple[asyncio.base_events.Server, int, list[bytes]]:
    seen: list[bytes] = []

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = await r.readuntil(b"\r\n\r\n")
        seen.append(head)
        if head.startswith(b"CONNECT"):
            w.write(b"HTTP/1.1 200 OK\r\n\r\n")
        else:
            w.write(b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nContent-Length: 2\r\n\r\nok")
        await w.drain()
        w.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], seen


async def test_upstream_receives_pinned_ip_not_hostname(monkeypatch) -> None:
    """Security review: with a corporate upstream, DNS pinning must survive (anti-rebinding)."""
    loop = asyncio.get_running_loop()

    async def resolver(host, port, type=0):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(loop, "getaddrinfo", resolver)
    srv, port, seen = await _fake_upstream()
    async with EgressProxy(UrlPolicy(), upstream="http://user:" + f"pa%40ss@127.0.0.1:{port}") as proxy:
        out = await _raw(proxy.port, b"CONNECT rebind.example.com:443 HTTP/1.1\r\nHost: rebind.example.com\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 200")
        out = await _raw(
            proxy.port,
            b"GET http://rebind.example.com/p HTTP/1.1\r\nHost: rebind.example.com\r\nConnection: keep-alive\r\n\r\n",
        )
        assert b"Connection: close" in out and b"keep-alive" not in out
    connect, get = seen[0].decode(), seen[1].decode()
    assert connect.startswith("CONNECT 93.184.216.34:443 ")
    assert "Proxy-Authorization: Basic " in connect and "rebind" not in connect.split("\r\n")[0]
    assert get.startswith("GET http://93.184.216.34:80/p ")
    assert "Host: rebind.example.com" in get and "Proxy-Authorization: Basic " in get
    srv.close()


async def test_upstream_refusal_is_reported() -> None:
    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        await w.drain()
        w.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    up = srv.sockets[0].getsockname()[1]
    policy = UrlPolicy(unsafe_allow_hosts=frozenset({"127.0.0.1"}))
    async with EgressProxy(policy, upstream=f"http://127.0.0.1:{up}") as proxy:
        out = await _raw(proxy.port, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 502") and b"407" in out
    srv.close()
