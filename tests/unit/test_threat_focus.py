"""Threat-intelligence campaign (``simio hunt --focus ameacas``) and credential-harvesting detection."""

from __future__ import annotations

import json

import httpx

from simio.agents.brand import apply_sensitive_data_rule
from simio.agents.compliance import ComplianceAnalyst, RequirementCatalog
from simio.agents.discovery import FOCUS_THREAT, CandidateGroup, DiscoveryAgent, prioritise
from simio.agents.evidence import build_catalog, classify_input, compute_signals
from simio.agents.identity import CobanRegistry, IdentityAgent
from simio.core.decision import compose_final
from simio.core.models import (
    BrandAssessment,
    DiscoveryHit,
    EvidenceSufficiency,
    InputInfo,
    Level,
    Priority,
    Review,
    ReviewDecision,
    ScenarioCase,
    SearchHypothesis,
)
from simio.core.models import FinalClassification as C
from simio.providers.search import SerperSearch
from simio.security.url_policy import UrlPolicy
from tests.helpers import BRAND, make_evidence


class _Named:
    def __init__(self, name: str, queries: list[str] | None = None) -> None:
        self.name = name
        self._q = queries or []

    def available(self) -> tuple[bool, str]:
        return True, self.name

    def queries(self) -> list[str]:
        return self._q


def test_threat_plan_drops_coban_context_and_lens() -> None:
    conns = [_Named("serper"), _Named("lens", ["https://img.example/bv.png"]), _Named("typosquat", ["typo:bv"])]
    agent = DiscoveryAgent(BRAND, conns, UrlPolicy(), max_queries=500, focus=FOCUS_THREAT)
    plan = agent.plan(conns)
    families = {p.hypothesis.id.split(":")[0] for p in plan}
    assert {"H14-threat", "H10-fraud", "H7-threat-intel", "H8-lookalike"} <= families
    assert not any(p.connector == "lens" for p in plan)
    assert not any(f.startswith(("H2-", "H5-", "H9-", "H13-")) for f in families)


def test_free_builder_pages_are_kept_and_boosted_only_in_threat_campaigns() -> None:
    def group() -> CandidateGroup:
        hit = DiscoveryHit(source="serper", original_url="https://bv-acordo.weebly.com/", title="Banco BV acordo")
        return CandidateGroup(
            url=hit.original_url,
            normalized_url="https://bv-acordo.weebly.com/",
            hostname="bv-acordo.weebly.com",
            registrable_domain="bv-acordo.weebly.com",
            hits=[hit],
        )

    [broad] = prioritise([group()], BRAND)
    assert broad.excluded_reason is not None
    [threat] = prioritise([group()], BRAND, FOCUS_THREAT)
    assert threat.excluded_reason is None and any("free website builder" in r for r in threat.reasons)


async def test_serper_recovers_destination_host_of_opaque_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["num"] == 10
        return httpx.Response(
            200,
            json={
                "organic": [
                    {"title": "Direto", "link": "https://loja.example/financiamento", "displayedLink": "x"},
                    {
                        "title": "Opaco com host",
                        "link": "https://www.google.com/goto?url=CAES1",
                        "displayedLink": "https://www.revenda-bv.com.br › financiamento",
                    },
                    {
                        "title": "Opaco social",
                        "link": "https://www.google.com/goto?url=CAES2",
                        "displayedLink": "https://www.instagram.com › reel",
                    },
                    {"title": "Opaco sem host", "link": "https://www.google.com/goto?url=CAES3", "displayedLink": "há 4 dias"},
                ]
            },
        )

    conn = SerperSearch("k", transport=httpx.MockTransport(handler))
    hits, record = await conn.run(SearchHypothesis(id="H", description="d", grounding_prompt=""), "q")
    urls = [h.original_url for h in hits]
    assert urls == ["https://loja.example/financiamento", "https://www.revenda-bv.com.br/"]
    assert hits[1].metadata["recovered_from_google_redirect"] is True
    assert record.metadata["recovered_google_redirect_hosts"] == 1
    assert len(record.metadata["unresolved_google_redirects"]) == 2


