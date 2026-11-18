from __future__ import annotations

import httpx
import pytest

from simio.agents.discovery import DiscoveryAgent, build_hypotheses, deduplicate, prioritise
from simio.core.models import DiscoveryHit, QueryRecord, SearchHypothesis
from simio.core.urls import extract_urls, normalize_url, registrable_domain
from simio.providers.grounding import GroundingConnector
from simio.providers.llm import Budget, LLMGateway, LLMUnavailable
from simio.providers.mock import MockProvider, grounded_result
from simio.providers.search import BraveSearch, SerperSearch, UrlscanSearch, import_file, import_text
from simio.providers.vertex import parse_grounded_response
from simio.security.url_policy import UrlPolicy
from tests.helpers import BRAND


# ------------------------------------------------------------------ normalisation / extraction
@pytest.mark.parametrize(
    ("a", "b"),
    [
        (
            "https://www.Loja.example/Financiamento/?utm_source=g&b=2&a=1#x",
            "https://loja.example/Financiamento?a=1&b=2",
        ),
        ("http://loja.example:80/", "https://loja.example/"),
        ("https://loja.example/index.html", "https://loja.example/"),
        ("loja.example/x//y/", "https://loja.example/x/y"),
        ("https://loja.example/p?gclid=1&fbclid=2", "https://loja.example/p"),
    ],
)
def test_normalize(a: str, b: str) -> None:
    assert normalize_url(a) == normalize_url(b)


def test_registrable_domain_br() -> None:
    assert registrable_domain("www.loja.com.br") == "loja.com.br"
    assert registrable_domain("a.b.example.co.uk") == "example.co.uk"


def test_extract_urls_from_text() -> None:
    text = "Veja https://a.example/x). Também www.b.example/y, e (http://c.test/z?q=1)."
    assert extract_urls(text) == ["https://a.example/x", "https://www.b.example/y", "http://c.test/z?q=1"]


def test_import_file_txt(tmp_path) -> None:
    from tests.conftest import SITES

    hits = import_file(SITES / "urls_sample.txt")
    assert [h.source for h in hits] == ["import:file:urls_sample.txt"] * 4
    csv = tmp_path / "u.csv"
    csv.write_text("nome;url\nx;https://a.example/\ny;https://a.example/\n", encoding="utf-8")
    assert [h.original_url for h in import_file(csv)] == ["https://a.example/"]


def test_dedup_preserves_sources_and_original_urls() -> None:
    hits = [
        DiscoveryHit(source="grounding", original_url="https://www.financiamento-bv.example/?utm_source=x", query="q1"),
        DiscoveryHit(source="brave", original_url="https://financiamento-bv.example/", query="q2"),
        DiscoveryHit(source="import:text", original_url="https://financiamento-bv.example/outra"),
        DiscoveryHit(source="brave", original_url="http://localhost/x"),
        DiscoveryHit(source="brave", original_url="ftp://x.example/"),
    ]
    groups, rejected = deduplicate(hits, UrlPolicy())
    assert len(groups) == 2
    home = groups[0]
    assert {h.source for h in home.hits} == {"grounding", "brave"}
    assert {h.original_url for h in home.hits} == {
        "https://www.financiamento-bv.example/?utm_source=x",
        "https://financiamento-bv.example/",
    }
    assert home.registrable_domain == "financiamento-bv.example"
    assert len(rejected) == 2


def test_prioritise_excludes_official_and_ranks() -> None:
    hits = [
        DiscoveryHit(source="brave", original_url="https://www.bv.com.br/financiamento", title="Banco BV"),
        DiscoveryHit(source="brave", original_url="https://noticias.example/bv", title="Notícia"),
        DiscoveryHit(source="brave", original_url="https://bancobv-oficial.example/", title="Banco BV canal oficial"),
        DiscoveryHit(source="brave", original_url="https://pt.wikipedia.org/wiki/Banco_BV", title="Banco BV"),
    ]
    groups, _ = deduplicate(hits, UrlPolicy())
    ordered = prioritise(groups, BRAND)
    assert ordered[0].hostname == "bancobv-oficial.example"
    assert ordered[-1].excluded_reason == "official institutional domain"
    wiki = next(g for g in ordered if "wikipedia" in g.hostname)
    assert wiki.score < ordered[0].score


def test_hypotheses_cover_required_axes() -> None:
    hs = build_hypotheses(BRAND)
    ids = {h.id.split(":")[0] for h in hs}
    assert {"H1-identity", "H2-product", "H3-service", "H4-official-language", "H5-establishment", "H6-platform"} <= ids
    assert all("-site:bv.com.br" in q for h in hs for q in h.keyword_queries)


# ------------------------------------------------------------------ connectors
def _transport(payload: dict, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(status, json=payload))


async def test_brave_parse_and_error() -> None:
    h = SearchHypothesis(id="H", description="", grounding_prompt="")
    ok = BraveSearch("k", transport=_transport({"web": {"results": [{"url": "https://a.example/", "title": "A"}]}}))
    hits, rec = await ok.run(h, "q")
    assert rec.status == "OK" and hits[0].original_url == "https://a.example/" and hits[0].source == "brave"
    bad = BraveSearch("k", transport=_transport({"error": "x"}, status=500))
    hits, rec = await bad.run(h, "q")
    assert hits == [] and rec.status == "ERROR"
    empty = SerperSearch("k", transport=_transport({"organic": []}))
    hits, rec = await empty.run(h, "q")
    assert rec.status == "EMPTY"
    us = UrlscanSearch(
        "k", transport=_transport({"results": [{"page": {"url": "https://u.example/", "domain": "u.example"}}]})
    )
    hits, rec = await us.run(h, "q")
    assert hits[0].original_url == "https://u.example/"


