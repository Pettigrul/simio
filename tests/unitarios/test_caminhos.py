from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from simio.nucleo import armazenamento as mod_armazenamento
from simio.nucleo import caminhos
from simio.nucleo.armazenamento import Armazenamento
from simio.relatorios import arquivar_relatorios_anteriores

FORMATO_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}h\d{2}_[a-z0-9.-]+_(ampla|ameacas|logo-antiga)(-\d+)?$")


def test_carimbo_usa_horario_de_brasilia() -> None:
    assert caminhos.carimbo(datetime(2026, 11, 25, 22, 5, 19, tzinfo=UTC)) == "2026-11-25_19h05"
    assert caminhos.carimbo(datetime(2026, 11, 26, 1, 30)) == "2026-11-25_22h30"  # sem fuso = UTC


def test_formato_do_id_da_campanha() -> None:
    momento = datetime(2026, 11, 25, 22, 5, tzinfo=UTC)
    assert caminhos.id_campanha(momento, "bv", "ameacas") == "2026-11-25_19h05_bv_ameacas"
    assert caminhos.id_campanha(momento, "bv", None) == "2026-11-25_19h05_bv_ampla"
    assert caminhos.id_campanha(momento, "BV", "logo-antiga") == "2026-11-25_19h05_bv_logo-antiga"
    assert caminhos.com_sufixo("x", 1) == "x" and caminhos.com_sufixo("x", 3) == "x-3"


