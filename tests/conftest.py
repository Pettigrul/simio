# Tudo aqui é local e sintético: nenhuma empresa real, nenhum acesso à rede.
from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from simio.configuracoes import Configuracoes
from simio.nucleo.armazenamento import Armazenamento

FIXTURES = Path(__file__).parent / "fixtures"
SITES = FIXTURES / "sites"


class _Tratador(BaseHTTPRequestHandler):
    porta = 0
    resultados: ClassVar[list[tuple[str, str]]] = []  # (cabeçalho Host, caminho) de cada requisição

    def log_message(self, *args: object) -> None:
        pass

    def _enviar(
        self, codigo: int, corpo: bytes, tipo_conteudo: str = "text/html; charset=utf-8", cabecalhos: dict[str, str] | None = None
    ) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo_conteudo)
        self.send_header("Content-Length", str(len(corpo)))
        for k, v in (cabecalhos or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self) -> None:
        caminho = self.path.split("?", 1)[0]
        _Tratador.resultados.append((self.headers.get("Host", ""), caminho))
        if caminho == "/redirect-ok":
            return self._enviar(302, b"", cabecalhos={"Location": "/legit_partner.html"})
        if caminho == "/redirect-private":
            return self._enviar(302, b"", cabecalhos={"Location": f"http://localhost:{self.porta}/legit_partner.html"})
        if caminho == "/redirect-metadata":
            return self._enviar(302, b"", cabecalhos={"Location": "http://169.254.169.254/latest/meta-data/"})
        if caminho == "/unavailable":
            return self._enviar(503, b"<h1>Service Unavailable</h1>")
        if caminho == "/slow":
            time.sleep(5)
            return self._enviar(200, b"<h1>slow</h1>")
        if caminho == "/download":
            return self._enviar(
                200,
                b"MZ fake binary",
                tipo_conteudo="application/octet-stream",
                cabecalhos={"Content-Disposition": "attachment; filename=x.exe"},
            )
        if caminho.startswith("/static/"):
            # PNG 1x1 transparente
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
            )
            return self._enviar(200, png, tipo_conteudo="image/png")
        f = SITES / caminho.lstrip("/")
        if f.is_file() and f.suffix == ".html":
            corpo = f.read_text(encoding="utf-8").replace("__PORT__", str(self.porta)).encode()
            return self._enviar(200, corpo)
        return self._enviar(404, b"<h1>Not found</h1>")


@pytest.fixture
def acessos_servidor() -> list[tuple[str, str]]:
    _Tratador.resultados.clear()
    return _Tratador.resultados


@pytest.fixture(scope="session")
def site() -> Iterator[str]:
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), _Tratador)
    _Tratador.porta = servidor.server_address[1]
    t = threading.Thread(target=servidor.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{servidor.server_address[1]}"
    servidor.shutdown()


@pytest.fixture
def configuracoes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Configuracoes:
    # Isola do .env e das variáveis de ambiente de quem está rodando.
    for chave in list(os.environ):
        if chave.startswith("SIMIO_"):
            monkeypatch.delenv(chave, raising=False)
    monkeypatch.chdir(tmp_path)
    return Configuracoes(
        dir_saida=tmp_path / "out",
        hosts_liberados_inseguro=["127.0.0.1"],
        timeout_navegacao_ms=4000,
        orcamento_pagina_s=20,
        espera_estabilizar_ms=200,
        atraso_base_retentativa_s=0,
        max_tentativas=2,
        paralelismo=3,
    )


@pytest.fixture
def armazenamento(configuracoes: Configuracoes) -> Iterator[Armazenamento]:
    st = Armazenamento(configuracoes.caminho_db)
    yield st
    st.fechar()


def navegador_disponivel() -> bool:
    try:
        import asyncio

        from playwright.async_api import async_playwright

        from simio.coletores.navegador import iniciar_chromium

        async def chamar() -> None:
            async with async_playwright() as p:
                b = await iniciar_chromium(p, sandbox=True)
                await b.close()

        asyncio.run(chamar())
        return True
    except Exception:
        return False


_NAVEGADOR = None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    global _NAVEGADOR
    real = os.environ.get("SIMIO_TESTES_REAIS") == "1"
    precisa_navegador = any("browser" in i.keywords for i in items)
    if precisa_navegador and _NAVEGADOR is None:
        _NAVEGADOR = navegador_disponivel()
    for item in items:
        if "live" in item.keywords and not real:
            item.add_marker(pytest.mark.skip(reason="teste real: defina SIMIO_TESTES_REAIS=1 num ambiente autorizado"))
        if "browser" in item.keywords and not _NAVEGADOR:
            item.add_marker(pytest.mark.skip(reason="Chromium do Playwright não instalado"))


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    # Roda os testes async quando o pytest-asyncio não está instalado.
    import asyncio
    import inspect

    if pyfuncitem.config.pluginmanager.has_plugin("asyncio"):
        return None
    func = pyfuncitem.obj
    if inspect.iscoroutinefunction(func):
        nomes_args = pyfuncitem._fixtureinfo.argnames
        kwargs = {arg: pyfuncitem.funcargs[arg] for arg in nomes_args if arg in pyfuncitem.funcargs}
        asyncio.run(func(**kwargs))
        return True
    return None
