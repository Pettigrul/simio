# Esquema v3: um banco v2 com nomes em inglês é migrado no lugar, e um banco novo já nasce no
# mesmo esquema final.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from simio.agentes.inteligencia_dominio import caminho_inteligencia, carregar_inteligencia
from simio.marcas import carregar_marca
from simio.nucleo import modelos as m
from simio.nucleo.armazenamento import MIGRACOES, Armazenamento
from simio.nucleo.traducao_legado import TABELAS, traduzir_modelo
from simio.relatorios.dados import carregar_registros

# Dump de um banco v2 gravado pela versão em inglês (e2e offline nos sites sintéticos com o LLM
# falso: descoberta por grounding, análise, revisão, uso e checkpoints).
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "banco_legado_v2.sql"

# Algumas linhas antigas a mais, com códigos que a execução do fixture não gera.
EXTRAS = [
    "INSERT INTO campaigns(id, brand, status, params_json, created_at, updated_at) VALUES ('simio-x', 'bv', 'PAUSED_BUDGET',"
    """ '{"brand": "bv", "limit": 5, "focus": "ameacas", "workers": 2, "review_all": false, "imported_urls": 1}',"""
    " '2026-11-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00')",
    "INSERT INTO queries(campaign_id, source, hypothesis_id, query_text, status, result_count, error, metadata_json,"
    " executed_at) VALUES ('simio-x', 'typosquat', 'H8-lookalike:bancobv.com.br', 'q', 'EMPTY', 0, NULL,"
    """ '{"permutations_checked": 120, "resolved_non_public": ["a.example"], "supplement": "social_dork","""
    """ "unresolved": [{"title": "t", "snippet": "s"}]}', '2026-11-01T00:00:00+00:00')""",
    "INSERT INTO queries(campaign_id, source, hypothesis_id, query_text, status, result_count, error, metadata_json,"
    " executed_at) VALUES ('simio-x', 'import:file:lista.txt', 'H13-legacy-focus:regiao-sul', 'q', 'ERROR', 0, 'x',"
    " '{}', '2026-11-01T00:00:00+00:00')",
    "INSERT INTO agent_decisions(campaign_id, candidate_id, agent, decision, detail_json, at) VALUES ('simio-x', NULL,"
    """ 'orchestrator', 'run_finished', '{"status": "PAUSED_BUDGET", "counts": {"COMPLETED": 2, "QUEUED": 1},"""
    """ "llm_requests": 3, "llm_tokens": 10}', '2026-11-01T00:00:00+00:00')""",
    "INSERT INTO agent_decisions(campaign_id, candidate_id, agent, decision, detail_json, at) VALUES ('simio-x', NULL,"
    """ 'discovery', 'completed', '{"hits": 1, "unique_urls": 1, "sources_used": ["import:text"],"""
    """ "queries": {"serper:EMPTY": 2, "grounding:OK": 1}}', '2026-11-01T00:00:00+00:00')""",
    "INSERT INTO errors(campaign_id, candidate_id, stage, error_type, message, at) VALUES ('simio-x', NULL, 'brand',"
    " 'LLMUnavailable', 'm', '2026-11-01T00:00:00+00:00')",
    "INSERT INTO checkpoints(campaign_id, key, value_json, updated_at) VALUES ('simio-x', 'discovery_done', 'true',"
    " '2026-11-01T00:00:00+00:00')",
]


def _banco_legado(caminho: Path) -> dict[str, int]:
    con = sqlite3.connect(caminho)
    con.executescript(FIXTURE.read_text(encoding="utf-8"))
    for sql in EXTRAS:
        con.execute(sql)
    con.commit()
    contagens = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in TABELAS}
    con.close()
    return contagens


def _esquema(con: sqlite3.Connection) -> dict[str, object]:
    saida: dict[str, object] = {}
    tabelas = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for t in tabelas:
        indices = sorted(
            (r[1], r[2], tuple(c[2] for c in con.execute(f'PRAGMA index_info("{r[1]}")')))
            for r in con.execute(f'PRAGMA index_list("{t}")')
        )
        saida[t] = (
            [tuple(r[1:]) for r in con.execute(f'PRAGMA table_info("{t}")')],
            sorted(tuple(r[2:5]) for r in con.execute(f'PRAGMA foreign_key_list("{t}")')),
            indices,
        )
    return saida


