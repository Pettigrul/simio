from __future__ import annotations

from simio.marcas import carregar_marca
from simio.nucleo.modelos import (
    CasoCenario,
    ClasseIdentidade,
    ClassificacaoFinal,
    DecisaoFinal,
    Nivel,
    Prioridade,
    SuficienciaEvidencia,
)
from simio.relatorios.executivo import gravar_relatorio_executivo, montar_contexto


def _candidata(armazenamento, cid: str, url: str, host: str, dominio: str, **decisao) -> None:
    id_candidata, _ = armazenamento.upsert_candidata(
        cid, url=url, url_normalizada=url, hostname=host, dominio_registravel=dominio
    )
    final = DecisaoFinal(
        id_candidata=id_candidata,
        prioridade=Prioridade.ALTA,
        confianca_extracao=Nivel.ALTA,
        confianca_atribuicao=Nivel.MEDIA,
        suficiencia_evidencia=SuficienciaEvidencia.SUFICIENTE,
        exige_revisao_humana=True,
        justificativa="teste",
        **decisao,
    )
    armazenamento.salvar_avaliacao(id_candidata, "final", final)


def _popular(armazenamento) -> None:
    fraca = dict(
        classificacao=ClassificacaoFinal.REQUER_REVISAO,
        caso=CasoCenario.B_COMUNICACAO_AMBIGUA,
        classe_identidade=ClasseIdentidade.IDENTIDADE_DESCONHECIDA,
    )
    forte = dict(
        classificacao=ClassificacaoFinal.INDICIO_FORTE,
        caso=CasoCenario.C_POSSIVEL_CANAL_OFICIAL,
        classe_identidade=ClasseIdentidade.COBAN_CONFIRMADO,
        identidade_visual_antiga=True,
        fonte_identidade_visual_antiga="modelo",
    )
    loja = ("https://loja.example/bv", "loja.example", "loja.example")
    primeira = armazenamento.criar_campanha("bv", {})
    _candidata(armazenamento, primeira, *loja, **fraca)
    _candidata(armazenamento, primeira, "https://www.bv.com.br/", "www.bv.com.br", "bv.com.br", **forte)
    segunda = armazenamento.criar_campanha("bv", {"foco": "ameacas"})
    _candidata(armazenamento, segunda, *loja, **forte)
    armazenamento.con.commit()


def test_soma_campanhas_com_url_unica_e_sem_dominio_oficial(configuracoes, armazenamento) -> None:
    _popular(armazenamento)
    ctx = montar_contexto(configuracoes, armazenamento, carregar_marca("bv"))
    assert len(ctx["campanhas"]) == 2
    assert ctx["analisadas"] == 1  # a URL repetida conta uma vez; o domínio oficial fica fora
    assert ctx["achados"]["total"] == 1
    assert ctx["achados"]["forte"] == 1  # vale a classificação mais grave entre as campanhas
    assert ctx["achados"]["recorrentes"] == 1
    assert ctx["coban"]["confirmado"] == 1
    assert ctx["logo"]["total"] == 1
    assert ctx["por_modo"] == {"ampla": 1, "ameacas": 1}


def test_grava_html_sem_pdf(configuracoes, armazenamento, tmp_path) -> None:
    _popular(armazenamento)
    res = gravar_relatorio_executivo(configuracoes, armazenamento, tmp_path / "exec", com_pdf=False)
    assert res.pdf is None
    html = res.html.read_text(encoding="utf-8")
    assert "Relatório executivo do SIMIO" in html
    assert "loja.example" in html
    assert "www.bv.com.br" not in html
    assert 'contenteditable="true"' in html
