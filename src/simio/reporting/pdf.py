"""PDF generation module using Playwright Chromium."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path

logger = logging.getLogger("simio.reporting.pdf")


def _render_pdf_worker(html_uri: str, pdf_path: Path) -> Path | None:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        logger.warning("Playwright is not installed. PDF report will not be generated.")
        return None

    try:
        with sync_playwright() as p:
            browser = None
            # 1. Try launching with channel="chromium"
            try:
                browser = p.chromium.launch(channel="chromium", headless=True)
            except Exception as e1:
                logger.debug("Failed to launch chromium with channel='chromium': %s", e1)

            # 2. If channel="chromium" failed, try locating installed chromium binary in user local appdata
            if browser is None:
                import os  # noqa: PLC0415

                local_app_data = os.environ.get("LOCALAPPDATA", "")
                ms_playwright_dir = Path(local_app_data) / "ms-playwright"
                candidate_exes = list(ms_playwright_dir.glob("**/chrome-win64/chrome.exe")) + list(
                    ms_playwright_dir.glob("**/chrome.exe")
                )
                for exe in candidate_exes:
                    if exe.exists():
                        try:
                            browser = p.chromium.launch(executable_path=str(exe), headless=True)
                            break
                        except Exception as e2:
                            logger.debug("Failed to launch chromium at %s: %s", exe, e2)

            # 3. Default fallback
            if browser is None:
                try:
                    browser = p.chromium.launch(headless=True)
                except Exception as e3:
                    logger.warning("Could not launch Chromium for PDF generation: %s", e3)
                    return None

            try:
                page = browser.new_page()
                page.goto(html_uri, wait_until="networkidle", timeout=30000)
                page.pdf(
                    path=str(pdf_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
                )
                logger.info("PDF report successfully written to %s", pdf_path)
                return pdf_path
            finally:
                browser.close()

    except Exception as exc:
        logger.warning("Failed to render PDF from HTML (%s): %s", html_uri, exc)
        return None


def generate_campaign_pdf(html_path: Path, pdf_path: Path) -> Path | None:
    """Generate a print-ready A4 PDF from the consolidated HTML campaign report.

    Runs in a dedicated worker thread so it never conflicts with an active asyncio event loop.
    Returns the Path to the generated PDF, or None if PDF generation is unavailable.
    """
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    html_uri = html_path.resolve().as_uri()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_render_pdf_worker, html_uri, pdf_path)
        return future.result()
