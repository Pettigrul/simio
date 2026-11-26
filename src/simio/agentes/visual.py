from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import numpy as np
from PIL import Image

from simio.nucleo.modelos import CorrespondenciaVisual, Evidencia

# A logo atual e a antiga têm o mesmo monograma ("B" com traço + "V"); só muda a cor: na antiga o "B"
# é vermelho e o "V" azul, na atual tudo é azul. Por isso a comparação é feita por canal de cor
# (máscara vermelha e máscara azul contra a referência, IoU suave numa grade) e os dois precisam bater.
# As regiões saem das capturas com hash (caixas dos <img>/svg visíveis e janelas ancoradas em
# componentes azuis com proporção de "V"), então qualquer achado é reproduzível a partir da evidência.
GRADE = 24
MIN_TINTA_PX = 120  # abaixo disso não há tinta suficiente para formar o desenho
MIN_LADO_PX = 14
MAX_PIXELS_COMPONENTE = 400_000  # limita a BFS por captura (depois de reduzir a imagem)
LIMIAR_PADRAO = 0.62

# No HSV do PIL o matiz vai de 0 a 255. Vermelho da logo antiga ~225-255 (e 0-6); azul ~125-151.
# O azul atual fica em ~152-159, fora do miolo da faixa azul, mas quem decide é o "B" vermelho:
# logo monocromática não tem tinta vermelha e nunca casa.
_MATIZES_VERMELHOS = ((222, 256), (0, 7))
_MATIZES_AZUIS = ((122, 154),)
_S_MIN, _V_MIN = 80, 60

Image.MAX_IMAGE_PIXELS = 60_000_000  # o coletor já limita o tamanho da captura completa


def _mascara_matiz(h: np.ndarray, faixas: tuple[tuple[int, int], ...]) -> np.ndarray:
    m = np.zeros(h.shape, dtype=bool)
    for lo, hi in faixas:
        m |= (h >= lo) & (h < hi)
    return m


