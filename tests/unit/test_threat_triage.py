"""Threat triage: re-reading an analysed campaign as a threat analyst, with no new model calls."""

from __future__ import annotations

from simio.agents.threat_triage import triage
from simio.core.models import (
    Candidate,
    DiscoveryHit,
    DomainIntel,
    EvidenceSufficiency,
    FinalClassification,
    FinalDecision,
    IdentityClass,
    Level,
    Priority,
    RdapIntel,
    ScenarioCase,
)
from simio.core.urls import hostname_of
from simio.reporting.data import CandidateRecord
from simio.reporting.threat_html import defang
from tests.helpers import BRAND, make_evidence


def _rec(
    url: str,
    cls: FinalClassification,
    case: ScenarioCase,
    *,
    title: str = "",
    body: str = "",
    identity: IdentityClass = IdentityClass.UNKNOWN_IDENTITY,
    sensitive: list[str] | None = None,
    hits: list[DiscoveryHit] | None = None,
) -> CandidateRecord:
    host = hostname_of(url)
    cand = Candidate(id="c1", campaign_id="k", url=url, normalized_url=url, hostname=host, registrable_domain=host)
    final = FinalDecision(
        candidate_id="c1",
        classification=cls,
        priority=Priority.HIGH,
        case=case,
        identity_class=identity,
        extraction_confidence=Level.HIGH,
        attribution_confidence=Level.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
        requires_human_review=True,
        justification="",
    )
    if sensitive is not None:
        final.sensitive_data_requested = sensitive
    return CandidateRecord(
        candidate=cand,
        hits=hits or [],
        evidence=make_evidence(url=url, title=title, body=body),
        all_evidence=[],
        catalog=[],
        identity=None,
        brand=None,
        compliance=None,
        review=None,
        final=final,
        transitions=[],
    )


def test_fake_portal_on_free_builder_asking_password_is_critical() -> None:
    r = _rec(
        "https://bvboleto.weebly.com/",
        FinalClassification.STRONG_INDICATION,
        ScenarioCase.D_STRONG_IMPERSONATION,
        title="Banco BV - 2ª via do boleto",
        body="Banco BV: emita a 2ª via do boleto e faça a quitação. Informe CPF e senha.",
        sensitive=["cpf", "senha"],
    )
    t = triage(r, BRAND)
    assert t.level == "CRÍTICO"
    assert "Coleta de credenciais (phishing)" in t.categories and "Hospedagem gratuita" in t.categories


def test_identified_partner_using_its_own_brand_is_not_a_threat() -> None:
    r = _rec(
        "https://loja-motos.example/financiamento",
        FinalClassification.REVIEW_REQUIRED,
        ScenarioCase.B_AMBIGUOUS_COMMUNICATION,
        title="Loja Motos - financiamento",
        body="Financiamento com o Banco BV. Loja Motos Ltda, CNPJ 00.000.000/0001-00.",
        identity=IdentityClass.CONFIRMED_COBAN,
    )
    assert not triage(r, BRAND).is_threat


def test_dismissed_case_does_not_count() -> None:
    # The reviewer discarded the analyst's "impersonation" (a directory of bank branches).
    r = _rec(
        "https://agencias.example/bv-financeira/sp",
        FinalClassification.NO_INDICATION,
        ScenarioCase.D_STRONG_IMPERSONATION,
        title="BV Financeira - agências em SP",
        body="Endereço e telefone das agências da BV Financeira.",
    )
    assert not triage(r, BRAND).is_threat


def test_lookalike_registered_by_the_bank_itself_is_defensive() -> None:
    hit = DiscoveryHit(
        source="typosquat",
        original_url="http://bvcartao.com.br/",
        metadata={"lookalike_of": "bv.com.br", "fuzzer": "combosquatting"},
    )
    r = _rec("http://bvcartao.com.br/", FinalClassification.INCONCLUSIVE, ScenarioCase.E_INSUFFICIENT_EVIDENCE, hits=[hit])
    assert triage(r, BRAND).is_threat  # without registration data it is worth watching
    intel = DomainIntel(
        hostname="bvcartao.com.br",
        registered_domain="bvcartao.com.br",
        rdap=RdapIntel(holder="Banco Votorantim S.A.", holder_document="59.588.111/0001-03"),
    )
    t = triage(r, BRAND, intel)
    assert not t.is_threat and any("registro defensivo" in f for f in t.facts)


def test_new_domain_adds_weight() -> None:
    r = _rec(
        "https://bvquitacao.example/",
        FinalClassification.REVIEW_REQUIRED,
        ScenarioCase.C_POSSIBLE_OFFICIAL_CHANNEL,
        title="Banco BV quitação",
        body="Banco BV: quitação de contrato.",
    )
    old = triage(r, BRAND, DomainIntel(hostname="bvquitacao.example", registered_domain="bvquitacao.example",
                                       a=["1.2.3.4"], rdap=RdapIntel(age_days=4000)))
    new = triage(r, BRAND, DomainIntel(hostname="bvquitacao.example", registered_domain="bvquitacao.example",
                                       a=["1.2.3.4"], rdap=RdapIntel(age_days=20)))
    assert new.score == old.score + 2 and "Domínio recente" in new.categories


def test_defang() -> None:
    assert defang("https://bv.weebly.com/a.b?x=1") == "hxxps://bv[.]weebly[.]com/a.b?x=1"
    assert defang("http://golpe.example") == "hxxp://golpe[.]example"
