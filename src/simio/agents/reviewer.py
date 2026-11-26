"""Reviewer Agent: independent, sceptical second look at suspicious conclusions.

It receives raw evidence plus the analyst's output explicitly framed as a *hypothesis to be
challenged*. Deterministic checks always run (also offline); the LLM reviewer adds a critical
reading when available. Recollection requests are bounded by the orchestrator.
"""

from __future__ import annotations

from pathlib import Path

from simio.agents.brand import context_block, load_images, untrusted_block
from simio.agents.evidence import PageSignals
from simio.brands import BrandProfile
from simio.core.models import (
    BrandAssessment,
    Challenge,
    CollectionStatus,
    Evidence,
    EvidenceItem,
    FinalClassification,
    IdentityAttribution,
    IdentityClass,
    Level,
    Priority,
    Review,
    ReviewDecision,
    ReviewOutput,
)
from simio.prompts import render
from simio.providers.llm import LLMGateway, Part

C = FinalClassification
HEURISTIC_ID = "heuristic-reviewer-v1"


def deterministic_challenges(
    a: BrandAssessment, ev: Evidence, sig: PageSignals, identity: IdentityAttribution
) -> list[Challenge]:
    ch: list[Challenge] = []
    if (
        a.classification in (C.STRONG_INDICATION, C.REVIEW_REQUIRED)
        and identity.classification == IdentityClass.UNKNOWN_IDENTITY
    ):
        ch.append(
            Challenge(
                topic="domain_attribution",
                severity=Level.MEDIUM,
                concern="Responsável pela página não identificado; a conclusão sobre a marca "
                "não pode ser atribuída a um estabelecimento específico.",
                evidence_refs=["E-URL"],
            )
        )
    if ev.status == CollectionStatus.PARTIAL or any("challenge" in e or "CAPTCHA" in e for e in ev.errors):
        ch.append(
            Challenge(
                topic="evidence_sufficiency",
                severity=Level.HIGH,
                concern="Coleta parcial ou desafio anti-bot: conteúdo pode estar incompleto.",
                evidence_refs=["E-COLLECT"] if ev.errors else ["E-URL"],
            )
        )
    if sig.disclosures and a.classification == C.STRONG_INDICATION:
        ch.append(
            Challenge(
                topic="legitimate_disclosure",
                severity=Level.HIGH,
                concern="Há declaração de correspondente/independência na página; avaliar se "
                "o destaque é suficiente antes de manter indício forte.",
                evidence_refs=sorted({h.ref for h in sig.disclosures})[:5],
            )
        )
    if (
        sig.multi_bank_context
        and not sig.brand_in_domain
        and not sig.brand_linked_claims
        and a.classification == C.STRONG_INDICATION
    ):
        ch.append(
            Challenge(
                topic="multi_bank_intermediation",
                severity=Level.HIGH,
                concern="A página menciona múltiplas instituições financeiras concorrentes "
                f"({', '.join(sig.competitor_banks[:4])}) sem domínio próprio da marca nem alegação "
                "de canal oficial; provável intermediação multimarcas.",
                evidence_refs=["E-URL"],
            )
        )
    if sig.editorial_context and not sig.brand_in_domain and a.classification == C.STRONG_INDICATION:
        ch.append(
            Challenge(
                topic="editorial_or_third_party_platform",
                severity=Level.HIGH,
                concern="A URL/página aparenta ser conteúdo editorial, portal de reclamações, processo "
                "judicial ou documentação de terceiros, não um canal de captação passando-se pelo banco.",
                evidence_refs=["E-URL"],
            )
        )
    if a.brand_confusion_indicators and all(i.strength == Level.LOW for i in a.brand_confusion_indicators):
        ch.append(
            Challenge(
                topic="over_interpretation",
                severity=Level.MEDIUM,
                concern="Todos os indícios de confusão têm força baixa.",
                evidence_refs=[r for i in a.brand_confusion_indicators for r in i.evidence_refs][:5],
            )
        )
    if a.analyzer.startswith("heuristic") and a.classification != C.NO_INDICATION:
        ch.append(
            Challenge(
                topic="unanalyzed_visual_context",
                severity=Level.MEDIUM,
                concern="Identidade visual não foi avaliada por modelo multimodal; verificar screenshots.",
                evidence_refs=["E-SHOT-VIEWPORT"] if ev.artifact("screenshot_viewport") else ["E-URL"],
            )
        )
    if sig.disclosures and a.brand_confusion_indicators:
        ch.append(
            Challenge(
                topic="contradictory_evidence",
                severity=Level.LOW,
                concern="Coexistem declaração de correspondente e indícios de confusão.",
                evidence_refs=sorted({h.ref for h in sig.disclosures})[:3],
            )
        )
    return ch


