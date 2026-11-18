"""Brand Analyst Agent: multimodal (Gemini) assessment of brand-identity confusion, with a
deterministic heuristic analyst used in offline mode or when the model is unavailable."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from simio.agents.evidence import PageSignals
from simio.brands import BrandProfile
from simio.core.decision import CASE_DEFAULTS, ScoringConfig, apply_brand_guardrails
from simio.core.models import (
    BrandAnalysisOutput,
    BrandAssessment,
    Dimension,
    DimensionAssessment,
    Evidence,
    EvidenceItem,
    EvidenceSufficiency,
    IdentityAttribution,
    Indicator,
    Level,
    ScenarioCase,
    Signal,
)
from simio.prompts import render
from simio.providers.llm import ImagePart, LLMGateway, Part

log = logging.getLogger(__name__)

HEURISTIC_ID = "heuristic-v1"
MAX_IMAGE_BYTES = 6_000_000


def untrusted_block(catalog: list[EvidenceItem]) -> str:
    payload = [item.model_dump(exclude_none=True) for item in catalog]
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    # Neutralise attempts to close the delimiter from inside the page content.
    body = body.replace("</conteudo_nao_confiavel>", "</conteudo_nao_confiavel_>")
    return f"<conteudo_nao_confiavel>\n{body}\n</conteudo_nao_confiavel>"


def context_block(brand: BrandProfile, ev: Evidence, signals: PageSignals, identity: IdentityAttribution | None) -> str:
    lines = [
        f"Marca investigada: {brand.display_name}",
        f"Domínios oficiais conhecidos da instituição: {', '.join(brand.official_domains) or '(não informados)'}",
        f"URL original: {ev.original_url}",
        f"URL final: {ev.final_url}",
        f"Status da coleta: {ev.status.value} (HTTP {ev.http_status})",
        f"Ocorrências de coleta: {'; '.join(ev.errors) or 'nenhuma'}",
        f"Conteúdo não inspecionado: {'; '.join(ev.not_inspected) or '-'}",
        "Observações determinísticas (fatos extraídos por regras, não conclusões):",
        *[f"  - {o}" for o in signals.as_observations()],
    ]
    if identity is not None:
        lines.append(
            f"Atribuição de identidade (agente de identidade): {identity.classification.value}"
            f" / confiança {identity.attribution_confidence.value} — {identity.rationale}"
        )
    return "\n".join(lines)


def load_images(ev: Evidence, campaign_dir: Path) -> list[ImagePart]:
    parts: list[ImagePart] = []
    for kind, label in (
        ("screenshot_viewport", "E-SHOT-VIEWPORT (área visível inicial)"),
        ("screenshot_full", "E-SHOT-FULL (página completa)"),
    ):
        art = ev.artifact(kind)
        if art is None:
            continue
        p = campaign_dir / art.path
        if p.is_file() and p.stat().st_size <= MAX_IMAGE_BYTES:
            parts.append(ImagePart(data=p.read_bytes(), mime_type="image/png", label=label))
    return parts


class BrandAnalyst:
    def __init__(
        self,
        brand: BrandProfile,
        scoring: ScoringConfig,
        campaign_dir: Path,
        gateway: LLMGateway | None = None,
        model: str | None = None,
    ) -> None:
        self.brand = brand
        self.scoring = scoring
        self.campaign_dir = campaign_dir
        self.gateway = gateway
        self.model = model

    @property
    def uses_llm(self) -> bool:
        return self.gateway is not None and bool(self.model)

    async def analyze(
        self, ev: Evidence, catalog: list[EvidenceItem], signals: PageSignals, identity: IdentityAttribution | None
    ) -> BrandAssessment:
        valid_refs = {i.ref for i in catalog}
        if self.uses_llm and ev.usable:
            assert self.gateway is not None and self.model is not None
            version, system = render("brand_analyst", brand_name=self.brand.display_name)
            parts: list[Part] = [
                "## Contexto da coleta\n" + context_block(self.brand, ev, signals, identity),
                "## Catálogo de evidências (dados não confiáveis)\n" + untrusted_block(catalog),
                *load_images(ev, self.campaign_dir),
                "Produza a avaliação estruturada em JSON.",
            ]
            out = await self.gateway.structured(
                agent="brand_analyst",
                model=self.model,
                system=system,
                parts=parts,
                schema=BrandAnalysisOutput,
            )
            analyzer, pv = self.model, version
        else:
            out = heuristic_analysis(signals, self.scoring, ev, identity, valid_refs)
            analyzer, pv = HEURISTIC_ID, None
        return apply_brand_guardrails(
            out,
            analyzer=analyzer,
            prompt_version=pv,
            valid_refs=valid_refs,
            evidence=ev,
            signals=signals,
            scoring=self.scoring,
        )


# --------------------------------------------------------------------------------------
# Offline heuristic analyst
# --------------------------------------------------------------------------------------
def heuristic_analysis(
    sig: PageSignals,
    sc: ScoringConfig,
    ev: Evidence,
    identity: IdentityAttribution | None,
    valid_refs: set[str] | None = None,
) -> BrandAnalysisOutput:
    refs_ok = valid_refs or set()

    def pick(*cands: str) -> list[str]:
        found = [c for c in cands if c in refs_ok]
        return found[:2] or ["E-URL"]

    if not ev.usable:
        return BrandAnalysisOutput(
            case=ScenarioCase.E_INSUFFICIENT_EVIDENCE,
            classification=CASE_DEFAULTS[ScenarioCase.E_INSUFFICIENT_EVIDENCE][0],
            priority=CASE_DEFAULTS[ScenarioCase.E_INSUFFICIENT_EVIDENCE][1],
            observed_facts=[f"coleta: {ev.status.value}", *ev.errors[:3]],
            uncertainties=["conteúdo da página não disponível para análise"],
            evidence_references=["E-URL"] + (["E-COLLECT"] if ev.errors else []),
            reasoning_summary="Página não coletada de forma utilizável; não há base para conclusão.",
            requires_human_review=True,
            extraction_confidence=Level.NONE,
            evidence_sufficiency=EvidenceSufficiency.INSUFFICIENT,
        )

    confusion: list[Indicator] = []
    legit: list[Indicator] = []
    dims: list[DimensionAssessment] = []
    score_c = 0.0
    score_l = 0.0
    prominent = sig.brand_in_domain or sig.brand_in_title or sig.brand_in_header or sig.brand_images_above_fold

    if sig.brand_in_domain:
        score_c += sc.w_brand_in_domain
        confusion.append(
            Indicator(
                dimension=Dimension.DECLARED_COMMERCIAL_IDENTITY,
                strength=Level.MEDIUM,
                description=f"Domínio contém padrão da marca ({sig.brand_in_domain})",
                evidence_refs=["E-URL"],
            )
        )
    if sig.brand_in_title:
        score_c += sc.w_brand_in_title
        confusion.append(
            Indicator(
                dimension=Dimension.DECLARED_COMMERCIAL_IDENTITY,
                strength=Level.MEDIUM,
                description="Marca no título/nome do site",
                evidence_refs=["E-TITLE"],
            )
        )
    if sig.brand_in_header:
        score_c += sc.w_brand_in_header
        confusion.append(
            Indicator(
                dimension=Dimension.VISUAL_IDENTITY,
                strength=Level.LOW,
                description="Marca no cabeçalho/navegação",
                evidence_refs=pick("E-HDR", "E-NAV"),
            )
        )
    if sig.brand_images_above_fold:
        score_c += sc.w_brand_logo_above_fold
        refs = [h.ref for h in sig.brand_images][:3]
        confusion.append(
            Indicator(
                dimension=Dimension.LOGO_USAGE,
                strength=Level.MEDIUM,
                description="Imagem possivelmente de logotipo da marca acima da dobra",
                evidence_refs=refs,
            )
        )
    if sig.brand_linked_claims:
        score_c += sc.w_brand_linked_official_claim
        confusion.append(
            Indicator(
                dimension=Dimension.INSTITUTIONAL_LANGUAGE,
                strength=Level.HIGH,
                description="Expressões de oficialidade associadas à marca: "
                + ", ".join(sorted({h.value for h in sig.brand_linked_claims})),
                evidence_refs=sorted({h.ref for h in sig.brand_linked_claims})[:5],
            )
        )
    elif sig.official_claims:
        score_c += sc.w_generic_official_claim
        confusion.append(
            Indicator(
                dimension=Dimension.INSTITUTIONAL_LANGUAGE,
                strength=Level.LOW,
                description="Expressões de oficialidade (não associadas diretamente à marca)",
                evidence_refs=sorted({h.ref for h in sig.official_claims})[:5],
            )
        )
    if not sig.disclosures and sig.brand_mentions:
        score_c += sc.w_no_disclosure
        confusion.append(
            Indicator(
                dimension=Dimension.CORRESPONDENT_TRANSPARENCY,
                strength=Level.MEDIUM,
                description="Nenhuma declaração de correspondente/independência observada no conteúdo coletado",
                evidence_refs=pick("E-FTR", "E-FOLD", "E-TITLE"),
            )
        )
    if sig.whatsapp_contacts and prominent:
        score_c += sc.w_whatsapp_with_prominent_brand
        confusion.append(
            Indicator(
                dimension=Dimension.CONTACT_CHANNELS,
                strength=Level.LOW,
                description="Atendimento por WhatsApp sob identidade destacada da marca",
                evidence_refs=[h.ref for h in sig.whatsapp_contacts][:3],
            )
        )
    if sig.disclosures:
        refs = sorted({h.ref for h in sig.disclosures})[:5]
        if sig.disclosure_above_fold:
            score_l += sc.w_disclosure_prominent
            legit.append(
                Indicator(
                    dimension=Dimension.CORRESPONDENT_TRANSPARENCY,
                    strength=Level.HIGH,
                    description="Declaração de correspondente/independência em posição de destaque (topo/área visível)",
                    evidence_refs=refs,
                )
            )
        else:
            score_l += sc.w_disclosure_present
            legit.append(
                Indicator(
                    dimension=Dimension.CORRESPONDENT_TRANSPARENCY,
                    strength=Level.LOW,
                    description="Declaração de correspondente apenas fora da área de destaque (ex.: rodapé)",
                    evidence_refs=refs,
                )
            )
    if sig.ouvidoria:
        score_l += sc.w_ouvidoria
        legit.append(
            Indicator(
                dimension=Dimension.CONTACT_CHANNELS,
                strength=Level.LOW,
                description="Menção a ouvidoria",
                evidence_refs=[sig.ouvidoria[0].ref],
            )
        )
    if identity is not None and identity.owner_cnpjs:
        score_l += sc.w_owner_identified
        cnpj_refs = [f.evidence_ref for f in identity.facts if f.kind == "cnpj" and f.evidence_ref]
        legit.append(
            Indicator(
                dimension=Dimension.DECLARED_COMMERCIAL_IDENTITY,
                strength=Level.LOW,
                description="Empresa responsável identificada por CNPJ",
                evidence_refs=cnpj_refs[:2] or pick("E-FTR"),
            )
        )
    # A bare "parceiro" mention does not resolve the analysis: noted as uncertainty, not scored.

    net = score_c - score_l
    relevant = sig.brand_mentions > 0 or bool(sig.brand_in_domain) or sig.brand_in_title or bool(sig.brand_images)
    if not relevant:
        case = ScenarioCase.NOT_RELEVANT
    elif net >= sc.threshold_d and sig.brand_linked_claims and not sig.disclosure_above_fold:
        case = ScenarioCase.D_STRONG_IMPERSONATION
    elif sig.brand_linked_claims or (
        net >= sc.threshold_c and sig.brand_in_domain and (sig.brand_in_title or sig.brand_in_header)
    ):
        # C needs a signal of *direct service by the institution* (official language, or the brand
        # used as the site's own domain + identity); brand prominence alone is case B.
        case = ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL
    elif net >= sc.threshold_b or (prominent and not sig.disclosure_above_fold):
        case = ScenarioCase.B_AMBIGUOUS_COMMUNICATION
    else:
        case = ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION
    cls, prio = CASE_DEFAULTS[case]

    def dim(d: Dimension, inds_c: list[Indicator], inds_l: list[Indicator]) -> DimensionAssessment:
        c = [i for i in inds_c if i.dimension == d]
        g = [i for i in inds_l if i.dimension == d]
        if c and not g:
            s, f, refs = Signal.CONFUSION, "; ".join(i.description for i in c), [r for i in c for r in i.evidence_refs]
        elif g and not c:
            s, f, refs = Signal.LEGITIMATE, "; ".join(i.description for i in g), [r for i in g for r in i.evidence_refs]
        elif c and g:
            s, f, refs = Signal.NEUTRAL, "sinais contraditórios", [r for i in c + g for r in i.evidence_refs]
        else:
            s, f, refs = Signal.NOT_ASSESSED, "não avaliado pela heurística", []
        return DimensionAssessment(dimension=d, signal=s, finding=f, evidence_refs=refs[:5])

    dims = [dim(d, confusion, legit) for d in Dimension]
    uncertainties = [
        "análise heurística sem avaliação visual por modelo; identidade visual e logotipo "
        "inferidos apenas por textos alternativos e nomes de arquivos"
    ]
    if sig.partnership and not sig.disclosures:
        uncertainties.append("menção a 'parceiro' sem declaração de correspondente: não resolve a análise")
    if sig.disclosures and confusion:
        uncertainties.append("evidências contraditórias: há declaração de correspondente e indícios de confusão")
    return BrandAnalysisOutput(
        case=case,
        classification=cls,
        priority=prio,
        dimensions=dims,
        brand_confusion_indicators=confusion,
        legitimate_relationship_indicators=legit,
        observed_facts=sig.as_observations(),
        uncertainties=uncertainties,
        evidence_references=sorted({r for i in confusion + legit for r in i.evidence_refs}),
        reasoning_summary=(
            f"Triagem heurística: pontuação de confusão {score_c:.1f}, de transparência "
            f"{score_l:.1f} (líquida {net:.1f}); cenário {case.value}."
        ),
        requires_human_review=case != ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION
        and case != ScenarioCase.NOT_RELEVANT,
        extraction_confidence=Level.MEDIUM if ev.status.value == "OK" else Level.LOW,
        evidence_sufficiency=EvidenceSufficiency.PARTIAL if ev.not_inspected else EvidenceSufficiency.SUFFICIENT,
        possible_prompt_injection=bool(sig.injection_markers),
    )
