from __future__ import annotations

import httpx
import pytest

from simio.agentes.descoberta import AgenteDescoberta, deduplicar, montar_hipoteses, priorizar
from simio.nucleo.modelos import HipoteseBusca, RegistroConsulta, ResultadoDescoberta
from simio.nucleo.urls import dominio_registravel, extrair_urls, normalizar_url
from simio.provedores.busca import BuscaBrave, BuscaSerper, BuscaUrlscan, importar_arquivo, importar_texto
from simio.provedores.grounding import ConectorGrounding
from simio.provedores.llm import GatewayLLM, LLMIndisponivel, Orcamento
from simio.provedores.mock import ProvedorMock, resultado_grounding
from simio.provedores.vertex import interpretar_resposta_grounding
from simio.seguranca.politica_url import PoliticaUrl
from tests.auxiliares import MARCA


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (
            "https://www.Loja.example/Financiamento/?utm_source=g&b=2&a=1#x",
            "https://loja.example/Financiamento?a=1&b=2",
        ),
        ("http://loja.example:80/", "https://loja.example/"),
        ("https://loja.example/index.html", "https://loja.example/"),
        ("loja.example/x//y/", "https://loja.example/x/y"),
        ("https://loja.example/p?gclid=1&fbclid=2", "https://loja.example/p"),
        ("https://wa.me/canal_exemplo?lang=el_GR", "https://wa.me/canal_exemplo"),
        ("https://www.instagram.com/reel/AbCdEf/?hl=da&igsh=xyz", "https://instagram.com/reel/AbCdEf"),
        ("https://www.facebook.com/loja.exemplo/videos/123/?locale=pt_BR", "https://facebook.com/loja.exemplo/videos/123"),
        (
            "https://portal.tjpr.jus.br/e-dj/publico/carregarDocumento.do;jsessionid=fead3062?id=1",
            "https://portal.tjpr.jus.br/e-dj/publico/carregarDocumento.do?id=1",
        ),
    ],
)
def test_normalizar(a: str, b: str) -> None:
    assert normalizar_url(a) == normalizar_url(b)


def test_dominio_registravel_br() -> None:
    assert dominio_registravel("www.loja.com.br") == "loja.com.br"
    assert dominio_registravel("a.b.example.co.uk") == "example.co.uk"


def test_extrair_urls_do_texto() -> None:
    texto = "Veja https://a.example/x). Também www.b.example/y, e (http://c.test/z?q=1)."
    assert extrair_urls(texto) == ["https://a.example/x", "https://www.b.example/y", "http://c.test/z?q=1"]


def test_importar_arquivo_txt(tmp_path) -> None:
    from tests.conftest import SITES

    resultados = importar_arquivo(SITES / "urls_sample.txt")
    assert [h.fonte for h in resultados] == ["importacao:arquivo:urls_sample.txt"] * 4
    csv = tmp_path / "u.csv"
    csv.write_text("nome;url\nx;https://a.example/\ny;https://a.example/\n", encoding="utf-8")
    assert [h.url_original for h in importar_arquivo(csv)] == ["https://a.example/"]


def test_dedup_preserva_fontes_e_urls_originais() -> None:
    resultados = [
        ResultadoDescoberta(fonte="grounding", url_original="https://www.financiamento-bv.example/?utm_source=x", consulta="q1"),
        ResultadoDescoberta(fonte="brave", url_original="https://financiamento-bv.example/", consulta="q2"),
        ResultadoDescoberta(fonte="importacao:texto", url_original="https://financiamento-bv.example/outra"),
        ResultadoDescoberta(fonte="brave", url_original="http://localhost/x"),
        ResultadoDescoberta(fonte="brave", url_original="ftp://x.example/"),
    ]
    grupos, rejeitados = deduplicar(resultados, PoliticaUrl())
    assert len(grupos) == 2
    inicial = grupos[0]
    assert {h.fonte for h in inicial.resultados} == {"grounding", "brave"}
    assert {h.url_original for h in inicial.resultados} == {
        "https://www.financiamento-bv.example/?utm_source=x",
        "https://financiamento-bv.example/",
    }
    assert inicial.dominio_registravel == "financiamento-bv.example"
    assert len(rejeitados) == 2


