from __future__ import annotations

from simio.agentes.triagem_ameacas import triar
from simio.nucleo.modelos import (
    Candidata,
    CasoCenario,
    ClasseIdentidade,
    ClassificacaoFinal,
    DecisaoFinal,
    InteligenciaDominio,
    InteligenciaRdap,
    Nivel,
    Prioridade,
    ResultadoDescoberta,
    SuficienciaEvidencia,
)
from simio.nucleo.urls import hostname_de
from simio.relatorios.dados import RegistroCandidata
from simio.relatorios.paginas import link_url
from tests.auxiliares import MARCA, montar_evidencia


def _registro(
    url: str,
    cls: ClassificacaoFinal,
    caso: CasoCenario,
    *,
    titulo: str = "",
    corpo: str = "",
    identidade: ClasseIdentidade = ClasseIdentidade.IDENTIDADE_DESCONHECIDA,
    sensiveis: list[str] | None = None,
    resultados: list[ResultadoDescoberta] | None = None,
) -> RegistroCandidata:
    host = hostname_de(url)
    cand = Candidata(id="c1", id_campanha="k", url=url, url_normalizada=url, hostname=host, dominio_registravel=host)
    final = DecisaoFinal(
        id_candidata="c1",
        classificacao=cls,
        prioridade=Prioridade.ALTA,
        caso=caso,
        classe_identidade=identidade,
        confianca_extracao=Nivel.ALTA,
        confianca_atribuicao=Nivel.ALTA,
        suficiencia_evidencia=SuficienciaEvidencia.SUFICIENTE,
        exige_revisao_humana=True,
        justificativa="",
    )
    if sensiveis is not None:
        final.dados_sensiveis_solicitados = sensiveis
    return RegistroCandidata(
        candidata=cand,
        resultados=resultados or [],
        evidencia=montar_evidencia(url=url, titulo=titulo, corpo=corpo),
        todas_evidencias=[],
        catalogo=[],
        identidade=None,
        marca=None,
        conformidade=None,
        revisao=None,
        final=final,
        transicoes=[],
    )


def test_portal_falso_em_construtor_gratuito_pedindo_senha_eh_critico() -> None:
    r = _registro(
        "https://bvboleto.weebly.com/",
        ClassificacaoFinal.INDICIO_FORTE,
        CasoCenario.D_PERSONIFICACAO_FORTE,
        titulo="Banco BV - 2ª via do boleto",
        corpo="Banco BV: emita a 2ª via do boleto e faça a quitação. Informe CPF e senha.",
        sensiveis=["cpf", "senha"],
    )
    t = triar(r, MARCA)
    assert t.nivel == "CRÍTICO"
    assert "Página que pede senha (phishing)" in t.categorias and "Hospedagem gratuita" in t.categorias


def test_parceiro_identificado_com_marca_propria_nao_eh_ameaca() -> None:
    r = _registro(
        "https://loja-motos.example/financiamento",
        ClassificacaoFinal.REQUER_REVISAO,
        CasoCenario.B_COMUNICACAO_AMBIGUA,
        titulo="Loja Motos - financiamento",
        corpo="Financiamento com o Banco BV. Loja Motos Ltda, CNPJ 00.000.000/0001-00.",
        identidade=ClasseIdentidade.COBAN_CONFIRMADO,
    )
    assert not triar(r, MARCA).eh_ameaca


def test_caso_descartado_nao_conta() -> None:
    # O revisor descartou a "personificação" do analista (era um diretório de agências).
    r = _registro(
        "https://agencias.example/bv-financeira/sp",
        ClassificacaoFinal.SEM_INDICIO,
        CasoCenario.D_PERSONIFICACAO_FORTE,
        titulo="BV Financeira - agências em SP",
        corpo="Endereço e telefone das agências da BV Financeira.",
    )
    assert not triar(r, MARCA).eh_ameaca


def test_semelhante_registrado_pelo_proprio_banco_eh_defensivo() -> None:
    resultado = ResultadoDescoberta(
        fonte="typosquat",
        url_original="http://bvcartao.com.br/",
        metadados={"semelhante_a": "bv.com.br", "fuzzer": "combosquatting"},
    )
    r = _registro("http://bvcartao.com.br/", ClassificacaoFinal.INCONCLUSIVA, CasoCenario.E_EVIDENCIA_INSUFICIENTE, resultados=[resultado])
    assert triar(r, MARCA).eh_ameaca  # sem dados de registro, vale monitorar
    intel = InteligenciaDominio(
        hostname="bvcartao.com.br",
        dominio_registrado="bvcartao.com.br",
        rdap=InteligenciaRdap(titular="Banco Votorantim S.A.", documento_titular="59.588.111/0001-03"),
    )
    t = triar(r, MARCA, intel)
    assert not t.eh_ameaca and any("registro defensivo" in f for f in t.fatos)


def test_dominio_novo_soma_peso() -> None:
    r = _registro(
        "https://bvquitacao.example/",
        ClassificacaoFinal.REQUER_REVISAO,
        CasoCenario.C_POSSIVEL_CANAL_OFICIAL,
        titulo="Banco BV quitação",
        corpo="Banco BV: quitação de contrato.",
    )
    antigo = triar(r, MARCA, InteligenciaDominio(hostname="bvquitacao.example", dominio_registrado="bvquitacao.example",
                                       a=["1.2.3.4"], rdap=InteligenciaRdap(idade_dias=4000)))
    novo = triar(r, MARCA, InteligenciaDominio(hostname="bvquitacao.example", dominio_registrado="bvquitacao.example",
                                       a=["1.2.3.4"], rdap=InteligenciaRdap(idade_dias=20)))
    assert novo.pontuacao == antigo.pontuacao + 2 and "Domínio recente" in novo.categorias


def test_link_url() -> None:
    assert str(link_url("https://bv.weebly.com/a.b?x=1&y=2")) == (
        '<a href="https://bv.weebly.com/a.b?x=1&amp;y=2" target="_blank" rel="noopener noreferrer nofollow">'
        "https://bv.weebly.com/a.b?x=1&amp;y=2</a>"
    )
    assert "<a" not in str(link_url("javascript:alert(1)"))
    assert '"><script>' not in str(link_url('https://x.example/"><script>'))
    assert link_url(None) == ""
