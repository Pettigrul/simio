"""H9: discovery seeded by the registered correspondents (complements the open-web sweep)."""

from __future__ import annotations

from pathlib import Path

from simio.agents.discovery import DiscoveryAgent, build_hypotheses, coban_hypotheses, coban_search_name
from simio.brands import load_brand
from simio.core.models import QueryRecord
from simio.core.storage import Storage
from simio.security.url_policy import UrlPolicy

BV = load_brand("bv", None)


def _rec(name: str, date: str) -> dict[str, str]:
    return {"nome": name, "data_cadastro_recente": date}


RECORDS = {
    "11111111000111": _rec("ANTIGA VEICULOS LTDA", "2016-01-01"),
    "22222222000122": _rec("NOVA MOTOS LTDA - ME", "2026-09-30"),
    "33333333000133": _rec("JOAO DA SILVA 12345678901", "2026-10-01"),  # MEI with CPF: never searched
    "44444444000144": _rec("BANCO EXEMPLO SA", "2026-10-02"),  # financial institution
    "55555555000155": _rec("SOLAR FORTE ENERGIA EIRELI", "2026-08-15"),
    "66666666000166": _rec("NOVA MOTOS LTDA", "2026-07-01"),  # same searchable name as 2222...
}


def test_search_name_strips_legal_suffixes_and_skips_people_and_banks() -> None:
    assert coban_search_name("DORNELAS MOTOS LTDA - ME") == "DORNELAS MOTOS"
    assert coban_search_name("AUTO PECAS JOSE E CIA") == "AUTO PECAS JOSE"
    assert coban_search_name("VALTER LIMA DA SILVA 09893246725") is None
    assert coban_search_name("BANCO DO BRASIL SA") is None
    assert coban_search_name("ME LTDA") is None


def test_most_recent_first_rotating_and_batched() -> None:
    hyps, n = coban_hypotheses(RECORDS, BV, limit=10, batch=2)
    assert n == 3  # person, bank and duplicate name skipped
    assert [h.id for h in hyps] == ["H9-coban:22222222000122,55555555000155", "H9-coban:11111111000111"]
    assert "NOVA MOTOS" in hyps[0].grounding_prompt and "22.222.222/0001-22" in hyps[0].grounding_prompt
    assert all("-site:bv.com.br" in q for q in hyps[0].keyword_queries)
    hyps2, n2 = coban_hypotheses(RECORDS, BV, limit=1, batch=4, exclude={"22222222000122"})
    assert n2 == 1 and hyps2[0].id == "H9-coban:55555555000155"
    assert coban_hypotheses(RECORDS, BV, limit=0)[0] == []


def test_coban_hypotheses_are_added_after_the_open_web_sweep() -> None:
    class Grounding:
        name = "grounding"

    hyps, _ = coban_hypotheses(RECORDS, BV, limit=2, batch=2)
    agent = DiscoveryAgent(BV, [], UrlPolicy(), max_queries=500, extra_hypotheses=hyps)
    plan = agent.plan([Grounding()])  # type: ignore[list-item]
    ids = [p.hypothesis.id for p in plan]
    assert len(ids) == len(build_hypotheses(BV)) + 1
    assert ids[-1].startswith("H9-coban:") and not any(i.startswith("H9") for i in ids[:-1])


def test_storage_remembers_searched_correspondents_across_campaigns(tmp_path: Path) -> None:
    st = Storage(tmp_path / "s.db")
    c1 = st.create_campaign("bv", {})
    c2 = st.create_campaign("bv", {})
    st.record_query(c1, QueryRecord(source="grounding", query="q", hypothesis_id="H9-coban:1,2", status="OK"))
    st.record_query(c1, QueryRecord(source="grounding", query="q", hypothesis_id="H9-coban:3", status="ERROR"))
    st.record_query(c2, QueryRecord(source="grounding", query="q", hypothesis_id="H9-coban:4", status="EMPTY"))
    assert st.searched_coban_cnpjs() == {"1", "2", "4"}  # failed searches are retried later
    assert st.searched_coban_cnpjs(exclude_campaign=c2) == {"1", "2"}  # resume keeps its own selection
    st.close()
