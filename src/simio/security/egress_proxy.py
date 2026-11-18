"""In-process filtering forward proxy for the browser (network-layer SSRF enforcement).

Why: Playwright ``route()`` handlers are *not* invoked for redirect hops, so request
interception alone cannot stop ``public page -> 302 -> http://169.254.169.254/``. Every browser
connection (navigations, redirect hops, sub-resources, iframes, fetch/XHR, websockets) is instead
forced through this proxy, which:

* validates the target with :class:`UrlPolicy` (scheme/port/hostname rules);
* resolves DNS itself and connects to the **validated IP** (DNS pinned -> no rebinding TOCTOU);
* forwards plain HTTP with ``Connection: close`` so a connection is never reused for another host;
* tunnels HTTPS/WSS with CONNECT (TLS stays end-to-end; certificate checks remain Chromium's);
* optionally chains to an upstream corporate proxy.

Chromium is launched with ``--proxy-bypass-list=<-loopback>`` so loopback is not exempted.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from simio.security.url_policy import PolicyViolation, UrlPolicy

log = logging.getLogger(__name__)

HOP_HEADERS = {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "upgrade"}
MAX_HEAD = 64 * 1024


@dataclass
class EgressProxy:
    policy: UrlPolicy
    upstream: str | None = None
    connect_timeout_s: float = 15.0
    idle_timeout_s: float = 60.0
    blocked: list[str] = field(default_factory=list)
    connections: list[tuple[str, str, int]] = field(default_factory=list)  # (host, ip, port)
    _server: asyncio.base_events.Server | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> EgressProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=MAX_HEAD)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            for t in list(self._tasks):
                t.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)

    def ip_for(self, host: str) -> str | None:
        for h, ip, _ in reversed(self.connections):
            if h == host:
                return ip
        return None

    # ------------------------------------------------------------------ connection handling
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        except Exception:  # pragma: no cover - defensive
            log.exception("egress proxy error")
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            if task is not None:
                self._tasks.discard(task)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.idle_timeout_s)
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            await self._reply(writer, 400, "bad request")
            return
        headers = [h for h in lines[1:] if h]

        if method.upper() == "CONNECT":
            host, port = _split_hostport(target, 443)
            scheme = "http" if port == 80 else "https"
            try:
                ip = await self._authorize(host, port, scheme)
            except PolicyViolation as exc:
                self.blocked.append(f"{scheme}://{target} -> {exc}")
                await self._reply(writer, 403, "blocked by SIMIO egress policy")
                return
            up = await self._open(ip, port, writer)
            if up is None:
                return
            up_r, up_w = up
            if self.upstream:
                up_w.write(head)
            else:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._pipe(reader, writer, up_r, up_w)
            return

        parts = urlsplit(target)
        if parts.scheme.lower() != "http" or not parts.hostname:
            await self._reply(writer, 400, "absolute http URL required")
            return
        host = parts.hostname.lower()
        port = parts.port or 80
        try:
            ip = await self._authorize(host, port, "http")
        except PolicyViolation as exc:
            self.blocked.append(f"{target[:200]} -> {exc}")
            await self._reply(writer, 403, "blocked by SIMIO egress policy")
            return
        kept = [h for h in headers if h.split(":", 1)[0].strip().lower() not in HOP_HEADERS]
        if self.upstream:
            request_line = f"{method} {target} {version}"
            auth = _proxy_auth(self.upstream)
            if auth:
                kept.append(f"Proxy-Authorization: {auth}")
        else:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            request_line = f"{method} {path} {version}"
        new_head = "\r\n".join([request_line, *kept, "Connection: close", "", ""]).encode("latin-1")
        up = await self._open(ip, port, writer)
        if up is None:
            return
        up_r, up_w = up
        up_w.write(new_head)
        await up_w.drain()
        await self._pipe(reader, writer, up_r, up_w)

    async def _authorize(self, host: str, port: int, scheme: str) -> str:
        host = host.strip("[]").lower()
        hostport = f"[{host}]" if ":" in host else host
        url = f"{scheme}://{hostport}:{port}/"
        addresses = await self.policy.aresolve_checked(url)
        ip = addresses[0]
        self.connections.append((host, ip, port))
        return ip

    async def _open(
        self, ip: str, port: int, client: asyncio.StreamWriter
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        try:
            if self.upstream:
                up = urlsplit(self.upstream)
                target_host, target_port = up.hostname or "", up.port or 8080
            else:
                target_host, target_port = ip, port
            return await asyncio.wait_for(
                asyncio.open_connection(target_host, target_port, limit=MAX_HEAD), self.connect_timeout_s
            )
        except (OSError, TimeoutError) as exc:
            await self._reply(client, 502, f"upstream connection failed: {type(exc).__name__}")
            return None

    async def _pipe(
        self,
        c_r: asyncio.StreamReader,
        c_w: asyncio.StreamWriter,
        u_r: asyncio.StreamReader,
        u_w: asyncio.StreamWriter,
    ) -> None:
        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await asyncio.wait_for(src.read(65536), self.idle_timeout_s)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, TimeoutError, asyncio.CancelledError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    dst.close()

        await asyncio.gather(copy(c_r, u_w), copy(u_r, c_w))

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, code: int, msg: str) -> None:
        body = msg.encode()
        reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}.get(code, "Error")
        writer.write(
            f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n".encode()
            + body
        )
        with contextlib.suppress(Exception):
            await writer.drain()


def _split_hostport(target: str, default_port: int) -> tuple[str, int]:
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = target.rpartition(":") if target.count(":") == 1 else (target, "", "")
    try:
        return host.lower(), int(port) if port else default_port
    except ValueError:
        return host.lower(), -1


def _proxy_auth(upstream: str) -> str | None:
    parts = urlsplit(upstream)
    if parts.username is None:
        return None
    token = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
    return "Basic " + base64.b64encode(token).decode()