def test_priorizar_exclui_oficiais_e_ordena() -> None:
    resultados = [
        ResultadoDescoberta(fonte="brave", url_original="https://www.bv.com.br/financiamento", titulo="Banco BV"),
        ResultadoDescoberta(fonte="brave", url_original="https://noticias.example/bv", titulo="Notícia"),
        ResultadoDescoberta(fonte="brave", url_original="https://bancobv-oficial.example/", titulo="Banco BV canal oficial"),
        ResultadoDescoberta(fonte="brave", url_original="https://pt.wikipedia.org/wiki/Banco_BV", titulo="Banco BV"),
    ]
    grupos, _ = deduplicar(resultados, PoliticaUrl())
    ordenados = priorizar(grupos, MARCA)
    assert ordenados[0].hostname == "bancobv-oficial.example"
    assert ordenados[-1].motivo_exclusao == "domínio institucional oficial"
    wiki = next(g for g in ordenados if "wikipedia" in g.hostname)
    assert wiki.pontuacao < ordenados[0].pontuacao


def test_hipoteses_cobrem_eixos_exigidos() -> None:
    hs = montar_hipoteses(MARCA)
    ids = {h.id.split(":")[0] for h in hs}
    assert {"H1-identidade", "H2-produto", "H3-servico", "H4-linguagem-oficial", "H5-estabelecimento", "H6-plataforma"} <= ids
    assert all("-site:bv.com.br" in q for h in hs for q in h.consultas_palavras_chave)


def test_priorizar_exclui_dominios_oficiais_perfis_sociais_e_indices_de_tags() -> None:
    resultados = [
        ResultadoDescoberta(fonte="brave", url_original="https://www2.bvfinanceira.com.br/acesso/clientes", titulo="Acesso"),
        ResultadoDescoberta(fonte="brave", url_original="https://developers-sandbox.bvopen.com.br/apis", titulo="APIs"),
        ResultadoDescoberta(fonte="brave", url_original="https://napista.com.br/motos", titulo="NaPista Motos"),
        ResultadoDescoberta(fonte="brave", url_original="https://www.instagram.com/bancobv/", titulo="banco BV (@bancobv)"),
        ResultadoDescoberta(fonte="brave", url_original="https://www.facebook.com/bancobv/videos/123/", titulo="BV"),
        ResultadoDescoberta(fonte="brave", url_original="https://www.tiktok.com/@bancobv/video/123", titulo="banco BV"),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://www.instagram.com/reel/DYpFTKTpnS-/",
            titulo='banco BV on Instagram: "Aqui o crédito é o primeiro passo"',
        ),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://www.instagram.com/popular/bv-financeira-parceiro/",
            titulo="Bv Financeira Parceiro",
        ),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://www.tiktok.com/discover/banco-bv-es-seguro",
            titulo="Banco Bv Es Seguro",
        ),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://m.facebook.com/public/Bv-Financiamentos/",
            titulo="People named Bv Financiamentos",
        ),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://armazviplprd.blob.core.windows.net/pdf/72b9840d-edc0",
            titulo="Edital Leilão PDF",
        ),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://guiacidade.example/brasilia-df/empresa/bv-financeira-sa-credito",
            titulo="Guia Cidade - BV Financeira SA",
        ),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://bvinvestimentos.com.br/fundos",
            titulo="BV Investimentos",
        ),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://bkbv.lovable.app/",
            titulo="Banco BV — 2ª via de boleto e quitação",
        ),
    ]
    grupos, _ = deduplicar(resultados, PoliticaUrl())
    ordenados = priorizar(grupos, MARCA)
    mantidos = [g for g in ordenados if g.motivo_exclusao is None]
    excluidos = {g.url_normalizada: g.motivo_exclusao for g in ordenados if g.motivo_exclusao is not None}
    assert [g.hostname for g in mantidos] == ["bkbv.lovable.app"]
    assert len(excluidos) == 13
    assert excluidos["https://www2.bvfinanceira.com.br/acesso/clientes"] == "domínio institucional oficial"
    assert excluidos["https://bvinvestimentos.com.br/fundos"] == "domínio institucional oficial"
    assert excluidos["https://developers-sandbox.bvopen.com.br/apis"] == "domínio institucional oficial"
    assert excluidos["https://instagram.com/bancobv"] == "perfil social oficial da instituição"
    assert excluidos["https://instagram.com/reel/DYpFTKTpnS-"] == "perfil social oficial da instituição"
    assert excluidos["https://instagram.com/popular/bv-financeira-parceiro"] == "página de tag/busca de rede social"
    assert excluidos["https://tiktok.com/discover/banco-bv-es-seguro"] == "página de tag/busca de rede social"
    assert excluidos["https://armazviplprd.blob.core.windows.net/pdf/72b9840d-edc0"] == "link direto para PDF/arquivo"
    assert (
        excluidos["https://guiacidade.example/brasilia-df/empresa/bv-financeira-sa-credito"]
        == "ficha da própria instituição em diretório de empresas"
    )


