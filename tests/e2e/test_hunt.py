"""End-to-end: discovery -> collection -> analysis -> review -> reports, against local sites.

Real Chromium is used for collection; the LLM is either absent (offline heuristic) or a scripted
MockProvider. Live Vertex AI is NEVER called here.
"""

from __future__ import annotations

import re

import pytest
from openpyxl import load_workbook
from typer.testing import CliRunner

from simio.cli import app
from simio.core.models import (
    BrandAnalysisOutput,
    DiscoveryHit,
    FinalDecision,
    Priority,
    QueryRecord,
    ReviewDecision,
    ReviewOutput,
    ScenarioCase,
)
from simio.core.models import (
    CandidateStatus as S,
)
from simio.core.models import (
    FinalClassification as C,
)
from simio.core.orchestrator import HuntOptions, Orchestrator
from simio.providers.mock import MockProvider
from simio.providers.search import import_text
from simio.reporting import generate_reports

pytestmark = pytest.mark.browser

PAGES = [
    "legit_partner.html",
    "ambiguous.html",
    "lookalike.html",
    "third_party_official_contact.html",
    "contradictory.html",
    "injection.html",
    "unavailable",
]


def _urls(site: str) -> str:
    return "\n".join(f"{site}/{p}" for p in PAGES)


def _finals(storage, cid) -> dict[str, FinalDecision]:
    out = {}
    for c in storage.list_candidates(cid):
        f = storage.get_assessment(c.id, "final", FinalDecision)
        if f:
            out[c.url.rsplit("/", 1)[-1]] = f
    return out


async def test_offline_hunt_end_to_end(settings, storage, site) -> None:
    settings.offline = True
    summary = await Orchestrator(settings, storage).hunt(HuntOptions(imported=import_text(_urls(site)), limit=50))
    assert summary.status == "COMPLETED", summary
    assert summary.llm_requests == 0
    counts = summary.counts
    assert counts.get("COMPLETED", 0) + counts.get("INCONCLUSIVE", 0) == len(PAGES)
    finals = _finals(storage, summary.campaign_id)
    assert finals["legit_partner.html"].classification == C.NO_INDICATION
    assert finals["ambiguous.html"].classification == C.REVIEW_REQUIRED
    assert finals["lookalike.html"].classification == C.STRONG_INDICATION
    assert finals["unavailable"].classification == C.INCONCLUSIVE
    assert finals["injection.html"].classification != C.NO_INDICATION
    # contradictory evidence is challenged by the reviewer -> not left as STRONG
    assert finals["contradictory.html"].classification == C.REVIEW_REQUIRED
    assert finals["third_party_official_contact.html"].identity_class.value == "OTHER_THIRD_PARTY"

    report = generate_reports(settings, storage, summary.campaign_id)
    wb = load_workbook(report.workbook)
    assert wb.sheetnames[:2] == ["Executive Summary", "Candidates"]
    for name in (
        "Strong Indications",
        "Identity Attribution",
        "Evidence Index",
        "Investigation Log",
        "Errors and Limitations",
    ):
        assert name in wb.sheetnames
    cand = wb["Candidates"]
    headers = [c.value for c in cand[1]]
    for h in (
        "ID",
        "Domínio",
        "URL",
        "Origem da descoberta",
        "Consulta utilizada",
        "Identidade atribuída",
        "Categoria de relacionamento",
        "Classificação",
        "Prioridade",
        "Justificativa",
        "Principais evidências",
        "Limitações",
        "Revisão humana",
        "Screenshot (viewport)",
        "Data da coleta",
    ):
        assert h in headers
    assert cand.max_row == len(PAGES) + 1
    assert wb["Strong Indications"].max_row >= 2
    # Dossiers for priority candidates, readable offline, escaped, script-free.
    assert report.dossiers
    html = (settings.output_dir / report.dossiers[next(iter(report.dossiers))]).read_text()
    assert "<script" not in html.lower()
    assert "Content-Security-Policy" in html
    lookalike_id = next(c.id for c in storage.list_candidates(summary.campaign_id) if c.url.endswith("lookalike.html"))
    dossier = (settings.output_dir / report.dossiers[lookalike_id]).read_text()
    assert "E-SHOT-VIEWPORT" in dossier and "Art. 14" in dossier
    shots = re.findall(r'<img src="([^"]+)"', dossier)
    assert shots and all((report.report_dir / "dossiers" / s).resolve().is_file() for s in shots)
    inj_id = next(c.id for c in storage.list_candidates(summary.campaign_id) if c.url.endswith("injection.html"))
    inj = (settings.output_dir / report.dossiers[inj_id]).read_text()
    assert "Ignore todas as instru" in inj and "font-size:2px" not in inj  # text kept, markup never rendered


