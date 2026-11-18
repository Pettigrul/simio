"""SQLite persistence with versioned migrations.

The database is local (``<output_dir>/simio.db``) and holds campaigns, queries, candidates,
domains, entities, evidence references, assessments, decisions, errors and checkpoints.
All JSON payloads are produced from validated Pydantic models.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from simio.core.models import (
    Candidate,
    CandidateStatus,
    DiscoveryHit,
    Evidence,
    IdentityAttribution,
    QueryRecord,
)
from simio.core.state import IN_FLIGHT, RESUME_FROM, check_transition
from simio.core.state import InvalidTransition as _InvalidTransition

M = TypeVar("M", bound=BaseModel)

MIGRATIONS: list[str] = [
    # v1 - initial schema
    """
    CREATE TABLE campaigns (
        id TEXT PRIMARY KEY,
        brand TEXT NOT NULL,
        status TEXT NOT NULL,
        params_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE TABLE checkpoints (
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (campaign_id, key)
    );
    CREATE TABLE queries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        source TEXT NOT NULL,
        hypothesis_id TEXT,
        query_text TEXT NOT NULL,
        status TEXT NOT NULL,
        result_count INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        executed_at TEXT NOT NULL
    );
    CREATE TABLE domains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        registrable_domain TEXT NOT NULL,
        hostnames_json TEXT NOT NULL DEFAULT '[]',
        first_seen TEXT NOT NULL,
        UNIQUE (campaign_id, registrable_domain)
    );
    CREATE TABLE candidates (
        id TEXT PRIMARY KEY,
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        domain_id INTEGER REFERENCES domains(id),
        url TEXT NOT NULL,
        normalized_url TEXT NOT NULL,
        hostname TEXT NOT NULL,
        registrable_domain TEXT NOT NULL,
        status TEXT NOT NULL,
        priority_score REAL NOT NULL DEFAULT 0,
        priority_reasons_json TEXT NOT NULL DEFAULT '[]',
        attempts INTEGER NOT NULL DEFAULT 0,
        recollects INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (campaign_id, normalized_url)
    );
    CREATE INDEX idx_candidates_status ON candidates(campaign_id, status);
    CREATE TABLE discovery_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id TEXT NOT NULL REFERENCES candidates(id),
        query_id INTEGER REFERENCES queries(id),
        source TEXT NOT NULL,
        original_url TEXT NOT NULL,
        hit_json TEXT NOT NULL,
        discovered_at TEXT NOT NULL
    );
    CREATE TABLE state_transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id TEXT NOT NULL REFERENCES candidates(id),
        from_status TEXT NOT NULL,
        to_status TEXT NOT NULL,
        note TEXT,
        at TEXT NOT NULL
    );
    CREATE TABLE evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id TEXT NOT NULL REFERENCES candidates(id),
        attempt INTEGER NOT NULL,
        status TEXT NOT NULL,
        final_url TEXT,
        manifest_path TEXT,
        manifest_sha256 TEXT,
        data_json TEXT NOT NULL,
        collected_at TEXT NOT NULL
    );
    CREATE TABLE entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        entity_key TEXT NOT NULL,
        display_name TEXT,
        classification TEXT NOT NULL,
        data_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL,
        UNIQUE (campaign_id, entity_key)
    );
    CREATE TABLE entity_links (
        entity_id INTEGER NOT NULL REFERENCES entities(id),
        candidate_id TEXT NOT NULL REFERENCES candidates(id),
        basis TEXT NOT NULL,
        PRIMARY KEY (entity_id, candidate_id)
    );
    CREATE TABLE assessments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id TEXT NOT NULL REFERENCES candidates(id),
        kind TEXT NOT NULL,
        data_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_assessments ON assessments(candidate_id, kind);
    CREATE TABLE agent_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT NOT NULL,
        candidate_id TEXT,
        agent TEXT NOT NULL,
        decision TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        at TEXT NOT NULL
    );
    CREATE TABLE errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT NOT NULL,
        candidate_id TEXT,
        stage TEXT NOT NULL,
        error_type TEXT NOT NULL,
        message TEXT NOT NULL,
        at TEXT NOT NULL
    );
    CREATE TABLE llm_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT NOT NULL,
        agent TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        at TEXT NOT NULL
    );
    """,
    # v2: assessments tied to the evidence they were computed from (retry/resume reuse and
    # consistent reports); idempotent discovery results; single-writer campaign lease.
    """
    ALTER TABLE assessments ADD COLUMN evidence_id INTEGER;
    CREATE INDEX idx_assessments_ev ON assessments(candidate_id, kind, evidence_id);
    DELETE FROM discovery_results WHERE id NOT IN (
        SELECT MIN(id) FROM discovery_results GROUP BY candidate_id, source, original_url
    );
    CREATE UNIQUE INDEX idx_discovery_unique ON discovery_results(candidate_id, source, original_url);
    CREATE TABLE campaign_leases (
        campaign_id TEXT PRIMARY KEY REFERENCES campaigns(id),
        owner TEXT NOT NULL,
        heartbeat TEXT NOT NULL
    );
    """,
]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dumps(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        return obj.model_dump_json()
    return json.dumps(obj, ensure_ascii=False, default=str)


def candidate_id_for(campaign_id: str, normalized_url: str) -> str:
    digest = hashlib.sha256(f"{campaign_id}|{normalized_url}".encode()).hexdigest()
    return "C" + digest[:10]


def new_campaign_id() -> str:
    return datetime.now(UTC).strftime("simio-%Y%m%d-%H%M%S-") + secrets.token_hex(2)


class ConcurrentModification(_InvalidTransition):
    """Compare-and-set transition failed: the candidate was moved by another writer."""


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.migrate()

    # ------------------------------------------------------------------ infrastructure
    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def schema_version(self) -> int:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)")
        row = self.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def migrate(self) -> None:
        current = self.schema_version()
        for version, sql in enumerate(MIGRATIONS, start=1):
            if version <= current:
                continue
            with self.tx() as c:
                for stmt in (s.strip() for s in sql.split(";")):
                    if stmt:
                        c.execute(stmt)
                c.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _now()),
                )
        if current > len(MIGRATIONS):
            raise RuntimeError(f"Database schema v{current} is newer than this SIMIO build (v{len(MIGRATIONS)}).")

    # ------------------------------------------------------------------ campaigns
    def create_campaign(self, brand: str, params: dict[str, Any]) -> str:
        cid = new_campaign_id()
        now = _now()
        self.conn.execute(
            "INSERT INTO campaigns(id, brand, status, params_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (cid, brand, "RUNNING", _dumps(params), now, now),
        )
        return cid

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["params"] = json.loads(data.pop("params_json"))
        return data

    def latest_campaign_id(self) -> str | None:
        row = self.conn.execute("SELECT id FROM campaigns ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def list_campaigns(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, brand, status, created_at, finished_at FROM campaigns"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_campaign_status(self, campaign_id: str, status: str, finished: bool = False) -> None:
        self.conn.execute(
            "UPDATE campaigns SET status=?, updated_at=?, finished_at=? WHERE id=?",
            (status, _now(), _now() if finished else None, campaign_id),
        )

    def set_checkpoint(self, campaign_id: str, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO checkpoints(campaign_id, key, value_json, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(campaign_id, key) DO UPDATE SET value_json=excluded.value_json,"
            " updated_at=excluded.updated_at",
            (campaign_id, key, _dumps(value), _now()),
        )

    def get_checkpoint(self, campaign_id: str, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value_json FROM checkpoints WHERE campaign_id=? AND key=?", (campaign_id, key)
        ).fetchone()
        return json.loads(row[0]) if row else default

    # ------------------------------------------------------------------ discovery
    def record_query(self, campaign_id: str, q: QueryRecord) -> int:
        cur = self.conn.execute(
            "INSERT INTO queries(campaign_id, source, hypothesis_id, query_text, status,"
            " result_count, error, metadata_json, executed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                campaign_id,
                q.source,
                q.hypothesis_id,
                q.query,
                q.status,
                q.result_count,
                q.error,
                _dumps(q.metadata),
                q.executed_at.isoformat(),
            ),
        )
        return int(cur.lastrowid or 0)

    def list_queries(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM queries WHERE campaign_id=? ORDER BY id", (campaign_id,)).fetchall()
        return [dict(r) for r in rows]

    def _upsert_domain(self, campaign_id: str, registrable: str, hostname: str) -> int:
        row = self.conn.execute(
            "SELECT id, hostnames_json FROM domains WHERE campaign_id=? AND registrable_domain=?",
            (campaign_id, registrable),
        ).fetchone()
        if row:
            hosts = set(json.loads(row["hostnames_json"]))
            if hostname not in hosts:
                hosts.add(hostname)
                self.conn.execute(
                    "UPDATE domains SET hostnames_json=? WHERE id=?",
                    (_dumps(sorted(hosts)), row["id"]),
                )
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO domains(campaign_id, registrable_domain, hostnames_json, first_seen) VALUES (?,?,?,?)",
            (campaign_id, registrable, _dumps([hostname]), _now()),
        )
        return int(cur.lastrowid or 0)

    def upsert_candidate(
        self,
        campaign_id: str,
        *,
        url: str,
        normalized_url: str,
        hostname: str,
        registrable_domain: str,
    ) -> tuple[str, bool]:
        cid = candidate_id_for(campaign_id, normalized_url)
        exists = self.conn.execute("SELECT 1 FROM candidates WHERE id=?", (cid,)).fetchone()
        if exists:
            return cid, False
        domain_id = self._upsert_domain(campaign_id, registrable_domain, hostname)
        now = _now()
        self.conn.execute(
            "INSERT INTO candidates(id, campaign_id, domain_id, url, normalized_url, hostname,"
            " registrable_domain, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                campaign_id,
                domain_id,
                url,
                normalized_url,
                hostname,
                registrable_domain,
                CandidateStatus.DISCOVERED.value,
                now,
                now,
            ),
        )
        return cid, True

    def add_discovery_result(self, candidate_id: str, query_id: int | None, hit: DiscoveryHit) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO discovery_results(candidate_id, query_id, source, original_url, hit_json,"
            " discovered_at) VALUES (?,?,?,?,?,?)",
            (
                candidate_id,
                query_id,
                hit.source,
                hit.original_url,
                hit.model_dump_json(),
                hit.discovered_at.isoformat(),
            ),
        )

    def discovery_hits(self, candidate_id: str) -> list[DiscoveryHit]:
        rows = self.conn.execute(
            "SELECT hit_json FROM discovery_results WHERE candidate_id=? ORDER BY id",
            (candidate_id,),
        ).fetchall()
        return [DiscoveryHit.model_validate_json(r[0]) for r in rows]

    def set_priority(self, candidate_id: str, score: float, reasons: list[str]) -> None:
        self.conn.execute(
            "UPDATE candidates SET priority_score=?, priority_reasons_json=?, updated_at=? WHERE id=?",
            (score, _dumps(reasons), _now(), candidate_id),
        )

    # ------------------------------------------------------------------ candidates
    @staticmethod
    def _row_to_candidate(row: sqlite3.Row) -> Candidate:
        return Candidate(
            id=row["id"],
            campaign_id=row["campaign_id"],
            url=row["url"],
            normalized_url=row["normalized_url"],
            hostname=row["hostname"],
            registrable_domain=row["registrable_domain"],
            status=CandidateStatus(row["status"]),
            priority_score=row["priority_score"],
            priority_reasons=json.loads(row["priority_reasons_json"]),
            attempts=row["attempts"],
            recollects=row["recollects"],
            last_error=row["last_error"],
        )

    def get_candidate(self, candidate_id: str) -> Candidate:
        row = self.conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise KeyError(candidate_id)
        return self._row_to_candidate(row)

    def list_candidates(self, campaign_id: str, statuses: Iterable[CandidateStatus] | None = None) -> list[Candidate]:
        sql = "SELECT * FROM candidates WHERE campaign_id=?"
        args: list[Any] = [campaign_id]
        if statuses is not None:
            st = [s.value for s in statuses]
            if not st:
                return []
            sql += f" AND status IN ({','.join('?' * len(st))})"
            args.extend(st)
        sql += " ORDER BY priority_score DESC, created_at, id"
        return [self._row_to_candidate(r) for r in self.conn.execute(sql, args).fetchall()]

    def status_counts(self, campaign_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM candidates WHERE campaign_id=? GROUP BY status",
            (campaign_id,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def transition(
        self,
        candidate_id: str,
        target: CandidateStatus,
        note: str | None = None,
        expected: CandidateStatus | None = None,
    ) -> None:
        """Validated, atomic transition. With ``expected`` it is a compare-and-set: it fails if the
        candidate is no longer in the state the caller observed (another writer moved it)."""
        with self.tx() as c:
            row = c.execute("SELECT status FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if not row:
                raise KeyError(candidate_id)
            current = CandidateStatus(row[0])
            if expected is not None and current != expected:
                raise ConcurrentModification(
                    f"{candidate_id}: expected {expected.value}, found {current.value} (another process?)"
                )
            check_transition(current, target)
            c.execute(
                "UPDATE candidates SET status=?, updated_at=?,"
                " last_error=CASE WHEN ? IS NULL THEN last_error ELSE ? END WHERE id=?",
                (target.value, _now(), note if target == CandidateStatus.FAILED else None, note, candidate_id),
            )
            c.execute(
                "INSERT INTO state_transitions(candidate_id, from_status, to_status, note, at) VALUES (?,?,?,?,?)",
                (candidate_id, current.value, target.value, note, _now()),
            )

    def bump(self, candidate_id: str, field: str) -> int:
        if field not in ("attempts", "recollects"):
            raise ValueError(field)
        self.conn.execute(
            f"UPDATE candidates SET {field}={field}+1, updated_at=? WHERE id=?",  # noqa: S608
            (_now(), candidate_id),
        )
        row = self.conn.execute(
            f"SELECT {field} FROM candidates WHERE id=?",  # noqa: S608
            (candidate_id,),
        ).fetchone()
        return int(row[0])

    def reset_attempts(self, candidate_id: str) -> None:
        self.conn.execute("UPDATE candidates SET attempts=0 WHERE id=?", (candidate_id,))

    def set_last_error(self, candidate_id: str, message: str | None) -> None:
        self.conn.execute("UPDATE candidates SET last_error=? WHERE id=?", (message, candidate_id))

    def recover_in_flight(self, campaign_id: str, max_attempts: int | None = None) -> int:
        """Return interrupted in-flight candidates to their last stable state. A candidate that has
        already used ``max_attempts`` (e.g. it crashes or hangs the process every time) goes to
        FAILED instead, so one poison candidate cannot block every resume."""
        n = 0
        for cand in self.list_candidates(campaign_id, IN_FLIGHT):
            if max_attempts is not None and cand.attempts >= max_attempts:
                self.transition(
                    cand.id,
                    CandidateStatus.FAILED,
                    note=f"interrupted {cand.attempts} times in {cand.status.value}; marked FAILED",
                )
            else:
                self.transition(cand.id, RESUME_FROM[cand.status], note="recovered after interruption")
            n += 1
        return n

    # ------------------------------------------------------------------ campaign lease
    def acquire_lease(self, campaign_id: str, owner: str, stale_after_s: float = 120.0) -> str | None:
        """Single writer per campaign. Returns None on success, or the current live holder."""
        now = datetime.now(UTC)
        with self.tx() as c:
            row = c.execute(
                "SELECT owner, heartbeat FROM campaign_leases WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if row and row[0] != owner:
                age = (now - datetime.fromisoformat(row[1])).total_seconds()
                if age < stale_after_s:
                    return str(row[0])
            c.execute(
                "INSERT INTO campaign_leases(campaign_id, owner, heartbeat) VALUES (?,?,?)"
                " ON CONFLICT(campaign_id) DO UPDATE SET owner=excluded.owner, heartbeat=excluded.heartbeat",
                (campaign_id, owner, now.isoformat()),
            )
        return None

    def heartbeat_lease(self, campaign_id: str, owner: str) -> None:
        self.conn.execute(
            "UPDATE campaign_leases SET heartbeat=? WHERE campaign_id=? AND owner=?",
            (datetime.now(UTC).isoformat(), campaign_id, owner),
        )

    def release_lease(self, campaign_id: str, owner: str) -> None:
        self.conn.execute("DELETE FROM campaign_leases WHERE campaign_id=? AND owner=?", (campaign_id, owner))

    def transitions(self, candidate_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM state_transitions WHERE candidate_id=? ORDER BY id", (candidate_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ evidence
    def save_evidence(self, ev: Evidence) -> int:
        cur = self.conn.execute(
            "INSERT INTO evidence(candidate_id, attempt, status, final_url, manifest_path,"
            " manifest_sha256, data_json, collected_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                ev.candidate_id,
                ev.attempt,
                ev.status.value,
                ev.final_url,
                ev.manifest_path,
                ev.manifest_sha256,
                ev.model_dump_json(),
                ev.collected_at.isoformat(),
            ),
        )
        return int(cur.lastrowid or 0)

    def latest_evidence_with_id(self, candidate_id: str) -> tuple[int, Evidence] | None:
        row = self.conn.execute(
            "SELECT id, data_json FROM evidence WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        return (int(row[0]), Evidence.model_validate_json(row[1])) if row else None

    def evidence_by_id(self, evidence_id: int) -> Evidence | None:
        row = self.conn.execute("SELECT data_json FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        return Evidence.model_validate_json(row[0]) if row else None

    def latest_evidence(self, candidate_id: str) -> Evidence | None:
        row = self.conn.execute(
            "SELECT data_json FROM evidence WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        return Evidence.model_validate_json(row[0]) if row else None

    def all_evidence(self, candidate_id: str) -> list[Evidence]:
        rows = self.conn.execute(
            "SELECT data_json FROM evidence WHERE candidate_id=? ORDER BY id", (candidate_id,)
        ).fetchall()
        return [Evidence.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------------ assessments
    def save_assessment(self, candidate_id: str, kind: str, model: BaseModel, evidence_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO assessments(candidate_id, kind, data_json, created_at, evidence_id) VALUES (?,?,?,?,?)",
            (candidate_id, kind, model.model_dump_json(), _now(), evidence_id),
        )

    def get_assessment(self, candidate_id: str, kind: str, cls: type[M], evidence_id: int | None = None) -> M | None:
        """Latest assessment of a kind; with ``evidence_id``, only one computed from that evidence."""
        sql = "SELECT data_json FROM assessments WHERE candidate_id=? AND kind=?"
        args: list[Any] = [candidate_id, kind]
        if evidence_id is not None:
            sql += " AND evidence_id=?"
            args.append(evidence_id)
        row = self.conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return cls.model_validate_json(row[0]) if row else None

    def assessment_evidence_id(self, candidate_id: str, kind: str) -> int | None:
        row = self.conn.execute(
            "SELECT evidence_id FROM assessments WHERE candidate_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (candidate_id, kind),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    # ------------------------------------------------------------------ entities
    def upsert_entity(self, campaign_id: str, attribution: IdentityAttribution, basis: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM entities WHERE campaign_id=? AND entity_key=?",
            (campaign_id, attribution.entity_key),
        ).fetchone()
        payload = {
            "owner_cnpjs": attribution.owner_cnpjs,
            "registry_match": attribution.registry_match.model_dump() if attribution.registry_match else None,
        }
        if row:
            entity_id = int(row[0])
            self.conn.execute(
                "UPDATE entities SET display_name=COALESCE(?, display_name), classification=?,"
                " data_json=?, updated_at=? WHERE id=?",
                (attribution.display_name, attribution.classification.value, _dumps(payload), _now(), entity_id),
            )
        else:
            cur = self.conn.execute(
                "INSERT INTO entities(campaign_id, entity_key, display_name, classification,"
                " data_json, updated_at) VALUES (?,?,?,?,?,?)",
                (
                    campaign_id,
                    attribution.entity_key,
                    attribution.display_name,
                    attribution.classification.value,
                    _dumps(payload),
                    _now(),
                ),
            )
            entity_id = int(cur.lastrowid or 0)
        # A candidate belongs to the entity of its *latest* attribution (no stale links after recollect).
        self.conn.execute("DELETE FROM entity_links WHERE candidate_id=?", (attribution.candidate_id,))
        self.conn.execute(
            "INSERT OR REPLACE INTO entity_links(entity_id, candidate_id, basis) VALUES (?,?,?)",
            (entity_id, attribution.candidate_id, basis),
        )
        return entity_id

    def entities(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT e.*, GROUP_CONCAT(l.candidate_id) AS candidate_ids,"
            " GROUP_CONCAT(DISTINCT c.registrable_domain) AS domains"
            " FROM entities e LEFT JOIN entity_links l ON l.entity_id=e.id"
            " LEFT JOIN candidates c ON c.id=l.candidate_id"
            " WHERE e.campaign_id=? GROUP BY e.id ORDER BY e.id",
            (campaign_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ logs
    def log_decision(
        self,
        campaign_id: str,
        candidate_id: str | None,
        agent: str,
        decision: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO agent_decisions(campaign_id, candidate_id, agent, decision, detail_json, at)"
            " VALUES (?,?,?,?,?,?)",
            (campaign_id, candidate_id, agent, decision, _dumps(detail or {}), _now()),
        )

    def decisions(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_decisions WHERE campaign_id=? ORDER BY id", (campaign_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def log_error(
        self,
        campaign_id: str,
        candidate_id: str | None,
        stage: str,
        error_type: str,
        message: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO errors(campaign_id, candidate_id, stage, error_type, message, at) VALUES (?,?,?,?,?,?)",
            (campaign_id, candidate_id, stage, error_type, message[:2000], _now()),
        )

    def errors(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM errors WHERE campaign_id=? ORDER BY id", (campaign_id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ usage / budget
    def record_usage(self, campaign_id: str, agent: str, model: str, prompt: int, output: int, total: int) -> None:
        self.conn.execute(
            "INSERT INTO llm_usage(campaign_id, agent, model, prompt_tokens, output_tokens,"
            " total_tokens, at) VALUES (?,?,?,?,?,?,?)",
            (campaign_id, agent, model, prompt, output, total, _now()),
        )

    def usage_totals(self, campaign_id: str) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_tokens),0) FROM llm_usage WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
        return int(row[0]), int(row[1])
