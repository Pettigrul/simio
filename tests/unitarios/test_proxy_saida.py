from __future__ import annotations

import asyncio

from simio.seguranca.politica_url import PoliticaUrl
from simio.seguranca.proxy_saida import ProxySaida, _separar_host_porta


async def _bruto(porta: int, payload: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", porta)
    w.write(payload)
    await w.drain()
    dados = await asyncio.wait_for(r.read(65536), 5)
    if b"Content-Length:" in dados and dados.endswith(b"\r\n\r\n"):
        dados += await asyncio.wait_for(r.read(65536), 5)
    w.close()
    return dados


async def _origem() -> tuple[asyncio.base_events.Server, int, list[bytes]]:
    vistos: list[bytes] = []

    async def tratar(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        cabeca = await r.readuntil(b"\r\n\r\n")
        vistos.append(cabeca)
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await w.drain()
        w.close()

    srv = await asyncio.start_server(tratar, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], vistos


async def test_connect_para_privado_eh_recusado() -> None:
    async with ProxySaida(PoliticaUrl()) as proxy:
        saida = await _bruto(proxy.porta, b"CONNECT 169.254.169.254:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        assert saida.startswith(b"HTTP/1.1 403")
        saida = await _bruto(proxy.porta, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
        assert saida.startswith(b"HTTP/1.1 403")
        assert len(proxy.bloqueadas) == 2


async def test_http_simples_para_privado_eh_recusado() -> None:
    async with ProxySaida(PoliticaUrl()) as proxy:
        saida = await _bruto(proxy.porta, b"GET http://10.1.1.1/admin HTTP/1.1\r\nHost: 10.1.1.1\r\n\r\n")
        assert saida.startswith(b"HTTP/1.1 403")
        saida = await _bruto(proxy.porta, b"GET /relative HTTP/1.1\r\n\r\n")
        assert saida.startswith(b"HTTP/1.1 400")


async def test_http_permitido_eh_encaminhado_com_connection_close_e_ip_fixado() -> None:
    srv, porta, vistos = await _origem()
    politica = PoliticaUrl(hosts_liberados_inseguro=frozenset({"127.0.0.1"}))
    async with ProxySaida(politica) as proxy:
        saida = await _bruto(
            proxy.porta,
            f"GET http://127.0.0.1:{porta}/p?q=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\n\r\n".encode(),
        )
        assert saida.endswith(b"ok")
        cabeca = vistos[0].decode()
        assert cabeca.startswith("GET /p?q=1 HTTP/1.1")
        assert "Connection: close" in cabeca and "Proxy-Connection" not in cabeca and "keep-alive" not in cabeca
        assert proxy.ip_de("127.0.0.1") == "127.0.0.1"
    srv.close()


async def test_resposta_dns_eh_validada_e_fixada(monkeypatch) -> None:
    politica = PoliticaUrl()
    loop = asyncio.get_running_loop()

    async def rebinding(host, porta, type=0):
        return [(2, 1, 6, "", ("127.0.0.1", porta))]

    monkeypatch.setattr(loop, "getaddrinfo", rebinding)
    async with ProxySaida(politica) as proxy:
        saida = await _bruto(proxy.porta, b"CONNECT rebind.example.com:443 HTTP/1.1\r\n\r\n")
        assert saida.startswith(b"HTTP/1.1 403") and "não público" in proxy.bloqueadas[0]


async def test_ipv6_primeiro_recorre_a_ipv4_alcancavel(monkeypatch) -> None:
    # A Meta responde AAAA primeiro; sem rota IPv6 o proxy tem que ir pelo IPv4 validado em vez de
    # derrubar a página (ERR_TUNNEL_CONNECTION_FAILED).
    srv, porta, _ = await _origem()
    loop = asyncio.get_running_loop()

    async def pilha_dupla(host, porta, type=0):
        return [(10, 1, 6, "", ("2001:db8::1", porta, 0, 0)), (2, 1, 6, "", ("127.0.0.1", porta))]

    monkeypatch.setattr(loop, "getaddrinfo", pilha_dupla)
    politica = PoliticaUrl(hosts_liberados_inseguro=frozenset({"dual.example.com"}))
    async with ProxySaida(politica) as proxy:
        saida = await _bruto(proxy.porta, f"CONNECT dual.example.com:{porta} HTTP/1.1\r\n\r\n".encode())
        assert saida.startswith(b"HTTP/1.1 200"), saida
        assert proxy.ip_de("dual.example.com") == "127.0.0.1"
    srv.close()


def test_separar_host_porta() -> None:
    assert _separar_host_porta("example.com:443", 1) == ("example.com", 443)
    assert _separar_host_porta("[::1]:8443", 1) == ("::1", 8443)
    assert _separar_host_porta("example.com", 443) == ("example.com", 443)


async def _upstream_falso() -> tuple[asyncio.base_events.Server, int, list[bytes]]:
    vistos: list[bytes] = []

    async def tratar(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        cabeca = await r.readuntil(b"\r\n\r\n")
        vistos.append(cabeca)
        if cabeca.startswith(b"CONNECT"):
            w.write(b"HTTP/1.1 200 OK\r\n\r\n")
        else:
            w.write(b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nContent-Length: 2\r\n\r\nok")
        await w.drain()
        w.close()

    srv = await asyncio.start_server(tratar, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], vistos


async def test_upstream_recebe_ip_fixado_nao_hostname(monkeypatch) -> None:
    # com proxy corporativo o DNS continua fixado (anti-rebinding)
    loop = asyncio.get_running_loop()

    async def resolvedor(host, porta, type=0):
        return [(2, 1, 6, "", ("93.184.216.34", porta))]

    monkeypatch.setattr(loop, "getaddrinfo", resolvedor)
    srv, porta, vistos = await _upstream_falso()
    async with ProxySaida(PoliticaUrl(), upstream="http://user:" + f"pa%40ss@127.0.0.1:{porta}") as proxy:
        saida = await _bruto(proxy.porta, b"CONNECT rebind.example.com:443 HTTP/1.1\r\nHost: rebind.example.com\r\n\r\n")
        assert saida.startswith(b"HTTP/1.1 200")
        saida = await _bruto(
            proxy.porta,
            b"GET http://rebind.example.com/p HTTP/1.1\r\nHost: rebind.example.com\r\nConnection: keep-alive\r\n\r\n",
        )
        assert b"Connection: close" in saida and b"keep-alive" not in saida
    conectar, obter = vistos[0].decode(), vistos[1].decode()
    assert conectar.startswith("CONNECT 93.184.216.34:443 ")
    assert "Proxy-Authorization: Basic " in conectar and "rebind" not in conectar.split("\r\n")[0]
    assert obter.startswith("GET http://93.184.216.34:80/p ")
    assert "Host: rebind.example.com" in obter and "Proxy-Authorization: Basic " in obter
    srv.close()


async def test_recusa_do_upstream_eh_informada() -> None:
    async def tratar(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        await w.drain()
        w.close()

    srv = await asyncio.start_server(tratar, "127.0.0.1", 0)
    up = srv.sockets[0].getsockname()[1]
    politica = PoliticaUrl(hosts_liberados_inseguro=frozenset({"127.0.0.1"}))
    async with ProxySaida(politica, upstream=f"http://127.0.0.1:{up}") as proxy:
        saida = await _bruto(proxy.porta, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
        assert saida.startswith(b"HTTP/1.1 502") and b"407" in saida
    srv.close()
