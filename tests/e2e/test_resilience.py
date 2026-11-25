"""Architecture-review regressions: crash isolation, resume semantics, idempotency, budgets."""

from __future__ import annotations

import asyncio

import pytest

from simio.agents import evidence as evidence_mod
from simio.core.models import BrandAnalysisOutput, DiscoveryHit, Priority, QueryRecord, ReviewOutput, ScenarioCase
from simio.core.models import CandidateStatus as S
from simio.core.models import FinalClassification as C
from simio.core.orchestrator import CampaignBusy, HuntOptions, Orchestrator
from simio.core.storage import ConcurrentModification
from simio.providers.llm import Budget, BudgetExceeded, Usage
from simio.providers.mock import MockProvider
from simio.providers.search import import_text
from simio.reporting.data import load_records
from tests.helpers import BRAND

pytestmark = pytest.mark.browser

STRONG = BrandAnalysisOutput(
    case=ScenarioCase.B_AMBIGUOUS_COMMUNICATION, classification=C.REVIEW_REQUIRED, priority=Priority.MEDIUM
)


async def test_unexpected_error_is_isolated_to_one_candidate(settings, storage, site, monkeypatch) -> None:
    settings.offline = True
    real = evidence_mod.compute_signals

    def boom(ev, brand, catalog):
        if ev.original_url.endswith("ambiguous.html"):
            raise ValueError("poison page")
        return real(ev, brand, catalog)

    monkeypatch.setattr("simio.core.orchestrator.compute_signals", boom)
    urls = import_text(f"{site}/ambiguous.html\n{site}/legit_partner.html\n{site}/lookalike.html")
    s = await Orchestrator(settings, storage).hunt(HuntOptions(imported=urls))
    by_url = {c.url.rsplit("/", 1)[-1]: c for c in storage.list_candidates(s.campaign_id)}
    assert by_url["ambiguous.html"].status == S.FAILED
    assert by_url["legit_partner.html"].status == S.COMPLETED and by_url["lookalike.html"].status == S.COMPLETED
    assert s.status == "COMPLETED"


async def test_poison_candidate_cannot_block_resume(settings, storage, site) -> None:
    settings.offline = True
    cid = storage.create_campaign("bv", {"brand": "bv", "limit": 5, "sources": [], "offline": True})
    storage.set_checkpoint(cid, "discovery_done", True)
    cand, _ = storage.upsert_candidate(
        cid, url=f"{site}/ambiguous.html", normalized_url="n", hostname="127.0.0.1", registrable_domain="127.0.0.1"
    )
    storage.transition(cand, S.QUEUED)
    storage.transition(cand, S.COLLECTING)
    for _ in range(settings.max_attempts):
        storage.bump(cand, "attempts")  # crashed/hung the process max_attempts times
    s = await Orchestrator(settings, storage).resume(cid)
    assert storage.get_candidate(cand).status == S.FAILED
    assert s.status == "COMPLETED"


async def test_resume_keeps_offline_mode_and_model(settings, storage, site) -> None:
    settings.offline = True
    urls = import_text(f"{site}/ambiguous.html")
    s1 = await Orchestrator(settings, storage).hunt(HuntOptions(imported=urls, limit=0))
    assert s1.counts == {"DISCOVERED": 1}
    # Operator now has Vertex configured: resume must NOT silently start sending pages to the model.
    calls: list[str] = []
    prov = MockProvider({"BrandAnalysisOutput": lambda sy, p: calls.append("x") or STRONG})
    settings.offline = False
    settings.analysis_model = "other-model"
    orch = Orchestrator(settings, storage, provider=prov)
    await orch.resume(s1.campaign_id)
    assert orch.settings.offline is True and calls == []


async def test_campaign_lease_prevents_concurrent_processing(settings, storage) -> None:
    cid = storage.create_campaign("bv", {"brand": "bv", "limit": 1, "sources": []})
    assert storage.acquire_lease(cid, "other-host:1:abcd") is None
    with pytest.raises(CampaignBusy):
        await Orchestrator(settings, storage).resume(cid)
    storage.release_lease(cid, "other-host:1:abcd")


def test_transition_compare_and_set(storage) -> None:
    cid = storage.create_campaign("bv", {})
    c, _ = storage.upsert_candidate(
        cid, url="https://a.example/", normalized_url="a", hostname="a.example", registrable_domain="a.example"
    )
    storage.transition(c, S.QUEUED)
    storage.transition(c, S.COLLECTING, expected=S.QUEUED)
    with pytest.raises(ConcurrentModification):
        storage.transition(c, S.COLLECTED, expected=S.QUEUED)


