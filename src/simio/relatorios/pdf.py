"""PDF generation module using Playwright Chromium."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("simio.relatorios.pdf")


def _renderizar_pdf_em_thread(uri_html: str, caminho_pdf: Path) -> Path | None:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        logger.warning("Playwright is not installed. PDF report will not be generated.")
        return None

    try:
        with sync_playwright() as p:
            navegador = None
            # 1. Try launching with channel="chromium"
            try:
                navegador = p.chromium.launch(channel="chromium", headless=True)
            except Exception as e1:
                logger.debug("Failed to launch chromium with channel='chromium': %s", e1)

            # 2. If channel="chromium" failed, try locating installed chromium binary in user local appdata
            if navegador is None:
                import os  # noqa: PLC0415

                dados_app_local = os.environ.get("LOCALAPPDATA", "")
                dir_ms_playwright = Path(dados_app_local) / "ms-playwright"
                exes_candidatos = list(dir_ms_playwright.glob("**/chrome-win64/chrome.exe")) + list(
                    dir_ms_playwright.glob("**/chrome.exe")
                )
                for exe in exes_candidatos:
                    if exe.exists():
                        try:
                            navegador = p.chromium.launch(executable_path=str(exe), headless=True)
                            break
                        except Exception as e2:
                            logger.debug("Failed to launch chromium at %s: %s", exe, e2)

            # 3. Default fallback
            if navegador is None:
                try:
                    navegador = p.chromium.launch(headless=True)
                except Exception as e3:
                    logger.warning("Could not launch Chromium for PDF generation: %s", e3)
                    return None

            try:
                pagina = navegador.new_page()
                pagina.goto(uri_html, wait_until="networkidle", timeout=30000)
                pagina.pdf(
                    path=str(caminho_pdf),
                    format="A4",
                    print_background=True,
                    margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
                )
                logger.info("PDF report successfully written to %s", caminho_pdf)
                return caminho_pdf
            finally:
                navegador.close()

    except Exception as exc:
        logger.warning("Failed to render PDF from HTML (%s): %s", uri_html, exc)
        return None


def gerar_pdf_campanha(caminho_html: Path, caminho_pdf: Path) -> Path | None:
    """Generate a print-ready A4 PDF from the consolidated HTML campaign report.

    Runs in a dedicated worker thread so it never conflicts with an active asyncio event loop.
    Returns the Path to the generated PDF, or None if PDF generation is unavailable.
    """
    caminho_pdf.parent.mkdir(parents=True, exist_ok=True)
    uri_html = caminho_html.resolve().as_uri()

    with ThreadPoolExecutor(max_workers=1) as executor:
        futuro = executor.submit(_renderizar_pdf_em_thread, uri_html, caminho_pdf)
        return futuro.result()