def _relogio_parado(monkeypatch: pytest.MonkeyPatch, momento: datetime) -> None:
    class Parado(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return momento if tz is None else momento.astimezone(tz)

    monkeypatch.setattr(mod_armazenamento, "datetime", Parado)


def test_campanhas_no_mesmo_minuto_ganham_sufixo(configuracoes, monkeypatch: pytest.MonkeyPatch) -> None:
    _relogio_parado(monkeypatch, datetime(2026, 11, 25, 20, 45, 10, tzinfo=UTC))
    st = Armazenamento(configuracoes.caminho_db)
    try:
        ids = [st.criar_campanha("bv", {"foco": "logo-antiga"}) for _ in range(3)]
        assert ids == [
            "2026-11-25_17h45_bv_logo-antiga",
            "2026-11-25_17h45_bv_logo-antiga-2",
            "2026-11-25_17h45_bv_logo-antiga-3",
        ]
        assert st.criar_campanha("bv", {}) == "2026-11-25_17h45_bv_ampla"
        # pasta que ficou no disco sem linha no banco também não é reaproveitada
        caminhos.dir_campanha(configuracoes.dir_saida, "2026-11-25_17h45_bv_ameacas").mkdir(parents=True)
        assert st.criar_campanha("bv", {"foco": "ameacas"}) == "2026-11-25_17h45_bv_ameacas-2"
        assert all(FORMATO_ID.match(i) for i in ids)
    finally:
        st.fechar()


def test_candidata_existente_eh_achada_pela_url_mesmo_com_id_de_campanha_novo(armazenamento) -> None:
    cid_campanha = armazenamento.criar_campanha("bv", {})
    url = "https://loja.example/"
    cid, criada = armazenamento.upsert_candidata(
        cid_campanha, url=url, url_normalizada=url, hostname="loja.example", dominio_registravel="loja.example"
    )
    assert criada
    novo = "2026-01-01_00h00_bv_ampla"
    con = armazenamento.con
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("UPDATE campanhas SET id=?, parametros_json='{\"id_anterior\": \"simio-velho\"}' WHERE id=?", (novo, cid_campanha))
    con.execute("UPDATE candidatas SET id_campanha=? WHERE id_campanha=?", (novo, cid_campanha))
    con.execute("UPDATE dominios SET id_campanha=? WHERE id_campanha=?", (novo, cid_campanha))
    con.execute("PRAGMA foreign_keys=ON")
    mesma, criada = armazenamento.upsert_candidata(
        novo, url=url, url_normalizada=url, hostname="loja.example", dominio_registravel="loja.example"
    )
    assert (mesma, criada) == (cid, False)
    assert armazenamento.resolver_id_campanha("simio-velho") == novo
    assert armazenamento.resolver_id_campanha(novo) == novo
    assert armazenamento.resolver_id_campanha("nao-existe") is None


def test_chave_da_candidata() -> None:
    assert caminhos.chave_candidata("www.Loja.Example", "C00e3c53d8c") == "loja.example_00e3c53d"
    assert caminhos.chave_candidata("xn--bv-oja.example", "C1") == "xn--bv-oja.example_1"
    assert caminhos.chave_candidata("bvção.example", "C2").startswith("xn--")
    assert caminhos.chave_candidata("", "C3") == "sem-host_3"
    assert caminhos.chave_candidata("a_b:8080", "Cff") == "a-b-8080_ff"
    longo = caminhos.chave_candidata("a" * 50 + "." + "b" * 50 + ".example", "C0123456789")
    assert longo == "a" * 50 + "." + "b" * 9 + "_01234567"


def test_caminhos_da_campanha(tmp_path: Path) -> None:
    pasta = caminhos.dir_campanha(tmp_path, "2026-11-25_19h05_bv_ameacas")
    assert pasta == tmp_path / "campanhas" / "2026-11-25_19h05_bv_ameacas"
    rel = caminhos.relatorios(pasta)
    assert [p.name for p in (rel.planilha, rel.html, rel.pdf, rel.ameacas, rel.dossies, rel.logo_antiga)] == [
        "relatorio.xlsx",
        "relatorio.html",
        "relatorio.pdf",
        "ameacas.html",
        "dossies",
        "logo-antiga",
    ]
    assert caminhos.arquivo_dossie(pasta, "loja.example", "C00e3c53d8c") == pasta / "dossies/loja.example_00e3c53d.html"
    assert caminhos.arquivo_logo_antiga(pasta, "loja.example", "C1").name == "loja.example_1.png"
    assert str(caminhos.dir_tentativa("loja.example", "C1", 2)) == "evidencias/loja.example_1/tentativa-2"
    assert caminhos.arquivo_dominios(pasta).name == "dominios.json"
    assert caminhos.arquivo_log(tmp_path) == tmp_path / "registros" / "simio.jsonl"
    assert caminhos.arquivo_banco(tmp_path) == tmp_path / "simio.db"
    assert caminhos.arquivo_renomeacoes(pasta) == pasta / "evidencias" / "renomeacoes.json"
    assert set(caminhos.ARTEFATOS.values()) == {
        "tela-inicial.png",
        "pagina-inteira.png",
        "codigo-fonte.html.txt",
        "texto-visivel.txt",
        "dados-da-pagina.json",
    }
    dossie = caminhos.arquivo_dossie(pasta, "loja.example", "C1")
    captura = pasta / caminhos.dir_tentativa("loja.example", "C1", 1) / "tela-inicial.png"
    assert caminhos.link(dossie, captura) == "../evidencias/loja.example_1/tentativa-1/tela-inicial.png"
    assert caminhos.link(rel.html, dossie) == "dossies/loja.example_1.html"


def test_pontuacao_aceita_o_nome_antigo(tmp_path: Path) -> None:
    assert caminhos.arquivo_pontuacao(tmp_path).name == "pontuacao.toml"
    (tmp_path / "scoring.toml").write_text("", encoding="utf-8")
    assert caminhos.arquivo_pontuacao(tmp_path).name == "scoring.toml"
    (tmp_path / "pontuacao.toml").write_text("", encoding="utf-8")
    assert caminhos.arquivo_pontuacao(tmp_path).name == "pontuacao.toml"


def test_relatorio_anterior_vai_para_anteriores_com_links_ajustados(tmp_path: Path) -> None:
    pasta = tmp_path / "campanha"
    captura = pasta / "evidencias/loja.example_1/tentativa-1/tela-inicial.png"
    captura.parent.mkdir(parents=True)
    captura.write_bytes(b"png")
    rel = caminhos.relatorios(pasta)
    rel.planilha.write_bytes(b"xlsx")
    rel.ameacas.write_text('<img src="evidencias/loja.example_1/tentativa-1/tela-inicial.png">', encoding="utf-8")
    rel.dossies.mkdir()
    (rel.dossies / "loja.example_1.html").write_text(
        '<img src="../evidencias/loja.example_1/tentativa-1/tela-inicial.png"><a href="#E-TXT-001">x</a>',
        encoding="utf-8",
    )
    assert arquivar_relatorios_anteriores(tmp_path / "vazia") is None
    destino = arquivar_relatorios_anteriores(pasta)
    assert destino is not None and destino.parent == pasta / "anteriores"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}h\d{2}", destino.name)
    assert not rel.planilha.exists() and not rel.dossies.exists() and not rel.ameacas.exists()
    assert (destino / "relatorio.xlsx").read_bytes() == b"xlsx"
    for html in (destino / "ameacas.html", destino / "dossies" / "loja.example_1.html"):
        src = re.search(r'src="([^"]+)"', html.read_text(encoding="utf-8"))
        assert src and (html.parent / src.group(1)).resolve() == captura.resolve()
    assert 'href="#E-TXT-001"' in (destino / "dossies" / "loja.example_1.html").read_text(encoding="utf-8")
    rel.planilha.write_bytes(b"xlsx2")
    segundo = arquivar_relatorios_anteriores(pasta)
    assert segundo is not None and segundo != destino and (segundo / "relatorio.xlsx").read_bytes() == b"xlsx2"


def test_planilha_arquivada_mantem_links_para_as_evidencias(tmp_path: Path) -> None:
    from openpyxl import Workbook, load_workbook

    pasta = tmp_path / "campanha"
    captura = pasta / "evidencias/loja.example_1/tentativa-1/tela-inicial.png"
    captura.parent.mkdir(parents=True)
    captura.write_bytes(b"png")
    rel = caminhos.relatorios(pasta)
    wb = Workbook()
    ws = wb.active
    alvo = "evidencias/loja.example_1/tentativa-1/tela-inicial.png"
    ws["A1"], ws["B1"] = alvo, "dossies/loja.example_1.html"
    ws["A1"].hyperlink, ws["B1"].hyperlink = alvo, "dossies/loja.example_1.html"
    wb.save(rel.planilha)
    destino = arquivar_relatorios_anteriores(pasta)
    assert destino is not None
    ws = load_workbook(destino / "relatorio.xlsx").active
    assert ws["A1"].value == ws["A1"].hyperlink.target == "../../" + alvo
    assert (destino / ws["A1"].hyperlink.target).resolve() == captura.resolve()
    assert ws["B1"].hyperlink.target == "dossies/loja.example_1.html"