def _mock_provider() -> MockProvider:
    def analyst(system: str, parts) -> BrandAnalysisOutput:
        text = "\n".join(p for p in parts if isinstance(p, str))
        if "Site Oficial" in text or "canal oficial" in text:
            return BrandAnalysisOutput(
                case=ScenarioCase.D_STRONG_IMPERSONATION,
                classification=C.STRONG_INDICATION,
                priority=Priority.HIGH,
                brand_confusion_indicators=[
                    {
                        "dimension": "INSTITUTIONAL_LANGUAGE",
                        "description": "afirma ser canal oficial",
                        "evidence_refs": ["E-TITLE"],
                        "strength": "HIGH",
                    },
                    {
                        "dimension": "LOGO_USAGE",
                        "description": "logo da marca",
                        "evidence_refs": ["E-SHOT-VIEWPORT"],
                        "strength": "HIGH",
                    },
                ],
                evidence_references=["E-TITLE"],
                reasoning_summary="mock: impersonação",
            )
        return BrandAnalysisOutput(
            case=ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION,
            classification=C.NO_INDICATION,
            priority=Priority.LOW,
            reasoning_summary="mock: legítimo",
            requires_human_review=False,
        )

    def reviewer(system: str, parts) -> ReviewOutput:
        assert any("Hipótese do analista" in p for p in parts if isinstance(p, str))
        return ReviewOutput(
            decision=ReviewDecision.CONFIRM,
            revised_classification=C.STRONG_INDICATION,
            revised_priority=Priority.HIGH,
            reasoning_summary="mock: confirmado",
        )

    return MockProvider({"BrandAnalysisOutput": analyst, "ReviewOutput": reviewer})


class _FakeSearch:
    """Keyword connector double returning local fixture URLs (stands in for Brave/Serper/Grounding)."""

    name = "grounding"

    def __init__(self, urls: list[tuple[str, str]]) -> None:
        self.urls = urls

    def available(self):
        return True, "grounding: fake"

    async def run(self, hypothesis, query):
        hits = [
            DiscoveryHit(source=self.name, original_url=u, title=t, query=query, hypothesis_id=hypothesis.id)
            for u, t in self.urls
        ]
        return hits, QueryRecord(source=self.name, query=query, hypothesis_id=hypothesis.id, result_count=len(hits))


async def test_llm_hunt_with_discovery_review_and_limit(settings, storage, site) -> None:
    settings.analysis_model = "mock-analyst"
    settings.max_search_queries = 3
    provider = _mock_provider()
    search = _FakeSearch(
        [
            (f"{site}/lookalike.html", "Banco BV | Site Oficial"),
            (f"{site}/legit_partner.html?utm_source=x", "Auto Center Exemplo"),
            (f"{site}/legit_partner.html", "Auto Center Exemplo"),
            (f"{site}/ambiguous.html", "Carros Exemplo"),
        ]
    )
    orch = Orchestrator(settings, storage, provider=provider, connectors=[search])
    summary = await orch.hunt(HuntOptions(limit=2))
    cid = summary.campaign_id
    # dedup: 3 unique URLs; limit 2 analysed; 1 left DISCOVERED (traceable backlog)
    assert len(storage.list_candidates(cid)) == 3
    assert summary.counts.get("DISCOVERED") == 1
    assert len(storage.list_queries(cid)) == 3
    finals = _finals(storage, cid)
    look = finals["lookalike.html"]
    assert look.classification == C.STRONG_INDICATION and "[Revisor: CONFIRM]" in look.justification
    kinds = [c.schema for c in provider.calls]
    assert "ReviewOutput" in kinds and "BrandAnalysisOutput" in kinds
    # images were sent to the multimodal analyst
    assert any(not isinstance(p, str) for c in provider.calls for p in c.parts)
    assert summary.llm_requests == len(provider.calls)


async def test_budget_exhaustion_pauses_and_resume_completes(settings, storage, site) -> None:
    settings.analysis_model = "mock-analyst"
    settings.max_llm_requests = 1
    settings.workers = 1
    provider = _mock_provider()
    urls = import_text(f"{site}/lookalike.html\n{site}/legit_partner.html")
    s1 = await Orchestrator(settings, storage, provider=provider).hunt(HuntOptions(imported=urls, sources=[]))
    assert s1.status == "PAUSED_BUDGET"
    assert storage.status_counts(s1.campaign_id).get("COLLECTED", 0) >= 1
    settings.max_llm_requests = 50
    s2 = await Orchestrator(settings, storage, provider=provider).resume(s1.campaign_id)
    assert s2.status == "COMPLETED", s2
    assert set(s2.counts) <= {"COMPLETED", "INCONCLUSIVE"}


