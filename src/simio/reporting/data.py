"""Assemble per-candidate report records straight from persisted, validated data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from simio.agents.evidence import build_catalog
from simio.brands import BrandProfile
from simio.core.models import (
    CLASSIFICATION_RANK,
    PRIORITY_RANK,
    BrandAssessment,
    Candidate,
    ComplianceAssessment,
    DiscoveryHit,
    Evidence,
    EvidenceItem,
    FinalDecision,
    IdentityAttribution,
    Review,
)
from simio.core.storage import Storage

M = TypeVar("M", bound=BaseModel)


@dataclass
class CandidateRecord:
    candidate: Candidate
    hits: list[DiscoveryHit]
    evidence: Evidence | None
    all_evidence: list[Evidence]
    catalog: list[EvidenceItem]
    identity: IdentityAttribution | None
    brand: BrandAssessment | None
    compliance: ComplianceAssessment | None
    review: Review | None
    final: FinalDecision | None
    transitions: list[dict[str, Any]]

    @property
    def sort_key(self) -> tuple[int, int, float]:
        if self.final is None:
            return (1, 0, -self.candidate.priority_score)
        return (
            -CLASSIFICATION_RANK[self.final.classification],
            -PRIORITY_RANK[self.final.priority],
            -self.candidate.priority_score,
        )

    @property
    def is_priority(self) -> bool:
        f = self.final
        return f is not None and (
            f.classification.value in ("STRONG_INDICATION", "REVIEW_REQUIRED") or f.priority.value == "HIGH"
        )


def load_records(storage: Storage, campaign_id: str, brand: BrandProfile) -> list[CandidateRecord]:
    out: list[CandidateRecord] = []
    for cand in storage.list_candidates(campaign_id):
        # Report one consistent round: the evidence that produced the final decision and the
        # assessments computed from it (not, e.g., a RECOLLECT review from an earlier round).
        ev_id = storage.assessment_evidence_id(cand.id, "final")
        ev = storage.evidence_by_id(ev_id) if ev_id is not None else storage.latest_evidence(cand.id)

        def get(kind: str, cls: type[M], ev_id: int | None = ev_id, cid: str = cand.id) -> M | None:
            return storage.get_assessment(cid, kind, cls, evidence_id=ev_id)

        hits = storage.discovery_hits(cand.id)
        out.append(
            CandidateRecord(
                candidate=cand,
                hits=hits,
                evidence=ev,
                all_evidence=storage.all_evidence(cand.id),
                catalog=build_catalog(ev, brand, discovery_hits=hits) if ev else [],
                identity=get("identity", IdentityAttribution),
                brand=get("brand", BrandAssessment),
                compliance=get("compliance", ComplianceAssessment),
                review=get("review", Review),
                final=get("final", FinalDecision),
                transitions=storage.transitions(cand.id),
            )
        )
    out.sort(key=lambda r: r.sort_key)
    return out
