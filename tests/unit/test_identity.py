from __future__ import annotations

from pathlib import Path

from simio.agents.evidence import build_catalog, compute_signals
from simio.agents.identity import CobanRegistry, IdentityAgent
from simio.core.models import CollectionStatus, IdentityClass, Level
from tests.helpers import BRAND, make_evidence

CNPJ_A = "11.222.333/0001-81"  # canonical documentation example
CNPJ_ALNUM = "12.ABC.345/01DE-35"  # Receita Federal's alphanumeric example


def _attr(ev, registry: CobanRegistry | None = None, brand=BRAND):
    cat = build_catalog(ev, brand)
    sig = compute_signals(ev, brand, cat)
    return IdentityAgent(brand, registry or CobanRegistry()).attribute("C1", ev, cat, sig)


def _registry(tmp_path: Path, rows: str, authoritative: bool = True) -> CobanRegistry:
    p = tmp_path / "reg.csv"
    p.write_text(rows, encoding="utf-8")
    return CobanRegistry.load(p, authoritative=authoritative)


def test_confirmed_coban_requires_registry_cnpj(tmp_path: Path) -> None:
    ev = make_evidence(footer=f"Loja Exemplo Ltda — CNPJ {CNPJ_A} — correspondente bancário do Banco BV")
    without = _attr(ev)
    assert without.classification == IdentityClass.POSSIBLE_COBAN
    assert any("CONFIRMED_COBAN indisponível" in x for x in without.limitations)
    reg = _registry(tmp_path, "cnpj;nome\n11222333000181;LOJA EXEMPLO LTDA\n")
    with_reg = _attr(ev, reg)
    assert with_reg.classification == IdentityClass.CONFIRMED_COBAN
    assert with_reg.registry_match and with_reg.registry_match.matched_on == "cnpj"
    assert with_reg.entity_key == "cnpj:11222333000181"
    cnpj_fact = next(f for f in with_reg.facts if f.kind == "cnpj")
    assert cnpj_fact.evidence_ref and cnpj_fact.valid is True


def test_brand_products_alone_do_not_imply_coban() -> None:
    ev = make_evidence(
        title="Financiamento Banco BV",
        body="Financiamento Banco BV com taxas baixas",
        footer=f"Revenda Exemplo Ltda CNPJ {CNPJ_A}",
    )
    a = _attr(ev)
    assert a.classification == IdentityClass.OTHER_THIRD_PARTY


def test_unknown_identity_without_identifiers() -> None:
    a = _attr(make_evidence(title="Banco BV", body="Financiamento Banco BV"))
    assert a.classification == IdentityClass.UNKNOWN_IDENTITY
    assert a.entity_key == "domain:loja.example"
    assert any("domínio" in i for i in a.inferences)


def test_name_only_registry_match_is_not_confirmation(tmp_path: Path) -> None:
    reg = _registry(tmp_path, "cnpj,nome\n11222333000181,Loja Exemplo Ltda\n")
    ev = make_evidence(footer="Razão social: Loja Exemplo Ltda")
    a = _attr(ev, reg)
    assert a.classification == IdentityClass.POSSIBLE_COBAN
    assert a.attribution_confidence == Level.LOW


def test_non_authoritative_registry_never_confirms(tmp_path: Path) -> None:
    reg = _registry(tmp_path, "cnpj;nome\n11222333000181;X\n", authoritative=False)
    a = _attr(make_evidence(footer=f"CNPJ {CNPJ_A}"), reg)
    assert a.classification != IdentityClass.CONFIRMED_COBAN


def test_invalid_cnpj_is_not_used() -> None:
    a = _attr(make_evidence(footer="Empresa X — CNPJ 11.222.333/0001-82"))
    assert a.owner_cnpjs == []
    assert any("inválido" in i for i in a.inferences)


def test_institution_cnpj_is_not_owner() -> None:
    brand = BRAND.model_copy(update={"institution_cnpjs": [CNPJ_A]})
    ev = make_evidence(footer=f"Financiamento pelo Banco BV (CNPJ {CNPJ_A}). Loja Fictícia Ltda CNPJ {CNPJ_ALNUM}")
    a = _attr(ev, brand=brand)
    assert a.referenced_institution_cnpjs == [CNPJ_A]
    assert a.owner_cnpjs == [CNPJ_ALNUM]


