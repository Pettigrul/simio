"""SQLite persistence with versioned migrations.

The database is local (``<output_dir>/simio.db``) and holds campaigns, queries, candidates,
domains, entities, evidence references, assessments, decisions, errors and checkpoints.
All JSON payloads are produced from validated Pydantic models.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from simio.nucleo import caminhos
from simio.nucleo.estado import EM_ANDAMENTO, RETOMAR_DE, verificar_transicao
from simio.nucleo.estado import TransicaoInvalida as _TransicaoInvalida
from simio.nucleo.modelos import (
    AtribuicaoIdentidade,
    Candidata,
    Evidencia,
    RegistroConsulta,
    ResultadoDescoberta,
    StatusCandidata,
)
from simio.nucleo.traducao_legado import migrar_v3

M = TypeVar("M", bound=BaseModel)

MIGRACOES: list[str | Callable[[sqlite3.Connection], None]] = [
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
    # v3: Portuguese table/column/index names; every stored payload and code translated
    # (simio.nucleo.traducao_legado). Runs with foreign keys off, checked afterwards.
    migrar_v3,
]

# Latest schema, used to create a new database directly (ends in the same state as v1 -> v3).
ESQUEMA_ATUAL = """
    CREATE TABLE campanhas (
        id TEXT PRIMARY KEY,
        marca TEXT NOT NULL,
        status TEXT NOT NULL,
        parametros_json TEXT NOT NULL,
        criado_em TEXT NOT NULL,
        atualizado_em TEXT NOT NULL,
        finalizado_em TEXT
    );
    CREATE TABLE checkpoints (
        id_campanha TEXT NOT NULL REFERENCES campanhas(id),
        chave TEXT NOT NULL,
        valor_json TEXT NOT NULL,
        atualizado_em TEXT NOT NULL,
        PRIMARY KEY (id_campanha, chave)
    );
    CREATE TABLE consultas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_campanha TEXT NOT NULL REFERENCES campanhas(id),
        fonte TEXT NOT NULL,
        id_hipotese TEXT,
        texto_consulta TEXT NOT NULL,
        status TEXT NOT NULL,
        qtd_resultados INTEGER NOT NULL DEFAULT 0,
        erro TEXT,
        metadados_json TEXT NOT NULL DEFAULT '{}',
        executada_em TEXT NOT NULL
    );
    CREATE TABLE dominios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_campanha TEXT NOT NULL REFERENCES campanhas(id),
        dominio_registravel TEXT NOT NULL,
        hostnames_json TEXT NOT NULL DEFAULT '[]',
        visto_em TEXT NOT NULL,
        UNIQUE (id_campanha, dominio_registravel)
    );
    CREATE TABLE candidatas (
        id TEXT PRIMARY KEY,
        id_campanha TEXT NOT NULL REFERENCES campanhas(id),
        id_dominio INTEGER REFERENCES dominios(id),
        url TEXT NOT NULL,
        url_normalizada TEXT NOT NULL,
        hostname TEXT NOT NULL,
        dominio_registravel TEXT NOT NULL,
        status TEXT NOT NULL,
        pontuacao_prioridade REAL NOT NULL DEFAULT 0,
        motivos_prioridade_json TEXT NOT NULL DEFAULT '[]',
        tentativas INTEGER NOT NULL DEFAULT 0,
        recoletas INTEGER NOT NULL DEFAULT 0,
        ultimo_erro TEXT,
        criado_em TEXT NOT NULL,
        atualizado_em TEXT NOT NULL,
        UNIQUE (id_campanha, url_normalizada)
    );
    CREATE TABLE resultados_descoberta (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_candidata TEXT NOT NULL REFERENCES candidatas(id),
        id_consulta INTEGER REFERENCES consultas(id),
        fonte TEXT NOT NULL,
        url_original TEXT NOT NULL,
        resultado_json TEXT NOT NULL,
        descoberto_em TEXT NOT NULL
    );
    CREATE TABLE transicoes_estado (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_candidata TEXT NOT NULL REFERENCES candidatas(id),
        status_origem TEXT NOT NULL,
        status_destino TEXT NOT NULL,
        nota TEXT,
        em TEXT NOT NULL
    );
    CREATE TABLE evidencias (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_candidata TEXT NOT NULL REFERENCES candidatas(id),
        tentativa INTEGER NOT NULL,
        status TEXT NOT NULL,
        url_final TEXT,
        caminho_manifesto TEXT,
        sha256_manifesto TEXT,
        dados_json TEXT NOT NULL,
        coletada_em TEXT NOT NULL
    );
    CREATE TABLE entidades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_campanha TEXT NOT NULL REFERENCES campanhas(id),
        chave_entidade TEXT NOT NULL,
        nome_exibicao TEXT,
        classificacao TEXT NOT NULL,
        dados_json TEXT NOT NULL DEFAULT '{}',
        atualizado_em TEXT NOT NULL,
        UNIQUE (id_campanha, chave_entidade)
    );
    CREATE TABLE vinculos_entidade (
        id_entidade INTEGER NOT NULL REFERENCES entidades(id),
        id_candidata TEXT NOT NULL REFERENCES candidatas(id),
        fundamento TEXT NOT NULL,
        PRIMARY KEY (id_entidade, id_candidata)
    );
    CREATE TABLE avaliacoes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_candidata TEXT NOT NULL REFERENCES candidatas(id),
        tipo TEXT NOT NULL,
        dados_json TEXT NOT NULL,
        criado_em TEXT NOT NULL
    , id_evidencia INTEGER);
    CREATE TABLE decisoes_agentes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_campanha TEXT NOT NULL,
        id_candidata TEXT,
        agente TEXT NOT NULL,
        decisao TEXT NOT NULL,
        detalhe_json TEXT NOT NULL DEFAULT '{}',
        em TEXT NOT NULL
    );
    CREATE TABLE erros (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_campanha TEXT NOT NULL,
        id_candidata TEXT,
        etapa TEXT NOT NULL,
        tipo_erro TEXT NOT NULL,
        mensagem TEXT NOT NULL,
        em TEXT NOT NULL
    );
    CREATE TABLE uso_llm (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_campanha TEXT NOT NULL,
        agente TEXT NOT NULL,
        modelo TEXT NOT NULL,
        tokens_prompt INTEGER NOT NULL DEFAULT 0,
        tokens_saida INTEGER NOT NULL DEFAULT 0,
        tokens_total INTEGER NOT NULL DEFAULT 0,
        em TEXT NOT NULL
    );
    CREATE TABLE travas_campanha (
        id_campanha TEXT PRIMARY KEY REFERENCES campanhas(id),
        dono TEXT NOT NULL,
        pulso TEXT NOT NULL
    );
    CREATE INDEX idx_candidatas_status ON candidatas(id_campanha, status);
    CREATE INDEX idx_avaliacoes ON avaliacoes(id_candidata, tipo);
    CREATE INDEX idx_avaliacoes_evid ON avaliacoes(id_candidata, tipo, id_evidencia);
    CREATE UNIQUE INDEX idx_descoberta_unico ON resultados_descoberta(id_candidata, fonte, url_original);