def mascaras_cor(img: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    # (vermelho, azul) só com pixels saturados e com brilho razoável
    hsv = np.asarray(img.convert("RGB").convert("HSV"), dtype=np.int16)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    vivo = (s >= _S_MIN) & (v >= _V_MIN)
    return vivo & _mascara_matiz(h, _MATIZES_VERMELHOS), vivo & _mascara_matiz(h, _MATIZES_AZUIS)


def _componentes(mascara: np.ndarray, *, min_pixels: int = 1) -> list[tuple[int, int, int, int, int]]:
    # componentes 4-conexos -> [(x0, y0, x1, y1, n_pixels)], x1/y1 exclusivos
    h, w = mascara.shape
    vistos = np.zeros_like(mascara)
    saida: list[tuple[int, int, int, int, int]] = []
    ys, xs = np.nonzero(mascara)
    for y0, x0 in zip(ys.tolist(), xs.tolist(), strict=True):
        if vistos[y0, x0]:
            continue
        vistos[y0, x0] = True
        q = deque([(y0, x0)])
        minx = maxx = x0
        miny = maxy = y0
        n = 0
        while q:
            y, x = q.popleft()
            n += 1
            minx, maxx, miny, maxy = min(minx, x), max(maxx, x), min(miny, y), max(maxy, y)
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < h and 0 <= nx < w and mascara[ny, nx] and not vistos[ny, nx]:
                    vistos[ny, nx] = True
                    q.append((ny, nx))
        if n >= min_pixels:
            saida.append((minx, miny, maxx + 1, maxy + 1, n))
    return saida


def _descartar_componentes_borda(mascara: np.ndarray) -> np.ndarray:
    # Tira a tinta que encosta na borda do recorte (fundo da página vazando para dentro do elemento).
    h, w = mascara.shape
    manter = mascara.copy()
    borda = np.zeros_like(mascara)
    borda[0, :] = borda[-1, :] = True
    borda[:, 0] = borda[:, -1] = True
    q = deque((int(y), int(x)) for y, x in zip(*np.nonzero(mascara & borda), strict=True))
    for y, x in q:
        manter[y, x] = False
    while q:
        y, x = q.popleft()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < h and 0 <= nx < w and manter[ny, nx]:
                manter[ny, nx] = False
                q.append((ny, nx))
    return manter


@dataclass(frozen=True)
class Assinatura:
    vermelho: np.ndarray  # cobertura GRADE x GRADE em [0, 1]
    azul: np.ndarray
    proporcao: float  # largura / altura da caixa da tinta
    fracao_vermelho: float  # tinta vermelha / tinta total


def _redimensionar(mascara: np.ndarray) -> np.ndarray:
    im = Image.fromarray((mascara * 255).astype(np.uint8))
    return np.asarray(im.resize((GRADE, GRADE), Image.Resampling.BOX), dtype=np.float32) / 255.0


def assinatura(vermelho: np.ndarray, azul: np.ndarray) -> Assinatura | None:
    tinta = vermelho | azul
    if int(tinta.sum()) < MIN_TINTA_PX or not vermelho.any() or not azul.any():
        return None
    ys, xs = np.nonzero(tinta)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    if (x1 - x0) < MIN_LADO_PX or (y1 - y0) < MIN_LADO_PX:
        return None
    r, b = vermelho[y0:y1, x0:x1], azul[y0:y1, x0:x1]
    return Assinatura(
        _redimensionar(r), _redimensionar(b), (x1 - x0) / (y1 - y0), float(r.sum()) / float(tinta[y0:y1, x0:x1].sum())
    )


def _iou_suave(a: np.ndarray, b: np.ndarray) -> float:
    den = float(np.maximum(a, b).sum())
    return float(np.minimum(a, b).sum()) / den if den else 0.0


def similaridade(candidata: Assinatura, referencia: Assinatura) -> float:
    if abs(math.log(candidata.proporcao / referencia.proporcao)) > 0.35:
        return 0.0
    if not (
        0.45 * referencia.fracao_vermelho <= candidata.fracao_vermelho <= min(0.97, 1.8 * referencia.fracao_vermelho)
    ):
        return 0.0
    return 0.5 * (_iou_suave(candidata.vermelho, referencia.vermelho) + _iou_suave(candidata.azul, referencia.azul))


def correspondencia_regiao(img: Image.Image, referencia: Assinatura) -> tuple[float, tuple[int, int, int, int] | None]:
    # Melhor similaridade do recorte (com e sem a tinta da borda) e a caixa (x0, y0, x1, y1) da tinta.
    vermelho, azul = mascaras_cor(img)
    melhor: tuple[float, tuple[int, int, int, int] | None] = (0.0, None)
    for r, b in ((vermelho, azul), (_descartar_componentes_borda(vermelho), _descartar_componentes_borda(azul))):
        sig = assinatura(r, b)
        if sig is None:
            continue
        pontuacao = similaridade(sig, referencia)
        if pontuacao > melhor[0]:
            ys, xs = np.nonzero(r | b)
            melhor = (pontuacao, (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    return melhor


def pontuacao_regiao(img: Image.Image, referencia: Assinatura) -> float:
    return correspondencia_regiao(img, referencia)[0]


@dataclass(frozen=True)
class Referencia:
    assinatura: Assinatura
    # Geometria da logo em relação ao "V" azul (maior mancha azul): transforma um componente azul
    # achado na página em janela candidata.
    proporcao_v: float
    esquerda: float
    topo: float
    largura: float
    altura: float


def carregar_referencia(caminho: Path) -> Referencia:
    img = Image.open(caminho).convert("RGB")
    vermelho, azul = mascaras_cor(img)
    sig = assinatura(vermelho, azul)
    if sig is None:
        raise ValueError(f"a logo de referência não tem tinta vermelha e azul: {caminho}")
    tinta = vermelho | azul
    ys, xs = np.nonzero(tinta)
    lx0, ly0, lx1, ly1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    vx0, vy0, vx1, vy1, _ = max(_componentes(azul, min_pixels=50), key=lambda c: c[4])
    vw, vh = vx1 - vx0, vy1 - vy0
    return Referencia(
        assinatura=sig,
        proporcao_v=vw / vh,
        esquerda=(lx0 - vx0) / vw,
        topo=(ly0 - vy0) / vh,
        largura=(lx1 - lx0) / vw,
        altura=(ly1 - ly0) / vh,
    )


def _recortar(
    img: Image.Image, caixa: tuple[float, float, float, float], margem: float = 0.12
) -> tuple[Image.Image, int, int] | None:
    x0, y0, x1, y1 = caixa
    pw, ph = (x1 - x0) * margem, (y1 - y0) * margem
    x0, y0 = max(0, int(x0 - pw)), max(0, int(y0 - ph))
    x1, y1 = min(img.width, int(x1 + pw)), min(img.height, int(y1 + ph))
    if x1 - x0 < MIN_LADO_PX or y1 - y0 < MIN_LADO_PX:
        return None
    return img.crop((x0, y0, x1, y1)), x0, y0


def _janelas_ancora(img: Image.Image, ref: Referencia) -> list[tuple[float, float, float, float]]:
    escala = 1.0
    trabalho = img
    _, azul = mascaras_cor(trabalho)
    while azul.sum() > MAX_PIXELS_COMPONENTE and trabalho.width > 200:
        escala *= 2
        trabalho = img.resize((max(1, int(img.width / escala)), max(1, int(img.height / escala))), Image.Resampling.BOX)
        _, azul = mascaras_cor(trabalho)
    caixas = []
    for x0, y0, x1, y1, n in _componentes(azul, min_pixels=max(8, int(40 / (escala * escala)))):
        w, h = (x1 - x0) * escala, (y1 - y0) * escala
        if h < MIN_LADO_PX or abs(math.log((w / h) / ref.proporcao_v)) > 0.5:
            continue
        if n * escala * escala < 0.15 * w * h:  # o traço do "V" ocupa boa parte da caixa
            continue
        lx, ly = x0 * escala + ref.esquerda * w, y0 * escala + ref.topo * h
        caixas.append((lx, ly, lx + ref.largura * w, ly + ref.altura * h))
    return caixas


def detectar_logo_antiga(
    ev: Evidencia,
    dir_campanha: Path,
    referencia: Referencia,
    *,
    limiar: float = LIMIAR_PADRAO,
    max_correspondencias: int = 5,
    altura_viewport: int = 900,
) -> list[CorrespondenciaVisual]:
    correspondencias: list[CorrespondenciaVisual] = []
    capturas = [("captura_viewport", "E-SHOT-VIEWPORT"), ("captura_completa", "E-SHOT-FULL")]
    imagens = ev.pagina.imagens if ev.pagina else []
    for tipo, id_ref in capturas:
        art = ev.artefato(tipo)
        caminho = dir_campanha / art.caminho if art else None
        if caminho is None or not caminho.is_file():
            continue
        with Image.open(caminho) as bruto:
            captura = bruto.convert("RGB")
        candidatas: list[tuple[tuple[float, float, float, float], str | None, float]] = []
        for im in imagens:
            if im.largura < MIN_LADO_PX or im.altura < MIN_LADO_PX or im.pagina_x is None or im.pagina_y is None:
                continue
            if tipo == "captura_viewport" and not im.acima_dobra:
                continue
            candidatas.append(
                ((im.pagina_x, im.pagina_y, im.pagina_x + im.largura, im.pagina_y + im.altura), im.src or None, 0.02)
            )
        # janela ancorada é estimativa: recorta com folga, a caixa da tinta dá a posição real
        candidatas += [(caixa, None, 0.3) for caixa in _janelas_ancora(captura, referencia)]
        for caixa, src, margem in candidatas:
            corte = _recortar(captura, caixa, margem)
            if corte is None:
                continue
            recorte, ox, oy = corte
            pontuacao, tinta = correspondencia_regiao(recorte, referencia.assinatura)
            if pontuacao >= limiar and tinta is not None:
                x0, y0, x1, y1 = tinta[0] + ox, tinta[1] + oy, tinta[2] + ox, tinta[3] + oy
                correspondencias.append(
                    CorrespondenciaVisual(
                        tipo="logo_antiga",
                        ref_captura=id_ref,
                        bbox=[x0, y0, x1 - x0, y1 - y0],
                        pontuacao=round(pontuacao, 3),
                        src_imagem=src,
                        acima_dobra=tipo == "captura_viewport" or y0 < altura_viewport,
                    )
                )
    correspondencias.sort(key=lambda m: -m.pontuacao)
    saida: list[CorrespondenciaVisual] = []
    for m in correspondencias:
        cx, cy = m.bbox[0] + m.bbox[2] / 2, m.bbox[1] + m.bbox[3] / 2
        dup = any(
            abs(o.bbox[0] + o.bbox[2] / 2 - cx) < max(o.bbox[2], m.bbox[2]) / 2
            and abs(o.bbox[1] + o.bbox[3] / 2 - cy) < max(o.bbox[3], m.bbox[3]) / 2
            for o in saida
        )
        if not dup:
            saida.append(m)
        if len(saida) >= max_correspondencias:
            break
    return saida


_PISTA_ARQUIVO_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:bv|bancobv|bvfinanceira|votorantim)(?:[^a-z0-9]|$)|banco[-_ ]?bv|bv[-_ ]?financeira"
)
_PISTA_ALT_RE = re.compile(
    r"(?i)^\s*(?:logo(?:tipo|marca)?\s*(?:do|da)?\s*)?(?:banco\s+)?(?:bv|votorantim)(?:\s+financeira)?"
    r"(?:\s+logo(?:tipo)?)?\s*$|\blogo\w*\b.{0,25}\bbv\b|\bbv\b.{0,25}\blogo"
)


def pistas_logo_antiga(ev: Evidencia) -> list[str]:
    # <img> visíveis cujo nome de arquivo ou alt curto cita a marca (logo-bv-financeira.png, bv.png, alt="BV").
    # A logo está na página de alguma forma que a comparação de cor pode não confirmar (carrossel em
    # cinza, logo sobre fundo carmim); na campanha focada a página vai para o analista multimodal mesmo assim.
    saida: list[str] = []
    for im in ev.pagina.imagens if ev.pagina else []:
        if im.largura < MIN_LADO_PX or im.altura < MIN_LADO_PX:
            continue
        nome = unquote(urlsplit(im.src or "").path.rsplit("/", 1)[-1])
        if _PISTA_ARQUIVO_RE.search(nome) or _PISTA_ALT_RE.search(im.alt or ""):
            saida.append(im.src or im.alt or "")
    return list(dict.fromkeys(saida))


def recortar_correspondencia(
    ev: Evidencia, dir_campanha: Path, correspondencia: CorrespondenciaVisual, margem: float = 0.6
) -> Image.Image | None:
    tipo = {"E-SHOT-VIEWPORT": "captura_viewport", "E-SHOT-FULL": "captura_completa"}.get(correspondencia.ref_captura)
    art = ev.artefato(tipo) if tipo else None
    caminho = dir_campanha / art.caminho if art else None
    if caminho is None or not caminho.is_file():
        return None
    x, y, w, h = correspondencia.bbox
    m = int(max(w, h) * margem)
    with Image.open(caminho) as bruto:
        return bruto.convert("RGB").crop(
            (max(0, x - m), max(0, y - m), min(bruto.width, x + w + m), min(bruto.height, y + h + m))
        )
