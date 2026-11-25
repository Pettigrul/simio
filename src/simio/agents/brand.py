"""Brand Analyst Agent: multimodal (Gemini) assessment of brand-identity confusion, with a
deterministic heuristic analyst used in offline mode or when the model is unavailable."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from simio.agents.evidence import PageSignals
from simio.brands import BrandProfile
from simio.core.decision import CASE_DEFAULTS, ScoringConfig, apply_brand_guardrails
from simio.core.models import (
    PRIORITY_RANK,
    BrandAnalysisOutput,
    BrandAssessment,
    CollectionStatus,
    Dimension,
    DimensionAssessment,
    Evidence,
    EvidenceItem,
    EvidenceSufficiency,
    FinalClassification,
    IdentityAttribution,
    Indicator,
    Level,
    Priority,
    ScenarioCase,
    Signal,
)
from simio.prompts import render
from simio.providers.llm import ImagePart, LLMGateway, Part

log = logging.getLogger(__name__)

HEURISTIC_ID = "heuristic-v1"
MAX_IMAGE_BYTES = 6_000_000


_DELIM_RE = re.compile(r"(?i)<(\s*/?\s*)(conteudo_nao_confiavel)")


def _wrap_untrusted(body: str) -> str:
    # Neutralise attempts (any case/spacing) to open or close the delimiter from inside the data.
    body = _DELIM_RE.sub(lambda m: "‹" + m.group(1) + m.group(2), body)
    return f"<conteudo_nao_confiavel>\n{body}\n</conteudo_nao_confiavel>"


def untrusted_block(catalog: list[EvidenceItem]) -> str:
    payload = [item.model_dump(exclude_none=True) for item in catalog]
    return _wrap_untrusted(json.dumps(payload, ensure_ascii=False, indent=1))


def context_block(brand: BrandProfile, ev: Evidence, signals: PageSignals, identity: IdentityAttribution | None) -> str:
    # URLs and collection errors are attacker-influenced: they go inside the untrusted delimiter.
    collection = {
        "url_original": ev.original_url,
        "url_final": ev.final_url,
        "ocorrencias_de_coleta": ev.errors,
    }
    lines = [
        f"Marca investigada: {brand.display_name}",
        f"Domínios oficiais conhecidos da instituição: {', '.join(brand.official_domains) or '(não informados)'}",
        f"Status da coleta: {ev.status.value} (HTTP {ev.http_status})",
        "Dados da coleta (não confiáveis):",
        _wrap_untrusted(json.dumps(collection, ensure_ascii=False)),
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


_SIGNAL_CASES = (
    ScenarioCase.B_AMBIGUOUS_COMMUNICATION,
    ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL,
    ScenarioCase.D_STRONG_IMPERSONATION,
)


def cross_check(assessment: BrandAssessment, heuristic: BrandAnalysisOutput) -> BrandAssessment:
    """Model vs deterministic rules. Page content (or text inside screenshots) may persuade the model
    to clear a page: when the rules see B/C/D signals and the model says NO_INDICATION, the case is
    forced to human review instead of being silently cleared, preserving the deterministic indicators."""
    if assessment.classification != FinalClassification.NO_INDICATION or heuristic.case not in _SIGNAL_CASES:
        return assessment
    note = (
        f"divergência modelo × regras: o modelo indicou {assessment.case.value}/NO_INDICATION, mas as regras "
        f"determinísticas indicam {heuristic.case.value}; encaminhado para revisão (possível erro ou "
        "influência do conteúdo da página)"
    )
    merged_case = (
        heuristic.case
        if assessment.case in (ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION, ScenarioCase.NOT_RELEVANT)
        else assessment.case
    )
    merged_confusion = assessment.brand_confusion_indicators or heuristic.brand_confusion_indicators
    merged_refs = sorted(set(assessment.evidence_references) | set(heuristic.evidence_references))
    return assessment.model_copy(
        update={
            "case": merged_case,
            "classification": FinalClassification.REVIEW_REQUIRED,
            "priority": max(assessment.priority, Priority.MEDIUM, key=lambda p: PRIORITY_RANK[p]),
            "brand_confusion_indicators": merged_confusion,
            "evidence_references": merged_refs,
            "requires_human_review": True,
            "uncertainties": [*assessment.uncertainties, note],
            "guardrail_notes": [*assessment.guardrail_notes, note],
        }
    )


LEGACY_LOGO_NOTE = (
    "uso da identidade visual antiga da marca (logotipo descontinuado): terceiros, inclusive correspondentes, "
    "não podem utilizá-la; mantido no mínimo REVIEW_REQUIRED com prioridade HIGH"
)


LEGACY_LOGO_EXEMPT_NOTE = (
    "logotipo antigo registrado em site de referência (repositório de logos, banco de imagens, "
    "enciclopédia ou imprensa); classificação não elevada"
)


def apply_legacy_logo_rule(
    assessment: BrandAssessment, catalog: list[EvidenceItem], *, exempt: bool = False
) -> BrandAssessment:
    """Legacy logo seen by the model and/or by the deterministic image comparison (``E-VIS-LEGACY-*``):
    record the source, make sure a LOGO_USAGE indicator cites it and floor the result at
    REVIEW_REQUIRED / HIGH. Clearing such a page is a human decision.

    ``exempt`` (site that only catalogues or reports the logo): record the finding, keep the result."""
    vis_refs = [i.ref for i in catalog if i.kind == "visual"]
    by_model = assessment.legacy_visual_identity
    if not by_model and not vis_refs:
        return assessment
    source = " + ".join(s for s, on in (("modelo", by_model), ("comparação de imagem", bool(vis_refs))) if on)
    notes = list(assessment.guardrail_notes)
    confusion = list(assessment.brand_confusion_indicators)
    if vis_refs and not any(set(vis_refs) & set(ind.evidence_refs) for ind in confusion):
        confusion.append(
            Indicator(
                dimension=Dimension.LOGO_USAGE,
                strength=Level.HIGH,
                description="Logotipo antigo da marca (identidade visual descontinuada) confirmado por comparação "
                "de imagem com a referência",
                evidence_refs=vis_refs[:3],
            )
        )
        if not by_model:
            notes.append(
                "a comparação de imagem encontrou o logotipo antigo que a análise do modelo não registrou"
            )
    cls, prio = assessment.classification, assessment.priority
    if exempt:
        notes.append(LEGACY_LOGO_EXEMPT_NOTE)
        source += "; site de referência"
    else:
        if cls == FinalClassification.NO_INDICATION:
            cls = FinalClassification.REVIEW_REQUIRED
        prio = max(prio, Priority.HIGH, key=lambda p: PRIORITY_RANK[p])
        if (cls, prio) != (assessment.classification, assessment.priority):
            notes.append(LEGACY_LOGO_NOTE)
    return assessment.model_copy(
        update={
            "legacy_visual_identity": True,
            "legacy_visual_identity_source": source,
            "legacy_logo_exempt": exempt,
            "classification": cls,
            "priority": prio,
            "brand_confusion_indicators": confusion,
            "requires_human_review": assessment.requires_human_review or not exempt,
            "evidence_references": sorted(set(assessment.evidence_references) | set(vis_refs)),
            "guardrail_notes": notes,
        }
    )


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
        heuristic = heuristic_analysis(signals, self.scoring, ev, identity, valid_refs)
        if self.uses_llm and (ev.usable or signals.has_discovery_evidence):
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
            out = heuristic
            analyzer, pv = HEURISTIC_ID, None
        assessment = apply_brand_guardrails(
            out,
            analyzer=analyzer,
            prompt_version=pv,
            valid_refs=valid_refs,
            evidence=ev,
            signals=signals,
            scoring=self.scoring,
        )
        if analyzer != HEURISTIC_ID:
            assessment = cross_check(assessment, heuristic)
        host = urlsplit(ev.final_url or ev.original_url).hostname or ""
        return apply_legacy_logo_rule(assessment, catalog, exempt=self.brand.is_legacy_logo_exempt(host))


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
        return found[:2] or (["E-DISC-001"] if "E-DISC-001" in refs_ok else ["E-URL"])

    if not ev.usable and not sig.has_discovery_evidence:
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
    prominent = (
        sig.brand_in_domain
        or sig.brand_in_title
        or sig.brand_in_header
        or sig.brand_images_above_fold
        or (not ev.usable and sig.has_discovery_evidence and sig.brand_mentions > 0)
    )

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
                dimension=Dimension.DECLARED_COMMERCIAL_IDENTITY,
                strength=Level.LOW,
                description="Marca no cabeçalho/navegação fora de declaração de correspondente",
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
    if sig.legacy_logo_visual:
        score_c += sc.w_brand_logo_above_fold
        confusion.append(
            Indicator(
                dimension=Dimension.LOGO_USAGE,
                strength=Level.HIGH,
                description="Logotipo antigo da marca (identidade visual descontinuada) confirmado por comparação "
                "de imagem",
                evidence_refs=[h.ref for h in sig.legacy_logo_visual][:3],
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
    # Absence of a disclosure only matters when the brand is *used* (prominence or official claims);
    # a plain mention in body text is not, by itself, a transparency problem.
    if not sig.disclosures and (prominent or sig.brand_linked_claims):
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
    has_active_capture = bool(sig.whatsapp_contacts or sig.forms_present > 0 or sig.phones)
    if (
        sig.sensitive_service_claims
        and has_active_capture
        and not (sig.disclosure_above_fold and sig.disclosure_names_institution)
        and not sig.editorial_context
    ):
        score_c += sc.w_sensitive_service_bait
        confusion.append(
            Indicator(
                dimension=Dimension.SERVICE_JOURNEY,
                strength=Level.HIGH,
                description="Oferta de serviço pós-venda sensível (2ª via de boleto/quitação/acordo) com captação "
                "de contato sem identificação destacada de correspondente/assessoria: "
                + ", ".join(sorted({h.value for h in sig.sensitive_service_claims})),
                evidence_refs=sorted({h.ref for h in sig.sensitive_service_claims})[:5],
            )
        )
    if (
        identity is not None
        and identity.referenced_institution_cnpjs
        and not identity.owner_cnpjs
        and not sig.disclosures
        and not sig.editorial_context
    ):
        score_c += sc.w_institution_cnpj_impersonation
        inst_refs = [
            f.evidence_ref
            for f in identity.facts
            if f.kind == "cnpj" and f.value in identity.referenced_institution_cnpjs and f.evidence_ref
        ]
        confusion.append(
            Indicator(
                dimension=Dimension.DECLARED_COMMERCIAL_IDENTITY,
                strength=Level.HIGH,
                description="Página de terceiro exibe CNPJ oficial da instituição sem identificar CNPJ próprio "
                "do responsável nem declarar condição de correspondente",
                evidence_refs=inst_refs[:3] or pick("E-FTR", "E-URL"),
            )
        )
    if sig.disclosures:
        refs = sorted({h.ref for h in sig.disclosures})[:5]
        names = "" if sig.disclosure_names_institution else " (sem nomear a instituição contratante)"
        if sig.disclosure_above_fold and sig.disclosure_names_institution:
            score_l += sc.w_disclosure_prominent
            legit.append(
                Indicator(
                    dimension=Dimension.CORRESPONDENT_TRANSPARENCY,
                    strength=Level.HIGH,
                    description="Declaração de correspondente nomeando a instituição, no cabeçalho/área visível "
                    "inicial (posição observada; tamanho, contraste e legibilidade a verificar no screenshot)",
                    evidence_refs=refs,
                )
            )
        else:
            score_l += sc.w_disclosure_present
            where = (
                "na área visível inicial" if sig.disclosure_above_fold else "apenas fora da área inicial (ex.: rodapé)"
            )
            legit.append(
                Indicator(
                    dimension=Dimension.CORRESPONDENT_TRANSPARENCY,
                    strength=Level.LOW,
                    description=f"Declaração de correspondente/independência {where}{names}",
                    evidence_refs=refs,
                )
            )
    owner_identified = bool(identity is not None and identity.owner_cnpjs) or bool(sig.disclosures)
    multi_bank_transparent = (
        sig.multi_bank_context
        and not sig.brand_in_domain
        and not sig.brand_in_title
        and not sig.brand_in_header
        and not sig.brand_linked_claims
        and not sig.sensitive_service_claims
        and owner_identified
    )
    if multi_bank_transparent:
        score_l += sc.w_multi_bank_context
        mb_refs = sorted({h.ref for h in sig.partnership})[:3] or pick("E-FOLD", "E-TITLE", "E-URL")
        legit.append(
            Indicator(
                dimension=Dimension.CORRESPONDENT_TRANSPARENCY,
                strength=Level.MEDIUM,
                description="Apresentação da marca em contexto multimarcas/multibancos junto a outras instituições "
                f"financeiras ({', '.join(sig.competitor_banks[:5])}), caracterizando oferta intermediada por terceiro",
                evidence_refs=mb_refs,
            )
        )
    if sig.ouvidoria:
        score_l += sc.w_ouvidoria
        legit.append(
            Indicator(
                dimension=Dimension.CONTACT_CHANNELS,
                strength=Level.LOW,
                description="Menção a ouvidoria (não verificado se é a da instituição contratante)",
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
    relevant = (
        sig.brand_mentions > 0
        or bool(sig.brand_in_domain)
        or sig.brand_in_title
        or sig.brand_in_header
        or bool(sig.brand_images)
        or bool(sig.brand_linked_claims)
    )
    # The page presents *itself* with the brand (vs. a news article or review that talks about it).
    self_presented = bool(sig.brand_in_domain) or sig.brand_in_header or (
        sig.brand_in_title and not sig.article_page and not sig.editorial_context
    )
    # Only a disclosure that names the institution (Art. 14, III) and is not contradicted in the same
    # sentence, or a clear multi-bank intermediary listing without official claims/domain, counts as
    # sufficient context to avoid treating brand mentions as impersonation.
    named_disclosure = sig.disclosure_above_fold and sig.disclosure_names_institution
    transparent = named_disclosure or multi_bank_transparent
    editorial_only = (
        sig.editorial_context
        and not sig.brand_in_domain
        and not sig.brand_in_header
        and not sig.brand_linked_claims
        and not (sig.sensitive_service_claims and has_active_capture)
        and not sig.whatsapp_contacts
        and not sig.brand_images_above_fold
    )
    caps: list[str] = []
    if sig.official_channel:
        case = ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION
        confusion.clear()
        legit.append(
            Indicator(
                dimension=Dimension.DECLARED_COMMERCIAL_IDENTITY,
                strength=Level.HIGH,
                description="URL final pertence a domínio ou perfil institucional oficial da própria marca",
                evidence_refs=["E-URL"],
            )
        )
    elif not ev.usable and (not relevant or editorial_only):
        case = ScenarioCase.E_INSUFFICIENT_EVIDENCE
    elif not relevant:
        case = ScenarioCase.NOT_RELEVANT
    elif editorial_only:
        case = ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION
    elif (
        net >= sc.threshold_d
        and (sig.brand_linked_claims or (sig.sensitive_service_claims and has_active_capture))
        and self_presented
        and not named_disclosure
    ):
        case = ScenarioCase.D_STRONG_IMPERSONATION
    elif (
        sig.brand_linked_claims
        or (sig.sensitive_service_claims and has_active_capture and not named_disclosure and not sig.editorial_context)
        or (net >= sc.threshold_c and sig.brand_in_domain and (sig.brand_in_title or sig.brand_in_header))
    ):
        # C needs a signal of *direct service by the institution* (official language, sensitive
        # post-sale capture, or the brand used as the site's own domain + identity); brand prominence
        # alone is case B.
        case = ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL
    elif net >= sc.threshold_b or (prominent and not transparent):
        case = ScenarioCase.B_AMBIGUOUS_COMMUNICATION
    else:
        case = ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION
    if (
        (sig.article_page or sig.editorial_context)
        and not sig.brand_in_domain
        and case
        in (
            ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL,
            ScenarioCase.D_STRONG_IMPERSONATION,
        )
    ):
        caps.append(
            "página declarada/identificada como artigo/notícia/editorial: cenário limitado a B para revisão humana"
        )
        case = ScenarioCase.B_AMBIGUOUS_COMMUNICATION
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
    if sig.reported_claims:
        uncertainties.append(
            "expressões de oficialidade em contexto de terceiros (notícia, alerta, citação) não foram tratadas "
            "como autoapresentação da página"
        )
    if sig.disclosures and not sig.disclosure_names_institution:
        uncertainties.append("declaração de correspondente/independência sem nomear a instituição contratante")
    uncertainties.extend(caps)
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
        reasoning_summary=_explain(case, confusion, legit, score_c, score_l),
        requires_human_review=case != ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION
        and case != ScenarioCase.NOT_RELEVANT,
        extraction_confidence=Level.MEDIUM if ev.status.value == "OK" else Level.LOW,
        evidence_sufficiency=(
            EvidenceSufficiency.PARTIAL
            if ev.not_inspected or ev.status != CollectionStatus.OK
            else EvidenceSufficiency.SUFFICIENT
        ),
        possible_prompt_injection=bool(sig.injection_markers),
    )


_CASE_TEXT = {
    ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION: "menção comercial com condição de correspondente/terceiro clara",
    ScenarioCase.B_AMBIGUOUS_COMMUNICATION: "uso destacado da marca sem transparência suficiente",
    ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL: "elementos que sugerem atendimento direto pela instituição",
    ScenarioCase.D_STRONG_IMPERSONATION: "apresentação consistente como a própria instituição",
    ScenarioCase.E_INSUFFICIENT_EVIDENCE: "evidência insuficiente",
    ScenarioCase.NOT_RELEVANT: "a marca não aparece de forma relevante",
}


def _explain(case: ScenarioCase, confusion: list[Indicator], legit: list[Indicator], sc: float, sl: float) -> str:
    """Plain-language rationale: which elements could lead a consumer to believe they deal directly
    with the institution, and which point the other way."""
    parts = [f"Triagem heurística — cenário {case.value}: {_CASE_TEXT[case]}."]
    if confusion:
        parts.append(
            "Elementos que podem levar o consumidor a crer que trata diretamente com a instituição: "
            + "; ".join(f"{i.description} [{', '.join(i.evidence_refs[:3])}]" for i in confusion[:5])
            + "."
        )
    if legit:
        parts.append(
            "Elementos de transparência/identificação do terceiro: "
            + "; ".join(f"{i.description} [{', '.join(i.evidence_refs[:3])}]" for i in legit[:4])
            + "."
        )
    parts.append(f"(Pontuação de triagem: confusão {sc:.1f}, transparência {sl:.1f}; ordena casos, não prova.)")
    return " ".join(parts)
