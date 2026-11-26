from __future__ import annotations

import base64
import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

logger = logging.getLogger("simio.relatorios.pdf")

_RODAPE = (
    '<div style="font:8px Inter, system-ui, sans-serif; color:#667085; width:100%; padding:0 10mm;'
    ' display:flex; justify-content:space-between;"><span class="title"></span>'
    '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'
)


def _abrir_chromium(p):  # p: Playwright síncrono
    try:
        return p.chromium.launch(channel="chromium", headless=True)
    except Exception as exc:
        logger.debug("Chromium (channel='chromium') não abriu: %s", exc)
    # No Windows o Chromium baixado pelo Playwright às vezes só é achado pelo caminho em LOCALAPPDATA.
    pasta = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    for exe in [*pasta.glob("**/chrome-win64/chrome.exe"), *pasta.glob("**/chrome.exe")]:
        try:
            return p.chromium.launch(executable_path=str(exe), headless=True)
        except Exception as exc:
            logger.debug("Chromium em %s não abriu: %s", exe, exc)
    try:
        return p.chromium.launch(headless=True)
    except Exception as exc:
        logger.warning("Não foi possível abrir o Chromium para gerar o PDF: %s", exc)
        return None


def _miniatura(arquivo: Path, largura: int = 600) -> str | None:
    """JPEG reduzido (recorte do topo em 3:2, como o cartão mostra) em data URI."""
    from PIL import Image

    try:
        with Image.open(arquivo) as img:
            img = img.convert("RGB")
            img = img.crop((0, 0, img.width, min(img.height, img.width * 2 // 3)))
            if img.width > largura:
                img = img.resize((largura, img.height * largura // img.width), Image.Resampling.LANCZOS)
            saida = io.BytesIO()
            img.save(saida, "JPEG", quality=72, optimize=True)
    except OSError:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(saida.getvalue()).decode("ascii")


def _reduzir_capturas(pagina, caminho_html: Path) -> None:
    # As capturas originais (centenas de KB cada) deixariam o PDF com dezenas de MB; no papel o cartão
    # mostra só uma miniatura, então ela entra no lugar da imagem (o HTML em disco não muda).
    srcs = pagina.evaluate("Array.from(document.querySelectorAll('img.shot'), i => i.getAttribute('src'))")
    trocas = {}
    for src in set(srcs):
        if src and not src.startswith("data:"):
            miniatura = _miniatura(caminho_html.parent / unquote(src))
            if miniatura:
                trocas[src] = miniatura
    pagina.evaluate(
        "m => document.querySelectorAll('img.shot')"
        ".forEach(i => { const s = m[i.getAttribute('src')]; if (s) i.src = s; })",
        trocas,
    )


def _renderizar_pdf(caminho_html: Path, caminho_pdf: Path) -> Path | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright não instalado: o PDF do relatório não será gerado.")
        return None
    try:
        with sync_playwright() as p:
            navegador = _abrir_chromium(p)
            if navegador is None:
                return None
            try:
                pagina = navegador.new_page()
                pagina.goto(caminho_html.resolve().as_uri(), wait_until="networkidle", timeout=30000)
                _reduzir_capturas(pagina, caminho_html)
                # No papel não dá para clicar: justificativas e tabelas recolhidas saem abertas, e as
                # capturas com carregamento preguiçoso precisam estar carregadas antes de imprimir.
                pagina.evaluate(
                    "document.querySelectorAll('details').forEach(d => d.open = true);"
                    "document.querySelectorAll('img[loading=lazy]').forEach(i => i.loading = 'eager')"
                )
                pagina.wait_for_function("Array.from(document.images).every(i => i.complete)", timeout=120000)
                pagina.pdf(
                    path=str(caminho_pdf),
                    format="A4",
                    print_background=True,
                    display_header_footer=True,
                    header_template="<span></span>",
                    footer_template=_RODAPE,
                    margin={"top": "12mm", "bottom": "14mm", "left": "10mm", "right": "10mm"},
                )
                logger.info("PDF gravado em %s", caminho_pdf)
                return caminho_pdf
            finally:
                navegador.close()
    except Exception as exc:
        logger.warning("Falha ao gerar o PDF a partir de %s: %s", caminho_html, exc)
        return None


def gerar_pdf_campanha(caminho_html: Path, caminho_pdf: Path) -> Path | None:
    """PDF A4 do relatório da campanha, ou None se o Playwright/Chromium não estiver disponível."""
    caminho_pdf.parent.mkdir(parents=True, exist_ok=True)
    # Thread própria: a API síncrona do Playwright não roda dentro de um loop asyncio ativo.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_renderizar_pdf, caminho_html, caminho_pdf).result()
