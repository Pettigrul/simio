"""Threat-intelligence campaign (``simio investigar --foco ameacas``) and credential-harvesting detection."""

from __future__ import annotations

import json

import httpx

from simio.agentes.conformidade import AnalistaConformidade, CatalogoRequisitos
from simio.agentes.descoberta import FOCO_AMEACAS, AgenteDescoberta, GrupoCandidatas, priorizar
from simio.agentes.evidencia import calcular_sinais, classificar_campo_entrada, montar_catalogo
from simio.agentes.identidade import AgenteIdentidade, CadastroCoban
from simio.agentes.marca import aplicar_regra_dados_sensiveis
from simio.nucleo.decisao import compor_final
from simio.nucleo.modelos import (
    AvaliacaoMarca,
    CasoCenario,
    DecisaoRevisao,
    HipoteseBusca,
    InfoCampoEntrada,
    Nivel,
    Prioridade,
    ResultadoDescoberta,
    Revisao,
    SuficienciaEvidencia,
)
from simio.nucleo.modelos import ClassificacaoFinal as C
from simio.provedores.busca import BuscaSerper
from simio.seguranca.politica_url import PoliticaUrl
from tests.auxiliares import MARCA, montar_evidencia


class _Nomeado:
    def __init__(self, nome: str, consultas: list[str] | None = None) -> None:
        self.nome = nome
        self._q = consultas or []

    def disponivel(self) -> tuple[bool, str]:
        return True, self.nome

    def consultas(self) -> list[str]:
        return self._q


def test_plano_de_ameacas_descarta_contexto_coban_e_lens() -> None:
    cons = [_Nomeado("serper"), _Nomeado("lens", ["https://img.example/bv.png"]), _Nomeado("typosquat", ["typo:bv"])]
    agente = AgenteDescoberta(MARCA, cons, PoliticaUrl(), max_consultas=500, foco=FOCO_AMEACAS)
    planejar = agente.planejar(cons)
    familias = {p.hipotese.id.split(":")[0] for p in planejar}
    assert {"H14-ameaca", "H10-fraude", "H7-inteligencia-ameacas", "H8-semelhante"} <= familias
    assert not any(p.conector == "lens" for p in planejar)
    assert not any(f.startswith(("H2-", "H5-", "H9-", "H13-")) for f in familias)


def test_paginas_de_construtor_gratuito_mantidas_e_reforcadas_so_em_campanhas_de_ameacas() -> None:
    def grupo() -> GrupoCandidatas:
        resultado = ResultadoDescoberta(fonte="serper", url_original="https://bv-acordo.weebly.com/", titulo="Banco BV acordo")
        return GrupoCandidatas(
            url=resultado.url_original,
            url_normalizada="https://bv-acordo.weebly.com/",
            hostname="bv-acordo.weebly.com",
            dominio_registravel="bv-acordo.weebly.com",
            resultados=[resultado],
        )

    [ampla] = priorizar([grupo()], MARCA)
    assert ampla.motivo_exclusao is not None
    [ameaca] = priorizar([grupo()], MARCA, FOCO_AMEACAS)
    assert ameaca.motivo_exclusao is None and any("free website builder" in r for r in ameaca.motivos)


async def test_serper_recupera_host_destino_de_resultados_opacos() -> None:
    def tratador(requisicao: httpx.Request) -> httpx.Response:
        assert json.loads(requisicao.content)["num"] == 10
        return httpx.Response(
            200,
            json={
                "organic": [
                    {"title": "Direto", "link": "https://loja.example/financiamento", "displayedLink": "x"},
                    {
                        "title": "Opaco com host",
                        "link": "https://www.google.com/goto?url=CAES1",
                        "displayedLink": "https://www.revenda-bv.com.br › financiamento",
                    },
                    {
                        "title": "Opaco social",
                        "link": "https://www.google.com/goto?url=CAES2",
                        "displayedLink": "https://www.instagram.com › reel",
                    },
                    {"title": "Opaco sem host", "link": "https://www.google.com/goto?url=CAES3", "displayedLink": "há 4 dias"},
                ]
            },
        )

    con = BuscaSerper("k", transporte=httpx.MockTransport(tratador))
    resultados, registro = await con.executar(HipoteseBusca(id="H", descricao="d", prompt_grounding=""), "q")
    urls = [h.url_original for h in resultados]
    assert urls == ["https://loja.example/financiamento", "https://www.revenda-bv.com.br/"]
    assert resultados[1].metadados["recuperado_de_redirecionamento_google"] is True
    assert registro.metadados["hosts_redirecionamento_google_recuperados"] == 1
    assert len(registro.metadados["redirecionamentos_google_nao_resolvidos"]) == 2


