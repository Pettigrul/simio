"""Legacy (discontinued) logo: deterministic image comparison, decision floor and reverse image search."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from simio.agents.brand import apply_legacy_logo_rule
from simio.agents.compliance import ComplianceAnalyst, RequirementCatalog
from simio.agents.discovery import DiscoveryAgent, deduplicate, prioritise
from simio.agents.evidence import build_catalog, compute_signals
from simio.agents.identity import CobanRegistry, IdentityAgent
from simio.agents.visual import colour_masks, detect_legacy_logo, load_reference, region_score
from simio.core.decision import compose_final
from simio.core.models import (
    Artifact,
    BrandAssessment,
    CollectionStatus,
    Evidence,
    EvidenceSufficiency,
    ImageInfo,
    Level,
    PageData,
    Priority,
    Review,
    ReviewDecision,
    ScenarioCase,
)
from simio.core.models import FinalClassification as C
from simio.providers.search import LensSearch
from simio.security.url_policy import UrlPolicy
from tests.helpers import BRAND, make_evidence

REF_PATH = BRAND.legacy_logo_paths()[0]
REF = load_reference(REF_PATH)
LOGO = Image.open(REF_PATH).convert("RGB")


def _monochrome(img: Image.Image, rgb: tuple[int, int, int] = (30, 100, 255)) -> Image.Image:
    """The *current* logo: same monogram, a single blue."""
    red, blue = colour_masks(img)
    arr = np.full((img.height, img.width, 3), 255, dtype=np.uint8)
    arr[red | blue] = rgb
    return Image.fromarray(arr)


def _screenshot_evidence(tmp_path: Path, canvas: Image.Image, images: list[ImageInfo] | None = None) -> Evidence:
    rel = Path("evidence/C1/attempt-1")
    (tmp_path / rel).mkdir(parents=True)
    canvas.save(tmp_path / rel / "viewport.png")
    return Evidence(
        candidate_id="C1",
        attempt=1,
        status=CollectionStatus.OK,
        original_url="https://loja.example/",
        page=PageData(title="Loja", images=images or []),
        artifacts=[Artifact(kind="screenshot_viewport", path=str(rel / "viewport.png"), sha256="x", size_bytes=1)],
    )


def _page_with(logo: Image.Image, background: str = "white", at: tuple[int, int] = (180, 140)) -> Image.Image:
    canvas = Image.new("RGB", (1366, 900), background)
    # distractors: red button, blue header bar and text-like blocks
    canvas.paste(Image.new("RGB", (240, 60), (220, 20, 60)), (900, 600))
    canvas.paste(Image.new("RGB", (1366, 50), (0, 90, 170)), (0, 0))
    for i in range(6):
        canvas.paste(Image.new("RGB", (300, 10), (20, 110, 190)), (200, 400 + 30 * i))
    canvas.paste(logo, at)
    return canvas


def test_reference_matches_itself_at_small_sizes_and_current_logo_never_matches() -> None:
    for w in (950, 240, 95):
        small = LOGO.resize((w, int(w * LOGO.height / LOGO.width)))
        assert region_score(small, REF.signature) > 0.85
        assert region_score(_monochrome(small), REF.signature) == 0.0


def test_legacy_logo_found_in_screenshot_without_element_coordinates(tmp_path: Path) -> None:
    logo = LOGO.resize((110, 58))
    ev = _screenshot_evidence(tmp_path, _page_with(logo))
    [m] = detect_legacy_logo(ev, tmp_path, REF)
    assert m.screenshot_ref == "E-SHOT-VIEWPORT" and m.score > 0.7 and m.above_fold
    red, blue = colour_masks(logo)  # expected box = coloured ink of the pasted logo (it has white margins)
    ys, xs = np.nonzero(red | blue)
    x, y, w, h = m.bbox
    assert abs(x - (180 + xs.min())) <= 3 and abs(y - (140 + ys.min())) <= 3
    assert abs(w - (xs.max() + 1 - xs.min())) <= 4 and abs(h - (ys.max() + 1 - ys.min())) <= 4


def test_current_monochrome_logo_is_not_reported(tmp_path: Path) -> None:
    ev = _screenshot_evidence(tmp_path, _page_with(_monochrome(LOGO.resize((110, 58)))))
    assert detect_legacy_logo(ev, tmp_path, REF) == []


def test_logo_in_white_box_on_crimson_page_found_via_image_element(tmp_path: Path) -> None:
    box = Image.new("RGB", (90, 70), "white")
    box.paste(LOGO.resize((70, 37)), (10, 16))
    canvas = _page_with(box, background=(216, 15, 90), at=(128, 136))
    img = ImageInfo(
        alt="", src="https://loja.example/img/bv.png", above_fold=True, width=90, height=70, page_x=128, page_y=136
    )
    ev = _screenshot_evidence(tmp_path, canvas, [img])
    matches = detect_legacy_logo(ev, tmp_path, REF)
    assert matches and any(m.image_src == "https://loja.example/img/bv.png" for m in matches)


def _assessment(**kw) -> BrandAssessment:
    base = dict(
        case=ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION,
        classification=C.NO_INDICATION,
        priority=Priority.LOW,
        extraction_confidence=Level.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
        analyzer="model-x",
    )
    base.update(kw)
    return BrandAssessment(**base)


def test_image_match_floors_the_decision_and_survives_reviewer_downgrade(tmp_path: Path) -> None:
    ev = _screenshot_evidence(tmp_path, _page_with(LOGO.resize((110, 58))))
    matches = detect_legacy_logo(ev, tmp_path, REF)
    cat = build_catalog(ev, BRAND, visual_matches=matches)
    sig = compute_signals(ev, BRAND, cat)
    assert sig.legacy_logo_visual and any(i.ref == "E-VIS-LEGACY-001" for i in cat)

    a = apply_legacy_logo_rule(_assessment(), cat)
    assert a.legacy_visual_identity and a.legacy_visual_identity_source == "comparação de imagem"
    assert (a.classification, a.priority) == (C.REVIEW_REQUIRED, Priority.HIGH)
    assert any("E-VIS-LEGACY-001" in ind.evidence_refs for ind in a.brand_confusion_indicators)
    assert any("não registrou" in n for n in a.guardrail_notes)

    idn = IdentityAgent(BRAND, CobanRegistry()).attribute("C1", ev, cat, sig)
    comp = ComplianceAnalyst(RequirementCatalog.load()).assess(a, idn, False)
    clear = Review(
        decision=ReviewDecision.DOWNGRADE,
        revised_classification=C.NO_INDICATION,
        revised_priority=Priority.LOW,
        reviewer="r",
    )
    f = compose_final("C1", a, clear, idn, comp, ev)
    assert f.classification == C.REVIEW_REQUIRED and f.priority == Priority.HIGH
    assert f.legacy_visual_identity and f.requires_human_review


def test_model_only_detection_is_recorded_and_absence_changes_nothing() -> None:
    ev = make_evidence(title="Loja")
    cat = build_catalog(ev, BRAND)
    a = apply_legacy_logo_rule(_assessment(legacy_visual_identity=True), cat)
    assert a.legacy_visual_identity_source == "modelo" and a.classification == C.REVIEW_REQUIRED
    untouched = _assessment()
    assert apply_legacy_logo_rule(untouched, cat) == untouched


async def test_lens_reverse_image_search_plans_and_prioritises() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "organic": [
                    {"title": "Financiamento", "link": "https://loja-exemplo.com.br/financiamento", "source": "Loja"},
                    {
                        "title": "BV logo vector",
                        "link": "https://www.shutterstock.com/image-vector/bv-1",
                        "source": "x",
                    },
                    {"title": "raw", "link": "x-raw-image:///abc", "source": "y"},
                ]
            },
        )

    seed = "https://logos.example/bv-financeira.png"
    conn = LensSearch("k", [seed], transport=httpx.MockTransport(handler))
    agent = DiscoveryAgent(BRAND, [conn], UrlPolicy(), max_queries=10)
    plan = agent.plan([conn])
    assert [(p.query, p.hypothesis.id) for p in plan] == [(seed, "H12-legacy-logo:lens")]
    outcome = await agent.run()
    assert seen[0]["url"] == seed
    groups, _ = deduplicate(outcome.hits, UrlPolicy())
    ranked = {g.hostname: g for g in prioritise(groups, BRAND)}
    assert "loja-exemplo.com.br" in ranked and ranked["loja-exemplo.com.br"].score > ranked["shutterstock.com"].score
    assert any("legacy logo" in r for r in ranked["loja-exemplo.com.br"].reasons)
    assert LensSearch("k", []).available()[0] is False and LensSearch(None, [seed]).available()[0] is False


def test_model_flag_survives_the_brand_guardrails() -> None:
    from simio.core.decision import ScoringConfig, apply_brand_guardrails
    from simio.core.models import BrandAnalysisOutput

    ev = make_evidence(title="Loja")
    cat = build_catalog(ev, BRAND)
    out = BrandAnalysisOutput(
        case=ScenarioCase.B_AMBIGUOUS_COMMUNICATION,
        classification=C.REVIEW_REQUIRED,
        priority=Priority.MEDIUM,
        legacy_visual_identity=True,
    )
    a = apply_brand_guardrails(
        out,
        analyzer="model-x",
        prompt_version="v",
        valid_refs={i.ref for i in cat},
        evidence=ev,
        signals=compute_signals(ev, BRAND, cat),
        scoring=ScoringConfig(),
    )
    assert a.legacy_visual_identity


def test_reference_site_records_the_logo_without_raising_the_case(tmp_path: Path) -> None:
    assert BRAND.is_legacy_logo_exempt("commons.wikimedia.org") and not BRAND.is_legacy_logo_exempt("linkedin.com")
    ev = _screenshot_evidence(tmp_path, _page_with(LOGO.resize((110, 58))))
    cat = build_catalog(ev, BRAND, visual_matches=detect_legacy_logo(ev, tmp_path, REF))
    a = apply_legacy_logo_rule(_assessment(), cat, exempt=True)
    assert a.legacy_visual_identity and a.legacy_logo_exempt and "site de referência" in a.legacy_visual_identity_source
    assert (a.classification, a.priority) == (C.NO_INDICATION, Priority.LOW)
    idn = IdentityAgent(BRAND, CobanRegistry()).attribute("C1", ev, cat, compute_signals(ev, BRAND, cat))
    comp = ComplianceAnalyst(RequirementCatalog.load()).assess(a, idn, False)
    f = compose_final("C1", a, None, idn, comp, ev)
    assert f.classification == C.NO_INDICATION and f.legacy_visual_identity


def test_lens_discovery_snippet_is_not_a_page_fact() -> None:
    from simio.core.models import DiscoveryHit

    hit = DiscoveryHit(
        source="lens",
        original_url="https://loja.example/",
        title="Loja",
        snippet="Imagem visualmente semelhante ao logotipo antigo (fonte: Loja)",
        hypothesis_id="H12-legacy-logo:lens",
        query="https://img.example/bv.png",
    )
    ev = make_evidence(title="Loja")
    cat = build_catalog(ev, BRAND, discovery_hits=[hit])
    sig = compute_signals(ev, BRAND, cat)  # used to raise UnboundLocalError (E-DISC item first)
    assert not sig.legacy_logo_markers