def test_banco_novo_nasce_no_mesmo_esquema_que_o_migrado(tmp_path: Path) -> None:
    novo = Armazenamento(tmp_path / "novo.db")
    assert novo.versao_esquema() == len(MIGRACOES) == 3
    legado = tmp_path / "v2.db"
    con = sqlite3.connect(legado)
    con.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)")
    for versao, sql in enumerate(MIGRACOES[:2], start=1):
        assert isinstance(sql, str)
        con.executescript(sql)
        con.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (versao,))
    con.commit()
    con.close()
    migrado = Armazenamento(legado)
    assert migrado.versao_esquema() == 3
    assert _esquema(novo.con) == _esquema(migrado.con)
    novo.fechar()
    migrado.fechar()


def test_migracao_v3_traduz_banco_legado(tmp_path: Path) -> None:
    caminho = tmp_path / "simio.db"
    antes = _banco_legado(caminho)
    st = Armazenamento(caminho)
    assert st.versao_esquema() == 3
    for velha, (nova, _) in TABELAS.items():
        depois = st.con.execute(f'SELECT COUNT(*) FROM "{nova}"').fetchone()[0]
        assert depois == antes[velha] + (1 if velha == "schema_migrations" else 0), velha
    assert st.con.execute("PRAGMA foreign_key_check").fetchall() == []

    # todo payload valida nos modelos novos (extra="forbid")
    por_tipo = {
        "identidade": m.AtribuicaoIdentidade,
        "marca": m.AvaliacaoMarca,
        "conformidade": m.AvaliacaoConformidade,
        "revisao": m.Revisao,
        "final": m.DecisaoFinal,
    }
    tipos = set()
    for tipo, dados in st.con.execute("SELECT tipo, dados_json FROM avaliacoes"):
        por_tipo[tipo].model_validate_json(dados)
        tipos.add(tipo)
    assert tipos == set(por_tipo)
    for (dados,) in st.con.execute("SELECT dados_json FROM evidencias"):
        m.Evidencia.model_validate_json(dados)
    for (dados,) in st.con.execute("SELECT resultado_json FROM resultados_descoberta"):
        m.ResultadoDescoberta.model_validate_json(dados)

    camp = st.obter_campanha("simio-x")
    assert camp is not None and camp["status"] == "PAUSADA_ORCAMENTO"
    assert camp["parametros"] == {
        "marca": "bv",
        "limite": 5,
        "foco": "ameacas",
        "paralelismo": 2,
        "revisar_todas": False,
        "urls_importadas": 1,
    }
    consultas = {c["id_hipotese"]: c for c in st.listar_consultas("simio-x")}
    tipo = consultas["H8-semelhante:bancobv.com.br"]
    assert tipo["status"] == "VAZIA"
    assert json.loads(tipo["metadados_json"]) == {
        "permutacoes_verificadas": 120,
        "resolvidos_nao_publicos": ["a.example"],
        "complemento": "dork_social",
        "nao_resolvidos": [{"title": "t", "snippet": "s"}],  # payload de API não é percorrido
    }
    arquivo = consultas["H13-foco-logo-antiga:regiao-sul"]
    assert arquivo["fonte"] == "importacao:arquivo:lista.txt" and arquivo["status"] == "ERRO"
    decisoes = {(d["agente"], d["decisao"]): json.loads(d["detalhe_json"]) for d in st.decisoes("simio-x")}
    assert decisoes[("orquestrador", "execucao_finalizada")] == {
        "status": "PAUSADA_ORCAMENTO",
        "contagens": {"CONCLUIDA": 2, "NA_FILA": 1},
        "requisicoes_llm": 3,
        "tokens_llm": 10,
    }
    assert decisoes[("descoberta", "concluida")]["consultas"] == {"serper:VAZIA": 2, "grounding:OK": 1}
    assert decisoes[("descoberta", "concluida")]["fontes_usadas"] == ["importacao:texto"]
    assert [(e["etapa"], e["tipo_erro"]) for e in st.erros("simio-x")] == [("marca", "LLMIndisponivel")]
    assert st.obter_checkpoint("simio-x", "descoberta_concluida") is True

    # a campanha gravada pela versão antiga sai com os códigos e o vocabulário da nova
    (cid,) = st.con.execute("SELECT id FROM campanhas WHERE id != 'simio-x'").fetchone()
    assert {r[0] for r in st.con.execute("SELECT DISTINCT agente FROM uso_llm")} <= {
        "analista_marca",
        "revisor",
        "descoberta.grounding",
    }
    hipoteses = {c["id_hipotese"] for c in st.listar_consultas(cid)}
    assert hipoteses and all(h.startswith("H1-identidade:") for h in hipoteses)
    assert st.obter_checkpoint(cid, "descoberta_concluida") is True
    assert "fontes_usadas" in st.obter_checkpoint(cid, "descoberta")
    for c in st.listar_candidatas(cid):
        ev = st.evidencia_mais_recente(c.id)
        if ev is not None:
            assert {a.tipo for a in ev.artefatos} <= {
                "captura_viewport",
                "captura_completa",
                "html",
                "texto",
                "dados_pagina",
            }
    registros = carregar_registros(st, cid, carregar_marca("bv"))
    assert registros and any(r.final is not None for r in registros)
    st.fechar()