def test_classificacao_campos_entrada() -> None:
    assert classificar_campo_entrada(InfoCampoEntrada(tipo_campo="password", nome="x")) == "senha"
    assert classificar_campo_entrada(InfoCampoEntrada(tipo_campo="text", nome="cc-number", autocomplete="cc-number")) == "cartão"
    assert classificar_campo_entrada(InfoCampoEntrada(tipo_campo="tel", placeholder="Código SMS de verificação")) == "token/código"
    assert classificar_campo_entrada(InfoCampoEntrada(tipo_campo="text", rotulo="CPF")) == "cpf"
    assert classificar_campo_entrada(InfoCampoEntrada(tipo_campo="email", rotulo="E-mail da conta")) is None
    assert classificar_campo_entrada(InfoCampoEntrada(tipo_campo="text", nome="nome")) is None


def _evidencia_phishing(**kw):
    ev = montar_evidencia(titulo="Banco BV - Acesse sua conta", corpo="Banco BV internet banking. Digite sua senha.", **kw)
    ev.pagina.campos_entrada = [
        InfoCampoEntrada(tipo_campo="text", nome="cpf", placeholder="CPF"),
        InfoCampoEntrada(tipo_campo="password", nome="senha"),
    ]
    ev.pagina.acoes_formulario = ["https://coletor.example/post.php"]
    return ev


def _avaliacao(**kw) -> AvaliacaoMarca:
    base = dict(
        caso=CasoCenario.A_MENCAO_COMERCIAL_LEGITIMA,
        classificacao=C.SEM_INDICIO,
        prioridade=Prioridade.BAIXA,
        confianca_extracao=Nivel.ALTA,
        suficiencia_evidencia=SuficienciaEvidencia.SUFICIENTE,
        analisador="model-x",
    )
    base.update(kw)
    return AvaliacaoMarca(**base)


def test_pagina_com_marca_pedindo_senha_tem_piso_e_sobrevive_ao_revisor() -> None:
    ev = _evidencia_phishing()
    cat = montar_catalogo(ev, MARCA)
    sig = calcular_sinais(ev, MARCA, cat)
    assert [h.valor for h in sig.campos_credencial] == ["senha"] and sig.acao_formulario_externa

    a = aplicar_regra_dados_sensiveis(_avaliacao(), sig)
    assert a.dados_sensiveis_solicitados == ["cpf", "senha"]
    assert (a.classificacao, a.prioridade) == (C.REQUER_REVISAO, Prioridade.ALTA)
    assert any("E-FORM-ACTION" in ind.refs_evidencia for ind in a.indicadores_confusao_marca)

    idn = AgenteIdentidade(MARCA, CadastroCoban()).atribuir("C1", ev, cat, sig)
    comp = AnalistaConformidade(CatalogoRequisitos.carregar()).avaliar(a, idn, False)
    liberada = Revisao(
        decisao=DecisaoRevisao.REBAIXAR,
        classificacao_revisada=C.SEM_INDICIO,
        prioridade_revisada=Prioridade.BAIXA,
        revisor="r",
    )
    f = compor_final("C1", a, liberada, idn, comp, ev)
    assert f.classificacao == C.REQUER_REVISAO and f.prioridade == Prioridade.ALTA
    assert f.dados_sensiveis_solicitados == ["cpf", "senha"]


def test_so_cpf_ou_sem_marca_nao_dispara() -> None:
    ev = montar_evidencia(titulo="Simule seu financiamento", corpo="Banco BV parceiro. Simule.")
    ev.pagina.campos_entrada = [InfoCampoEntrada(tipo_campo="text", nome="cpf", placeholder="CPF")]
    cat = montar_catalogo(ev, MARCA)
    assert aplicar_regra_dados_sensiveis(_avaliacao(), calcular_sinais(ev, MARCA, cat)).dados_sensiveis_solicitados == []

    ev2 = montar_evidencia(titulo="Loja de roupas", corpo="Entre na sua conta")
    ev2.pagina.campos_entrada = [InfoCampoEntrada(tipo_campo="password", nome="senha")]
    cat2 = montar_catalogo(ev2, MARCA)
    intocado = _avaliacao()
    assert aplicar_regra_dados_sensiveis(intocado, calcular_sinais(ev2, MARCA, cat2)) == intocado


def test_campo_de_senha_em_pagina_que_so_cita_a_marca_nao_dispara() -> None:
    ev = montar_evidencia(titulo="Portal de notícias", corpo="O Banco BV teve lucro recorde no trimestre. Assine já.")
    ev.pagina.campos_entrada = [InfoCampoEntrada(tipo_campo="password", nome="senha")]
    cat = montar_catalogo(ev, MARCA)
    assert aplicar_regra_dados_sensiveis(_avaliacao(), calcular_sinais(ev, MARCA, cat)).dados_sensiveis_solicitados == []


def test_barreira_de_login_de_rede_social_nao_eh_formulario_de_credenciais() -> None:
    ev = montar_evidencia(
        url="https://www.facebook.com/groups/1/posts/2/", titulo="Banco BV repasse", corpo="Entrar. Banco BV repasse."
    )
    ev.pagina.campos_entrada = [InfoCampoEntrada(tipo_campo="password", nome="pass")]
    assert not any(i.ref.startswith("E-FORM") for i in montar_catalogo(ev, MARCA))
