"""Evidence collection with a real headless Chromium against local synthetic sites."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import pytest

from simio.agents.brand import heuristic_analysis
from simio.agents.evidence import build_catalog, compute_signals
from simio.collectors.browser import BrowserCollector, sha256_file
from simio.core.decision import ScoringConfig, apply_brand_guardrails
from simio.core.models import CollectionStatus, FinalClassification, ScenarioCase
from simio.security.url_policy import UrlPolicy
from tests.helpers import BRAND

pytestmark = pytest.mark.browser


def _collector(settings) -> BrowserCollector:
    return BrowserCollector(settings, UrlPolicy.from_settings(settings.allowed_ports, settings.unsafe_allow_hosts))


async def _collect(settings, url: str, cid: str = "Cx"):
    cdir = settings.output_dir / "campaigns" / "t"
    async with _collector(settings) as col:
        return await col.collect(cid, url, 1, cdir), cdir


async def test_collects_screenshots_text_and_hashes(settings, site) -> None:
    ev, cdir = await _collect(settings, f"{site}/legit_partner.html")
    assert ev.status == CollectionStatus.OK, ev.errors
    assert ev.http_status == 200 and ev.final_url.endswith("/legit_partner.html")
    assert ev.page and "Auto Center Exemplo" in ev.page.title
    assert "correspondente bancário" in ev.page.header_text
    assert "CNPJ 11.222.333/0001-81" in ev.page.footer_text
    assert any("Simular" in c.text for c in ev.page.ctas)
    kinds = {a.kind for a in ev.artifacts}
    assert {"screenshot_viewport", "screenshot_full", "html", "text", "page_data"} <= kinds
    for a in ev.artifacts:
        p = cdir / a.path
        assert p.is_file() and sha256_file(p) == a.sha256 and p.stat().st_size == a.size_bytes
    vp = cdir / ev.artifact("screenshot_viewport").path
    assert vp.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert not any(a.path.endswith(".html") for a in ev.artifacts)  # raw HTML stored as .html.txt
    manifest = json.loads((cdir / ev.manifest_path).read_text(encoding="utf-8"))
    assert manifest["hash_algorithm"] == "SHA-256" and len(manifest["artifacts"]) == len(ev.artifacts)
    assert sha256_file(cdir / ev.manifest_path) == ev.manifest_sha256


async def test_follows_allowed_redirect(settings, site) -> None:
    ev, _ = await _collect(settings, f"{site}/redirect-ok")
    assert ev.status == CollectionStatus.OK
    assert ev.redirect_chain == [f"{site}/redirect-ok"]
    assert ev.final_url.endswith("/legit_partner.html")


@pytest.mark.parametrize("path", ["/redirect-private", "/redirect-metadata"])
async def test_redirect_to_internal_destination_is_blocked(settings, site, server_hits, path: str) -> None:
    """Regression: Playwright route() is not invoked for redirect hops; the egress proxy must be."""
    ev, _ = await _collect(settings, f"{site}{path}")
    assert ev.status == CollectionStatus.BLOCKED
    assert ev.page is None
    assert any("localhost" in b or "169.254" in b for b in ev.blocked_requests), ev.blocked_requests
    assert not [h for h in server_hits if h[0].startswith("localhost")], server_hits


async def test_subresource_redirect_to_internal_is_blocked(settings, site, server_hits) -> None:
    ev, _ = await _collect(settings, f"{site}/subresource_redirect.html")
    assert ev.status == CollectionStatus.OK
    assert any("localhost" in b for b in ev.blocked_requests), ev.blocked_requests
    assert ("127.0.0.1:" + site.rsplit(":", 1)[1], "/redirect-private") in server_hits
    assert not [h for h in server_hits if h[0].startswith("localhost")], server_hits


async def test_websocket_to_internal_is_blocked(settings, site) -> None:
    ev, _ = await _collect(settings, f"{site}/websocket_probe.html")
    assert any("10.0.0.2" in b for b in ev.blocked_requests), ev.blocked_requests


async def test_internal_subresources_are_blocked(settings, site) -> None:
    ev, _ = await _collect(settings, f"{site}/ssrf_probe.html")
    assert ev.status == CollectionStatus.OK
    blocked = " ".join(ev.blocked_requests)
    for target in ("169.254.169.254", "10.0.0.1", "localhost", "192.168.0.1", "[::1]"):
        assert target in blocked, (target, ev.blocked_requests)


async def test_blocked_start_url_is_never_fetched(settings) -> None:
    ev, _ = await _collect(settings, "http://169.254.169.254/latest/meta-data/")
    assert ev.status == CollectionStatus.BLOCKED and not ev.artifacts[:-1]
    ev2, _ = await _collect(settings, "file:///etc/passwd")
    assert ev2.status == CollectionStatus.BLOCKED


async def test_unavailable_page_is_failed_not_absent(settings, site) -> None:
    ev, _ = await _collect(settings, f"{site}/unavailable")
    assert ev.status == CollectionStatus.FAILED and ev.http_status == 503
    assert any("HTTP 503" in e for e in ev.errors)


async def test_unreachable_host_is_failed(settings) -> None:
    ev, _ = await _collect(settings, "http://127.0.0.1:9/")  # nothing listens on port 9
    assert ev.status == CollectionStatus.FAILED and ev.page is None
    assert any("não inspecionado" in n for n in ev.not_inspected)


async def test_timeout_is_failed_and_retryable(settings, site) -> None:
    settings.nav_timeout_ms = 1500
    ev, _ = await _collect(settings, f"{site}/slow")
    assert ev.status == CollectionStatus.FAILED and ev.retryable
    assert any("timeout" in e for e in ev.errors)


async def test_download_is_not_saved(settings, site) -> None:
    ev, cdir = await _collect(settings, f"{site}/download")
    assert ev.status in (CollectionStatus.FAILED, CollectionStatus.BLOCKED)
    assert not list(cdir.rglob("*.exe"))


@pytest.mark.parametrize(
    ("page", "expected_cases"),
    [
        ("legit_partner.html", {ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION}),
        ("ambiguous.html", {ScenarioCase.B_AMBIGUOUS_COMMUNICATION}),
        ("lookalike.html", {ScenarioCase.D_STRONG_IMPERSONATION}),
        (
            "third_party_official_contact.html",
            {ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL, ScenarioCase.D_STRONG_IMPERSONATION},
        ),
        ("unrelated.html", {ScenarioCase.NOT_RELEVANT}),
    ],
)
async def test_heuristic_classification_on_rendered_fixtures(settings, site, page, expected_cases) -> None:
    ev, _ = await _collect(settings, f"{site}/{page}")
    cat = build_catalog(ev, BRAND)
    sig = compute_signals(ev, BRAND, cat)
    refs = {i.ref for i in cat}
    out = heuristic_analysis(sig, ScoringConfig(), ev, None, refs)
    a = apply_brand_guardrails(
        out,
        analyzer="heuristic-v1",
        prompt_version=None,
        valid_refs=refs,
        evidence=ev,
        signals=sig,
        scoring=ScoringConfig(),
    )
    assert a.case in expected_cases, (a.case, sig.as_observations())
    if page == "legit_partner.html":
        assert sig.disclosure_above_fold and sig.ouvidoria and sig.official_links
        assert a.classification == FinalClassification.NO_INDICATION


async def test_prompt_injection_page_flagged(settings, site) -> None:
    ev, _ = await _collect(settings, f"{site}/injection.html")
    cat = build_catalog(ev, BRAND)
    sig = compute_signals(ev, BRAND, cat)
    assert sig.injection_markers


def test_evidence_dir_layout(settings) -> None:
    assert isinstance(settings.output_dir, Path)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc inspection is Linux-only")
async def test_chromium_runs_with_os_sandbox(settings) -> None:
    """Security review: Playwright disables the sandbox by default; SIMIO must keep it on."""

    def descendants(pid: int) -> list[int]:
        kids: dict[int, list[int]] = {}
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    ppid = int(Path(f"/proc/{d}/stat").read_text().rsplit(")", 1)[1].split()[1])
                except (OSError, IndexError, ValueError):
                    continue
                kids.setdefault(ppid, []).append(int(d))
        out, todo = [], [pid]
        while todo:
            for k in kids.get(todo.pop(), []):
                out.append(k)
                todo.append(k)
        return out

    async with BrowserCollector(settings, UrlPolicy()):
        cmdlines = []
        for p in descendants(os.getpid()):
            with contextlib.suppress(OSError):
                cmdlines.append(Path(f"/proc/{p}/cmdline").read_bytes().replace(b"\0", b" ").decode())
    chrome = [c for c in cmdlines if "chrom" in c.lower()]
    assert chrome, "no Chromium process found"
    assert not [c for c in chrome if "--no-sandbox" in c]