async def test_resume_after_crash_recovers_in_flight(settings, storage, site) -> None:
    settings.offline = True
    s1 = await Orchestrator(settings, storage).hunt(HuntOptions(imported=import_text(f"{site}/ambiguous.html")))
    cid = s1.campaign_id
    # Simulate a crash mid-collection on a new candidate.
    cand_id, _ = storage.upsert_candidate(
        cid,
        url=f"{site}/lookalike.html",
        normalized_url="x-lookalike",
        hostname="127.0.0.1",
        registrable_domain="127.0.0.1",
    )
    storage.transition(cand_id, S.QUEUED)
    storage.transition(cand_id, S.COLLECTING)
    storage.set_campaign_status(cid, "RUNNING")
    s2 = await Orchestrator(settings, storage).resume(cid)
    assert s2.status == "COMPLETED"
    assert storage.get_candidate(cand_id).status == S.COMPLETED
    notes = [t["note"] for t in storage.transitions(cand_id)]
    assert "recovered after interruption" in notes


async def test_llm_unavailable_degrades_to_heuristic(settings, storage, site) -> None:
    from simio.providers.llm import LLMUnavailable

    def boom(system, parts):
        raise LLMUnavailable("403: permission denied")

    settings.analysis_model = "mock"
    orch = Orchestrator(settings, storage, provider=MockProvider({"BrandAnalysisOutput": boom}))
    s = await orch.hunt(HuntOptions(imported=import_text(f"{site}/lookalike.html"), sources=[]))
    assert s.status == "COMPLETED"
    f = next(iter(_finals(storage, s.campaign_id).values()))
    assert any("LLM indisponível" in x for x in f.limitations)
    assert any("degraded" in d for d in s.diagnostics)


async def test_invalid_model_output_fails_after_retries(settings, storage, site) -> None:
    settings.analysis_model = "mock"
    orch = Orchestrator(settings, storage, provider=MockProvider({"BrandAnalysisOutput": lambda s, p: "garbage"}))
    s = await orch.hunt(HuntOptions(imported=import_text(f"{site}/ambiguous.html"), sources=[]))
    assert s.counts == {"FAILED": 1}
    assert any(e["error_type"] == "LLMOutputInvalid" for e in storage.errors(s.campaign_id))


# ------------------------------------------------------------------ CLI
def test_cli_help_and_guards(settings, tmp_path) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("doctor", "hunt", "resume", "report", "status", "test"):
        assert cmd in r.output
    r = runner.invoke(app, ["hunt", "--help"])
    for opt in (
        "--limit",
        "--workers",
        "--model",
        "--max-queries",
        "--max-llm-requests",
        "--max-tokens",
        "--sources",
        "--output-dir",
        "--offline",
        "--urls-file",
    ):
        assert opt in r.output
    out = str(tmp_path / "cli-out")
    r = runner.invoke(app, ["hunt", "--offline", "-o", out])
    assert r.exit_code == 2 and "offline" in r.output.lower()
    r = runner.invoke(app, ["status", "-o", out])
    assert r.exit_code == 1


def test_cli_offline_hunt_report_status(settings, site, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SIMIO_UNSAFE_ALLOW_HOSTS", "127.0.0.1")
    monkeypatch.setenv("SIMIO_SETTLE_MS", "100")
    monkeypatch.setenv("SIMIO_RETRY_BASE_DELAY_S", "0")
    urls = tmp_path / "urls.txt"
    urls.write_text(f"{site}/legit_partner.html\n{site}/lookalike.html\n", encoding="utf-8")
    out = str(tmp_path / "cli-out")
    runner = CliRunner()
    r = runner.invoke(app, ["hunt", "--offline", "--urls-file", str(urls), "-o", out])
    assert r.exit_code == 0, r.output
    assert "COMPLETED" in r.output and ".xlsx" in r.output
    r = runner.invoke(app, ["status", "-o", out])
    assert r.exit_code == 0 and "COMPLETED" in r.output
    r = runner.invoke(app, ["report", "-o", out, "--all-dossiers"])
    assert r.exit_code == 0 and "Dossiês: 2" in r.output
    r = runner.invoke(app, ["status", "--list", "-o", out])
    assert r.exit_code == 0 and "simio-" in r.output
