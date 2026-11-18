"""Deterministic guardrails and final decision composition.

Model outputs are *proposals*. These rules enforce consistency between scenario, classification,
evidence references and collection status, and keep classification, priority, extraction
confidence, attribution confidence and evidence sufficiency as separate axes.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from simio.agents.evidence import PageSignals
from simio.core.models import (
    CLASSIFICATION_RANK,
    PRIORITY_RANK,
    BrandAnalysisOutput,
    BrandAssessment,
    CollectionStatus,
    ComplianceAssessment,
    Evidence,
    EvidenceSufficiency,
    FinalClassification,
    FinalDecision,
    FindingStatus,
    IdentityAttribution,
    IdentityClass,
    Indicator,
    Level,
    Priority,
    Review,
    ReviewDecision,
    ScenarioCase,
)

C = FinalClassification


class ScoringConfig(BaseModel):
    """Weights for the offline heuristic analyst and STRONG-indication requirements.

    Scores only *order and route* cases for human review; they never confirm an infraction.
    """

    w_brand_in_domain: float = 2.0
    w_brand_in_title: float = 1.5
    w_brand_in_header: float = 1.0
    w_brand_logo_above_fold: float = 1.0
    w_brand_linked_official_claim: float = 3.0
    w_generic_official_claim: float = 0.5
    w_no_disclosure: float = 1.5
    w_whatsapp_with_prominent_brand: float = 0.5
    w_disclosure_prominent: float = 2.5
    w_disclosure_present: float = 1.0
    w_ouvidoria: float = 0.5
    w_owner_identified: float = 0.5
    threshold_b: float = 1.5
    threshold_c: float = 3.5
    threshold_d: float = 5.5
    strong_min_indicators: int = Field(default=2, ge=1)

    @classmethod
    def load(cls, path: Path | None) -> ScoringConfig:
        if path is None or not path.is_file():
            return cls()
        with path.open("rb") as fh:
            return cls(**tomllib.load(fh))


CASE_DEFAULTS: dict[ScenarioCase, tuple[C, Priority]] = {
    ScenarioCase.NOT_RELEVANT: (C.NO_INDICATION, Priority.LOW),
    ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION: (C.NO_INDICATION, Priority.LOW),
    ScenarioCase.B_AMBIGUOUS_COMMUNICATION: (C.REVIEW_REQUIRED, Priority.MEDIUM),
    ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL: (C.REVIEW_REQUIRED, Priority.HIGH),
    ScenarioCase.D_STRONG_IMPERSONATION: (C.STRONG_INDICATION, Priority.HIGH),
    ScenarioCase.E_INSUFFICIENT_EVIDENCE: (C.INCONCLUSIVE, Priority.MEDIUM),
}


def _filter_refs(refs: list[str], valid: set[str], dropped: list[str]) -> list[str]:
    out = []
    for r in refs:
        if r in valid:
            out.append(r)
        elif r not in dropped:
            dropped.append(r)
    return out


def apply_brand_guardrails(
    out: BrandAnalysisOutput,
    *,
    analyzer: str,
    prompt_version: str | None,
    valid_refs: set[str],
    evidence: Evidence,
    signals: PageSignals,
    scoring: ScoringConfig,
) -> BrandAssessment:
    notes: list[str] = []
    dropped: list[str] = []

    def clean_indicators(items: list[Indicator]) -> list[Indicator]:
        kept = []
        for ind in items:
            ind.evidence_refs = _filter_refs(ind.evidence_refs, valid_refs, dropped)
            if ind.evidence_refs:
                kept.append(ind)
            else:
                notes.append(f"indicador sem referência válida descartado: {ind.description[:120]}")
        return kept

    confusion = clean_indicators(list(out.brand_confusion_indicators))
    legit = clean_indicators(list(out.legitimate_relationship_indicators))
    for d in out.dimensions:
        d.evidence_refs = _filter_refs(d.evidence_refs, valid_refs, dropped)
    refs = _filter_refs(out.evidence_references, valid_refs, dropped)
    if dropped:
        notes.append(f"referências inexistentes no catálogo removidas: {', '.join(dropped[:10])}")

    case, cls, prio = out.case, out.classification, out.priority
    sufficiency = out.evidence_sufficiency
    injection = out.possible_prompt_injection or bool(signals.injection_markers)

    if not evidence.usable:
        if case != ScenarioCase.E_INSUFFICIENT_EVIDENCE or cls != C.INCONCLUSIVE:
            notes.append("coleta sem conteúdo utilizável: forçado cenário E / INCONCLUSIVE")
        case, cls, sufficiency = ScenarioCase.E_INSUFFICIENT_EVIDENCE, C.INCONCLUSIVE, EvidenceSufficiency.INSUFFICIENT
        prio = Priority.MEDIUM if signals.brand_in_domain else Priority.LOW
    if case in (ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION, ScenarioCase.NOT_RELEVANT) and cls in (
        C.STRONG_INDICATION,
        C.REVIEW_REQUIRED,
    ):
        notes.append(f"inconsistência cenário {case.value} × {cls.value}: encaminhado para revisão")
        cls = C.REVIEW_REQUIRED
    if case == ScenarioCase.E_INSUFFICIENT_EVIDENCE and cls != C.INCONCLUSIVE:
        notes.append("cenário E exige INCONCLUSIVE")
        cls = C.INCONCLUSIVE
    if (
        case in (ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL, ScenarioCase.D_STRONG_IMPERSONATION)
        and cls == C.NO_INDICATION
    ):
        notes.append(f"cenário {case.value} incompatível com NO_INDICATION: elevado a REVIEW_REQUIRED")
        cls = C.REVIEW_REQUIRED
    if cls == C.STRONG_INDICATION:
        strong_ok = (
            len(confusion) >= scoring.strong_min_indicators
            and sufficiency != EvidenceSufficiency.INSUFFICIENT
            and evidence.usable
        )
        if not strong_ok:
            notes.append(
                f"STRONG_INDICATION exige ≥{scoring.strong_min_indicators} indicadores com evidência "
                "válida e evidência não insuficiente: rebaixado para REVIEW_REQUIRED"
            )
            cls = C.REVIEW_REQUIRED
    if cls in (C.STRONG_INDICATION,) and prio != Priority.HIGH:
        prio = Priority.HIGH
    if injection:
        notes.append("possível prompt injection no conteúdo coletado: revisão humana obrigatória")
        if cls == C.NO_INDICATION:
            cls = C.REVIEW_REQUIRED
            prio = max(prio, Priority.MEDIUM, key=lambda p: PRIORITY_RANK[p])

    partial_clear = evidence.status == CollectionStatus.PARTIAL and cls == C.NO_INDICATION
    if evidence.status == CollectionStatus.PARTIAL and sufficiency == EvidenceSufficiency.SUFFICIENT:
        sufficiency = EvidenceSufficiency.PARTIAL
    if partial_clear:
        notes.append("coleta parcial: ausência de indícios não foi verificada em todo o conteúdo; revisão humana")
    requires_human = (
        cls != C.NO_INDICATION or injection or partial_clear or bool(out.uncertainties and cls != C.NO_INDICATION)
    )
    return BrandAssessment(
        case=case,
        classification=cls,
        priority=prio,
        dimensions=out.dimensions,
        brand_confusion_indicators=confusion,
        legitimate_relationship_indicators=legit,
        observed_facts=out.observed_facts,
        uncertainties=out.uncertainties,
        evidence_references=refs,
        reasoning_summary=out.reasoning_summary,
        requires_human_review=requires_human or (out.requires_human_review and cls != C.NO_INDICATION),
        extraction_confidence=out.extraction_confidence if evidence.usable else Level.NONE,
        evidence_sufficiency=sufficiency,
        possible_prompt_injection=injection,
        analyzer=analyzer,
        prompt_version=prompt_version,
        dropped_references=dropped,
        guardrail_notes=notes,
    )


def needs_review(assessment: BrandAssessment, review_all: bool = False) -> bool:
    if review_all:
        return True
    return (
        assessment.classification != C.NO_INDICATION
        or assessment.extraction_confidence in (Level.LOW, Level.NONE)
        or assessment.possible_prompt_injection
        or (assessment.classification == C.NO_INDICATION and assessment.requires_human_review)
    )


def apply_review(assessment: BrandAssessment, review: Review | None) -> tuple[C, Priority, list[str]]:
    """Combine analyst + reviewer. The reviewer may confirm or lower; it may not escalate to
    STRONG on its own (escalations become REVIEW_REQUIRED for a human)."""
    notes: list[str] = []
    cls, prio = assessment.classification, assessment.priority
    if review is None:
        return cls, prio, notes
    rc, rp = review.revised_classification, review.revised_priority
    if review.decision == ReviewDecision.INCONCLUSIVE:
        return C.INCONCLUSIVE, max(prio, Priority.MEDIUM, key=lambda p: PRIORITY_RANK[p]), notes
    if CLASSIFICATION_RANK[rc] > CLASSIFICATION_RANK[cls]:
        if rc == C.STRONG_INDICATION and cls != C.STRONG_INDICATION:
            notes.append(
                "revisor sugeriu escalar para STRONG_INDICATION; limitado a REVIEW_REQUIRED para decisão humana"
            )
            rc = C.REVIEW_REQUIRED
        if review.decision not in (ReviewDecision.HUMAN_REVIEW, ReviewDecision.CONFIRM):
            notes.append(
                f"revisor elevou a classificação com decisão {review.decision.value}; mantido para revisão humana"
            )
            rc = max(rc, C.REVIEW_REQUIRED, key=lambda c: CLASSIFICATION_RANK[c])
    if review.decision == ReviewDecision.CONFIRM:
        return cls, prio, notes
    return rc, rp, notes


ROUTE_COBAN = "Gestão/auditoria de correspondentes (Res. 4.935)"
ROUTE_LEGAL = "Jurídico / proteção de marca"
ROUTE_FRAUD = "Segurança / prevenção a fraudes"
ROUTE_RECOLLECT = "Verificação manual / nova coleta"
ROUTE_NONE = "Sem encaminhamento (amostrar para controle de qualidade)"


def suggest_routing(cls: C, identity: IdentityClass) -> list[str]:
    """Suggested follow-up owner(s). Identity drives *who* acts; classification drives *whether*."""
    if cls == C.NO_INDICATION:
        return [ROUTE_NONE]
    if cls == C.INCONCLUSIVE:
        return [ROUTE_RECOLLECT]
    out: list[str] = []
    if identity in (IdentityClass.CONFIRMED_COBAN, IdentityClass.POSSIBLE_COBAN):
        out.append(ROUTE_COBAN)
    if identity != IdentityClass.CONFIRMED_COBAN:
        out.append(ROUTE_LEGAL)
    if cls == C.STRONG_INDICATION and identity in (IdentityClass.UNKNOWN_IDENTITY, IdentityClass.OTHER_THIRD_PARTY):
        out.append(ROUTE_FRAUD)
    return out


def compose_final(
    candidate_id: str,
    assessment: BrandAssessment,
    review: Review | None,
    identity: IdentityAttribution,
    compliance: ComplianceAssessment,
    evidence: Evidence,
) -> FinalDecision:
    cls, prio, notes = apply_review(assessment, review)
    notes = [*assessment.guardrail_notes, *notes]
    limitations = [*evidence.not_inspected, *identity.limitations, *compliance.limitations]
    if assessment.analyzer.startswith("heuristic"):
        limitations.append(
            "análise heurística offline (sem modelo multimodal): indícios visuais não avaliados; resultado é triagem"
        )
    if review is None and cls != C.NO_INDICATION:
        limitations.append("revisão crítica automatizada não executada para este candidato")
    justification = assessment.reasoning_summary
    if review is not None:
        justification += f"\n[Revisor: {review.decision.value}] {review.reasoning_summary}"
    key_refs: list[str] = []
    for ind in assessment.brand_confusion_indicators + assessment.legitimate_relationship_indicators:
        for r in ind.evidence_refs:
            if r not in key_refs:
                key_refs.append(r)
    requires_human = (
        cls != C.NO_INDICATION
        or assessment.requires_human_review
        or (review is not None and review.requires_human_review and cls != C.NO_INDICATION)
    )
    return FinalDecision(
        candidate_id=candidate_id,
        routing=suggest_routing(cls, identity.classification),
        compliance_summary=[
            f"{f.requirement_id}: {f.status.value}"
            for f in compliance.findings
            if f.status not in (FindingStatus.NO_INDICATION_OBSERVED,)
        ],
        classification=cls,
        priority=prio,
        case=assessment.case,
        identity_class=identity.classification,
        extraction_confidence=assessment.extraction_confidence,
        attribution_confidence=identity.attribution_confidence,
        evidence_sufficiency=assessment.evidence_sufficiency,
        requires_human_review=requires_human,
        human_review_status="PENDING" if requires_human else "NOT_REQUIRED",
        justification=justification.strip(),
        key_evidence=key_refs[:20],
        limitations=list(dict.fromkeys(limitations)),
        guardrail_notes=notes,
    )
