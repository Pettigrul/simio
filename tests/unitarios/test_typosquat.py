from __future__ import annotations

from simio.agentes.descoberta import AgenteDescoberta, deduplicar, priorizar
from simio.marcas import carregar_marca
from simio.nucleo.modelos import HipoteseBusca
from simio.provedores.typosquat import ConectorTyposquat, dominios_semelhantes, variantes_typo
from simio.seguranca.politica_url import PoliticaUrl

BV = carregar_marca("bv", None)
H = HipoteseBusca(id="H8", descricao="", prompt_grounding="")


def test_variantes_typo_cobrem_fuzzers_estilo_dnstwist() -> None:
    v = variantes_typo("bancobv")
    assert v["bancbv"] == "omission"
    assert v["bancobbv"] == "repetition"
    assert v["bnacobv"] == "transposition"
    assert "bancobvv" in v
    assert v["banc0bv"] == "homoglyph"
    assert v["banco-bv"] == "hyphenation"
    assert v["bencobv"] == "vowel-swap"
    assert "bancobv" not in v
    assert all(not x.startswith("-") and not x.endswith("-") for x in v)


def test_rotulos_curtos_recebem_so_combos_e_trocas_de_tld() -> None:
    d = dominios_semelhantes(["bv.com.br"], ["bv"], ["financiamento"], excluir=BV.eh_oficial)
    fuzzers = {f for f, _ in d.values()}
    assert fuzzers == {"tld-swap", "combosquatting"}  # sem ruído do tipo "bb.com.br"
    assert "bv-financiamento.com.br" in d and "financiamentobv.com" in d
    assert "bv.com" in d and "bv.com.br" not in d


def test_dominios_oficiais_nunca_sao_candidatas() -> None:
    d = dominios_semelhantes(BV.sementes_typosquat, BV.rotulos_combo_typosquat, BV.palavras_chave_typosquat, excluir=BV.eh_oficial)
    assert not any(BV.eh_oficial(x) for x in d)
    assert "bvfinanceira.com.br" not in d and "bancobv.com.br" not in d


def _resolvedor(tabela: dict[str, list[str]]):
    async def resolver(host: str) -> list[str]:
        if host in tabela:
            return tabela[host]
        raise OSError("NXDOMAIN")

    return resolver


async def test_so_semelhantes_que_resolvem_publicamente_viram_resultados() -> None:
    con = ConectorTyposquat(
        BV,
        resolvedor=_resolvedor(
            {"bancobva.com.br": ["104.21.5.101"], "bancbv.com.br": ["127.0.0.1"], "banco-bv.com.br": ["10.0.0.5"]}
        ),
    )
    resultados, rec = await con.executar(H, "typo:bancobv.com.br")
    assert [h.url_original for h in resultados] == ["http://bancobva.com.br/"]
    assert resultados[0].metadados["fuzzer"] == "addition" and resultados[0].metadados["semelhante_a"] == "bancobv.com.br"
    assert "semelhante" in (resultados[0].trecho or "")
    assert rec.status == "OK" and rec.metadados["permutacoes_verificadas"] > 100
    assert set(rec.metadados["resolvidos_nao_publicos"]) == {"bancbv.com.br", "banco-bv.com.br"}


async def test_resolvedor_que_sequestra_nxdomain_desativa_conector() -> None:
    async def tudo_resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    con = ConectorTyposquat(BV, resolvedor=tudo_resolve)
    resultados, rec = await con.executar(H, "combo:bv")
    assert resultados == [] and rec.status == "ERRO" and "NXDOMAIN" in (rec.erro or "")
    assert con.disponivel()[0] is False


async def test_descoberta_planeja_consultas_semelhantes_e_prioriza_resultados() -> None:
    con = ConectorTyposquat(BV, resolvedor=_resolvedor({"feiraobv.com.br": ["104.21.74.215"]}))
    agente = AgenteDescoberta(BV, [con], PoliticaUrl(), max_consultas=50)
    planejar = agente.planejar([con])
    assert [p.consulta for p in planejar] == con.consultas()
    assert all(p.hipotese.id.startswith("H8-semelhante:") for p in planejar)
    desfecho = await agente.executar()
    grupos, rejeitados = deduplicar(desfecho.resultados, PoliticaUrl())
    assert not rejeitados
    [g] = priorizar(grupos, BV)
    assert g.hostname == "feiraobv.com.br" and g.pontuacao >= 3
    assert any("domínio parecido com bv" in r for r in g.motivos)
