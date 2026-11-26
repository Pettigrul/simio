"""In-process filtering forward proxy for the browser (network-layer SSRF enforcement).

Why: Playwright ``route()`` handlers are *not* invoked for redirect hops, so request
interception alone cannot stop ``public page -> 302 -> http://169.254.169.254/``. Every browser
connection (navigations, redirect hops, sub-resources, iframes, fetch/XHR, websockets) is instead
forced through this proxy, which:

* validates the target with :class:`UrlPolicy` (scheme/port/hostname rules);
* resolves DNS itself and connects to the **validated IP** (DNS pinned -> no rebinding TOCTOU);
* forwards plain HTTP with ``Connection: close`` so a connection is never reused for another host;
* tunnels HTTPS/WSS with CONNECT (TLS stays end-to-end; certificate checks remain Chromium's);
* rewrites the response with ``Connection: close`` so Chromium never reuses a socket for a request
  the proxy did not parse;
* optionally chains to an upstream corporate proxy, **still pinned**: the upstream receives the
  validated IP (``CONNECT 203.0.113.7:443`` / ``GET http://203.0.113.7/...`` + original Host), so a
  DNS answer that changes between SIMIO's check and the upstream's resolution cannot redirect the
  request to an internal address. ``pin_upstream=False`` (hostname-based corporate filtering)
  restores hostname forwarding and is a documented residual risk;
* caps the total bytes relayed per page.

Chromium is launched with ``--proxy-bypass-list=<-loopback>`` so loopback is not exempted.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from simio.seguranca.politica_url import PoliticaUrl, ViolacaoPolitica

log = logging.getLogger(__name__)

CABECALHOS_HOP = {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "upgrade"}
MAX_CABECALHO = 64 * 1024


@dataclass
class ProxySaida:
    politica: PoliticaUrl
    upstream: str | None = None
    fixar_upstream: bool = True
    timeout_conexao_s: float = 15.0
    timeout_ocioso_s: float = 60.0
    max_bytes_total: int = 200 * 1024 * 1024
    bytes_repassados: int = 0
    bloqueadas: list[str] = field(default_factory=list)
    conexoes: list[tuple[str, str, int]] = field(default_factory=list)  # (host, ip, port)
    _servidor: asyncio.base_events.Server | None = None
    _tarefas: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def porta(self) -> int:
        assert self._servidor is not None
        return int(self._servidor.sockets[0].getsockname()[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.porta}"

    async def __aenter__(self) -> ProxySaida:
        self._servidor = await asyncio.start_server(self._tratar, "127.0.0.1", 0, limit=MAX_CABECALHO)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._servidor is not None:
            self._servidor.close()
            for t in list(self._tarefas):
                t.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._servidor.wait_closed(), 5)

    def ip_de(self, host: str) -> str | None:
        for h, ip, _ in reversed(self.conexoes):
            if h == host:
                return ip
        return None

    # ------------------------------------------------------------------ connection handling
    async def _tratar(self, leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        tarefa = asyncio.current_task()
        if tarefa is not None:
            self._tarefas.add(tarefa)
        try:
            await self._servir(leitor, escritor)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        except Exception:  # pragma: no cover - defensive
            log.exception("egress proxy error")
        finally:
            with contextlib.suppress(Exception):
                escritor.close()
            if tarefa is not None:
                self._tarefas.discard(tarefa)

    async def _servir(self, leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        cabeca = await asyncio.wait_for(leitor.readuntil(b"\r\n\r\n"), self.timeout_ocioso_s)
        linhas = cabeca.decode("latin-1").split("\r\n")
        try:
            metodo, alvo, versao = linhas[0].split(" ", 2)
        except ValueError:
            await self._responder(escritor, 400, "bad request")
            return
        cabecalhos = [h for h in linhas[1:] if h]

        if metodo.upper() == "CONNECT":
            host, porta = _separar_host_porta(alvo, 443)
            esquema_url = "http" if porta == 80 else "https"
            try:
                ips = await self._autorizar(host, porta, esquema_url)
            except ViolacaoPolitica as exc:
                self.bloqueadas.append(f"{esquema_url}://{alvo} -> {exc}")
                await self._responder(escritor, 403, "blocked by SIMIO egress policy")
                return
            up = await self._abrir(host, ips, porta, escritor)
            if up is None:
                return
            ip, up_r, up_w = up
            host_alvo = ip if self.fixar_upstream else host
            if self.upstream and not await self._conectar_upstream(up_r, up_w, host_alvo, porta, escritor):
                return
            escritor.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await escritor.drain()
            await self._encanar(leitor, escritor, up_r, up_w)
            return

        partes = urlsplit(alvo)
        if partes.scheme.lower() != "http" or not partes.hostname:
            await self._responder(escritor, 400, "absolute http URL required")
            return
        host = partes.hostname.lower()
        porta = partes.port or 80
        try:
            ips = await self._autorizar(host, porta, "http")
        except ViolacaoPolitica as exc:
            self.bloqueadas.append(f"{alvo[:200]} -> {exc}")
            await self._responder(escritor, 403, "blocked by SIMIO egress policy")
            return
        mantidos = [h for h in cabecalhos if h.split(":", 1)[0].strip().lower() not in CABECALHOS_HOP]
        ip = ips[0]
        if self.upstream:
            if self.fixar_upstream:
                host_porta = f"[{ip}]" if ":" in ip else ip
                fixado = partes._replace(netloc=f"{host_porta}:{porta}").geturl()
                linha_requisicao = f"{metodo} {fixado} {versao}"
                if not any(h.lower().startswith("host:") for h in mantidos):
                    mantidos.append(f"Host: {partes.netloc}")
            else:
                linha_requisicao = f"{metodo} {alvo} {versao}"
            auth = _auth_proxy(self.upstream)
            if auth:
                mantidos.append(f"Proxy-Authorization: {auth}")
        else:
            caminho = partes.path or "/"
            if partes.query:
                caminho += "?" + partes.query
            linha_requisicao = f"{metodo} {caminho} {versao}"
        nova_cabeca = "\r\n".join([linha_requisicao, *mantidos, "Connection: close", "", ""]).encode("latin-1")
        up = await self._abrir(host, [ip] if self.upstream else ips, porta, escritor)
        if up is None:
            return
        _, up_r, up_w = up
        up_w.write(nova_cabeca)
        await up_w.drain()
        # Response head: force Connection: close towards Chromium (no socket reuse for requests the
        # proxy would otherwise pipe without parsing).
        try:
            cabeca_resp = await asyncio.wait_for(up_r.readuntil(b"\r\n\r\n"), self.timeout_ocioso_s)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            await self._responder(escritor, 502, "invalid upstream response")
            return
        linhas_r = cabeca_resp.decode("latin-1").split("\r\n")
        mantidos_r = [
            h for h in linhas_r[1:] if h and h.split(":", 1)[0].strip().lower() not in ("connection", "keep-alive")
        ]
        escritor.write("\r\n".join([linhas_r[0], *mantidos_r, "Connection: close", "", ""]).encode("latin-1"))
        await escritor.drain()
        await self._encanar(leitor, escritor, up_r, up_w)

    async def _conectar_upstream(
        self,
        up_r: asyncio.StreamReader,
        up_w: asyncio.StreamWriter,
        host_alvo: str,
        porta: int,
        cliente: asyncio.StreamWriter,
    ) -> bool:
        assert self.upstream is not None
        host_porta = f"[{host_alvo}]" if ":" in host_alvo else host_alvo
        linhas = [f"CONNECT {host_porta}:{porta} HTTP/1.1", f"Host: {host_porta}:{porta}"]
        auth = _auth_proxy(self.upstream)
        if auth:
            linhas.append(f"Proxy-Authorization: {auth}")
        up_w.write(("\r\n".join([*linhas, "", ""])).encode("latin-1"))
        await up_w.drain()
        try:
            resposta = await asyncio.wait_for(up_r.readuntil(b"\r\n\r\n"), self.timeout_conexao_s)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            await self._responder(cliente, 502, "upstream proxy did not answer CONNECT")
            return False
        status = resposta.split(b" ", 2)[1:2]
        if not status or not status[0].startswith(b"2"):
            codigo = status[0].decode("latin-1", "replace") if status else "?"
            await self._responder(cliente, 502, f"upstream proxy refused CONNECT ({codigo})")
            return False
        return True

    async def _autorizar(self, host: str, porta: int, esquema_url: str) -> list[str]:
        """All policy-validated addresses, IPv4 first: hosts without an IPv6 route (common on
        home networks) would otherwise fail on AAAA-first answers such as Meta's."""
        host = host.strip("[]").lower()
        host_porta = f"[{host}]" if ":" in host else host
        url = f"{esquema_url}://{host_porta}:{porta}/"
        enderecos = await self.politica.resolver_verificado_async(url)
        return sorted(enderecos, key=lambda a: ":" in a)

    async def _abrir(
        self, host: str, ips: list[str], porta: int, cliente: asyncio.StreamWriter
    ) -> tuple[str, asyncio.StreamReader, asyncio.StreamWriter] | None:
        """Connect to the first reachable validated address (or to the upstream proxy, pinned to
        the first one). Only addresses that passed the policy are ever tried."""
        ultimo: BaseException | None = None
        if self.upstream:
            up = urlsplit(self.upstream)
            candidatas = [(ips[0], up.hostname or "", up.port or 8080)]
        else:
            candidatas = [(ip, ip, porta) for ip in ips]
        for ip, host_alvo, porta_alvo in candidatas:
            try:
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(host_alvo, porta_alvo, limit=MAX_CABECALHO), self.timeout_conexao_s
                )
            except (OSError, TimeoutError) as exc:
                ultimo = exc
                continue
            self.conexoes.append((host.strip("[]").lower(), ip, porta))
            return ip, r, w
        await self._responder(cliente, 502, f"upstream connection failed: {type(ultimo).__name__}")
        return None

    async def _encanar(
        self,
        c_r: asyncio.StreamReader,
        c_w: asyncio.StreamWriter,
        u_r: asyncio.StreamReader,
        u_w: asyncio.StreamWriter,
    ) -> None:
        async def copiar(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    dados = await asyncio.wait_for(src.read(65536), self.timeout_ocioso_s)
                    if not dados:
                        break
                    self.bytes_repassados += len(dados)
                    if self.bytes_repassados > self.max_bytes_total:
                        self.bloqueadas.append(f"per-page transfer cap of {self.max_bytes_total} bytes reached")
                        break
                    dst.write(dados)
                    await dst.drain()
            except (ConnectionError, TimeoutError, asyncio.CancelledError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    dst.close()

        await asyncio.gather(copiar(c_r, u_w), copiar(u_r, c_w))

    @staticmethod
    async def _responder(escritor: asyncio.StreamWriter, codigo: int, msg: str) -> None:
        corpo = msg.encode()
        motivo = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}.get(codigo, "Error")
        escritor.write(
            f"HTTP/1.1 {codigo} {motivo}\r\nContent-Type: text/plain\r\nContent-Length: {len(corpo)}\r\n"
            f"Connection: close\r\n\r\n".encode()
            + corpo
        )
        with contextlib.suppress(Exception):
            await escritor.drain()


def _separar_host_porta(alvo: str, porta_padrao: int) -> tuple[str, int]:
    if alvo.startswith("["):
        host, _, resto = alvo[1:].partition("]")
        porta = resto.lstrip(":")
    else:
        host, _, porta = alvo.rpartition(":") if alvo.count(":") == 1 else (alvo, "", "")
    try:
        return host.lower(), int(porta) if porta else porta_padrao
    except ValueError:
        return host.lower(), -1


def _auth_proxy(upstream: str) -> str | None:
    partes = urlsplit(upstream)
    if partes.username is None:
        return None
    token = f"{unquote(partes.username)}:{unquote(partes.password or '')}".encode()
    return "Basic " + base64.b64encode(token).decode()
