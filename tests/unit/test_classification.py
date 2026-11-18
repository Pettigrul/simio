"""Classification over synthetic scenarios A–E, guardrails, review composition, compliance mapping."""

from __future__ import annotations

from simio.agents.brand import heuristic_analysis
from simio.agents.compliance import ComplianceAnalyst, RequirementCatalog
from simio.agents.evidence import build_catalog, compute_signals
from simio.agents.identity import CobanRegistry, IdentityAgent
from simio.agents.reviewer import heuristic_review
from simio.core.decision import ScoringConfig, apply_brand_guardrails, apply_review, compose_final
from simio.core.models import (
    BrandAnalysisOutput,
    CollectionStatus,
    Dimension,
    EvidenceSufficiency,
    FindingStatus,
    Indicator,
    Level,
    Priority,
    Review,
    ReviewDecision,
    ScenarioCase,
)
from simio.core.models import (
    FinalClassification as C,
)
from tests.helpers import BRAND, make_evidence

SC = ScoringConfig()

SCENARIOS = {
    "legit": dict(
        title="Auto Center Exemplo — Financiamento",
        header="Auto Center Exemplo Ltda é correspondente bancário do Banco BV",
        body="Simule o financiamento com o Banco BV",
        footer="Auto Center Exemplo Ltda CNPJ 11.222.333/0001-81. Ouvidoria Banco BV",
        images=[("Auto Center Exemplo", True)],
    ),
    "ambiguous": dict(
        title="Financiamento Banco BV - Carros Exemplo",
        header="Banco BV Financiamento",
        body="Somos parceiros. Aprovação rápida no Banco BV",
        images=[("Logo Banco BV", True)],
        contacts=[("WhatsApp", "https://wa.me/55000")],
    ),
    "lookalike": dict(
        url="https://bancobv-oficial.example/",
        title="Banco BV | Site Oficial",
        header="Banco BV",
        above_fold="Bem-vindo ao canal oficial do Banco BV. Aqui é o Banco BV.",
        images=[("Banco BV", True)],
        contacts=[("WhatsApp", "https://wa.me/55001")],
    ),
    "third_party_official_contact": dict(
        title="Central de Atendimento BV",
        header="Central de Atendimento Banco BV",
        body="Central de atendimento Banco BV: ligue 0800 000 0002. Atendimento oficial.",
        footer="Cobranças Exemplo Assessoria Ltda — CNPJ 12.ABC.345/01DE-35",
    ),
    "contradictory": dict(
        title="BV Financiamentos Exemplo",
        header="Site oficial Banco BV para financiamento de motos",
        footer="Motos Exemplo Ltda, CNPJ 11.222.333/0001-81, atua como correspondente bancário do Banco BV.",
        images=[("logo bv financeira", True)],
    ),
    "unrelated": dict(title="Padaria Exemplo", body="Pães artesanais"),
}


def run(name: str, **over):
    ev = make_evidence(**{**SCENARIOS[name], **over})
    cat = build_catalog(ev, BRAND)
    sig = compute_signals(ev, BRAND, cat)
    idn = IdentityAgent(BRAND, CobanRegistry()).attribute("C1", ev, cat, sig)
    out = heuristic_analysis(sig, SC, ev, idn, {i.ref for i in cat})
    a = apply_brand_guardrails(
        out,
        analyzer="heuristic-v1",
        prompt_version=None,
        valid_refs={i.ref for i in cat},
        evidence=ev,
        signals=sig,
        scoring=SC,
    )
    return ev, cat, sig, idn, a


def test_case_a_legitimate_partner_has_no_indication() -> None:
    *_, a = run("legit")
    assert a.case == ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION
    assert a.classification == C.NO_INDICATION
    assert a.legitimate_relationship_indicators


def test_case_b_ambiguous() -> None:
    *_, a = run("ambiguous")
    assert a.case == ScenarioCase.B_AMBIGUOUS_COMMUNICATION
    assert a.classification == C.REVIEW_REQUIRED
    assert any("parceiro" in u for u in a.uncertainties)  # "parceiro" does not resolve the case


def test_case_d_lookalike_bank_page() -> None:
    *_, a = run("lookalike")
    assert a.case == ScenarioCase.D_STRONG_IMPERSONATION
    assert a.classification == C.STRONG_INDICATION and a.priority == Priority.HIGH
    assert all(i.evidence_refs for i in a.brand_confusion_indicators)


def test_case_c_third_party_contact_presented_as_official() -> None:
    _, _, _, idn, a = run("third_party_official_contact")
    assert a.case in (ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL, ScenarioCase.D_STRONG_IMPERSONATION)
    assert a.classification in (C.REVIEW_REQUIRED, C.STRONG_INDICATION)
    # Strong confusion indication does NOT make the owner a COBAN; both conclusions are kept.
    assert idn.classification.value == "OTHER_THIRD_PARTY"