def test_traducao_eh_idempotente_sobre_dados_novos(armazenamento: Armazenamento) -> None:
    final = m.DecisaoFinal(
        id_candidata="C1",
        classificacao=m.ClassificacaoFinal.REQUER_REVISAO,
        prioridade=m.Prioridade.MEDIA,
        caso=m.CasoCenario.B_COMUNICACAO_AMBIGUA,
        classe_identidade=m.ClasseIdentidade.IDENTIDADE_DESCONHECIDA,
        confianca_extracao=m.Nivel.BAIXA,
        confianca_atribuicao=m.Nivel.NENHUMA,
        suficiencia_evidencia=m.SuficienciaEvidencia.PARCIAL,
        exige_revisao_humana=True,
        justificativa="x",
        resumo_conformidade=["CMN4935-ART3: NAO_AVALIAVEL"],
    )
    dados = json.loads(final.model_dump_json())
    assert traduzir_modelo(m.DecisaoFinal, dados) == dados


def test_inteligencia_de_dominio_gravada_pela_versao_antiga_eh_lida(tmp_path: Path) -> None:
    cid = "simio-legado"
    destino = caminho_inteligencia(tmp_path, cid)
    destino.parent.mkdir(parents=True)
    legado = {
        "loja.example": {
            "hostname": "loja.example",
            "registered_domain": "loja.example",
            "platform": None,
            "a": ["93.184.215.14"],
            "ns": ["ns1.example"],
            "rdap": {
                "registrar": "Registro.br",
                "created": "2020-01-01",
                "age_days": 2000,
                "holder": "Loja Ltda",
                "holder_document": "11222333000181",
                "abuse_emails": ["abuse@example"],
            },
            "ips": [
                {
                    "ip": "93.184.215.14",
                    "network": "EDGECAST",
                    "organisation": "Edgecast",
                    "country": "US",
                    "abuse_emails": [],
                }
            ],
            "notes": ["n"],
            "errors": [],
            "collected_at": "2026-11-01T00:00:00Z",
        }
    }
    destino.write_text(json.dumps(legado), encoding="utf-8")
    intel = carregar_inteligencia(tmp_path, cid)["loja.example"]
    assert intel.dominio_registrado == "loja.example" and intel.notas == ["n"]
    assert intel.rdap is not None and intel.rdap.registrador == "Registro.br" and intel.rdap.idade_dias == 2000
    assert intel.ips[0].organizacao == "Edgecast" and intel.ips[0].pais == "US"
