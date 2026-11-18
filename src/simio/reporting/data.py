"""Assemble per-candidate report records straight from persisted, validated data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
            return (-1, -1, -self.candidate.priority_score)
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
        ev = storage.latest_evidence(cand.id)
        out.append(
            CandidateRecord(
                candidate=cand,
                hits=storage.discovery_hits(cand.id),
                evidence=ev,
                all_evidence=storage.all_evidence(cand.id),
                catalog=build_catalog(ev, brand) if ev else [],
                identity=storage.get_assessment(cand.id, "identity", IdentityAttribution),
                brand=storage.get_assessment(cand.id, "brand", BrandAssessment),
                compliance=storage.get_assessment(cand.id, "compliance", ComplianceAssessment),
                review=storage.get_assessment(cand.id, "review", Review),
                final=storage.get_assessment(cand.id, "final", FinalDecision),
                transitions=storage.transitions(cand.id),
            )
        )
    out.sort(key=lambda r: r.sort_key)
    return out
