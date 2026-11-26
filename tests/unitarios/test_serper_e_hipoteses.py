from __future__ import annotations

import json

import httpx

from simio.agentes.descoberta import REGIOES_BR, montar_hipoteses
from simio.marcas import carregar_marca
from simio.nucleo.modelos import HipoteseBusca
from simio.provedores.busca import BuscaSerper

BV = carregar_marca("bv", None)
H = HipoteseBusca(id="H", descricao="", prompt_grounding="")


def _serper(paginas: dict[int, list[dict[str, str]]], vistos: list[dict]) -> httpx.MockTransport:
    def tratador(requisicao: httpx.Request) -> httpx.Response:
        corpo = json.loads(requisicao.content)
        vistos.append(corpo)
        if corpo.get("num", 10) > 10:
            return httpx.Response(400, json={"message": "Query pattern not allowed for free accounts."})
        return httpx.Response(200, json={"organic": paginas.get(corpo["page"], [])})

    return httpx.MockTransport(tratador)


async def test_serper_pagina_de_10_em_10_e_para_em_pagina_curta() -> None:
    completo = [{"link": f"https://a{i}.com.br/", "title": "t"} for i in range(10)]
    curto = [{"link": "https://b.com.br/", "title": "t"}]
    vistos: list[dict] = []
    con = BuscaSerper("k", transporte=_serper({1: completo, 2: curto, 3: completo}, vistos), paginas=5)
    resultados, rec = await con.executar(H, '"Banco BV"')
    assert len(resultados) == 11 and rec.status == "OK"
    assert [b["page"] for b in vistos] == [1, 2] and all(b["num"] == 10 for b in vistos)


async def test_serper_descarta_redirecionamentos_opacos_do_google_e_informa_erros() -> None:
    linhas = [
        {"link": "https://www.instagram.com/p/abc/", "title": "ok"},
        {"link": "https://www.google.com/goto?url=CAES123", "title": "Feirão", "snippet": "post"},
    ]
    resultados, rec = await BuscaSerper("k", transporte=_serper({1: linhas}, [])).executar(H, "q")
    assert [h.url_original for h in resultados] == ["https://www.instagram.com/p/abc/"]
    assert rec.qtd_resultados == 1 and rec.metadados["redirecionamentos_google_nao_resolvidos"][0]["title"] == "Feirão"

    def ruim(requisicao: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Not enough credits"})

    _, rec = await BuscaSerper("k", transporte=httpx.MockTransport(ruim)).executar(H, "q")
    assert rec.status == "ERRO" and "Not enough credits" in (rec.erro or "")


def test_hipoteses_web_aberta_cobrem_tipologias_de_protecao_de_marca_sem_cobans() -> None:
    hs = montar_hipoteses(BV)
    ids = {h.id for h in hs}
    assert not any(i.startswith("H9") for i in ids)  # não depende do cadastro de COBANs
    for necessario in (
        "H2-produto:bancos-parceiros",
        "H6-plataforma:marketplaces",
        "H6-plataforma:Telegram-grupos",
        "H6-plataforma:YouTube-Kwai-Threads",
        "H10-fraude:leilao-retomados",
        "H10-fraude:agio-repasse",
        "H10-fraude:falso-emprestimo",
        "H10-fraude:app-phishing",
        "H10-fraude:central-falsa",
        "H10-fraude:vagas",
        "H11-napista:social",
    ):
        assert necessario in ids, necessario
    regional = " ".join(q for h in hs if h.id.startswith("H5-estabelecimento:regiao-") for q in h.consultas_palavras_chave)
    assert all(f'"{uf}"' in regional for ufs in REGIOES_BR.values() for uf in ufs)
    assert sum(len(ufs) for ufs in REGIOES_BR.values()) == 27
    # todo dork exclui os domínios oficiais
    assert all("-site:bv.com.br" in q for h in hs for q in h.consultas_palavras_chave)