def test_cnpj_right_after_institution_name_with_other_cnpj_is_reference() -> None:
    ev = make_evidence(footer=f"Loja Exemplo Ltda CNPJ {CNPJ_ALNUM}. Operação financiada pelo Banco BV, CNPJ {CNPJ_A}.")
    a = _attr(ev)
    assert a.owner_cnpjs == [CNPJ_ALNUM] and a.referenced_institution_cnpjs == [CNPJ_A]


def test_sole_cnpj_near_institution_name_is_kept_but_flagged() -> None:
    # Ambiguous: could be the institution's or the owner's CNPJ -> never silently dropped.
    ev = make_evidence(footer=f"Operação financiada pelo Banco BV, CNPJ {CNPJ_A}.")
    a = _attr(ev)
    assert a.owner_cnpjs == [CNPJ_A] and a.referenced_institution_cnpjs == []
    assert any("confirmar se pertence" in i for i in a.inferences)
    assert a.attribution_confidence == Level.LOW


def test_owner_cnpj_after_brand_banner_is_not_the_institution() -> None:
    ev = make_evidence(footer=f"Financiamento Banco BV. Motos Exemplo Ltda, CNPJ {CNPJ_A}")
    a = _attr(ev)
    assert a.owner_cnpjs == [CNPJ_A] and not a.referenced_institution_cnpjs
    assert a.display_name == "Motos Exemplo Ltda"


def test_profile_institution_cnpj_is_always_reference() -> None:
    brand = BRAND.model_copy(update={"institution_cnpjs": [CNPJ_A]})
    ev = make_evidence(footer=f"CNPJ {CNPJ_A}")
    cat = build_catalog(ev, brand)
    a = IdentityAgent(brand, CobanRegistry()).attribute("C1", ev, cat, compute_signals(ev, brand, cat))
    assert a.owner_cnpjs == [] and a.referenced_institution_cnpjs == [CNPJ_A]


def test_institution_name_is_never_the_owner_name() -> None:
    ev = make_evidence(meta={"og:site_name": "Banco BV"}, footer="Nome fantasia: Banco BV S.A.")
    a = _attr(ev)
    assert a.display_name is None
    assert a.classification == IdentityClass.UNKNOWN_IDENTITY
    assert any(f.kind == "institution_name_displayed" for f in a.facts)
    assert any("identidade declarada/visual" in i for i in a.inferences)


def test_legal_name_extraction_does_not_capture_surrounding_text() -> None:
    ev = make_evidence(
        footer="Financie sua moto Condições especiais. Motos Exemplo Ltda\n"
        "Ligar agora WhatsApp Cobranças Exemplo Assessoria Ltda"
    )
    names = [f.value for f in _attr(ev).facts if f.kind == "legal_name"]
    assert "Motos Exemplo Ltda" in names and "Cobranças Exemplo Assessoria Ltda" in names


def test_registry_is_not_authoritative_by_default(tmp_path: Path) -> None:
    p = tmp_path / "reg.csv"
    p.write_text(f"cnpj;nome\n{CNPJ_A};Loja Exemplo Ltda\n", encoding="utf-8")
    reg = CobanRegistry.load(p)
    ev = make_evidence(footer=f"Loja Exemplo Ltda — CNPJ {CNPJ_A}")
    cat = build_catalog(ev, BRAND)
    a = IdentityAgent(BRAND, reg).attribute("C1", ev, cat, compute_signals(ev, BRAND, cat))
    assert a.classification == IdentityClass.POSSIBLE_COBAN


def test_multiple_establishments_lower_confidence() -> None:
    ev = make_evidence(footer=f"Loja A Ltda CNPJ {CNPJ_A} | Loja B Ltda CNPJ {CNPJ_ALNUM}")
    a = _attr(ev)
    assert len(a.owner_cnpjs) == 2
    assert a.attribution_confidence == Level.LOW
    assert any("2 CNPJs" in i for i in a.inferences)


def test_failed_collection_gives_unknown() -> None:
    a = _attr(make_evidence(status=CollectionStatus.FAILED))
    assert a.classification == IdentityClass.UNKNOWN_IDENTITY and a.attribution_confidence == Level.NONE


def test_facts_and_inferences_are_separated() -> None:
    ev = make_evidence(
        footer=f"Loja Exemplo Ltda CNPJ {CNPJ_A}. Contato: contato@loja.example",
        contacts=[("WhatsApp", "https://wa.me/550000000000")],
    )
    a = _attr(ev)
    kinds = {f.kind for f in a.facts}
    assert {"domain", "cnpj", "email", "whatsapp"} <= kinds
    assert all(f.evidence_ref for f in a.facts)
    assert a.inferences  # association reasoning kept apart from facts