def heuristic_review(
    a: BrandAssessment, ev: Evidence, sig: PageSignals, identity: IdentityAttribution, can_recollect: bool
) -> ReviewOutput:
    ch = deterministic_challenges(a, ev, sig, identity)
    topics = {c.topic for c in ch}
    cls, prio = a.classification, a.priority
    if "evidence_sufficiency" in topics and can_recollect and ev.retryable:
        return ReviewOutput(
            decision=ReviewDecision.RECOLLECT,
            revised_classification=cls,
            revised_priority=prio,
            challenges=ch,
            recollect_reason="coleta parcial com falha transitória",
            reasoning_summary="Nova coleta solicitada antes de concluir.",
        )
    if (
        topics & {"legitimate_disclosure", "multi_bank_intermediation", "editorial_or_third_party_platform"}
        and cls == C.STRONG_INDICATION
    ):
        return ReviewOutput(
            decision=ReviewDecision.DOWNGRADE,
            revised_classification=C.REVIEW_REQUIRED,
            revised_priority=Priority.HIGH,
            challenges=ch,
            contradictory_evidence=[c.concern for c in ch if c.topic == "contradictory_evidence"],
            reasoning_summary="Indício forte rebaixado: existem sinais de transparência, intermediação "
            "multimarcas ou contexto editorial a serem ponderados por auditor.",
        )
    if "over_interpretation" in topics and prio == Priority.HIGH:
        return ReviewOutput(
            decision=ReviewDecision.DOWNGRADE,
            revised_classification=cls,
            revised_priority=Priority.MEDIUM,
            challenges=ch,
            reasoning_summary="Prioridade reduzida: indícios de baixa força.",
        )
    if ch:
        return ReviewOutput(
            decision=ReviewDecision.HUMAN_REVIEW,
            revised_classification=cls,
            revised_priority=prio,
            challenges=ch,
            reasoning_summary="Questionamentos registrados; encaminhado para revisão humana.",
        )
    return ReviewOutput(
        decision=ReviewDecision.CONFIRM,
        revised_classification=cls,
        revised_priority=prio,
        reasoning_summary="Nenhum questionamento determinístico; indícios mantidos.",
        requires_human_review=cls != C.NO_INDICATION,
    )


class Reviewer:
    def __init__(
        self,
        brand: BrandProfile,
        campaign_dir: Path,
        gateway: LLMGateway | None = None,
        model: str | None = None,
        mode: str | None = None,
    ) -> None:
        self.brand = brand
        self.campaign_dir = campaign_dir
        self.gateway = gateway
        self.model = model
        self.mode = mode  # campaign-mode prompt addendum (e.g. "threat_mode")

    async def review(
        self,
        a: BrandAssessment,
        ev: Evidence,
        catalog: list[EvidenceItem],
        sig: PageSignals,
        identity: IdentityAttribution,
        can_recollect: bool,
    ) -> Review:
        if self.gateway is not None and self.model and ev.usable:
            version, system = render("reviewer", brand_name=self.brand.display_name)
            if self.mode:
                mode_version, addendum = render(self.mode, brand_name=self.brand.display_name)
                version, system = f"{version}+{mode_version}", f"{system}\n\n{addendum}"
            hypothesis = a.model_dump(
                include={
                    "case",
                    "classification",
                    "priority",
                    "brand_confusion_indicators",
                    "legitimate_relationship_indicators",
                    "uncertainties",
                    "reasoning_summary",
                    "evidence_sufficiency",
                },
                mode="json",
            )
            import json

            parts: list[Part] = [
                "## Contexto da coleta\n" + context_block(self.brand, ev, sig, identity),
                "## Catálogo de evidências (dados não confiáveis)\n" + untrusted_block(catalog),
                *load_images(ev, self.campaign_dir),
                "## Hipótese do analista (a ser contestada, não é verdade estabelecida)\n"
                + json.dumps(hypothesis, ensure_ascii=False, indent=1),
                f"Nova coleta permitida: {'sim' if can_recollect else 'não'}.",
            ]
            out = await self.gateway.structured(
                agent="reviewer", model=self.model, system=system, parts=parts, schema=ReviewOutput
            )
            if out.decision == ReviewDecision.RECOLLECT and not can_recollect:
                out.decision = ReviewDecision.INCONCLUSIVE
                out.reasoning_summary += " (nova coleta não permitida: limite atingido)"
            valid = {i.ref for i in catalog}
            for c in out.challenges:
                c.evidence_refs = [r for r in c.evidence_refs if r in valid]
            known = {c.topic for c in out.challenges}
            out.challenges.extend(c for c in deterministic_challenges(a, ev, sig, identity) if c.topic not in known)
            return Review(**out.model_dump(), reviewer=self.model, prompt_version=version)
        out = heuristic_review(a, ev, sig, identity, can_recollect)
        return Review(**out.model_dump(), reviewer=HEURISTIC_ID, prompt_version=None)
