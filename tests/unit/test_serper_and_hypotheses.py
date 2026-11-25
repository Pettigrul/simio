"""Serper pagination/redirect filtering and breadth of the open-web hypothesis set (no network)."""

from __future__ import annotations

import json

import httpx

from simio.agents.discovery import BR_REGIONS, build_hypotheses
from simio.brands import load_brand
from simio.core.models import SearchHypothesis
from simio.providers.search import SerperSearch

BV = load_brand("bv", None)
H = SearchHypothesis(id="H", description="", grounding_prompt="")


def _serper(pages: dict[int, list[dict[str, str]]], seen: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body.get("num", 10) > 10:
            return httpx.Response(400, json={"message": "Query pattern not allowed for free accounts."})
        return httpx.Response(200, json={"organic": pages.get(body["page"], [])})

    return httpx.MockTransport(handler)


async def test_serper_paginates_at_10_and_stops_on_short_page() -> None:
    full = [{"link": f"https://a{i}.com.br/", "title": "t"} for i in range(10)]
    short = [{"link": "https://b.com.br/", "title": "t"}]
    seen: list[dict] = []
    conn = SerperSearch("k", transport=_serper({1: full, 2: short, 3: full}, seen), pages=5)
    hits, rec = await conn.run(H, '"Banco BV"')
    assert len(hits) == 11 and rec.status == "OK"
    assert [b["page"] for b in seen] == [1, 2] and all(b["num"] == 10 for b in seen)


async def test_serper_drops_opaque_google_redirects_and_reports_errors() -> None:
    rows = [
        {"link": "https://www.instagram.com/p/abc/", "title": "ok"},
        {"link": "https://www.google.com/goto?url=CAES123", "title": "Feirão", "snippet": "post"},
    ]
    hits, rec = await SerperSearch("k", transport=_serper({1: rows}, [])).run(H, "q")
    assert [h.original_url for h in hits] == ["https://www.instagram.com/p/abc/"]
    assert rec.result_count == 1 and rec.metadata["unresolved_google_redirects"][0]["title"] == "Feirão"

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Not enough credits"})

    _, rec = await SerperSearch("k", transport=httpx.MockTransport(bad)).run(H, "q")
    assert rec.status == "ERROR" and "Not enough credits" in (rec.error or "")


def test_open_web_hypotheses_cover_brand_protection_typologies_without_cobans() -> None:
    hs = build_hypotheses(BV)
    ids = {h.id for h in hs}
    assert not any(i.startswith("H9") for i in ids)  # registry-independent
    for needed in (
        "H2-product:bancos-parceiros",
        "H6-platform:marketplaces",
        "H6-platform:Telegram-grupos",
        "H6-platform:YouTube-Kwai-Threads",
        "H10-fraud:leilao-retomados",
        "H10-fraud:agio-repasse",
        "H10-fraud:falso-emprestimo",
        "H10-fraud:app-phishing",
        "H10-fraud:central-falsa",
        "H10-fraud:vagas",
        "H11-napista:social",
    ):
        assert needed in ids, needed
    regional = " ".join(q for h in hs if h.id.startswith("H5-establishment:regiao-") for q in h.keyword_queries)
    assert all(f'"{uf}"' in regional for ufs in BR_REGIONS.values() for uf in ufs)
    assert sum(len(ufs) for ufs in BR_REGIONS.values()) == 27
    # every dork excludes the official domains
    assert all("-site:bv.com.br" in q for h in hs for q in h.keyword_queries)
