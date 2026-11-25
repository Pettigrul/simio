"""Lookalike-domain discovery (typosquatting / combosquatting / TLD swap) — no network."""

from __future__ import annotations

from simio.agents.discovery import DiscoveryAgent, deduplicate, prioritise
from simio.brands import load_brand
from simio.core.models import SearchHypothesis
from simio.providers.typosquat import TyposquatConnector, lookalike_domains, typo_variants
from simio.security.url_policy import UrlPolicy

BV = load_brand("bv", None)
H = SearchHypothesis(id="H8", description="", grounding_prompt="")


def test_typo_variants_cover_dnstwist_style_fuzzers() -> None:
    v = typo_variants("bancobv")
    assert v["bancbv"] == "omission"
    assert v["bancobbv"] == "repetition"
    assert v["bnacobv"] == "transposition"
    assert "bancobvv" in v
    assert v["banc0bv"] == "homoglyph"
    assert v["banco-bv"] == "hyphenation"
    assert v["bencobv"] == "vowel-swap"
    assert "bancobv" not in v
    assert all(not x.startswith("-") and not x.endswith("-") for x in v)


def test_short_labels_get_only_combos_and_tld_swaps() -> None:
    d = lookalike_domains(["bv.com.br"], ["bv"], ["financiamento"], exclude=BV.is_official)
    fuzzers = {f for f, _ in d.values()}
    assert fuzzers == {"tld-swap", "combosquatting"}  # no "bb.com.br"-style noise
    assert "bv-financiamento.com.br" in d and "financiamentobv.com" in d
    assert "bv.com" in d and "bv.com.br" not in d


def test_official_domains_are_never_candidates() -> None:
    d = lookalike_domains(BV.typosquat_seeds, BV.typosquat_combo_labels, BV.typosquat_keywords, exclude=BV.is_official)
    assert not any(BV.is_official(x) for x in d)
    assert "bvfinanceira.com.br" not in d and "bancobv.com.br" not in d


def _resolver(table: dict[str, list[str]]):
    async def resolve(host: str) -> list[str]:
        if host in table:
            return table[host]
        raise OSError("NXDOMAIN")

    return resolve


async def test_only_publicly_resolving_lookalikes_become_hits() -> None:
    conn = TyposquatConnector(
        BV,
        resolver=_resolver(
            {"bancobva.com.br": ["104.21.5.101"], "bancbv.com.br": ["127.0.0.1"], "banco-bv.com.br": ["10.0.0.5"]}
        ),
    )
    hits, rec = await conn.run(H, "typo:bancobv.com.br")
    assert [h.original_url for h in hits] == ["http://bancobva.com.br/"]
    assert hits[0].metadata["fuzzer"] == "addition" and hits[0].metadata["lookalike_of"] == "bancobv.com.br"
    assert "semelhante" in (hits[0].snippet or "")
    assert rec.status == "OK" and rec.metadata["permutations_checked"] > 100
    assert set(rec.metadata["resolved_non_public"]) == {"bancbv.com.br", "banco-bv.com.br"}


async def test_nxdomain_hijacking_resolver_disables_connector() -> None:
    async def everything_resolves(host: str) -> list[str]:
        return ["93.184.216.34"]

    conn = TyposquatConnector(BV, resolver=everything_resolves)
    hits, rec = await conn.run(H, "combo:bv")
    assert hits == [] and rec.status == "ERROR" and "NXDOMAIN" in (rec.error or "")
    assert conn.available()[0] is False


async def test_discovery_plans_lookalike_queries_and_prioritises_hits() -> None:
    conn = TyposquatConnector(BV, resolver=_resolver({"feiraobv.com.br": ["104.21.74.215"]}))
    agent = DiscoveryAgent(BV, [conn], UrlPolicy(), max_queries=50)
    plan = agent.plan([conn])
    assert [p.query for p in plan] == conn.queries()
    assert all(p.hypothesis.id.startswith("H8-lookalike:") for p in plan)
    outcome = await agent.run()
    groups, rejected = deduplicate(outcome.hits, UrlPolicy())
    assert not rejected
    [g] = prioritise(groups, BV)
    assert g.hostname == "feiraobv.com.br" and g.score >= 3
    assert any("lookalike of bv" in r for r in g.reasons)
