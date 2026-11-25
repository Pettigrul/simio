"""Deterministic detection of the brand's *legacy* logo in the collected screenshots.

The current Banco BV logo and the legacy one share the same monogram (a "B" with a crossbar and a
"V"); what changed is the colour: the legacy logo has a **red/crimson "B" and a blue "V"**, the
current one is monochrome blue. A shape-only hash would therefore confuse them, so every candidate
region is compared **per colour channel**: the red-ink mask and the blue-ink mask are each matched
against the reference logo (soft IoU on a normalised grid), and both must agree.

Candidate regions come from two places, all cut out of the hashed screenshots (so a match is fully
reproducible from the evidence):

* the bounding box of every visible ``<img>``/``svg`` element recorded by the collector;
* windows anchored on blue connected components with the proportions of the reference "V"
  (covers logos inside banners, social-media creatives and CSS backgrounds).

Nothing here calls a model or the network. The result is an independent confirmation of what the
multimodal analyst sees, and a safety net when the analyst misses a small logo.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from simio.core.models import Evidence, VisualMatch

GRID = 24
MIN_INK_PX = 120  # ignore regions with too little coloured ink to carry a shape
MIN_SIDE_PX = 14
MAX_COMPONENT_PIXELS = 400_000  # bound on BFS work per screenshot (after downscaling)
DEFAULT_THRESHOLD = 0.62

# PIL HSV uses 0-255 for hue. Legacy red/crimson ~ 225-255 (+ wrap to 0-6); legacy blue ~ 125-151.
# The current BV blue sits at ~152-159 and is deliberately outside the blue band's core, but the
# decisive criterion is the red "B": a monochrome logo has no red ink and never matches.
_RED_HUES = ((222, 256), (0, 7))
_BLUE_HUES = ((122, 154),)
_S_MIN, _V_MIN = 80, 60

Image.MAX_IMAGE_PIXELS = 60_000_000  # full-page screenshots are capped by the collector


def _hue_mask(h: np.ndarray, ranges: tuple[tuple[int, int], ...]) -> np.ndarray:
    m = np.zeros(h.shape, dtype=bool)
    for lo, hi in ranges:
        m |= (h >= lo) & (h < hi)
    return m


def colour_masks(img: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    """(red_mask, blue_mask) of saturated, reasonably bright pixels."""
    hsv = np.asarray(img.convert("RGB").convert("HSV"), dtype=np.int16)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    vivid = (s >= _S_MIN) & (v >= _V_MIN)
    return vivid & _hue_mask(h, _RED_HUES), vivid & _hue_mask(h, _BLUE_HUES)


def _components(mask: np.ndarray, *, min_pixels: int = 1) -> list[tuple[int, int, int, int, int]]:
    """4-connected components of a boolean mask -> [(x0, y0, x1, y1, n_pixels)] (x1/y1 exclusive)."""
    h, w = mask.shape
    seen = np.zeros_like(mask)
    out: list[tuple[int, int, int, int, int]] = []
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys.tolist(), xs.tolist(), strict=True):
        if seen[y0, x0]:
            continue
        seen[y0, x0] = True
        q = deque([(y0, x0)])
        minx = maxx = x0
        miny = maxy = y0
        n = 0
        while q:
            y, x = q.popleft()
            n += 1
            minx, maxx, miny, maxy = min(minx, x), max(maxx, x), min(miny, y), max(maxy, y)
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    q.append((ny, nx))
        if n >= min_pixels:
            out.append((minx, miny, maxx + 1, maxy + 1, n))
    return out


def _drop_border_components(mask: np.ndarray) -> np.ndarray:
    """Remove ink connected to the crop border (page background bleeding into an element crop)."""
    h, w = mask.shape
    keep = mask.copy()
    border = np.zeros_like(mask)
    border[0, :] = border[-1, :] = True
    border[:, 0] = border[:, -1] = True
    q = deque((int(y), int(x)) for y, x in zip(*np.nonzero(mask & border), strict=True))
    for y, x in q:
        keep[y, x] = False
    while q:
        y, x = q.popleft()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < h and 0 <= nx < w and keep[ny, nx]:
                keep[ny, nx] = False
                q.append((ny, nx))
    return keep


@dataclass(frozen=True)
class Signature:
    red: np.ndarray  # GRID x GRID coverage in [0, 1]
    blue: np.ndarray
    aspect: float  # width / height of the ink bounding box
    red_share: float  # red ink / all ink


def _resize(mask: np.ndarray) -> np.ndarray:
    im = Image.fromarray((mask * 255).astype(np.uint8))
    return np.asarray(im.resize((GRID, GRID), Image.Resampling.BOX), dtype=np.float32) / 255.0


def signature(red: np.ndarray, blue: np.ndarray) -> Signature | None:
    ink = red | blue
    if int(ink.sum()) < MIN_INK_PX or not red.any() or not blue.any():
        return None
    ys, xs = np.nonzero(ink)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    if (x1 - x0) < MIN_SIDE_PX or (y1 - y0) < MIN_SIDE_PX:
        return None
    r, b = red[y0:y1, x0:x1], blue[y0:y1, x0:x1]
    return Signature(_resize(r), _resize(b), (x1 - x0) / (y1 - y0), float(r.sum()) / float(ink[y0:y1, x0:x1].sum()))


def _soft_iou(a: np.ndarray, b: np.ndarray) -> float:
    den = float(np.maximum(a, b).sum())
    return float(np.minimum(a, b).sum()) / den if den else 0.0


def similarity(candidate: Signature, reference: Signature) -> float:
    if abs(math.log(candidate.aspect / reference.aspect)) > 0.35:
        return 0.0
    if not (0.45 * reference.red_share <= candidate.red_share <= min(0.97, 1.8 * reference.red_share)):
        return 0.0
    return 0.5 * (_soft_iou(candidate.red, reference.red) + _soft_iou(candidate.blue, reference.blue))


def region_match(img: Image.Image, reference: Signature) -> tuple[float, tuple[int, int, int, int] | None]:
    """Best similarity of a crop (with and without ink touching the crop border) and the bounding
    box (x0, y0, x1, y1) of the matching ink inside the crop."""
    red, blue = colour_masks(img)
    best: tuple[float, tuple[int, int, int, int] | None] = (0.0, None)
    for r, b in ((red, blue), (_drop_border_components(red), _drop_border_components(blue))):
        sig = signature(r, b)
        if sig is None:
            continue
        score = similarity(sig, reference)
        if score > best[0]:
            ys, xs = np.nonzero(r | b)
            best = (score, (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    return best


def region_score(img: Image.Image, reference: Signature) -> float:
    return region_match(img, reference)[0]


@dataclass(frozen=True)
class Reference:
    signature: Signature
    # Geometry of the logo box relative to the blue "V" component (largest blue blob), used to
    # turn a blue component found on a page into a candidate window.
    v_aspect: float
    left: float
    top: float
    width: float
    height: float


def load_reference(path: Path) -> Reference:
    img = Image.open(path).convert("RGB")
    red, blue = colour_masks(img)
    sig = signature(red, blue)
    if sig is None:
        raise ValueError(f"reference logo has no red and blue ink: {path}")
    ink = red | blue
    ys, xs = np.nonzero(ink)
    lx0, ly0, lx1, ly1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    vx0, vy0, vx1, vy1, _ = max(_components(blue, min_pixels=50), key=lambda c: c[4])
    vw, vh = vx1 - vx0, vy1 - vy0
    return Reference(
        signature=sig,
        v_aspect=vw / vh,
        left=(lx0 - vx0) / vw,
        top=(ly0 - vy0) / vh,
        width=(lx1 - lx0) / vw,
        height=(ly1 - ly0) / vh,
    )


def _crop(
    img: Image.Image, box: tuple[float, float, float, float], pad: float = 0.12
) -> tuple[Image.Image, int, int] | None:
    """Padded crop of ``box`` and its top-left offset in the screenshot."""
    x0, y0, x1, y1 = box
    pw, ph = (x1 - x0) * pad, (y1 - y0) * pad
    x0, y0 = max(0, int(x0 - pw)), max(0, int(y0 - ph))
    x1, y1 = min(img.width, int(x1 + pw)), min(img.height, int(y1 + ph))
    if x1 - x0 < MIN_SIDE_PX or y1 - y0 < MIN_SIDE_PX:
        return None
    return img.crop((x0, y0, x1, y1)), x0, y0


def _anchor_windows(img: Image.Image, ref: Reference) -> list[tuple[float, float, float, float]]:
    """Candidate logo boxes derived from blue components shaped like the reference "V"."""
    scale = 1.0
    work = img
    _, blue = colour_masks(work)
    while blue.sum() > MAX_COMPONENT_PIXELS and work.width > 200:
        scale *= 2
        work = img.resize((max(1, int(img.width / scale)), max(1, int(img.height / scale))), Image.Resampling.BOX)
        _, blue = colour_masks(work)
    boxes = []
    for x0, y0, x1, y1, n in _components(blue, min_pixels=max(8, int(40 / (scale * scale)))):
        w, h = (x1 - x0) * scale, (y1 - y0) * scale
        if h < MIN_SIDE_PX or abs(math.log((w / h) / ref.v_aspect)) > 0.5:
            continue
        if n * scale * scale < 0.15 * w * h:  # a "V" stroke fills a fair share of its box
            continue
        lx, ly = x0 * scale + ref.left * w, y0 * scale + ref.top * h
        boxes.append((lx, ly, lx + ref.width * w, ly + ref.height * h))
    return boxes


def detect_legacy_logo(
    ev: Evidence,
    campaign_dir: Path,
    reference: Reference,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_matches: int = 5,
    viewport_height: int = 900,
) -> list[VisualMatch]:
    """Legacy-logo matches in the evidence screenshots, best first (deduplicated by position)."""
    matches: list[VisualMatch] = []
    shots = [("screenshot_viewport", "E-SHOT-VIEWPORT"), ("screenshot_full", "E-SHOT-FULL")]
    images = ev.page.images if ev.page else []
    for kind, ref_id in shots:
        art = ev.artifact(kind)
        path = campaign_dir / art.path if art else None
        if path is None or not path.is_file():
            continue
        with Image.open(path) as raw:
            shot = raw.convert("RGB")
        candidates: list[tuple[tuple[float, float, float, float], str | None, float]] = []
        for im in images:
            if im.width < MIN_SIDE_PX or im.height < MIN_SIDE_PX or im.page_x is None or im.page_y is None:
                continue
            if kind == "screenshot_viewport" and not im.above_fold:
                continue
            candidates.append(
                ((im.page_x, im.page_y, im.page_x + im.width, im.page_y + im.height), im.src or None, 0.02)
            )
        # anchor windows are estimates: crop generously, the ink bounding box gives the real position
        candidates += [(box, None, 0.3) for box in _anchor_windows(shot, reference)]
        for box, src, pad in candidates:
            cut = _crop(shot, box, pad)
            if cut is None:
                continue
            crop, ox, oy = cut
            score, ink = region_match(crop, reference.signature)
            if score >= threshold and ink is not None:
                x0, y0, x1, y1 = ink[0] + ox, ink[1] + oy, ink[2] + ox, ink[3] + oy
                matches.append(
                    VisualMatch(
                        kind="legacy_logo",
                        screenshot_ref=ref_id,
                        bbox=[x0, y0, x1 - x0, y1 - y0],
                        score=round(score, 3),
                        image_src=src,
                        above_fold=kind == "screenshot_viewport" or y0 < viewport_height,
                    )
                )
    matches.sort(key=lambda m: -m.score)
    out: list[VisualMatch] = []
    for m in matches:
        cx, cy = m.bbox[0] + m.bbox[2] / 2, m.bbox[1] + m.bbox[3] / 2
        dup = any(
            abs(o.bbox[0] + o.bbox[2] / 2 - cx) < max(o.bbox[2], m.bbox[2]) / 2
            and abs(o.bbox[1] + o.bbox[3] / 2 - cy) < max(o.bbox[3], m.bbox[3]) / 2
            for o in out
        )
        if not dup:
            out.append(m)
        if len(out) >= max_matches:
            break
    return out