def test_case_e_failed_collection_is_inconclusive() -> None:
    ev = make_evidence(status=CollectionStatus.FAILED)
    cat = build_catalog(ev, BRAND)
    sig = compute_signals(ev, BRAND, cat)
    out = heuristic_analysis(sig, SC, ev, None, {i.ref for i in cat})
    a = apply_brand_guardrails(
        out, analyzer="h", prompt_version=None, valid_refs={i.ref for i in cat}, evidence=ev, signals=sig, scoring=SC
    )
    assert a.case == ScenarioCase.E_INSUFFICIENT_EVIDENCE and a.classification == C.INCONCLUSIVE
    assert a.evidence_sufficiency == EvidenceSufficiency.INSUFFICIENT


def test_partial_page_is_reviewable_not_conclusive() -> None:
    _, _, _, _, a = run("ambiguous", status=CollectionStatus.PARTIAL)
    assert a.evidence_sufficiency != EvidenceSufficiency.SUFFICIENT or a.classification != C.NO_INDICATION


def test_contradictory_evidence_flagged_and_reviewer_downgrades() -> None:
    ev, _, sig, idn, a = run("contradictory")
    assert a.legitimate_relationship_indicators and a.brand_confusion_indicators
    assert any("contraditórias" in u for u in a.uncertainties)
    r = heuristic_review(a, ev, sig, idn, can_recollect=True)
    topics = {c.topic for c in r.challenges}
    assert "contradictory_evidence" in topics
    if a.classification == C.STRONG_INDICATION:
        assert r.decision == ReviewDecision.DOWNGRADE and r.revised_classification == C.REVIEW_REQUIRED


def test_unrelated_page_is_not_relevant() -> None:
    *_, a = run("unrelated")
    assert a.case == ScenarioCase.NOT_RELEVANT and a.classification == C.NO_INDICATION


def test_mention_alone_is_not_irregular() -> None:
    *_, a = run("unrelated", body="Aceitamos pagamentos de boletos do Banco BV e de outros bancos.")
    assert a.classification in (C.NO_INDICATION, C.REVIEW_REQUIRED)
    assert a.case != ScenarioCase.D_STRONG_IMPERSONATION


# ------------------------------------------------------------------ guardrails
def _llm_out(**kw) -> BrandAnalysisOutput:
    base = dict(
        case=ScenarioCase.D_STRONG_IMPERSONATION,
        classification=C.STRONG_INDICATION,
        priority=Priority.HIGH,
        extraction_confidence=Level.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
    )
    base.update(kw)
    return BrandAnalysisOutput(**base)


def _guard(out: BrandAnalysisOutput, name: str = "lookalike"):
    ev = make_evidence(**SCENARIOS[name])
    cat = build_catalog(ev, BRAND)
    sig = compute_signals(ev, BRAND, cat)
    return apply_brand_guardrails(
        out,
        analyzer="model-x",
        prompt_version="v1",
        valid_refs={i.ref for i in cat},
        evidence=ev,
        signals=sig,
        scoring=SC,
    )


def test_hallucinated_references_are_dropped_and_strong_needs_evidence() -> None:
    out = _llm_out(
        brand_confusion_indicators=[
            Indicator(dimension=Dimension.LOGO_USAGE, description="logo", evidence_refs=["E-FAKE-99"]),
            Indicator(dimension=Dimension.INSTITUTIONAL_LANGUAGE, description="oficial", evidence_refs=["E-TITLE"]),
        ],
        evidence_references=["E-TITLE", "E-NOPE"],
    )
    a = _guard(out)
    assert "E-FAKE-99" in a.dropped_references and "E-NOPE" in a.dropped_references
    assert len(a.brand_confusion_indicators) == 1
    assert a.classification == C.REVIEW_REQUIRED  # STRONG requires >=2 valid indicators


def test_case_classification_inconsistency_is_corrected() -> None:
    a = _guard(_llm_out(case=ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION))
    assert a.classification == C.REVIEW_REQUIRED and a.guardrail_notes
    b = _guard(_llm_out(case=ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL, classification=C.NO_INDICATION))
    assert b.classification == C.REVIEW_REQUIRED


