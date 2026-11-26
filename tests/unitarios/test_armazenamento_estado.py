from __future__ import annotations

import sqlite3

import pytest

from simio.nucleo.armazenamento import MIGRACOES, Armazenamento
from simio.nucleo.estado import TransicaoInvalida, verificar_transicao
from simio.nucleo.modelos import RegistroConsulta
from simio.nucleo.modelos import StatusCandidata as S
from simio.provedores.llm import Orcamento, OrcamentoEsgotado, Uso


def _cand(armazenamento: Armazenamento, cid: str, url: str = "https://a.example/") -> str:
    c, criado = armazenamento.upsert_candidata(
        cid, url=url, url_normalizada=url, hostname="a.example", dominio_registravel="a.example"
    )
    assert criado
    return c


def test_migracoes_sao_idempotentes(configuracoes) -> None:
    s1 = Armazenamento(configuracoes.caminho_db)
    assert s1.versao_esquema() == len(MIGRACOES)
    s1.fechar()
    s2 = Armazenamento(configuracoes.caminho_db)
    assert s2.versao_esquema() == len(MIGRACOES)
    s2.fechar()


def test_esquema_mais_novo_eh_recusado(configuracoes) -> None:
    Armazenamento(configuracoes.caminho_db).fechar()
    con = sqlite3.connect(configuracoes.caminho_db)
    con.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (999, 'x')")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="mais novo"):
        Armazenamento(configuracoes.caminho_db)


def test_regras_da_maquina_de_estados() -> None:
    verificar_transicao(S.NA_FILA, S.COLETANDO)
    verificar_transicao(S.REVISANDO, S.NA_FILA)
    with pytest.raises(TransicaoInvalida):
        verificar_transicao(S.DESCOBERTA, S.CONCLUIDA)
    with pytest.raises(TransicaoInvalida):
        verificar_transicao(S.CONCLUIDA, S.NA_FILA)


def test_candidata_dedup_e_transicoes(armazenamento: Armazenamento) -> None:
    camp = armazenamento.criar_campanha("bv", {"limite": 1})
    cid = _cand(armazenamento, camp)
    de_novo, criado = armazenamento.upsert_candidata(
        camp,
        url="https://a.example/",
        url_normalizada="https://a.example/",
        hostname="a.example",
        dominio_registravel="a.example",
    )
    assert de_novo == cid and not criado
    armazenamento.transicionar(cid, S.NA_FILA)
    armazenamento.transicionar(cid, S.COLETANDO)
    with pytest.raises(TransicaoInvalida):
        armazenamento.transicionar(cid, S.CONCLUIDA)
    assert armazenamento.obter_candidata(cid).status == S.COLETANDO
    assert [t["status_destino"] for t in armazenamento.transicoes(cid)] == ["NA_FILA", "COLETANDO"]


def test_recuperar_em_andamento(armazenamento: Armazenamento) -> None:
    camp = armazenamento.criar_campanha("bv", {})
    a = _cand(armazenamento, camp, "https://a.example/")
    b = _cand(armazenamento, camp, "https://b.example/")
    for t in (S.NA_FILA, S.COLETANDO):
        armazenamento.transicionar(a, t)
    for t in (S.NA_FILA, S.COLETANDO, S.COLETADA, S.ANALISANDO, S.REVISANDO):
        armazenamento.transicionar(b, t)
    assert armazenamento.recuperar_em_andamento(camp) == 2
    assert armazenamento.obter_candidata(a).status == S.NA_FILA
    assert armazenamento.obter_candidata(b).status == S.COLETADA


def test_checkpoints_consultas_e_erros(armazenamento: Armazenamento) -> None:
    camp = armazenamento.criar_campanha("bv", {})
    armazenamento.salvar_checkpoint(camp, "descoberta_concluida", True)
    assert armazenamento.obter_checkpoint(camp, "descoberta_concluida") is True
    assert armazenamento.obter_checkpoint(camp, "missing", "d") == "d"
    qid = armazenamento.registrar_consulta(camp, RegistroConsulta(fonte="brave", consulta="q", status="VAZIA"))
    assert qid > 0 and armazenamento.listar_consultas(camp)[0]["status"] == "VAZIA"
    armazenamento.registrar_erro(camp, None, "descoberta", "X", "y" * 5000)
    assert len(armazenamento.erros(camp)[0]["mensagem"]) == 2000


async def test_orcamento_persiste_e_bloqueia(armazenamento: Armazenamento) -> None:
    camp = armazenamento.criar_campanha("bv", {})
    b = Orcamento(armazenamento, camp, max_requisicoes=2, max_tokens=1000)
    await b.reservar()
    await b.efetivar("a", "m", Uso(10, 5, 15))
    await b.reservar()
    await b.efetivar("a", "m", Uso(10, 5, 15))
    with pytest.raises(OrcamentoEsgotado):
        await b.reservar()
    # Um Orcamento novo (ex.: depois da retomada) enxerga o uso já gravado.
    b2 = Orcamento(armazenamento, camp, max_requisicoes=10, max_tokens=20)
    with pytest.raises(OrcamentoEsgotado, match="token"):
        await b2.reservar()