def test_priorizar_rebaixa_ruido_e_preserva_diversidade_de_dominios_e_pagina_inicial() -> None:
    resultados = [
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://www.bvfinanon.example/",
            titulo="Banco BV - Canal Oficial 2ª via de boleto",
        ),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://www.bv-brc.org/",
            titulo="BV-BRC: Bacterial and Viral Bioinformatics Resource Center",
        ),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://facebook.com/in.el/posts/agencia-bv-model",
            titulo="Agência BV Model Localizada em Moçambique",
        ),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://sp.olx.com.br/sao-paulo/autos-e-pecas/motos/honda-cg-160-12345",
            titulo="Honda CG 160 - financio pela BV Financeira",
        ),
        ResultadoDescoberta(
            fonte="brave",
            url_original="https://ajuda.crm.example/adicionar-simulador-da-bv-financeira/",
            titulo="Como adicionar o simulador da BV Financeira no site",
        ),
        # mesmo domínio: 3 páginas regionais + a inicial
        ResultadoDescoberta(fonte="brave", url_original="https://multicred.example/financiamento/recife", titulo="Banco BV"),
        ResultadoDescoberta(fonte="brave", url_original="https://multicred.example/financiamento/manaus", titulo="Banco BV"),
        ResultadoDescoberta(fonte="brave", url_original="https://multicred.example/financiamento/goiania", titulo="Banco BV"),
        ResultadoDescoberta(fonte="brave", url_original="https://multicred.example/", titulo="Multicred"),
    ]
    grupos, _ = deduplicar(resultados, PoliticaUrl())
    ordenados = priorizar(grupos, MARCA)
    por_url = {g.url_normalizada: g for g in ordenados}
    assert ordenados[0].hostname == "bvfinanon.example"
    # domínio científico estrangeiro com "bv-" não casa com os padrões da marca
    assert por_url["https://bv-brc.org/"].pontuacao < 0
    # "Agência BV Model" é agência de modelos, não canal oficial
    assert "linguagem de canal oficial no título/trecho" not in por_url["https://facebook.com/in.el/posts/agencia-bv-model"].motivos
    # anúncio avulso da OLX e tutorial de ajuda ficam para trás
    assert por_url["https://sp.olx.com.br/sao-paulo/autos-e-pecas/motos/honda-cg-160-12345"].pontuacao < 0
    assert por_url["https://ajuda.crm.example/adicionar-simulador-da-bv-financeira"].pontuacao < 0
    # a 3ª página regional (ordem alfabética: goiania, manaus, recife) é penalizada; a inicial nunca
    assert any("página extra" in r for r in por_url["https://multicred.example/financiamento/recife"].motivos)
    assert not any("página extra" in r for r in por_url["https://multicred.example/"].motivos)


def _transporte(payload: dict, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(status, json=payload))


async def test_brave_interpretacao_e_erro() -> None:
    h = HipoteseBusca(id="H", descricao="", prompt_grounding="")
    ok = BuscaBrave("k", transporte=_transporte({"web": {"results": [{"url": "https://a.example/", "title": "A"}]}}))
    resultados, rec = await ok.executar(h, "q")
    assert rec.status == "OK" and resultados[0].url_original == "https://a.example/" and resultados[0].fonte == "brave"
    ruim = BuscaBrave("k", transporte=_transporte({"error": "x"}, status=500))
    resultados, rec = await ruim.executar(h, "q")
    assert resultados == [] and rec.status == "ERRO"
    vazio = BuscaSerper("k", transporte=_transporte({"organic": []}))
    resultados, rec = await vazio.executar(h, "q")
    assert rec.status == "VAZIA"
    us = BuscaUrlscan(
        "k", transporte=_transporte({"results": [{"page": {"url": "https://u.example/", "domain": "u.example"}}]})
    )
    resultados, rec = await us.executar(h, "q")
    assert resultados[0].url_original == "https://u.example/"


def test_conectores_indisponiveis_sem_chaves() -> None:
    assert BuscaBrave(None).disponivel()[0] is False
    assert ConectorGrounding(None, None, PoliticaUrl(), []).disponivel()[0] is False


def test_interpretar_resposta_grounding_usa_so_metadados() -> None:
    from types import SimpleNamespace as NS

    resposta_falsa = NS(
        text="Veja também https://inventado.example/ que não está nas fontes.",
        usage_metadata=NS(prompt_token_count=10, candidates_token_count=5, total_token_count=15),
        candidates=[
            NS(
                grounding_metadata=NS(
                    web_search_queries=["financiamento banco bv whatsapp"],
                    search_entry_point=NS(rendered_content="<div>chips</div>"),
                    grounding_chunks=[
                        NS(
                            web=NS(
                                uri="https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
                                title="loja.example",
                                domain="loja.example",
                            )
                        ),
                        NS(web=None),
                    ],
                    grounding_supports=[NS(segment=NS(text="A loja usa a marca"), grounding_chunk_indices=[0])],
                )
            )
        ],
    )
    res = interpretar_resposta_grounding(resposta_falsa, "m")
    assert [s.uri for s in res.fontes] == ["https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"]
    assert res.fontes[0].textos_suporte == ["A loja usa a marca"]
    assert res.consultas_busca_web == ["financiamento banco bv whatsapp"]
    assert res.uso.tokens_total == 15
    assert all("inventado" not in s.uri for s in res.fontes)


