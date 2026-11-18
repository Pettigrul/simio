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


def test_split_hostport() -> None:
    assert _split_hostport("example.com:443", 1) == ("example.com", 443)
    assert _split_hostport("[::1]:8443", 1) == ("::1", 8443)
    assert _split_hostport("example.com", 443) == ("example.com", 443)