"""


def _agora() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _serializar(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        return obj.model_dump_json()
    return json.dumps(obj, ensure_ascii=False, default=str)


def id_candidata_para(id_campanha: str, url_normalizada: str) -> str:
    hash_hex = hashlib.sha256(f"{id_campanha}|{url_normalizada}".encode()).hexdigest()
    return "C" + hash_hex[:10]


class ModificacaoConcorrente(_TransicaoInvalida):
    """Compare-and-set transition failed: the candidate was moved by another writer."""


class Armazenamento:
    def __init__(self, caminho: Path) -> None:
        self.caminho = caminho
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(caminho, timeout=30, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.execute("PRAGMA busy_timeout=30000")
        self.migrar()

    # ------------------------------------------------------------------ infrastructure
    def fechar(self) -> None:
        self.con.close()

    @contextmanager
    def transacao(self) -> Iterator[sqlite3.Connection]:
        self.con.execute("BEGIN IMMEDIATE")
        try:
            yield self.con
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")

    def versao_esquema(self) -> int:
        self.con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)")
        linha = self.con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(linha[0] or 0)

    def migrar(self) -> None:
        atual = self.versao_esquema()
        if atual > len(MIGRACOES):
            raise RuntimeError(f"Database schema v{atual} is newer than this SIMIO build (v{len(MIGRACOES)}).")
        if atual == 0:
            self._criar_esquema_atual()
            return
        for versao, passo in enumerate(MIGRACOES, start=1):
            if versao <= atual:
                continue
            # PRAGMA foreign_keys is a no-op inside a transaction: switch it off around the step
            # (tables are renamed/rewritten) and verify the references once it is committed.
            self.con.execute("PRAGMA foreign_keys=OFF")
            try:
                with self.transacao() as c:
                    if callable(passo):
                        passo(c)
                    else:
                        for stmt in (s.strip() for s in passo.split(";")):
                            if stmt:
                                c.execute(stmt)
                    c.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (versao, _agora()),
                    )
            finally:
                self.con.execute("PRAGMA foreign_keys=ON")
            violacoes = self.con.execute("PRAGMA foreign_key_check").fetchall()
            if violacoes:
                raise RuntimeError(
                    f"Migration v{versao} left {len(violacoes)} foreign key violation(s): {violacoes[:5]}"
                )

    def _criar_esquema_atual(self) -> None:
        """A new database is created directly in the latest schema (same end state as v1..v3)."""
        with self.transacao() as c:
            for stmt in (s.strip() for s in ESQUEMA_ATUAL.split(";")):
                if stmt:
                    c.execute(stmt)
            agora = _agora()
            for versao in range(1, len(MIGRACOES) + 1):
                c.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (versao, agora))

    # ------------------------------------------------------------------ campaigns
    def criar_campanha(self, marca: str, parametros: dict[str, Any]) -> str:
        """Readable id ``AAAA-MM-DD_HHhMM_<marca>_<modo>`` (Brasília time); ``-2``, ``-3``... when taken."""
        momento = datetime.now(UTC)
        agora = momento.isoformat(timespec="seconds")
        base = caminhos.id_campanha(momento, marca, parametros.get("foco"))
        pasta_campanhas = caminhos.dir_campanhas(self.caminho.parent)
        n = 1
        while True:
            cid = caminhos.com_sufixo(base, n)
            n += 1
            if (pasta_campanhas / cid).exists():
                continue
            try:
                self.con.execute(
                    "INSERT INTO campanhas(id, marca, status, parametros_json, criado_em, atualizado_em)"
                    " VALUES (?,?,?,?,?,?)",
                    (cid, marca, "EM_EXECUCAO", _serializar(parametros), agora, agora),
                )
            except sqlite3.IntegrityError:
                if self.con.execute("SELECT 1 FROM campanhas WHERE id=?", (cid,)).fetchone() is None:
                    raise
                continue
            return cid

    def resolver_id_campanha(self, id_ou_anterior: str) -> str | None:
        """Current id of a campaign, also when given the id it had before the folder migration."""
        if self.con.execute("SELECT 1 FROM campanhas WHERE id=?", (id_ou_anterior,)).fetchone():
            return id_ou_anterior
        linha = self.con.execute(
            "SELECT id FROM campanhas WHERE json_extract(parametros_json, '$.id_anterior')=?", (id_ou_anterior,)
        ).fetchone()
        return str(linha[0]) if linha else None

    def obter_campanha(self, id_campanha: str) -> dict[str, Any] | None:
        linha = self.con.execute("SELECT * FROM campanhas WHERE id=?", (id_campanha,)).fetchone()
        if not linha:
            return None
        dados = dict(linha)
        dados["parametros"] = json.loads(dados.pop("parametros_json"))
        return dados

    def id_campanha_mais_recente(self) -> str | None:
        linha = self.con.execute("SELECT id FROM campanhas ORDER BY criado_em DESC, rowid DESC LIMIT 1").fetchone()
        return linha[0] if linha else None

    def listar_campanhas(self, limite: int = 20) -> list[dict[str, Any]]:
        linhas = self.con.execute(
            "SELECT id, marca, status, criado_em, finalizado_em FROM campanhas"
            " ORDER BY criado_em DESC, rowid DESC LIMIT ?",
            (limite,),
        ).fetchall()
        return [dict(r) for r in linhas]

    def definir_status_campanha(self, id_campanha: str, status: str, finalizada: bool = False) -> None:
        self.con.execute(
            "UPDATE campanhas SET status=?, atualizado_em=?, finalizado_em=? WHERE id=?",
            (status, _agora(), _agora() if finalizada else None, id_campanha),
        )

    def salvar_checkpoint(self, id_campanha: str, chave: str, valor: Any) -> None:
        self.con.execute(
            "INSERT INTO checkpoints(id_campanha, chave, valor_json, atualizado_em) VALUES (?,?,?,?)"
            " ON CONFLICT(id_campanha, chave) DO UPDATE SET valor_json=excluded.valor_json,"
            " atualizado_em=excluded.atualizado_em",
            (id_campanha, chave, _serializar(valor), _agora()),
        )

    def obter_checkpoint(self, id_campanha: str, chave: str, padrao: Any = None) -> Any:
        linha = self.con.execute(
            "SELECT valor_json FROM checkpoints WHERE id_campanha=? AND chave=?", (id_campanha, chave)
        ).fetchone()
        return json.loads(linha[0]) if linha else padrao

    # ------------------------------------------------------------------ discovery
    def registrar_consulta(self, id_campanha: str, q: RegistroConsulta) -> int:
        cur = self.con.execute(
            "INSERT INTO consultas(id_campanha, fonte, id_hipotese, texto_consulta, status,"
            " qtd_resultados, erro, metadados_json, executada_em) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                id_campanha,
                q.fonte,
                q.id_hipotese,
                q.consulta,
                q.status,
                q.qtd_resultados,
                q.erro,
                _serializar(q.metadados),
                q.executada_em.isoformat(),
            ),
        )
        return int(cur.lastrowid or 0)

    def cnpjs_coban_pesquisados(self, excluir_campanha: str | None = None) -> set[str]:
        """CNPJs already covered by a successful COBAN-seeded (H9) search in other campaigns."""
        linhas = self.con.execute(
            "SELECT id_hipotese FROM consultas WHERE id_hipotese LIKE 'H9-coban:%'"
            " AND status IN ('OK','VAZIA') AND id_campanha IS NOT ?",
            (excluir_campanha,),
        ).fetchall()
        saida: set[str] = set()
        for r in linhas:
            saida.update(c for c in r["id_hipotese"].split(":", 1)[1].split(",") if c)
        return saida

    def listar_consultas(self, id_campanha: str) -> list[dict[str, Any]]:
        linhas = self.con.execute("SELECT * FROM consultas WHERE id_campanha=? ORDER BY id", (id_campanha,)).fetchall()
        return [dict(r) for r in linhas]

    def _upsert_dominio(self, id_campanha: str, registravel: str, hostname: str) -> int:
        linha = self.con.execute(
            "SELECT id, hostnames_json FROM dominios WHERE id_campanha=? AND dominio_registravel=?",
            (id_campanha, registravel),
        ).fetchone()
        if linha:
            hosts = set(json.loads(linha["hostnames_json"]))
            if hostname not in hosts:
                hosts.add(hostname)
                self.con.execute(
                    "UPDATE dominios SET hostnames_json=? WHERE id=?",
                    (_serializar(sorted(hosts)), linha["id"]),
                )
            return int(linha["id"])
        cur = self.con.execute(
            "INSERT INTO dominios(id_campanha, dominio_registravel, hostnames_json, visto_em) VALUES (?,?,?,?)",
            (id_campanha, registravel, _serializar([hostname]), _agora()),
        )
        return int(cur.lastrowid or 0)

    def upsert_candidata(
        self,
        id_campanha: str,
        *,
        url: str,
        url_normalizada: str,
        hostname: str,
        dominio_registravel: str,
    ) -> tuple[str, bool]:
        # Look up by URL, not by the derived id: migrated campaigns keep their candidates' ids but not
        # the campaign id they were hashed from.
        existente = self.con.execute(
            "SELECT id FROM candidatas WHERE id_campanha=? AND url_normalizada=?", (id_campanha, url_normalizada)
        ).fetchone()
        if existente:
            return str(existente[0]), False
        cid = id_candidata_para(id_campanha, url_normalizada)
        id_dominio = self._upsert_dominio(id_campanha, dominio_registravel, hostname)
        agora = _agora()
        self.con.execute(
            "INSERT INTO candidatas(id, id_campanha, id_dominio, url, url_normalizada, hostname,"
            " dominio_registravel, status, criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                id_campanha,
                id_dominio,
                url,
                url_normalizada,
                hostname,
                dominio_registravel,
                StatusCandidata.DESCOBERTA.value,
                agora,
                agora,
            ),
        )
        return cid, True

    def adicionar_resultado_descoberta(
        self, id_candidata: str, id_consulta: int | None, resultado: ResultadoDescoberta
    ) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO resultados_descoberta(id_candidata, id_consulta, fonte, url_original,"
            " resultado_json, descoberto_em) VALUES (?,?,?,?,?,?)",
            (
                id_candidata,
                id_consulta,
                resultado.fonte,
                resultado.url_original,
                resultado.model_dump_json(),
                resultado.descoberto_em.isoformat(),
            ),
        )

    def resultados_descoberta(self, id_candidata: str) -> list[ResultadoDescoberta]:
        linhas = self.con.execute(
            "SELECT resultado_json FROM resultados_descoberta WHERE id_candidata=? ORDER BY id",
            (id_candidata,),
        ).fetchall()
        return [ResultadoDescoberta.model_validate_json(r[0]) for r in linhas]

    def definir_prioridade(self, id_candidata: str, pontuacao: float, motivos: list[str]) -> None:
        self.con.execute(
            "UPDATE candidatas SET pontuacao_prioridade=?, motivos_prioridade_json=?, atualizado_em=? WHERE id=?",
            (pontuacao, _serializar(motivos), _agora(), id_candidata),
        )

    # ------------------------------------------------------------------ candidates
    @staticmethod
    def _linha_para_candidata(linha: sqlite3.Row) -> Candidata:
        return Candidata(
            id=linha["id"],
            id_campanha=linha["id_campanha"],
            url=linha["url"],
            url_normalizada=linha["url_normalizada"],
            hostname=linha["hostname"],
            dominio_registravel=linha["dominio_registravel"],
            status=StatusCandidata(linha["status"]),
            pontuacao_prioridade=linha["pontuacao_prioridade"],
            motivos_prioridade=json.loads(linha["motivos_prioridade_json"]),
            tentativas=linha["tentativas"],
            recoletas=linha["recoletas"],
            ultimo_erro=linha["ultimo_erro"],
        )

    def obter_candidata(self, id_candidata: str) -> Candidata:
        linha = self.con.execute("SELECT * FROM candidatas WHERE id=?", (id_candidata,)).fetchone()
        if not linha:
            raise KeyError(id_candidata)
        return self._linha_para_candidata(linha)

    def listar_candidatas(
        self, id_campanha: str, lista_status: Iterable[StatusCandidata] | None = None
    ) -> list[Candidata]:
        sql = "SELECT * FROM candidatas WHERE id_campanha=?"
        args: list[Any] = [id_campanha]
        if lista_status is not None:
            st = [s.value for s in lista_status]
            if not st:
                return []
            sql += f" AND status IN ({','.join('?' * len(st))})"
            args.extend(st)
        sql += " ORDER BY pontuacao_prioridade DESC, criado_em, id"
        return [self._linha_para_candidata(r) for r in self.con.execute(sql, args).fetchall()]

    def contagem_por_status(self, id_campanha: str) -> dict[str, int]:
        linhas = self.con.execute(
            "SELECT status, COUNT(*) FROM candidatas WHERE id_campanha=? GROUP BY status",
            (id_campanha,),
        ).fetchall()
        return {r[0]: r[1] for r in linhas}

    def transicionar(
        self,
        id_candidata: str,
        alvo: StatusCandidata,
        nota: str | None = None,
        esperado: StatusCandidata | None = None,
    ) -> None:
        """Validated, atomic transition. With ``esperado`` it is a compare-and-set: it fails if the
        candidate is no longer in the state the caller observed (another writer moved it)."""
        with self.transacao() as c:
            linha = c.execute("SELECT status FROM candidatas WHERE id=?", (id_candidata,)).fetchone()
            if not linha:
                raise KeyError(id_candidata)
            atual = StatusCandidata(linha[0])
            if esperado is not None and atual != esperado:
                raise ModificacaoConcorrente(
                    f"{id_candidata}: expected {esperado.value}, found {atual.value} (another process?)"
                )
            verificar_transicao(atual, alvo)
            c.execute(
                "UPDATE candidatas SET status=?, atualizado_em=?,"
                " ultimo_erro=CASE WHEN ? IS NULL THEN ultimo_erro ELSE ? END WHERE id=?",
                (alvo.value, _agora(), nota if alvo == StatusCandidata.FALHOU else None, nota, id_candidata),
            )
            c.execute(
                "INSERT INTO transicoes_estado(id_candidata, status_origem, status_destino, nota, em)"
                " VALUES (?,?,?,?,?)",
                (id_candidata, atual.value, alvo.value, nota, _agora()),
            )

    def incrementar(self, id_candidata: str, campo: str) -> int:
        if campo not in ("tentativas", "recoletas"):
            raise ValueError(campo)
        self.con.execute(
            f"UPDATE candidatas SET {campo}={campo}+1, atualizado_em=? WHERE id=?",  # noqa: S608
            (_agora(), id_candidata),
        )
        linha = self.con.execute(
            f"SELECT {campo} FROM candidatas WHERE id=?",  # noqa: S608
            (id_candidata,),
        ).fetchone()
        return int(linha[0])

    def zerar_tentativas(self, id_candidata: str) -> None:
        self.con.execute("UPDATE candidatas SET tentativas=0 WHERE id=?", (id_candidata,))

    def definir_ultimo_erro(self, id_candidata: str, mensagem: str | None) -> None:
        self.con.execute("UPDATE candidatas SET ultimo_erro=? WHERE id=?", (mensagem, id_candidata))

    def recuperar_em_andamento(self, id_campanha: str, max_tentativas: int | None = None) -> int:
        """Return interrupted in-flight candidates to their last stable state. A candidate that has
        already used ``max_tentativas`` (e.g. it crashes or hangs the process every time) goes to
        FALHOU instead, so one poison candidate cannot block every resume."""
        n = 0
        for cand in self.listar_candidatas(id_campanha, EM_ANDAMENTO):
            if max_tentativas is not None and cand.tentativas >= max_tentativas:
                self.transicionar(
                    cand.id,
                    StatusCandidata.FALHOU,
                    nota=f"interrupted {cand.tentativas} times in {cand.status.value}; marked FALHOU",
                )
            else:
                self.transicionar(cand.id, RETOMAR_DE[cand.status], nota="recovered after interruption")
            n += 1
        return n

    # ------------------------------------------------------------------ campaign lease
    def adquirir_trava(self, id_campanha: str, dono: str, vencida_apos_s: float = 120.0) -> str | None:
        """Single writer per campaign. Returns None on success, or the current live holder."""
        agora = datetime.now(UTC)
        with self.transacao() as c:
            linha = c.execute(
                "SELECT dono, pulso FROM travas_campanha WHERE id_campanha=?", (id_campanha,)
            ).fetchone()
            if linha and linha[0] != dono:
                idade = (agora - datetime.fromisoformat(linha[1])).total_seconds()
                if idade < vencida_apos_s:
                    return str(linha[0])
            c.execute(
                "INSERT INTO travas_campanha(id_campanha, dono, pulso) VALUES (?,?,?)"
                " ON CONFLICT(id_campanha) DO UPDATE SET dono=excluded.dono, pulso=excluded.pulso",
                (id_campanha, dono, agora.isoformat()),
            )
        return None

    def renovar_trava(self, id_campanha: str, dono: str) -> None:
        self.con.execute(
            "UPDATE travas_campanha SET pulso=? WHERE id_campanha=? AND dono=?",
            (datetime.now(UTC).isoformat(), id_campanha, dono),
        )

    def liberar_trava(self, id_campanha: str, dono: str) -> None:
        self.con.execute("DELETE FROM travas_campanha WHERE id_campanha=? AND dono=?", (id_campanha, dono))

    def transicoes(self, id_candidata: str) -> list[dict[str, Any]]:
        linhas = self.con.execute(
            "SELECT * FROM transicoes_estado WHERE id_candidata=? ORDER BY id", (id_candidata,)
        ).fetchall()
        return [dict(r) for r in linhas]

    # ------------------------------------------------------------------ evidence
    def salvar_evidencia(self, ev: Evidencia) -> int:
        cur = self.con.execute(
            "INSERT INTO evidencias(id_candidata, tentativa, status, url_final, caminho_manifesto,"
            " sha256_manifesto, dados_json, coletada_em) VALUES (?,?,?,?,?,?,?,?)",
            (
                ev.id_candidata,
                ev.tentativa,
                ev.status.value,
                ev.url_final,
                ev.caminho_manifesto,
                ev.sha256_manifesto,
                ev.model_dump_json(),
                ev.coletada_em.isoformat(),
            ),
        )
        return int(cur.lastrowid or 0)

    def evidencia_mais_recente_com_id(self, id_candidata: str) -> tuple[int, Evidencia] | None:
        linha = self.con.execute(
            "SELECT id, dados_json FROM evidencias WHERE id_candidata=? ORDER BY id DESC LIMIT 1",
            (id_candidata,),
        ).fetchone()
        return (int(linha[0]), Evidencia.model_validate_json(linha[1])) if linha else None

    def evidencia_por_id(self, id_evidencia: int) -> Evidencia | None:
        linha = self.con.execute("SELECT dados_json FROM evidencias WHERE id=?", (id_evidencia,)).fetchone()
        return Evidencia.model_validate_json(linha[0]) if linha else None

    def evidencia_mais_recente(self, id_candidata: str) -> Evidencia | None:
        linha = self.con.execute(
            "SELECT dados_json FROM evidencias WHERE id_candidata=? ORDER BY id DESC LIMIT 1",
            (id_candidata,),
        ).fetchone()
        return Evidencia.model_validate_json(linha[0]) if linha else None

    def todas_evidencias(self, id_candidata: str) -> list[Evidencia]:
        linhas = self.con.execute(
            "SELECT dados_json FROM evidencias WHERE id_candidata=? ORDER BY id", (id_candidata,)
        ).fetchall()
        return [Evidencia.model_validate_json(r[0]) for r in linhas]

    # ------------------------------------------------------------------ assessments
    def salvar_avaliacao(
        self, id_candidata: str, tipo: str, modelo: BaseModel, id_evidencia: int | None = None
    ) -> None:
        self.con.execute(
            "INSERT INTO avaliacoes(id_candidata, tipo, dados_json, criado_em, id_evidencia) VALUES (?,?,?,?,?)",
            (id_candidata, tipo, modelo.model_dump_json(), _agora(), id_evidencia),
        )

    def obter_avaliacao(self, id_candidata: str, tipo: str, cls: type[M], id_evidencia: int | None = None) -> M | None:
        """Latest assessment of a kind; with ``id_evidencia``, only one computed from that evidence."""
        sql = "SELECT dados_json FROM avaliacoes WHERE id_candidata=? AND tipo=?"
        args: list[Any] = [id_candidata, tipo]
        if id_evidencia is not None:
            sql += " AND id_evidencia=?"
            args.append(id_evidencia)
        linha = self.con.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return cls.model_validate_json(linha[0]) if linha else None

    def id_evidencia_avaliacao(self, id_candidata: str, tipo: str) -> int | None:
        linha = self.con.execute(
            "SELECT id_evidencia FROM avaliacoes WHERE id_candidata=? AND tipo=? ORDER BY id DESC LIMIT 1",
            (id_candidata, tipo),
        ).fetchone()
        return int(linha[0]) if linha and linha[0] is not None else None

    # ------------------------------------------------------------------ entities
    def upsert_entidade(self, id_campanha: str, atribuicao: AtribuicaoIdentidade, fundamento: str) -> int:
        linha = self.con.execute(
            "SELECT id FROM entidades WHERE id_campanha=? AND chave_entidade=?",
            (id_campanha, atribuicao.chave_entidade),
        ).fetchone()
        payload = {
            "cnpjs_responsavel": atribuicao.cnpjs_responsavel,
            "correspondencia_cadastro": atribuicao.correspondencia_cadastro.model_dump()
            if atribuicao.correspondencia_cadastro
            else None,
        }
        if linha:
            id_entidade = int(linha[0])
            self.con.execute(
                "UPDATE entidades SET nome_exibicao=COALESCE(?, nome_exibicao), classificacao=?,"
                " dados_json=?, atualizado_em=? WHERE id=?",
                (atribuicao.nome_exibicao, atribuicao.classificacao.value, _serializar(payload), _agora(), id_entidade),
            )
        else:
            cur = self.con.execute(
                "INSERT INTO entidades(id_campanha, chave_entidade, nome_exibicao, classificacao,"
                " dados_json, atualizado_em) VALUES (?,?,?,?,?,?)",
                (
                    id_campanha,
                    atribuicao.chave_entidade,
                    atribuicao.nome_exibicao,
                    atribuicao.classificacao.value,
                    _serializar(payload),
                    _agora(),
                ),
            )
            id_entidade = int(cur.lastrowid or 0)
        # A candidate belongs to the entity of its *latest* attribution (no stale links after recollect).
        self.con.execute("DELETE FROM vinculos_entidade WHERE id_candidata=?", (atribuicao.id_candidata,))
        self.con.execute(
            "INSERT OR REPLACE INTO vinculos_entidade(id_entidade, id_candidata, fundamento) VALUES (?,?,?)",
            (id_entidade, atribuicao.id_candidata, fundamento),
        )
        return id_entidade

    def entidades(self, id_campanha: str) -> list[dict[str, Any]]:
        linhas = self.con.execute(
            "SELECT e.*, GROUP_CONCAT(l.id_candidata) AS ids_candidatas,"
            " GROUP_CONCAT(DISTINCT c.dominio_registravel) AS dominios"
            " FROM entidades e LEFT JOIN vinculos_entidade l ON l.id_entidade=e.id"
            " LEFT JOIN candidatas c ON c.id=l.id_candidata"
            " WHERE e.id_campanha=? GROUP BY e.id ORDER BY e.id",
            (id_campanha,),
        ).fetchall()
        return [dict(r) for r in linhas]

    # ------------------------------------------------------------------ logs
    def registrar_decisao(
        self,
        id_campanha: str,
        id_candidata: str | None,
        agente: str,
        decisao: str,
        detalhe: dict[str, Any] | None = None,
    ) -> None:
        self.con.execute(
            "INSERT INTO decisoes_agentes(id_campanha, id_candidata, agente, decisao, detalhe_json, em)"
            " VALUES (?,?,?,?,?,?)",
            (id_campanha, id_candidata, agente, decisao, _serializar(detalhe or {}), _agora()),
        )

    def decisoes(self, id_campanha: str) -> list[dict[str, Any]]:
        linhas = self.con.execute(
            "SELECT * FROM decisoes_agentes WHERE id_campanha=? ORDER BY id", (id_campanha,)
        ).fetchall()
        return [dict(r) for r in linhas]

    def registrar_erro(
        self,
        id_campanha: str,
        id_candidata: str | None,
        etapa: str,
        tipo_erro: str,
        mensagem: str,
    ) -> None:
        self.con.execute(
            "INSERT INTO erros(id_campanha, id_candidata, etapa, tipo_erro, mensagem, em) VALUES (?,?,?,?,?,?)",
            (id_campanha, id_candidata, etapa, tipo_erro, mensagem[:2000], _agora()),
        )

    def erros(self, id_campanha: str) -> list[dict[str, Any]]:
        linhas = self.con.execute("SELECT * FROM erros WHERE id_campanha=? ORDER BY id", (id_campanha,)).fetchall()
        return [dict(r) for r in linhas]

    # ------------------------------------------------------------------ usage / budget
    def registrar_uso(
        self, id_campanha: str, agente: str, modelo: str, prompt: int, qtd_saida: int, total: int
    ) -> None:
        self.con.execute(
            "INSERT INTO uso_llm(id_campanha, agente, modelo, tokens_prompt, tokens_saida,"
            " tokens_total, em) VALUES (?,?,?,?,?,?,?)",
            (id_campanha, agente, modelo, prompt, qtd_saida, total, _agora()),
        )

    def totais_uso(self, id_campanha: str) -> tuple[int, int]:
        linha = self.con.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_total),0) FROM uso_llm WHERE id_campanha=?",
            (id_campanha,),
        ).fetchone()
        return int(linha[0]), int(linha[1])