async def test_conector_grounding_resolve_redirecionamentos(armazenamento, monkeypatch) -> None:
    redirecionamento = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"

    def tratador(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "vertexaisearch.cloud.google.com"
        return httpx.Response(302, headers={"location": "https://loja-alvo.example/financiamento"})

    politica = PoliticaUrl()
    monkeypatch.setattr(politica, "_resolver_sincrono", lambda h, p: ["93.184.215.14"])

    async def verificar_async_falso(url: str) -> str:
        return politica.verificar(url)

    monkeypatch.setattr(politica, "verificar_async", verificar_async_falso)
    provedor = ProvedorMock(
        com_grounding=lambda p: resultado_grounding(
            [(redirecionamento, "loja-alvo.example"), ("https://www.google.com/search?q=x", "g")]
        )
    )
    cid = armazenamento.criar_campanha("bv", {})
    gw = GatewayLLM(provedor, Orcamento(armazenamento, cid, 10, 10_000), max_retentativas=0, atraso_base_s=0)
    con = ConectorGrounding(gw, "m", politica, ["bv.com.br"], transporte=httpx.MockTransport(tratador))
    resultados, rec = await con.executar(HipoteseBusca(id="H1", descricao="", prompt_grounding="p"), "p")
    assert [h.url_original for h in resultados] == ["https://loja-alvo.example/financiamento"]
    assert resultados[0].metadados["uri_grounding"] == redirecionamento
    assert rec.metadados["nao_resolvidos"][0]["error"].startswith("página de resultados")
    assert armazenamento.totais_uso(cid)[0] == 1


async def test_descoberta_alternativa_quando_grounding_falha(armazenamento) -> None:
    class GroundingComFalha:
        nome = "grounding"
        desativado: str | None = None

        def disponivel(self):
            return (self.desativado is None, "grounding: test")

        async def executar(self, h, q):
            self.desativado = "403 permission denied"
            return [], RegistroConsulta(fonte="grounding", consulta=q, status="ERRO", erro=self.desativado)

    class BraveFalso:
        nome = "brave"

        def disponivel(self):
            return True, "brave: configured"

        async def executar(self, h, q):
            return (
                [ResultadoDescoberta(fonte="brave", url_original="https://x.example/", consulta=q)],
                RegistroConsulta(fonte="brave", consulta=q, qtd_resultados=1),
            )

    agente = AgenteDescoberta(MARCA, [GroundingComFalha(), BraveFalso()], PoliticaUrl(), max_consultas=10)
    saida = await agente.executar()
    assert saida.fontes_usadas == ["brave"]
    assert sum(1 for q in saida.consultas if q.fonte == "grounding") == 1  # desligado após o primeiro erro
    assert any("seguindo com as outras fontes" in d for d in saida.diagnosticos)
    assert len([q for q in saida.consultas if q.fonte == "brave"]) >= 5


async def test_descoberta_sem_fonte_gera_diagnostico() -> None:
    agente = AgenteDescoberta(MARCA, [BuscaBrave(None), ConectorGrounding(None, None, PoliticaUrl(), [])], PoliticaUrl())
    saida = await agente.executar()
    assert saida.resultados == [] and any("Nenhuma fonte de descoberta" in d for d in saida.diagnosticos)
    saida2 = await agente.executar(importar_texto("https://a.example/"))
    assert len(saida2.resultados) == 1 and "importacao" in saida2.fontes_usadas


async def test_grounding_fica_indisponivel_em_erro_de_auth(armazenamento) -> None:
    def explodir(prompt: str):
        raise LLMIndisponivel("403: permission denied")

    provedor = ProvedorMock(com_grounding=explodir)
    cid = armazenamento.criar_campanha("bv", {})
    gw = GatewayLLM(provedor, Orcamento(armazenamento, cid, 10, 10_000), max_retentativas=0, atraso_base_s=0)
    con = ConectorGrounding(gw, "m", PoliticaUrl(), [])
    _, rec = await con.executar(HipoteseBusca(id="H", descricao="", prompt_grounding="p"), "p")
    assert rec.status == "ERRO" and con.disponivel()[0] is False
