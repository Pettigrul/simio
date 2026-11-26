from __future__ import annotations

from pathlib import Path

from simio.agentes.descoberta import AgenteDescoberta, hipoteses_coban, montar_hipoteses, nome_busca_coban
from simio.marcas import carregar_marca
from simio.nucleo.armazenamento import Armazenamento
from simio.nucleo.modelos import RegistroConsulta
from simio.seguranca.politica_url import PoliticaUrl

BV = carregar_marca("bv", None)


def _registro(nome: str, data: str) -> dict[str, str]:
    return {"nome": nome, "data_cadastro_recente": data}


REGISTROS = {
    "11111111000111": _registro("ANTIGA VEICULOS LTDA", "2016-01-01"),
    "22222222000122": _registro("NOVA MOTOS LTDA - ME", "2026-09-30"),
    "33333333000133": _registro("JOAO DA SILVA 12345678901", "2026-10-01"),  # MEI with CPF: never searched
    "44444444000144": _registro("BANCO EXEMPLO SA", "2026-10-02"),  # financial institution
    "55555555000155": _registro("SOLAR FORTE ENERGIA EIRELI", "2026-08-15"),
    "66666666000166": _registro("NOVA MOTOS LTDA", "2026-07-01"),  # same searchable name as 2222...
}


def test_nome_de_busca_remove_sufixos_juridicos_e_pula_pessoas_e_bancos() -> None:
    assert nome_busca_coban("DORNELAS MOTOS LTDA - ME") == "DORNELAS MOTOS"
    assert nome_busca_coban("AUTO PECAS JOSE E CIA") == "AUTO PECAS JOSE"
    assert nome_busca_coban("VALTER LIMA DA SILVA 09893246725") is None
    assert nome_busca_coban("BANCO DO BRASIL SA") is None
    assert nome_busca_coban("ME LTDA") is None


def test_mais_recentes_primeiro_em_rodizio_e_lotes() -> None:
    hips, n = hipoteses_coban(REGISTROS, BV, limite=10, lote=2)
    assert n == 3  # pula pessoa física, banco e nome repetido
    assert [h.id for h in hips] == ["H9-coban:22222222000122,55555555000155", "H9-coban:11111111000111"]
    assert "NOVA MOTOS" in hips[0].prompt_grounding and "22.222.222/0001-22" in hips[0].prompt_grounding
    assert all("-site:bv.com.br" in q for q in hips[0].consultas_palavras_chave)
    hips2, n2 = hipoteses_coban(REGISTROS, BV, limite=1, lote=4, excluir={"22222222000122"})
    assert n2 == 1 and hips2[0].id == "H9-coban:55555555000155"
    assert hipoteses_coban(REGISTROS, BV, limite=0)[0] == []


def test_hipoteses_coban_entram_depois_da_varredura_aberta() -> None:
    class GroundingFalso:
        nome = "grounding"

    hips, _ = hipoteses_coban(REGISTROS, BV, limite=2, lote=2)
    agente = AgenteDescoberta(BV, [], PoliticaUrl(), max_consultas=500, hipoteses_extras=hips)
    planejar = agente.planejar([GroundingFalso()])  # type: ignore[list-item]
    ids = [p.hipotese.id for p in planejar]
    assert len(ids) == len(montar_hipoteses(BV)) + 1
    assert ids[-1].startswith("H9-coban:") and not any(i.startswith("H9") for i in ids[:-1])


def test_armazenamento_lembra_correspondentes_pesquisados_entre_campanhas(tmp_path: Path) -> None:
    st = Armazenamento(tmp_path / "s.db")
    c1 = st.criar_campanha("bv", {})
    c2 = st.criar_campanha("bv", {})
    st.registrar_consulta(c1, RegistroConsulta(fonte="grounding", consulta="q", id_hipotese="H9-coban:1,2", status="OK"))
    st.registrar_consulta(c1, RegistroConsulta(fonte="grounding", consulta="q", id_hipotese="H9-coban:3", status="ERRO"))
    st.registrar_consulta(c2, RegistroConsulta(fonte="grounding", consulta="q", id_hipotese="H9-coban:4", status="VAZIA"))
    assert st.cnpjs_coban_pesquisados() == {"1", "2", "4"}  # busca que falhou é refeita depois
    assert st.cnpjs_coban_pesquisados(excluir_campanha=c2) == {"1", "2"}  # a retomada mantém a própria seleção
    st.fechar()
