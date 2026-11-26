"""Operator-free fallback for Serper (Google hides operator results behind opaque links) and the public
Facebook plugins used to read pages and photos without a login wall."""

from __future__ import annotations

import json

import httpx

from simio.collectors.browser import _social_embed_url
from simio.core.models import SearchHypothesis
from simio.providers.search import SerperSearch, plain_variants


def test_or_groups_are_expanded_round_robin_and_operators_removed() -> None:
    q = '(intitle:"Banco BV" OR intitle:"bv financeira") (login OR entrar) -site:bv.com.br -site:reclameaqui.com.br'
    variants, excluded = plain_variants(q)
    assert variants == ['"Banco BV" login', '"bv financeira" entrar']
    assert excluded == ["bv.com.br", "reclameaqui.com.br"]


def test_site_operator_becomes_a_platform_word() -> None:
    variants, _ = plain_variants('site:instagram.com ("Banco BV" OR "bv financeira") (feirão OR parceiro OR boleto)')
    assert variants == ['instagram "Banco BV" feirão', 'instagram "bv financeira" parceiro', 'instagram "Banco BV" boleto']
    assert plain_variants("site:t.me painel")[0] == ["telegram painel"]
    assert plain_variants('"NaPista" (leilão OR pix OR taxa)', limit=2)[0] == ['"NaPista" leilão', '"NaPista" pix']


def test_plain_query_has_no_variant() -> None:
    assert plain_variants('"bancos parceiros" "bv financeira"')[0] == []


async def test_serper_reruns_operator_free_versions_when_results_are_opaque() -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = json.loads(request.content)["q"]
        asked.append(q)
        if "site:" in q:
            organic = [{"title": "x", "link": f"https://www.google.com/goto?url=C{i}", "displayedLink": "há 2 dias"}
                       for i in range(8)]
            organic.append({"title": "Direto", "link": "https://loja.example/a"})
        else:
            organic = [
                {"title": "Post", "link": f"https://www.instagram.com/p/{q[-3:]}/"},
                {"title": "Oficial", "link": "https://www.bv.com.br/x"},  # excluded by -site: client-side
                {"title": "Direto", "link": "https://loja.example/a"},  # duplicate of the first query
            ]
        return httpx.Response(200, json={"organic": organic})

    conn = SerperSearch("k", transport=httpx.MockTransport(handler), plain_variants=2)
    hyp = SearchHypothesis(id="H", description="d", grounding_prompt="")
    hits, record = await conn.run(hyp, 'site:instagram.com "bv" (abc OR xyz) -site:bv.com.br')
    assert asked[1:] == ['instagram "bv" abc', 'instagram "bv" xyz']
    urls = [h.original_url for h in hits]
    assert urls == ["https://loja.example/a", "https://www.instagram.com/p/abc/", "https://www.instagram.com/p/xyz/"]
    assert hits[1].metadata["plain_variant_of"].startswith("site:instagram.com")
    assert hits[1].query == 'instagram "bv" abc'
    assert record.metadata["plain_variants"] == asked[1:]


async def test_serper_does_not_spend_credits_when_results_are_direct() -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(json.loads(request.content)["q"])
        return httpx.Response(200, json={"organic": [{"title": "t", "link": "https://loja.example/"}]})

    conn = SerperSearch("k", transport=httpx.MockTransport(handler), plain_variants=3)
    await conn.run(SearchHypothesis(id="H", description="d", grounding_prompt=""), 'site:loja.example "bv"')
    assert len(asked) == 1


def test_facebook_pages_and_photos_use_public_plugins() -> None:
    page = _social_embed_url("https://www.facebook.com/bvveiculoseconsorcios/?locale=zh_TW")
    assert page.startswith("https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2Fbvveiculos")
    assert "tabs=timeline" in page
    assert "profile.php%3Fid%3D615" in _social_embed_url("https://www.facebook.com/profile.php?id=61594249927645")
    assert "/plugins/post.php" in _social_embed_url("https://www.facebook.com/photo.php?fbid=1&set=a.2")
    # Public group posts render better as they are; reserved paths are left alone.
    group = "https://www.facebook.com/groups/1/posts/2/"
    assert _social_embed_url(group) == group
    assert _social_embed_url("https://www.facebook.com/marketplace/") == "https://www.facebook.com/marketplace/"


def test_hostname_of_tolerates_malformed_links() -> None:
    from simio.core.urls import hostname_of

    assert hostname_of("http://[broken/link") == ""
    assert hostname_of("https://WWW.Loja.example./a") == "www.loja.example"