async def test_paid_brand_analysis_is_reused_after_reviewer_failure(settings, storage, site) -> None:
    settings.analysis_model = "m"
    settings.max_attempts = 3
    n = {"brand": 0, "review": 0}

    def analyst(sy, p):
        n["brand"] += 1
        return STRONG

    def reviewer(sy, p):
        n["review"] += 1
        if n["review"] == 1:
            return "not json"  # invalid twice (initial + repair) -> analysis retry
        return ReviewOutput(
            decision="CONFIRM",
            revised_classification=C.REVIEW_REQUIRED,
            revised_priority=Priority.MEDIUM,
            reasoning_summary="ok",
        )

    prov = MockProvider({"BrandAnalysisOutput": analyst, "ReviewOutput": reviewer})
    s = await Orchestrator(settings, storage, provider=prov).hunt(
        HuntOptions(imported=import_text(f"{site}/ambiguous.html"), sources=[])
    )
    assert s.status == "COMPLETED"
    assert n["brand"] == 1, "brand analysis was bought twice"


async def test_token_budget_is_a_hard_cap(storage) -> None:
    cid = storage.create_campaign("bv", {})
    budget = Budget(storage, cid, max_requests=100, max_tokens=10_000)

    async def call() -> None:
        await budget.reserve(4_000)
        await asyncio.sleep(0)
        await budget.commit("a", "m", Usage(prompt_tokens=3_000, output_tokens=1_000, total_tokens=4_000), 4_000)

    results = await asyncio.gather(*(call() for _ in range(4)), return_exceptions=True)
    assert sum(isinstance(r, BudgetExceeded) for r in results) == 2
    assert storage.usage_totals(cid)[1] <= 10_000


class _CountingSearch:
    name = "brave"

    def __init__(self, urls: list[str], fail_after: int | None = None) -> None:
        self.urls, self.calls, self.fail_after = urls, [], fail_after

    def available(self):
        return True, "brave: fake"

    async def run(self, hypothesis, query):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise KeyboardInterrupt  # simulated Ctrl+C mid-discovery
        self.calls.append(query)
        hits = [
            DiscoveryHit(source=self.name, original_url=u, query=query, hypothesis_id=hypothesis.id) for u in self.urls
        ]
        return hits, QueryRecord(source=self.name, query=query, hypothesis_id=hypothesis.id, result_count=len(hits))


async def test_interrupted_discovery_resumes_without_losing_imports_or_repaying(settings, storage, site) -> None:
    settings.offline = False
    settings.max_search_queries = 4
    imported = import_text(f"{site}/legit_partner.html")
    first = _CountingSearch([f"{site}/ambiguous.html"], fail_after=2)
    with pytest.raises(KeyboardInterrupt):
        await Orchestrator(settings, storage, connectors=[first]).hunt(
            HuntOptions(imported=imported, sources=["brave"], limit=10)
        )
    cid = storage.latest_campaign_id()
    assert storage.get_campaign(cid)["status"] == "INTERRUPTED"
    second = _CountingSearch([f"{site}/lookalike.html"])
    await Orchestrator(settings, storage, connectors=[second]).resume(cid)
    assert len(storage.list_queries(cid)) == 1 + 4  # 1 import record + cap of 4 searches overall
    assert not set(first.calls) & set(second.calls)
    urls = {c.url.rsplit("/", 1)[-1] for c in storage.list_candidates(cid)}
    assert {"legit_partner.html", "ambiguous.html", "lookalike.html"} <= urls


async def test_report_shows_one_consistent_round(settings, storage, site) -> None:
    settings.offline = True
    s = await Orchestrator(settings, storage).hunt(HuntOptions(imported=import_text(f"{site}/ambiguous.html")))
    cand = storage.list_candidates(s.campaign_id)[0]
    # A stale review from an older round must not be reported next to the current final.
    storage.save_assessment(
        cand.id,
        "review",
        ReviewOutput(
            decision="RECOLLECT",
            revised_classification=C.INCONCLUSIVE,
            revised_priority=Priority.LOW,
            reasoning_summary="old",
        ),
        evidence_id=999,
    )
    rec = load_records(storage, s.campaign_id, BRAND)[0]
    assert rec.review is None or rec.review.reasoning_summary != "old"