def test_reviewer_cannot_escalate_to_strong_alone() -> None:
    a = _guard(
        _llm_out(
            case=ScenarioCase.B_AMBIGUOUS_COMMUNICATION, classification=C.REVIEW_REQUIRED, priority=Priority.MEDIUM
        )
    )
    r = Review(
        decision=ReviewDecision.HUMAN_REVIEW,
        revised_classification=C.STRONG_INDICATION,
        revised_priority=Priority.HIGH,
        reviewer="r",
    )
    cls, _, notes = apply_review(a, r)
    assert cls == C.REVIEW_REQUIRED and notes
    r2 = Review(
        decision=ReviewDecision.INCONCLUSIVE,
        revised_classification=C.REVIEW_REQUIRED,
        revised_priority=Priority.LOW,
        reviewer="r",
    )
    assert apply_review(a, r2)[0] == C.INCONCLUSIVE


def test_final_decision_keeps_axes_separate() -> None:
    ev, _cat, sig, idn, a = run("lookalike")
    comp = ComplianceAnalyst(RequirementCatalog.load()).assess(a, idn, bool(sig.ouvidoria))
    f = compose_final("C1", a, None, idn, comp, ev)
    assert f.classification == C.STRONG_INDICATION
    assert f.identity_class.value == "UNKNOWN_IDENTITY"
    assert f.attribution_confidence in (Level.NONE, Level.LOW)
    assert f.extraction_confidence == Level.MEDIUM
    assert f.requires_human_review and f.human_review_status == "PENDING"
    assert any("heurística" in x for x in f.limitations)


# ------------------------------------------------------------------ compliance
def test_compliance_separates_hypothesis_from_conclusion() -> None:
    _, _, sig, idn, a = run("lookalike")
    comp = ComplianceAnalyst(RequirementCatalog.load()).assess(a, idn, bool(sig.ouvidoria))
    by_id = {f.requirement_id: f for f in comp.findings}
    # Unknown owner -> the norm only applies if the owner is a contracted correspondent.
    assert by_id["CMN4935-ART14-III"].status == FindingStatus.NOT_APPLICABLE_UNLESS_COBAN
    assert comp.out_of_scope_notes
    assert any("contratuais" in x for x in comp.limitations)
    for f in comp.findings:
        assert f.requirement_text.startswith("Art.")


def test_compliance_hypothesis_for_possible_coban() -> None:
    _, _, sig, idn, a = run("ambiguous", footer="Carros Exemplo Ltda CNPJ 11.222.333/0001-81 parceiro autorizado")
    assert idn.classification.value == "POSSIBLE_COBAN"
    comp = ComplianceAnalyst(RequirementCatalog.load()).assess(a, idn, bool(sig.ouvidoria))
    f = next(x for x in comp.findings if x.requirement_id == "CMN4935-ART14-III")
    assert f.status == FindingStatus.HYPOTHESIS_OF_NONCONFORMITY
    assert any("não confirmado" in m for m in f.missing_information)
    assert any("ouvidoria" in m for m in f.missing_information)
    assert comp.related_controls


def test_compliance_cannot_assess_inconclusive() -> None:
    ev = make_evidence(status=CollectionStatus.FAILED)
    cat = build_catalog(ev, BRAND)
    sig = compute_signals(ev, BRAND, cat)
    idn = IdentityAgent(BRAND, CobanRegistry()).attribute("C1", ev, cat, sig)
    out = heuristic_analysis(sig, SC, ev, idn, {i.ref for i in cat})
    a = apply_brand_guardrails(
        out, analyzer="h", prompt_version=None, valid_refs={i.ref for i in cat}, evidence=ev, signals=sig, scoring=SC
    )
    comp = ComplianceAnalyst(RequirementCatalog.load()).assess(a, idn, False)
    assert {f.status for f in comp.findings} == {FindingStatus.CANNOT_ASSESS}


def test_normative_catalog_is_verbatim_and_sourced() -> None:
    cat = RequirementCatalog.load()
    ids = {r.id for r in cat.requirements}
    assert {"CMN4935-ART14-II", "CMN4935-ART14-III", "CMN4935-ART3"} <= ids
    art14ii = next(r for r in cat.requirements if r.id == "CMN4935-ART14-II")
    assert "vedação à utilização, pelo contratado, de logomarca" in art14ii.text
    assert cat.normative_source == "Resolução CMN nº 4.935/2021"


def test_scoring_is_parametrisable(tmp_path) -> None:
    p = tmp_path / "scoring.toml"
    p.write_text("threshold_b = 99\nthreshold_c = 100\nthreshold_d = 101\n", encoding="utf-8")
    sc = ScoringConfig.load(p)
    ev = make_evidence(**SCENARIOS["ambiguous"])
    cat = build_catalog(ev, BRAND)
    sig = compute_signals(ev, BRAND, cat)
    out = heuristic_analysis(sig, sc, ev, None, {i.ref for i in cat})
    # still B because brand is prominent without disclosure, but thresholds moved
    assert out.case in (ScenarioCase.B_AMBIGUOUS_COMMUNICATION, ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION)
