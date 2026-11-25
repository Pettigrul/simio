from __future__ import annotations

import sqlite3

import pytest

from simio.core.models import CandidateStatus as S
from simio.core.models import QueryRecord
from simio.core.state import InvalidTransition, check_transition
from simio.core.storage import MIGRATIONS, Storage
from simio.providers.llm import Budget, BudgetExceeded, Usage


def _cand(storage: Storage, cid: str, url: str = "https://a.example/") -> str:
    c, created = storage.upsert_candidate(
        cid, url=url, normalized_url=url, hostname="a.example", registrable_domain="a.example"
    )
    assert created
    return c


def test_migrations_are_idempotent(settings) -> None:
    s1 = Storage(settings.db_path)
    assert s1.schema_version() == len(MIGRATIONS)
    s1.close()
    s2 = Storage(settings.db_path)
    assert s2.schema_version() == len(MIGRATIONS)
    s2.close()


def test_newer_schema_is_refused(settings) -> None:
    Storage(settings.db_path).close()
    con = sqlite3.connect(settings.db_path)
    con.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (999, 'x')")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="newer"):
        Storage(settings.db_path)


def test_state_machine_rules() -> None:
    check_transition(S.QUEUED, S.COLLECTING)
    check_transition(S.REVIEWING, S.QUEUED)
    with pytest.raises(InvalidTransition):
        check_transition(S.DISCOVERED, S.COMPLETED)
    with pytest.raises(InvalidTransition):
        check_transition(S.COMPLETED, S.QUEUED)


def test_candidate_dedup_and_transitions(storage: Storage) -> None:
    camp = storage.create_campaign("bv", {"limit": 1})
    cid = _cand(storage, camp)
    again, created = storage.upsert_candidate(
        camp,
        url="https://a.example/",
        normalized_url="https://a.example/",
        hostname="a.example",
        registrable_domain="a.example",
    )
    assert again == cid and not created
    storage.transition(cid, S.QUEUED)
    storage.transition(cid, S.COLLECTING)
    with pytest.raises(InvalidTransition):
        storage.transition(cid, S.COMPLETED)
    assert storage.get_candidate(cid).status == S.COLLECTING
    assert [t["to_status"] for t in storage.transitions(cid)] == ["QUEUED", "COLLECTING"]


def test_recover_in_flight(storage: Storage) -> None:
    camp = storage.create_campaign("bv", {})
    a = _cand(storage, camp, "https://a.example/")
    b = _cand(storage, camp, "https://b.example/")
    for t in (S.QUEUED, S.COLLECTING):
        storage.transition(a, t)
    for t in (S.QUEUED, S.COLLECTING, S.COLLECTED, S.ANALYZING, S.REVIEWING):
        storage.transition(b, t)
    assert storage.recover_in_flight(camp) == 2
    assert storage.get_candidate(a).status == S.QUEUED
    assert storage.get_candidate(b).status == S.COLLECTED


def test_checkpoints_queries_and_errors(storage: Storage) -> None:
    camp = storage.create_campaign("bv", {})
    storage.set_checkpoint(camp, "discovery_done", True)
    assert storage.get_checkpoint(camp, "discovery_done") is True
    assert storage.get_checkpoint(camp, "missing", "d") == "d"
    qid = storage.record_query(camp, QueryRecord(source="brave", query="q", status="EMPTY"))
    assert qid > 0 and storage.list_queries(camp)[0]["status"] == "EMPTY"
    storage.log_error(camp, None, "discovery", "X", "y" * 5000)
    assert len(storage.errors(camp)[0]["message"]) == 2000


async def test_budget_persists_and_blocks(storage: Storage) -> None:
    camp = storage.create_campaign("bv", {})
    b = Budget(storage, camp, max_requests=2, max_tokens=1000)
    await b.reserve()
    await b.commit("a", "m", Usage(10, 5, 15))
    await b.reserve()
    await b.commit("a", "m", Usage(10, 5, 15))
    with pytest.raises(BudgetExceeded):
        await b.reserve()
    # A new Budget object (e.g. after resume) sees persisted usage.
    b2 = Budget(storage, camp, max_requests=10, max_tokens=20)
    with pytest.raises(BudgetExceeded, match="token"):
        await b2.reserve()
