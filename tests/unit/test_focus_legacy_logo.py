"""Focused campaign on the legacy logo (``simio hunt --focus logo-antiga``)."""

from __future__ import annotations

import contextlib
from pathlib import Path

from PIL import Image

from simio.agents.discovery import FOCUS_LEGACY_LOGO, DiscoveryAgent
from simio.agents.visual import legacy_logo_hints
from simio.core.models import (
    Artifact,
    BrandAnalysisOutput,
    BrandAssessment,
    CollectionStatus,
    Evidence,
    FinalDecision,
    ImageInfo,
    PageData,
    Priority,
    ReviewOutput,
    ScenarioCase,
)
from simio.core.models import FinalClassification as C
from simio.core.orchestrator import HuntOptions, Orchestrator
from simio.providers.mock import MockProvider
from simio.providers.search import import_text
from simio.security.url_policy import UrlPolicy
from tests.helpers import BRAND

LOGO = Image.open(BRAND.legacy_logo_paths()[0]).convert("RGB")


class _Named:
    def __init__(self, name: str, queries: list[str] | None = None) -> None:
        self.name = name
        self._q = queries or []

    def available(self) -> tuple[bool, str]:
        return True, self.name

    def queries(self) -> list[str]:
        return self._q

    async def run(self, hypothesis, query):  # pragma: no cover - planning only
        raise AssertionError


def test_focus_plan_uses_only_logo_hypotheses_and_skips_domain_connectors() -> None:
    conns = [_Named("serper"), _Named("lens", ["https://img.example/bv.png"]), _Named("typosquat", ["x"])]
    agent = DiscoveryAgent(BRAND, conns, UrlPolicy(), max_queries=500, focus=FOCUS_LEGACY_LOGO)
    plan = agent.plan(conns)
    ids = {p.hypothesis.id.split(":")[0] for p in plan}
    assert ids == {"H13-legacy-focus", "H12-legacy-logo"}
    assert not any(p.connector == "typosquat" for p in plan)
    assert any("typosquat: not used in a focused campaign" in d for d in agent.diagnostics)
    assert sum(p.connector == "serper" for p in plan) >= 40  # thematic + one per state


def _ev_with(images: list[ImageInfo]) -> Evidence:
    return Evidence(
        candidate_id="C1", attempt=1, status=CollectionStatus.OK, original_url="https://a.example/", page=PageData(images=images)
    )


def test_logo_hints_from_file_name_or_short_alt_text() -> None:
    def img(src: str, alt: str = "") -> ImageInfo:
        return ImageInfo(src=src, alt=alt, width=60, height=40)

    hit = [
        img("https://static.wixstatic.com/media/x~mv2.png/v1/fill/w_144/logo-bv-financeira-512.png"),
        img("https://loja.example/images/financial/bv.png"),
        img("https://loja.example/img/parceiro-3.png", alt="BV"),
        img("https://loja.example/img/p.png", alt="logo BV Financeira"),
    ]
    miss = [
        img("https://loja.example/img/bvx-banner.png"),
        img("https://loja.example/img/ilustracao.png", alt="Por que contratar o crédito da BV financeira com a gente"),
        ImageInfo(src="https://loja.example/bv.png", alt="", width=8, height=8),  # invisible pixel
    ]
    assert len(legacy_logo_hints(_ev_with(hit))) == 4
    assert legacy_logo_hints(_ev_with(miss)) == []


class _FakeCollector:
    """Writes a synthetic screenshot per URL: ``/logo`` shows the legacy logo, ``/hint`` has an image named
    ``bv.png`` but no visible logo, ``/plain`` has neither."""

    async def collect(self, candidate_id: str, url: str, attempt: int, campaign_dir: Path) -> Evidence:
        rel = Path("evidence") / candidate_id / f"attempt-{attempt}"
        (campaign_dir / rel).mkdir(parents=True, exist_ok=True)
        canvas = Image.new("RGB", (1366, 900), "white")
        images: list[ImageInfo] = []
        if url.endswith("/logo"):
            canvas.paste(LOGO.resize((110, 58)), (200, 150))
            images.append(ImageInfo(src=f"{url}/img/parceiro.png", alt="", width=110, height=58, page_x=200, page_y=150))
        elif url.endswith("/hint"):
            images.append(ImageInfo(src=f"{url}/img/bv.png", alt="", width=60, height=40, page_x=10, page_y=10))
        canvas.save(campaign_dir / rel / "viewport.png")
        return Evidence(
            candidate_id=candidate_id,
            attempt=attempt,
            status=CollectionStatus.OK,
            original_url=url,
            final_url=url,
            http_status=200,
            page=PageData(title="Loja", visible_text="Financiamento", images=images),
            artifacts=[
                Artifact(kind="screenshot_viewport", path=str(rel / "viewport.png"), sha256="x", size_bytes=1)
            ],
        )


async def test_focused_campaign_sends_only_logo_pages_to_the_model(settings, storage) -> None:
    settings.analysis_model = "m"
    calls: list[str] = []

    def analyst(sy, parts):
        calls.append("brand")
        return BrandAnalysisOutput(
            case=ScenarioCase.B_AMBIGUOUS_COMMUNICATION, classification=C.REVIEW_REQUIRED, priority=Priority.MEDIUM
        )

    def reviewer(sy, parts):
        return ReviewOutput(
            decision="CONFIRM", revised_classification=C.REVIEW_REQUIRED, revised_priority=Priority.MEDIUM
        )

    prov = MockProvider({"BrandAnalysisOutput": analyst, "ReviewOutput": reviewer})

    @contextlib.asynccontextmanager
    async def factory():
        yield _FakeCollector()

    urls = import_text("https://logo.example/logo\nhttps://hint.example/hint\nhttps://plain.example/plain")
    orch = Orchestrator(settings, storage, provider=prov, collector_factory=factory)
    s = await orch.hunt(HuntOptions(imported=urls, sources=[], focus=FOCUS_LEGACY_LOGO))
    assert s.status == "COMPLETED" and calls == ["brand", "brand"]

    by_host = {c.hostname: c for c in storage.list_candidates(s.campaign_id)}
    logo = storage.get_assessment(by_host["logo.example"].id, "final", FinalDecision)
    assert logo.legacy_visual_identity and logo.priority == Priority.HIGH
    plain = storage.get_assessment(by_host["plain.example"].id, "final", FinalDecision)
    assert plain.classification == C.NO_INDICATION and not plain.legacy_visual_identity
    assert any("campanha focada" in lim for lim in plain.limitations)
    brand = storage.get_assessment(by_host["plain.example"].id, "brand", BrandAssessment)
    assert brand.analyzer == f"focus:{FOCUS_LEGACY_LOGO}"
    assert storage.get_campaign(s.campaign_id)["params"]["focus"] == FOCUS_LEGACY_LOGO
