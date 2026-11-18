"""Shared test fixtures. Everything is local and synthetic: no real companies, no network."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from simio.config import Settings
from simio.core.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"
SITES = FIXTURES / "sites"


class _Handler(BaseHTTPRequestHandler):
    port = 0
    hits: ClassVar[list[tuple[str, str]]] = []  # (Host header, path) of every request received

    def log_message(self, *args: object) -> None:  # silence
        pass

    def _send(
        self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8", headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        _Handler.hits.append((self.headers.get("Host", ""), path))
        if path == "/redirect-ok":
            return self._send(302, b"", headers={"Location": "/legit_partner.html"})
        if path == "/redirect-private":
            return self._send(302, b"", headers={"Location": f"http://localhost:{self.port}/legit_partner.html"})
        if path == "/redirect-metadata":
            return self._send(302, b"", headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        if path == "/unavailable":
            return self._send(503, b"<h1>Service Unavailable</h1>")
        if path == "/slow":
            time.sleep(5)
            return self._send(200, b"<h1>slow</h1>")
        if path == "/download":
            return self._send(
                200,
                b"MZ fake binary",
                ctype="application/octet-stream",
                headers={"Content-Disposition": "attachment; filename=x.exe"},
            )
        if path.startswith("/static/"):
            # 1x1 transparent PNG
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
            )
            return self._send(200, png, ctype="image/png")
        f = SITES / path.lstrip("/")
        if f.is_file() and f.suffix == ".html":
            body = f.read_text(encoding="utf-8").replace("__PORT__", str(self.port)).encode()
            return self._send(200, body)
        return self._send(404, b"<h1>Not found</h1>")


@pytest.fixture
def server_hits() -> list[tuple[str, str]]:
    _Handler.hits.clear()
    return _Handler.hits


@pytest.fixture(scope="session")
def site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Isolate from any developer .env / environment variables.
    for key in list(os.environ):
        if key.startswith("SIMIO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return Settings(
        output_dir=tmp_path / "out",
        unsafe_allow_hosts=["127.0.0.1"],
        nav_timeout_ms=4000,
        page_budget_s=20,
        settle_ms=200,
        retry_base_delay_s=0,
        max_attempts=2,
        workers=3,
    )


@pytest.fixture
def storage(settings: Settings) -> Iterator[Storage]:
    st = Storage(settings.db_path)
    yield st
    st.close()


def browser_available() -> bool:
    try:
        import asyncio

        from playwright.async_api import async_playwright

        async def go() -> None:
            async with async_playwright() as p:
                b = await p.chromium.launch()
                await b.close()

        asyncio.run(go())
        return True
    except Exception:
        return False


_BROWSER = None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    global _BROWSER
    live = os.environ.get("SIMIO_LIVE_TESTS") == "1"
    needs_browser = any("browser" in i.keywords for i in items)
    if needs_browser and _BROWSER is None:
        _BROWSER = browser_available()
    for item in items:
        if "live" in item.keywords and not live:
            item.add_marker(pytest.mark.skip(reason="live test: set SIMIO_LIVE_TESTS=1 in an authorised environment"))
        if "browser" in item.keywords and not _BROWSER:
            item.add_marker(pytest.mark.skip(reason="Playwright Chromium not installed"))
