"""Operator-free fallback for Serper (Google hides operator results behind opaque links) and the public
Facebook plugins used to read pages and photos without a login wall."""

from __future__ import annotations

import json

import httpx

from simio.coletores.navegador import _url_incorporavel_social
from simio.nucleo.modelos import HipoteseBusca
from simio.provedores.busca import BuscaSerper, variantes_simples


def test_grupos_or_expandidos_em_rodizio_e_operadores_removidos() -> None:
    q = '(intitle:"Banco BV" OR intitle:"bv financeira") (login OR entrar) -site:bv.com.br -site:reclameaqui.com.br'
    variantes, excluidos = variantes_simples(q)
    assert variantes == ['"Banco BV" login', '"bv financeira" entrar']
    assert excluidos == ["bv.com.br", "reclameaqui.com.br"]


def test_operador_site_vira_palavra_da_plataforma() -> None:
    variantes, _ = variantes_simples('site:instagram.com ("Banco BV" OR "bv financeira") (feirão OR parceiro OR boleto)')
    assert variantes == ['instagram "Banco BV" feirão', 'instagram "bv financeira" parceiro', 'instagram "Banco BV" boleto']
    assert variantes_simples("site:t.me painel")[0] == ["telegram painel"]
    assert variantes_simples('"NaPista" (leilão OR pix OR taxa)', limite=2)[0] == ['"NaPista" leilão', '"NaPista" pix']


def test_consulta_simples_nao_tem_variante() -> None:
    assert variantes_simples('"bancos parceiros" "bv financeira"')[0] == []


async def test_serper_reexecuta_versoes_sem_operadores_quando_resultados_sao_opacos() -> None:
    pedidos: list[str] = []

    def tratador(requisicao: httpx.Request) -> httpx.Response:
        q = json.loads(requisicao.content)["q"]
        pedidos.append(q)
        if "site:" in q:
            organicos = [{"title": "x", "link": f"https://www.google.com/goto?url=C{i}", "displayedLink": "há 2 dias"}
                       for i in range(8)]
            organicos.append({"title": "Direto", "link": "https://loja.example/a"})
        else:
            organicos = [
                {"title": "Post", "link": f"https://www.instagram.com/p/{q[-3:]}/"},
                {"title": "Oficial", "link": "https://www.bv.com.br/x"},  # excluded by -site: client-side
                {"title": "Direto", "link": "https://loja.example/a"},  # duplicate of the first query
            ]
        return httpx.Response(200, json={"organic": organicos})

    con = BuscaSerper("k", transporte=httpx.MockTransport(tratador), variantes_simples=2)
    hip = HipoteseBusca(id="H", descricao="d", prompt_grounding="")
    resultados, registro = await con.executar(hip, 'site:instagram.com "bv" (abc OR xyz) -site:bv.com.br')
    assert pedidos[1:] == ['instagram "bv" abc', 'instagram "bv" xyz']
    urls = [h.url_original for h in resultados]
    assert urls == ["https://loja.example/a", "https://www.instagram.com/p/abc/", "https://www.instagram.com/p/xyz/"]
    assert resultados[1].metadados["variante_simples_de"].startswith("site:instagram.com")
    assert resultados[1].consulta == 'instagram "bv" abc'
    assert registro.metadados["variantes_simples"] == pedidos[1:]


async def test_serper_nao_gasta_creditos_quando_resultados_sao_diretos() -> None:
    pedidos: list[str] = []

    def tratador(requisicao: httpx.Request) -> httpx.Response:
        pedidos.append(json.loads(requisicao.content)["q"])
        return httpx.Response(200, json={"organic": [{"title": "t", "link": "https://loja.example/"}]})

    con = BuscaSerper("k", transporte=httpx.MockTransport(tratador), variantes_simples=3)
    await con.executar(HipoteseBusca(id="H", descricao="d", prompt_grounding=""), 'site:loja.example "bv"')
    assert len(pedidos) == 1


def test_paginas_e_fotos_do_facebook_usam_plugins_publicos() -> None:
    pagina = _url_incorporavel_social("https://www.facebook.com/bvveiculoseconsorcios/?locale=zh_TW")
    assert pagina.startswith("https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2Fbvveiculos")
    assert "tabs=timeline" in pagina
    assert "profile.php%3Fid%3D615" in _url_incorporavel_social("https://www.facebook.com/profile.php?id=61594249927645")
    assert "/plugins/post.php" in _url_incorporavel_social("https://www.facebook.com/photo.php?fbid=1&set=a.2")
    # Public group posts render better as they are; reserved paths are left alone.
    grupo = "https://www.facebook.com/groups/1/posts/2/"
    assert _url_incorporavel_social(grupo) == grupo
    assert _url_incorporavel_social("https://www.facebook.com/marketplace/") == "https://www.facebook.com/marketplace/"


def test_hostname_de_tolera_links_malformados() -> None:
    from simio.nucleo.urls import hostname_de

    assert hostname_de("http://[broken/link") == ""
    assert hostname_de("https://WWW.Loja.example./a") == "www.loja.example"