def test_input_classification() -> None:
    assert classify_input(InputInfo(type="password", name="x")) == "senha"
    assert classify_input(InputInfo(type="text", name="cc-number", autocomplete="cc-number")) == "cartão"
    assert classify_input(InputInfo(type="tel", placeholder="Código SMS de verificação")) == "token/código"
    assert classify_input(InputInfo(type="text", label="CPF")) == "cpf"
    assert classify_input(InputInfo(type="email", label="E-mail da conta")) is None
    assert classify_input(InputInfo(type="text", name="nome")) is None


def _phishing_evidence(**kw):
    ev = make_evidence(title="Banco BV - Acesse sua conta", body="Banco BV internet banking. Digite sua senha.", **kw)
    ev.page.inputs = [
        InputInfo(type="text", name="cpf", placeholder="CPF"),
        InputInfo(type="password", name="senha"),
    ]
    ev.page.form_actions = ["https://coletor.example/post.php"]
    return ev


def _assessment(**kw) -> BrandAssessment:
    base = dict(
        case=ScenarioCase.A_LEGITIMATE_COMMERCIAL_MENTION,
        classification=C.NO_INDICATION,
        priority=Priority.LOW,
        extraction_confidence=Level.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
        analyzer="model-x",
    )
    base.update(kw)
    return BrandAssessment(**base)


def test_brand_page_asking_for_password_is_floored_and_survives_reviewer() -> None:
    ev = _phishing_evidence()
    cat = build_catalog(ev, BRAND)
    sig = compute_signals(ev, BRAND, cat)
    assert [h.value for h in sig.credential_fields] == ["senha"] and sig.external_form_action

    a = apply_sensitive_data_rule(_assessment(), sig)
    assert a.sensitive_data_requested == ["cpf", "senha"]
    assert (a.classification, a.priority) == (C.REVIEW_REQUIRED, Priority.HIGH)
    assert any("E-FORM-ACTION" in ind.evidence_refs for ind in a.brand_confusion_indicators)

    idn = IdentityAgent(BRAND, CobanRegistry()).attribute("C1", ev, cat, sig)
    comp = ComplianceAnalyst(RequirementCatalog.load()).assess(a, idn, False)
    clear = Review(
        decision=ReviewDecision.DOWNGRADE,
        revised_classification=C.NO_INDICATION,
        revised_priority=Priority.LOW,
        reviewer="r",
    )
    f = compose_final("C1", a, clear, idn, comp, ev)
    assert f.classification == C.REVIEW_REQUIRED and f.priority == Priority.HIGH
    assert f.sensitive_data_requested == ["cpf", "senha"]


def test_cpf_only_or_no_brand_does_not_trigger() -> None:
    ev = make_evidence(title="Simule seu financiamento", body="Banco BV parceiro. Simule.")
    ev.page.inputs = [InputInfo(type="text", name="cpf", placeholder="CPF")]
    cat = build_catalog(ev, BRAND)
    assert apply_sensitive_data_rule(_assessment(), compute_signals(ev, BRAND, cat)).sensitive_data_requested == []

    ev2 = make_evidence(title="Loja de roupas", body="Entre na sua conta")
    ev2.page.inputs = [InputInfo(type="password", name="senha")]
    cat2 = build_catalog(ev2, BRAND)
    untouched = _assessment()
    assert apply_sensitive_data_rule(untouched, compute_signals(ev2, BRAND, cat2)) == untouched


def test_password_box_on_a_page_that_only_cites_the_brand_does_not_trigger() -> None:
    ev = make_evidence(title="Portal de notícias", body="O Banco BV teve lucro recorde no trimestre. Assine já.")
    ev.page.inputs = [InputInfo(type="password", name="senha")]
    cat = build_catalog(ev, BRAND)
    assert apply_sensitive_data_rule(_assessment(), compute_signals(ev, BRAND, cat)).sensitive_data_requested == []


def test_social_platform_login_wall_is_not_a_credential_form() -> None:
    ev = make_evidence(
        url="https://www.facebook.com/groups/1/posts/2/", title="Banco BV repasse", body="Entrar. Banco BV repasse."
    )
    ev.page.inputs = [InputInfo(type="password", name="pass")]
    assert not any(i.ref.startswith("E-FORM") for i in build_catalog(ev, BRAND))
