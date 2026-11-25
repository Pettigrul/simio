"""Orchestrator: explicit, checkpointed pipeline over a SQLite-backed state machine.

Flow per candidate::

    DISCOVERED -> QUEUED -> COLLECTING -> COLLECTED -> ANALYZING -> [REVIEWING] -> COMPLETED
                                                                         |-> INCONCLUSIVE
                                       (bounded recollect: REVIEWING -> QUEUED)
    any stage -> FAILED after ``max_attempts`` internal errors

Agents never talk to each other directly; the orchestrator passes typed outputs along.
Parallelism is limited to independent candidates (``workers``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import secrets
import socket
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from simio.agents.brand import BrandAnalyst
from simio.agents.compliance import ComplianceAnalyst, RequirementCatalog
from simio.agents.discovery import DiscoveryAgent, coban_hypotheses, deduplicate, prioritise, summarize_queries
from simio.agents.evidence import EvidenceAgent, build_catalog, compute_signals
from simio.agents.identity import CobanRegistry, IdentityAgent
from simio.agents.reviewer import Reviewer
from simio.agents.visual import Reference, detect_legacy_logo, load_reference
from simio.brands import BrandProfile, load_brand
from simio.collectors.browser import BrowserCollector
from simio.config import DISCOVERY_SOURCES, Settings
from simio.core.decision import ScoringConfig, compose_final, needs_review
from simio.core.models import (
    BrandAssessment,
    ComplianceAssessment,
    DiscoveryHit,
    Evidence,
    FinalClassification,
    IdentityAttribution,
    QueryRecord,
    Review,
    ReviewDecision,
    VisualMatch,
)
from simio.core.models import (
    CandidateStatus as S,
)
from simio.core.state import InvalidTransition
from simio.core.storage import ConcurrentModification, Storage
from simio.providers.grounding import GroundingConnector
from simio.providers.llm import (
    Budget,
    BudgetExceeded,
    LLMGateway,
    LLMProvider,
    LLMUnavailable,
)
from simio.providers.search import BraveSearch, Connector, CrtShSearch, LensSearch, SerperSearch, UrlscanSearch
from simio.providers.typosquat import TyposquatConnector
from simio.providers.vertex import VertexProvider
from simio.security.url_policy import UrlPolicy

log = logging.getLogger(__name__)


class Collector(Protocol):
    async def collect(self, candidate_id: str, url: str, attempt: int, campaign_dir: Path) -> Evidence: ...


@dataclass
class HuntOptions:
    brand: str = "bv"
    limit: int = 50
    sources: list[str] = field(default_factory=lambda: list(DISCOVERY_SOURCES))
    imported: list[DiscoveryHit] = field(default_factory=list)
    review_all: bool = False


@dataclass
class RunSummary:
    campaign_id: str
    status: str
    counts: dict[str, int]
    diagnostics: list[str]
    llm_requests: int
    llm_tokens: int


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        *,
        provider: LLMProvider | None = None,
        collector_factory: Any = None,
        connectors: list[Connector] | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self._provider_override = provider
        self._collector_factory = collector_factory
        self._connectors_override = connectors
        self.policy = UrlPolicy.from_settings(settings.allowed_ports, settings.unsafe_allow_hosts)
        self._stop = asyncio.Event()
        self._stop_reason: str | None = None
        self._degraded_reason: str | None = None

    # ------------------------------------------------------------------ wiring
    @staticmethod
    def _legacy_logo_references(brand: BrandProfile) -> list[Reference]:
        refs: list[Reference] = []
        for path in brand.legacy_logo_paths():
            try:
                refs.append(load_reference(path))
            except (OSError, ValueError) as exc:
                log.warning("legacy logo reference ignored (%s): %s", path, exc)
        return refs

    def campaign_dir(self, campaign_id: str) -> Path:
        return self.settings.output_dir / "campaigns" / campaign_id

    def _gateway(self, campaign_id: str) -> LLMGateway | None:
        if self.settings.offline:
            return None
        provider = self._provider_override
        if provider is None:
            if not self.settings.vertex_configured():
                return None
            provider = VertexProvider(self.settings)
        budget = Budget(self.storage, campaign_id, self.settings.max_llm_requests, self.settings.max_llm_tokens)
        return LLMGateway(
            provider,
            budget,
            max_retries=self.settings.llm_max_retries,
            base_delay_s=self.settings.retry_base_delay_s,
            concurrency=self.settings.workers,
            max_output_tokens=self.settings.llm_max_output_tokens,
        )

    def _connectors(self, gateway: LLMGateway | None, brand: BrandProfile, sources: list[str]) -> list[Connector]:
        if self._connectors_override is not None:
            return self._connectors_override
        s = self.settings
        key = lambda v: v.get_secret_value() if v else None  # noqa: E731
        registry: dict[str, Connector] = {
            "grounding": GroundingConnector(
                gateway, s.effective_discovery_model, self.policy, brand.official_domains, s.http_timeout_s
            ),
            "brave": BraveSearch(key(s.brave_api_key), s.http_timeout_s),
            "serper": SerperSearch(key(s.serper_api_key), s.http_timeout_s, pages=s.serper_pages),
            "urlscan": UrlscanSearch(key(s.urlscan_api_key), s.http_timeout_s),
            "crtsh": CrtShSearch(s.http_timeout_s),
            "typosquat": TyposquatConnector(brand),
            "lens": LensSearch(key(s.serper_api_key), brand.legacy_logo_search_urls, s.http_timeout_s),
        }
        return [registry[name] for name in sources if name in registry]

    def _collector(self) -> AbstractAsyncContextManager[Any]:
        if self._collector_factory is not None:
            return self._collector_factory()
        return BrowserCollector(self.settings, self.policy)

    # ------------------------------------------------------------------ public API
    async def hunt(self, opts: HuntOptions) -> RunSummary:
        brand = load_brand(opts.brand, self.settings.brand_file)
        params = {
            "brand": opts.brand,
            "limit": opts.limit,
            "sources": opts.sources,
            "offline": self.settings.offline,
            "review_all": opts.review_all,
            "analysis_model": self.settings.analysis_model,
            "review_model": self.settings.effective_review_model,
            "discovery_model": self.settings.effective_discovery_model,
            "workers": self.settings.workers,
            "max_llm_requests": self.settings.max_llm_requests,
            "max_llm_tokens": self.settings.max_llm_tokens,
            "max_search_queries": self.settings.max_search_queries,
            "imported_urls": len(opts.imported),
        }
        campaign_id = self.storage.create_campaign(opts.brand, params)
        log.info("campaign %s started", campaign_id)
        self.storage.log_decision(campaign_id, None, "orchestrator", "campaign_started", params)
        if self.settings.unsafe_allow_hosts:
            self.storage.log_decision(
                campaign_id,
                None,
                "orchestrator",
                "UNSAFE_ALLOW_HOSTS_ACTIVE",
                {"hosts": self.settings.unsafe_allow_hosts, "warning": "SSRF protection bypassed for these hosts"},
            )
        # Imported URLs are persisted before any network work so a crash cannot lose them.
        self.storage.set_checkpoint(campaign_id, "imported_hits", [h.model_dump(mode="json") for h in opts.imported])
        async with self._campaign_run(campaign_id):
            gateway = self._gateway(campaign_id)
            await self._discover(campaign_id, brand, gateway, opts)
            return await self._process(campaign_id, brand, gateway, opts.review_all)

    RESUMABLE_PARAMS = (
        "offline",
        "analysis_model",
        "review_model",
        "discovery_model",
        "workers",
        "max_llm_requests",
        "max_llm_tokens",
        "max_search_queries",
    )

    async def resume(
        self, campaign_id: str, retry_failed: bool = False, overrides: dict[str, Any] | None = None
    ) -> RunSummary:
        """Resume with the campaign's *original* parameters (model, offline mode, budgets...);
        only values the operator passes explicitly in ``overrides`` change them."""
        camp = self.storage.get_campaign(campaign_id)
        if camp is None:
            raise KeyError(f"campaign not found: {campaign_id}")
        params = camp["params"]
        merged = {k: params[k] for k in self.RESUMABLE_PARAMS if params.get(k) is not None}
        merged.update({k: v for k, v in (overrides or {}).items() if v is not None})
        self.settings = self.settings.model_copy(update=merged)
        brand = load_brand(params["brand"], self.settings.brand_file)
        async with self._campaign_run(campaign_id):
            recovered = self.storage.recover_in_flight(campaign_id, self.settings.max_attempts)
            self.storage.log_decision(
                campaign_id,
                None,
                "orchestrator",
                "resumed",
                {
                    "recovered_in_flight": recovered,
                    "retry_failed": retry_failed,
                    "effective_params": {k: merged.get(k) for k in self.RESUMABLE_PARAMS},
                    "overrides": {k: v for k, v in (overrides or {}).items() if v is not None},
                },
            )
            if retry_failed:
                for cand in self.storage.list_candidates(campaign_id, [S.FAILED]):
                    self.storage.reset_attempts(cand.id)
                    self.storage.transition(cand.id, S.QUEUED, note="retry requested by operator")
            gateway = self._gateway(campaign_id)
            if not self.storage.get_checkpoint(campaign_id, "discovery_done", False):
                imported = [
                    DiscoveryHit.model_validate(h)
                    for h in (self.storage.get_checkpoint(campaign_id, "imported_hits", []) or [])
                ]
                opts = HuntOptions(
                    brand=params["brand"],
                    limit=params["limit"],
                    sources=params["sources"],
                    review_all=params.get("review_all", False),
                    imported=imported,
                )
                await self._discover(campaign_id, brand, gateway, opts)
            return await self._process(campaign_id, brand, gateway, params.get("review_all", False))

    @asynccontextmanager
    async def _campaign_run(self, campaign_id: str) -> AsyncIterator[None]:
        """Single-writer lease (two processes on one campaign would double-process candidates),
        heartbeat while running, and an explicit INTERRUPTED status on Ctrl+C / crash."""
        owner = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
        holder = self.storage.acquire_lease(campaign_id, owner)
        if holder is not None:
            raise CampaignBusy(f"campaign {campaign_id} is being processed by {holder}")
        self.storage.set_campaign_status(campaign_id, "RUNNING")

        async def beat() -> None:
            while True:
                await asyncio.sleep(30)
                self.storage.heartbeat_lease(campaign_id, owner)

        hb = asyncio.create_task(beat())
        try:
            yield
        except BaseException as exc:
            status = "INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "FAILED"
            with contextlib.suppress(Exception):
                self.storage.set_campaign_status(campaign_id, status)
                self.storage.log_error(campaign_id, None, "orchestrator", type(exc).__name__, str(exc)[:500])
            raise
        finally:
            hb.cancel()
            with contextlib.suppress(Exception):
                self.storage.release_lease(campaign_id, owner)

    # ------------------------------------------------------------------ discovery
    async def _discover(
        self, campaign_id: str, brand: BrandProfile, gateway: LLMGateway | None, opts: HuntOptions
    ) -> None:
        sources = [s for s in opts.sources if s in DISCOVERY_SOURCES]
        connectors = (
            []
            if self.settings.offline and self._connectors_override is None
            else self._connectors(gateway, brand, sources)
        )
        # Resume-safety: queries already executed (and paid for) are skipped and count against the cap;
        # their hits were checkpointed as they arrived.
        done = {
            (q["source"], q["query_text"]) for q in self.storage.list_queries(campaign_id) if q["source"] in sources
        }
        coban_hyps, coban_note = self._coban_hypotheses(campaign_id, brand, connectors)
        agent = DiscoveryAgent(
            brand,
            connectors,
            self.policy,
            max_queries=self.settings.max_search_queries,
            already_done=done,
            extra_hypotheses=coban_hyps,
        )
        partial = [
            DiscoveryHit.model_validate(h)
            for h in (self.storage.get_checkpoint(campaign_id, "discovery_partial_hits", []) or [])
        ]

        if opts.imported and not self.storage.get_checkpoint(campaign_id, "imported_recorded", False):
            self.storage.set_checkpoint(campaign_id, "imported_recorded", True)
            by_source: dict[str, list[DiscoveryHit]] = {}
            for h in opts.imported:
                by_source.setdefault(h.source, []).append(h)
            for src, hits in by_source.items():
                qid = self.storage.record_query(
                    campaign_id, QueryRecord(source=src, query=src, status="OK", result_count=len(hits))
                )
                for h in hits:
                    h.metadata["query_db_id"] = qid

        def on_query(record: QueryRecord, hits: list[DiscoveryHit]) -> None:
            qid = self.storage.record_query(campaign_id, record)
            for h in hits:
                h.metadata["query_db_id"] = qid
            partial.extend(hits)
            self.storage.set_checkpoint(
                campaign_id, "discovery_partial_hits", [h.model_dump(mode="json") for h in partial]
            )

        outcome = await agent.run([*opts.imported, *partial], on_query=on_query)
        if coban_note:
            outcome.diagnostics.append(coban_note)
        if self.settings.offline:
            outcome.diagnostics.insert(
                0, "offline mode: automatic discovery and AI analysis disabled; only imported URLs are processed"
            )
        groups, rejected = deduplicate(outcome.hits, self.policy)
        for hit, reason in rejected:
            self.storage.log_decision(
                campaign_id,
                None,
                "discovery",
                "rejected_url",
                {"url": hit.original_url[:500], "source": hit.source, "reason": reason},
            )
        ordered = prioritise(groups, brand)
        # Candidates already queued/processed (earlier interrupted run) count against --limit.
        queued = sum(n for st, n in self.storage.status_counts(campaign_id).items() if st != S.DISCOVERED.value)
        for g in ordered:
            if g.excluded_reason:
                self.storage.log_decision(
                    campaign_id,
                    None,
                    "discovery",
                    "excluded_url",
                    {"url": g.url, "reason": g.excluded_reason, "sources": sorted({h.source for h in g.hits})},
                )
                continue
            cid, _ = self.storage.upsert_candidate(
                campaign_id,
                url=g.url,
                normalized_url=g.normalized_url,
                hostname=g.hostname,
                registrable_domain=g.registrable_domain,
            )
            for h in g.hits:
                self.storage.add_discovery_result(cid, h.metadata.get("query_db_id"), h)
            self.storage.set_priority(cid, g.score, g.reasons)
            cand = self.storage.get_candidate(cid)
            is_manual = any(h.source.startswith("import") for h in g.hits)
            if cand.status == S.DISCOVERED and queued < opts.limit and (g.score >= 0 or is_manual):
                self.storage.transition(cid, S.QUEUED, note=f"prioritised (score {g.score:.1f})")
                queued += 1
        info = {
            "hits": len(outcome.hits),
            "unique_urls": len(groups),
            "rejected": len(rejected),
            "queued": queued,
            "sources_used": outcome.sources_used,
            "queries": summarize_queries(outcome.queries),
            "diagnostics": outcome.diagnostics,
        }
        self.storage.set_checkpoint(campaign_id, "discovery", info)
        self.storage.set_checkpoint(campaign_id, "discovery_done", True)
        self.storage.log_decision(campaign_id, None, "discovery", "completed", info)
        log.info("discovery: %d hits, %d unique, %d queued", len(outcome.hits), len(groups), queued)

    def _coban_hypotheses(
        self, campaign_id: str, brand: BrandProfile, connectors: list[Connector]
    ) -> tuple[list[Any], str | None]:
        """H9: searches seeded by the registered correspondents (complements the open-web sweep).
        Rotates across campaigns: correspondents searched in earlier campaigns are skipped."""
        s = self.settings
        if s.coban_seed_limit <= 0 or not connectors:
            return [], None
        if not s.coban_registry_path or not s.coban_registry_path.is_file():
            return [], "coban-seeded search (H9): SIMIO_COBAN_REGISTRY_PATH not set; skipped"
        registry = CobanRegistry.load(s.coban_registry_path, authoritative=s.coban_registry_authoritative)
        searched = self.storage.searched_coban_cnpjs(exclude_campaign=campaign_id)
        hyps, n = coban_hypotheses(
            registry.records, brand, limit=s.coban_seed_limit, batch=s.coban_seed_batch, exclude=searched
        )
        remaining = len(registry.records) - len(searched) - n
        return hyps, (
            f"coban-seeded search (H9): {n} registered correspondents in {len(hyps)} prompt(s); "
            f"{len(searched)} searched in earlier campaigns, ~{max(0, remaining)} not yet searched"
        )

    # ------------------------------------------------------------------ processing
    async def _process(
        self, campaign_id: str, brand: BrandProfile, gateway: LLMGateway | None, review_all: bool
    ) -> RunSummary:
        s = self.settings
        cdir = self.campaign_dir(campaign_id)
        cdir.mkdir(parents=True, exist_ok=True)
        registry = CobanRegistry.load(
            s.coban_registry_path,
            authoritative=s.coban_registry_authoritative,
            reference_date=s.coban_registry_reference_date,
        )
        identity_agent = IdentityAgent(brand, registry)
        scoring = ScoringConfig.load(s.output_dir / "scoring.toml")
        catalog = RequirementCatalog.load(s.contract_requirements_path)
        analyst = BrandAnalyst(brand, scoring, cdir, gateway, s.analysis_model)
        reviewer = Reviewer(brand, cdir, gateway, s.effective_review_model)
        compliance = ComplianceAnalyst(catalog)
        if gateway is None and not s.offline:
            self.storage.log_decision(
                campaign_id,
                None,
                "orchestrator",
                "llm_unavailable",
                {"reason": "Vertex AI not configured; heuristic analysis used"},
            )

        pending = self.storage.list_candidates(campaign_id, [S.QUEUED, S.COLLECTED])
        if pending:
            sem = asyncio.Semaphore(s.workers)
            async with self._collector() as collector:
                evidence_agent = EvidenceAgent(collector, cdir)
                ctx = _Ctx(
                    campaign_id,
                    brand,
                    evidence_agent,
                    identity_agent,
                    analyst,
                    reviewer,
                    compliance,
                    review_all,
                    self._legacy_logo_references(brand),
                )

                async def worker(cid: str) -> None:
                    async with sem:
                        if self._stop.is_set():
                            return
                        try:
                            await self._run_candidate(ctx, cid)
                        except ConcurrentModification as exc:
                            log.warning("%s", exc)
                        except Exception as exc:
                            # One unexpected error must not abort sibling workers (or close the
                            # shared browser under them): isolate it to this candidate.
                            log.exception("unexpected error processing %s", cid)
                            self.storage.log_error(campaign_id, cid, "orchestrator", type(exc).__name__, str(exc))
                            self._fail_safely(cid, f"unexpected error: {type(exc).__name__}: {exc}")

                results = await asyncio.gather(*(worker(c.id) for c in pending), return_exceptions=True)
                for r in results:
                    if isinstance(r, BaseException) and not isinstance(r, Exception):
                        raise r

        counts = self.storage.status_counts(campaign_id)
        remaining = counts.get(S.QUEUED.value, 0) + counts.get(S.COLLECTED.value, 0)
        if self._stop_reason:
            status = "PAUSED_BUDGET"
        elif remaining:
            status = "PAUSED"
        else:
            status = "COMPLETED"
        self.storage.set_campaign_status(campaign_id, status, finished=status == "COMPLETED")
        disc = self.storage.get_checkpoint(campaign_id, "discovery", {}) or {}
        diagnostics = list(disc.get("diagnostics", []))
        if self._stop_reason:
            diagnostics.append(f"stopped: {self._stop_reason}. Use `simio resume` after adjusting budgets.")
        if self._degraded_reason:
            diagnostics.append(f"LLM degraded to heuristic analysis: {self._degraded_reason}")
        req, tok = self.storage.usage_totals(campaign_id)
        self.storage.log_decision(
            campaign_id,
            None,
            "orchestrator",
            "run_finished",
            {"status": status, "counts": counts, "llm_requests": req, "llm_tokens": tok},
        )
        return RunSummary(campaign_id, status, counts, diagnostics, req, tok)

    def _fail_safely(self, cid: str, note: str) -> None:
        cand = self.storage.get_candidate(cid)
        if cand.status in (S.COMPLETED, S.FAILED, S.INCONCLUSIVE):
            return
        try:
            if cand.status in (S.QUEUED, S.COLLECTING, S.ANALYZING, S.REVIEWING, S.COLLECTED):
                self.storage.transition(cid, S.FAILED, note=note[:500])
        except InvalidTransition:
            log.error("could not mark %s FAILED from %s", cid, cand.status.value)

    def _backoff(self, attempt: int) -> float:
        base = self.settings.retry_base_delay_s
        return base * (2 ** max(0, attempt - 1)) + random.uniform(0, base / 2 if base else 0)  # noqa: S311

    async def _run_candidate(self, ctx: _Ctx, cid: str) -> None:
        st = self.storage
        max_attempts = self.settings.max_attempts
        while not self._stop.is_set():
            cand = st.get_candidate(cid)
            if cand.status == S.QUEUED:
                st.transition(cid, S.COLLECTING, expected=S.QUEUED)
                attempt = st.bump(cid, "attempts")
                ev_index = len(st.all_evidence(cid)) + 1
                try:
                    ev = await ctx.evidence.collect(cid, cand.url, ev_index)
                except Exception as exc:
                    st.log_error(ctx.campaign_id, cid, "collect", type(exc).__name__, str(exc))
                    if attempt < max_attempts:
                        st.transition(cid, S.QUEUED, note=f"collector error, retry {attempt}")
                        await asyncio.sleep(self._backoff(attempt))
                        continue
                    st.transition(cid, S.FAILED, note=f"collector error: {exc}"[:500])
                    return
                ev_id = st.save_evidence(ev)
                st.log_decision(
                    ctx.campaign_id,
                    cid,
                    "evidence",
                    ev.status.value,
                    {
                        "attempt": ev_index,
                        "evidence_id": ev_id,
                        "final_url": ev.final_url,
                        "errors": ev.errors[:5],
                        "blocked_requests": len(ev.blocked_requests),
                        "manifest": ev.manifest_path,
                    },
                )
                if ev.status.value == "FAILED" and ev.retryable and attempt < max_attempts:
                    st.transition(cid, S.QUEUED, note=f"transient collection failure, retry {attempt}")
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                st.transition(cid, S.COLLECTED)
                st.reset_attempts(cid)
            elif cand.status == S.COLLECTED:
                done = await self._analyze(ctx, cid)
                if done:
                    return
            else:
                return

    async def _analyze(self, ctx: _Ctx, cid: str) -> bool:
        """Identity -> brand -> compliance -> review -> final. Returns True when the candidate is settled.

        Every assessment is stored with the id of the evidence it was computed from, so a retry or a
        resume after a crash/budget pause *reuses* paid LLM results for the same evidence instead of
        buying them again. Attempts are counted *before* the stage runs, so a candidate that crashes
        or hangs the process is eventually marked FAILED (see ``recover_in_flight``)."""
        st = self.storage
        cand = st.get_candidate(cid)
        st.transition(cid, S.ANALYZING, expected=S.COLLECTED)
        attempt = st.bump(cid, "attempts")
        stage = "prepare"
        try:
            latest = st.latest_evidence_with_id(cid)
            if latest is None:
                raise RuntimeError("no evidence stored for a COLLECTED candidate")
            ev_id, ev = latest
            disc_hits = st.discovery_hits(cid)
            visual: list[VisualMatch] = []
            if ctx.legacy_logos and ev.usable:
                cdir = self.campaign_dir(ctx.campaign_id)
                for ref in ctx.legacy_logos:
                    visual += await asyncio.to_thread(detect_legacy_logo, ev, cdir, ref)
                visual = sorted(visual, key=lambda m: -m.score)[:5]
            catalog = build_catalog(ev, ctx.brand, discovery_hits=disc_hits, visual_matches=visual)
            signals = compute_signals(ev, ctx.brand, catalog)

            stage = "identity"
            identity: IdentityAttribution = ctx.identity.attribute(cid, ev, catalog, signals)
            st.save_assessment(cid, "identity", identity, ev_id)
            st.upsert_entity(ctx.campaign_id, identity, identity.inferences[-1] if identity.inferences else "")
            st.log_decision(
                ctx.campaign_id,
                cid,
                "identity",
                identity.classification.value,
                {"entity_key": identity.entity_key, "confidence": identity.attribution_confidence.value},
            )

            stage = "brand"
            assessment = st.get_assessment(cid, "brand", BrandAssessment, evidence_id=ev_id)
            if assessment is None or (assessment.analyzer.startswith("heuristic") and ctx.analyst.uses_llm):
                assessment = await ctx.analyst.analyze(ev, catalog, signals, identity)
                assessment = assessment.model_copy(update={"visual_matches": visual})
                st.save_assessment(cid, "brand", assessment, ev_id)
                st.log_decision(
                    ctx.campaign_id,
                    cid,
                    "brand_analyst",
                    assessment.classification.value,
                    {
                        "case": assessment.case.value,
                        "priority": assessment.priority.value,
                        "analyzer": assessment.analyzer,
                        "guardrails": assessment.guardrail_notes,
                        "evidence_id": ev_id,
                    },
                )
            else:
                st.log_decision(ctx.campaign_id, cid, "brand_analyst", "reused", {"evidence_id": ev_id})

            stage = "compliance"
            comp: ComplianceAssessment = ctx.compliance.assess(assessment, identity, bool(signals.ouvidoria))
            st.save_assessment(cid, "compliance", comp, ev_id)
            st.log_decision(
                ctx.campaign_id,
                cid,
                "compliance",
                "mapped",
                {"findings": {f.requirement_id: f.status.value for f in comp.findings}},
            )

            review: Review | None = None
            if needs_review(assessment, ctx.review_all):
                stage = "review"
                st.transition(cid, S.REVIEWING)
                can_recollect = cand.recollects < self.settings.max_recollects
                review = st.get_assessment(cid, "review", Review, evidence_id=ev_id)
                if review is None:
                    review = await ctx.reviewer.review(assessment, ev, catalog, signals, identity, can_recollect)
                    st.save_assessment(cid, "review", review, ev_id)
                    st.log_decision(
                        ctx.campaign_id,
                        cid,
                        "reviewer",
                        review.decision.value,
                        {
                            "revised": review.revised_classification.value,
                            "challenges": [c.topic for c in review.challenges],
                            "reviewer": review.reviewer,
                            "evidence_id": ev_id,
                        },
                    )
                if review.decision == ReviewDecision.RECOLLECT and can_recollect:
                    st.bump(cid, "recollects")
                    st.reset_attempts(cid)
                    st.transition(cid, S.QUEUED, note=f"reviewer requested recollection: {review.recollect_reason}")
                    return False

            stage = "final"
            final = compose_final(cid, assessment, review, identity, comp, ev)
            if self._degraded_reason:
                final.limitations.append(f"LLM indisponível nesta execução: {self._degraded_reason}")
            st.save_assessment(cid, "final", final, ev_id)
            st.log_decision(
                ctx.campaign_id,
                cid,
                "orchestrator",
                f"final:{final.classification.value}",
                {"priority": final.priority.value, "human_review": final.requires_human_review},
            )
            target = S.INCONCLUSIVE if final.classification == FinalClassification.INCONCLUSIVE else S.COMPLETED
            st.transition(cid, target)
            st.reset_attempts(cid)
            return True
        except BudgetExceeded as exc:
            self._stop_reason = str(exc)
            self._stop.set()
            st.log_error(ctx.campaign_id, cid, stage, "BudgetExceeded", str(exc))
            st.reset_attempts(cid)  # not the candidate's fault
            st.transition(cid, S.COLLECTED, note="paused: budget exhausted (paid results kept for resume)")
            return True
        except LLMUnavailable as exc:
            # Degrade gracefully, only the agent whose model failed; record the limitation.
            self._degraded_reason = f"{stage}: {str(exc)[:300]}"
            st.log_error(ctx.campaign_id, cid, stage, "LLMUnavailable", str(exc))
            if stage == "review":
                ctx.reviewer.gateway = None
            else:
                ctx.analyst.gateway = None
                ctx.reviewer.gateway = None
            st.reset_attempts(cid)
            st.transition(cid, S.COLLECTED, note=f"LLM unavailable at {stage}; retrying with heuristic analysis")
            return False
        except Exception as exc:  # LLMOutputInvalid, TransientLLMError, unexpected bugs
            st.log_error(ctx.campaign_id, cid, stage, type(exc).__name__, str(exc))
            if attempt < self.settings.max_attempts:
                st.transition(cid, S.COLLECTED, note=f"{stage} error, retry {attempt}: {type(exc).__name__}")
                await asyncio.sleep(self._backoff(attempt))
                return False
            st.transition(cid, S.FAILED, note=f"{stage} failed: {type(exc).__name__}: {exc}"[:500])
            return True


class CampaignBusy(RuntimeError):
    """Another live process holds the campaign lease."""


@dataclass
class _Ctx:
    campaign_id: str
    brand: BrandProfile
    evidence: EvidenceAgent
    identity: IdentityAgent
    analyst: BrandAnalyst
    reviewer: Reviewer
    compliance: ComplianceAnalyst
    review_all: bool
    legacy_logos: list[Reference] = field(default_factory=list)