def test_connectors_unavailable_without_keys() -> None:
    assert BraveSearch(None).available()[0] is False
    assert GroundingConnector(None, None, UrlPolicy(), []).available()[0] is False


# ------------------------------------------------------------------ grounding
def test_parse_grounded_response_uses_only_metadata() -> None:
    from types import SimpleNamespace as NS

    resp = NS(
        text="Veja também https://inventado.example/ que não está nas fontes.",
        usage_metadata=NS(prompt_token_count=10, candidates_token_count=5, total_token_count=15),
        candidates=[
            NS(
                grounding_metadata=NS(
                    web_search_queries=["financiamento banco bv whatsapp"],
                    search_entry_point=NS(rendered_content="<div>chips</div>"),
                    grounding_chunks=[
                        NS(
                            web=NS(
                                uri="https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
                                title="loja.example",
                                domain="loja.example",
                            )
                        ),
                        NS(web=None),
                    ],
                    grounding_supports=[NS(segment=NS(text="A loja usa a marca"), grounding_chunk_indices=[0])],
                )
            )
        ],
    )
    res = parse_grounded_response(resp, "m")
    assert [s.uri for s in res.sources] == ["https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"]
    assert res.sources[0].support_texts == ["A loja usa a marca"]
    assert res.web_search_queries == ["financiamento banco bv whatsapp"]
    assert res.usage.total_tokens == 15
    assert all("inventado" not in s.uri for s in res.sources)


async def test_grounding_connector_resolves_redirects(storage, monkeypatch) -> None:
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "vertexaisearch.cloud.google.com"
        return httpx.Response(302, headers={"location": "https://loja-alvo.example/financiamento"})

    policy = UrlPolicy()
    monkeypatch.setattr(policy, "_resolve_sync", lambda h, p: ["93.184.215.14"])

    async def fake_acheck(url: str) -> str:
        return policy.check(url)

    monkeypatch.setattr(policy, "acheck", fake_acheck)
    provider = MockProvider(
        grounded=lambda p: grounded_result(
            [(redirect, "loja-alvo.example"), ("https://www.google.com/search?q=x", "g")]
        )
    )
    cid = storage.create_campaign("bv", {})
    gw = LLMGateway(provider, Budget(storage, cid, 10, 10_000), max_retries=0, base_delay_s=0)
    conn = GroundingConnector(gw, "m", policy, ["bv.com.br"], transport=httpx.MockTransport(handler))
    hits, rec = await conn.run(SearchHypothesis(id="H1", description="", grounding_prompt="p"), "p")
    assert [h.original_url for h in hits] == ["https://loja-alvo.example/financiamento"]
    assert hits[0].metadata["grounding_uri"] == redirect
    assert rec.metadata["unresolved"][0]["error"].startswith("search-results page")
    assert storage.usage_totals(cid)[0] == 1


async def test_discovery_fallback_when_grounding_fails(storage) -> None:
    class FailingGrounding:
        name = "grounding"
        disabled: str | None = None

        def available(self):
            return (self.disabled is None, "grounding: test")

        async def run(self, h, q):
            self.disabled = "403 permission denied"
            return [], QueryRecord(source="grounding", query=q, status="ERROR", error=self.disabled)

    class FakeBrave:
        name = "brave"

        def available(self):
            return True, "brave: configured"

        async def run(self, h, q):
            return (
                [DiscoveryHit(source="brave", original_url="https://x.example/", query=q)],
                QueryRecord(source="brave", query=q, result_count=1),
            )

    agent = DiscoveryAgent(BRAND, [FailingGrounding(), FakeBrave()], UrlPolicy(), max_queries=10)
    out = await agent.run()
    assert out.sources_used == ["brave"]
    assert sum(1 for q in out.queries if q.source == "grounding") == 1  # disabled after first error
    assert any("fallback" in d for d in out.diagnostics)
    assert len([q for q in out.queries if q.source == "brave"]) >= 5


async def test_discovery_without_any_source_gives_diagnostic() -> None:
    agent = DiscoveryAgent(BRAND, [BraveSearch(None), GroundingConnector(None, None, UrlPolicy(), [])], UrlPolicy())
    out = await agent.run()
    assert out.hits == [] and any("NO automatic discovery" in d for d in out.diagnostics)
    out2 = await agent.run(import_text("https://a.example/"))
    assert len(out2.hits) == 1 and "import" in out2.sources_used


async def test_grounding_marks_unavailable_on_auth_error(storage) -> None:
    def boom(prompt: str):
        raise LLMUnavailable("403: permission denied")

    provider = MockProvider(grounded=boom)
    cid = storage.create_campaign("bv", {})
    gw = LLMGateway(provider, Budget(storage, cid, 10, 10_000), max_retries=0, base_delay_s=0)
    conn = GroundingConnector(gw, "m", UrlPolicy(), [])
    _, rec = await conn.run(SearchHypothesis(id="H", description="", grounding_prompt="p"), "p")
    assert rec.status == "ERROR" and conn.available()[0] is False
