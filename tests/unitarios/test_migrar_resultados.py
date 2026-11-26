from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from simio.nucleo import caminhos
from simio.nucleo.armazenamento import Armazenamento
from simio.nucleo.modelos import Artefato, Evidencia, StatusColeta

RAIZ = Path(__file__).resolve().parents[2]

CAMPANHAS = [
    ("simio-20261118-184932-cc75", None, "2026-11-18T18:49:32+00:00", "2026-11-18_15h49_bv_ampla"),
    ("simio-20261125-204500-58aa", "logo-antiga", "2026-11-25T20:45:00+00:00", "2026-11-25_17h45_bv_logo-antiga"),
    ("simio-20261125-204527-2fb3", "logo-antiga", "2026-11-25T20:45:27+00:00", "2026-11-25_17h45_bv_logo-antiga-2"),
    ("simio-20261125-220519-d778", "ameacas", "2026-11-25T22:05:19+00:00", "2026-11-25_19h05_bv_ameacas"),
]
ANTIGOS = ("viewport.png", "fullpage.png", "page.html.txt", "text.txt", "page.json")
TIPOS_MANIFESTO = ("screenshot_viewport", "screenshot_full", "html", "text", "page_data")
TIPOS = ("captura_viewport", "captura_completa", "html", "texto", "dados_pagina")


def _modulo():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migrar_resultados", RAIZ / "scripts" / "migrar_resultados.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses procuram o próprio módulo
    spec.loader.exec_module(mod)
    return mod


def _sha(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def _coleta_antiga(pasta: Path, cid: str, url: str, tentativa: int, sep: str = "/") -> tuple[Evidencia, bytes]:
    """Pasta de tentativa como o coletor antigo gravava; ``sep="\\\\"`` imita uma execução no Windows."""
    rel = f"evidence{sep}{cid}{sep}attempt-{tentativa}"
    (pasta / "evidence" / cid / f"attempt-{tentativa}").mkdir(parents=True)
    artefatos = []
    for nome, tipo in zip(ANTIGOS, TIPOS_MANIFESTO, strict=True):
        dados = f"{cid} {tentativa} {nome}".encode()
        (pasta / "evidence" / cid / f"attempt-{tentativa}" / nome).write_bytes(dados)
        artefatos.append({"kind": tipo, "path": f"{rel}{sep}{nome}", "sha256": _sha(dados), "size_bytes": len(dados)})
    manifesto = json.dumps({"collector": "browser-collector/1", "artifacts": artefatos}, indent=2).encode()
    (pasta / "evidence" / cid / f"attempt-{tentativa}" / "manifest.json").write_bytes(manifesto)
    ev = Evidencia(
        id_candidata=cid,
        tentativa=tentativa,
        status=StatusColeta.OK,
        url_original=url,
        artefatos=[
            Artefato(tipo=tipo, caminho=a["path"], sha256=a["sha256"], tamanho_bytes=a["size_bytes"])
            for tipo, a in zip(TIPOS, artefatos, strict=True)
        ],
        caminho_manifesto=f"{rel}{sep}manifest.json",
        sha256_manifesto=_sha(manifesto),
    )
    return ev, manifesto


def _montar(raiz: Path) -> dict[str, bytes]:
    origem = raiz / "output"
    st = Armazenamento(origem / "simio.db")
    manifestos: dict[str, bytes] = {}
    try:
        for antigo, foco, criado, _ in CAMPANHAS:
            st.con.execute(
                "INSERT INTO campanhas(id, marca, status, parametros_json, criado_em, atualizado_em)"
                " VALUES (?,?,?,?,?,?)",
                (antigo, "bv", "CONCLUIDA", json.dumps({"marca": "bv", "foco": foco}), criado, criado),
            )
            st.salvar_checkpoint(antigo, "descoberta", {"urls_unicas": 1})
            st.registrar_erro(antigo, None, "descoberta", "X", "erro")
            st.registrar_uso(antigo, "analista_marca", "m", 1, 1, 2)
        windows, d778 = CAMPANHAS[0][0], CAMPANHAS[3][0]
        pasta_win = origem / "campaigns" / windows
        cid, _ = st.upsert_candidata(
            windows,
            url="https://www.loja.example/",
            url_normalizada="https://loja.example/",
            hostname="loja.example",
            dominio_registravel="loja.example",
        )
        ev, manifestos[f"{windows}/{cid}/1"] = _coleta_antiga(pasta_win, cid, "https://www.loja.example/", 1, "\\")
        st.salvar_evidencia(ev)
        st.registrar_decisao(windows, cid, "evidencia", "OK", {"tentativa": 1, "manifesto": ev.caminho_manifesto})

        pasta = origem / "campaigns" / d778
        cid2, _ = st.upsert_candidata(
            d778,
            url="https://falso-bv.example/login",
            url_normalizada="https://falso-bv.example/login",
            hostname="falso-bv.example",
            dominio_registravel="falso-bv.example",
        )
        for tentativa in (1, 2):
            ev, manifestos[f"{d778}/{cid2}/{tentativa}"] = _coleta_antiga(
                pasta, cid2, "https://falso-bv.example/login", tentativa
            )
            st.salvar_evidencia(ev)
            st.registrar_decisao(d778, cid2, "evidencia", "OK", {"tentativa": tentativa, "manifesto": ev.caminho_manifesto})
        (pasta / "domain-intel.json").write_text('{"falso-bv.example": {}}', encoding="utf-8")
        st.con.execute(
            "INSERT INTO travas_campanha(id_campanha, dono, pulso) VALUES (?,?,?)",
            (d778, "maquina:1:x", "2026-11-25T21:58:33+00:00"),
        )
    finally:
        st.fechar()
    rel = origem / "reports" / d778
    (rel / "dossiers").mkdir(parents=True)
    (rel / f"simio-{d778}-20261125T230000Z.xlsx").write_bytes(b"planilha antiga")
    (rel / "dossiers" / f"{cid2}.html").write_text("<html>dossie antigo</html>", encoding="utf-8")
    (origem / "logs").mkdir()
    (origem / "logs" / "simio.jsonl").write_text('{"msg": "x"}\n', encoding="utf-8")
    return manifestos


def _executar(mod, monkeypatch: pytest.MonkeyPatch, raiz: Path, *extra: str) -> int:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        sys, "argv", ["migrar", "--origem", str(raiz / "output"), "--destino", str(raiz / "resultados"), *extra]
    )
    return mod.principal()


def _foto(raiz: Path) -> dict[str, str]:
    saida = {}
    for p in sorted(raiz.rglob("*")):
        if p.is_file() and not p.name.startswith("simio.db"):
            saida[p.relative_to(raiz).as_posix()] = _sha(p.read_bytes())
    for db in raiz.rglob("simio.db"):
        con = sqlite3.connect(db)
        saida[db.relative_to(raiz).as_posix()] = _sha("\n".join(con.iterdump()).encode())
        con.close()
    return saida


def _conferir_resultado(raiz: Path, manifestos: dict[str, bytes]) -> None:
    destino = raiz / "resultados"
    assert not (raiz / "output").exists()
    mapa = json.loads((destino / "ids_campanhas.json").read_text(encoding="utf-8"))
    assert mapa == {antigo: novo for antigo, _, _, novo in CAMPANHAS}
    assert (destino / "registros" / "simio.jsonl").read_text(encoding="utf-8") == '{"msg": "x"}\n'

    con = sqlite3.connect(destino / "simio.db")
    con.row_factory = sqlite3.Row
    assert {r[0] for r in con.execute("SELECT id FROM campanhas")} == set(mapa.values())
    for antigo, novo in mapa.items():
        params = json.loads(con.execute("SELECT parametros_json FROM campanhas WHERE id=?", (novo,)).fetchone()[0])
        assert params["id_anterior"] == antigo
    for tabela in ("checkpoints", "consultas", "dominios", "candidatas", "decisoes_agentes", "erros", "uso_llm"):
        ids = {r[0] for r in con.execute(f"SELECT DISTINCT id_campanha FROM {tabela}")}
        assert ids <= set(mapa.values()), tabela
    assert con.execute("SELECT COUNT(*) FROM travas_campanha").fetchone()[0] == 0
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []

    linhas = con.execute(
        "SELECT c.id_campanha, e.caminho_manifesto, e.sha256_manifesto, e.dados_json FROM evidencias e"
        " JOIN candidatas c ON c.id=e.id_candidata"
    ).fetchall()
    assert len(linhas) == 3
    for r in linhas:
        pasta = caminhos.dir_campanha(destino, r["id_campanha"])
        ev = Evidencia.model_validate_json(r["dados_json"])
        assert r["caminho_manifesto"] == ev.caminho_manifesto
        assert ev.caminho_manifesto.startswith("evidencias/") and ev.caminho_manifesto.endswith("/manifesto.json")
        manifesto = (pasta / ev.caminho_manifesto).read_bytes()
        chave = f"{next(a for a, n in mapa.items() if n == r['id_campanha'])}/{ev.id_candidata}/{ev.tentativa}"
        assert manifesto == manifestos[chave]  # byte a byte igual: o hash gravado continua batendo
        assert _sha(manifesto) == r["sha256_manifesto"]
        for a in ev.artefatos:
            assert "\\" not in a.caminho and "/tentativa-" in a.caminho
            assert a.caminho.rsplit("/", 1)[1] == caminhos.ARTEFATOS[a.tipo]
            assert _sha((pasta / a.caminho).read_bytes()) == a.sha256
    for r in con.execute("SELECT detalhe_json FROM decisoes_agentes WHERE agente='evidencia'"):
        assert json.loads(r[0])["manifesto"].startswith("evidencias/")
    con.close()

    win = caminhos.dir_campanha(destino, "2026-11-18_15h49_bv_ampla")
    ren = json.loads(caminhos.arquivo_renomeacoes(win).read_text(encoding="utf-8"))
    assert ren["observacao"]
    pares = {r["antigo"]: r["novo"] for r in ren["arquivos"]}
    assert len(pares) == 6
    antigo_vp = next(a for a in pares if a.endswith("/viewport.png"))
    assert antigo_vp.startswith("evidence/C") and "/attempt-1/" in antigo_vp
    assert pares[antigo_vp].startswith("evidencias/loja.example_") and pares[antigo_vp].endswith("/tentativa-1/tela-inicial.png")

    ameacas = caminhos.dir_campanha(destino, "2026-11-25_19h05_bv_ameacas")
    assert (ameacas / "dominios.json").read_text(encoding="utf-8") == '{"falso-bv.example": {}}'
    legado = ameacas / "anteriores" / "legado"
    assert (legado / "simio-simio-20261125-220519-d778-20261125T230000Z.xlsx").read_bytes() == b"planilha antiga"
    assert len(list((legado / "dossiers").glob("C*.html"))) == 1
    assert len(list((ameacas / "evidencias").glob("falso-bv.example_*/tentativa-*/manifesto.json"))) == 2

    st = Armazenamento(destino / "simio.db")
    try:
        assert st.resolver_id_campanha("simio-20261125-220519-d778") == "2026-11-25_19h05_bv_ameacas"
        _, criada = st.upsert_candidata(
            "2026-11-25_19h05_bv_ameacas",
            url="https://falso-bv.example/login",
            url_normalizada="https://falso-bv.example/login",
            hostname="falso-bv.example",
            dominio_registravel="falso-bv.example",
        )
        assert not criada
    finally:
        st.fechar()


def test_simulacao_nao_altera_nada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _montar(tmp_path)
    antes = _foto(tmp_path)
    assert _executar(_modulo(), monkeypatch, tmp_path, "--simular") == 0
    assert _foto(tmp_path) == antes and not (tmp_path / "resultados").exists()
    saida = capsys.readouterr().out
    assert "simio-20261125-204527-2fb3 -> 2026-11-25_17h45_bv_logo-antiga-2" in saida
    assert "Nada foi alterado" in saida


def test_migracao_completa_e_idempotente(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    manifestos = _montar(tmp_path)
    mod = _modulo()
    assert _executar(mod, monkeypatch, tmp_path) == 0, capsys.readouterr().out
    saida = capsys.readouterr().out
    assert "0 divergência(s)" in saida and "Integridade do banco: ok" in saida
    _conferir_resultado(tmp_path, manifestos)
    depois = _foto(tmp_path)
    assert _executar(mod, monkeypatch, tmp_path) == 0
    assert _foto(tmp_path) == depois


def test_migracao_interrompida_eh_retomada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifestos = _montar(tmp_path)
    mod = _modulo()
    original = mod.mover
    chamadas = {"n": 0}

    def mover_e_cair(origem, destino, relato):  # type: ignore[no-untyped-def]
        chamadas["n"] += 1
        if chamadas["n"] > 6:
            raise OSError("disco cheio")
        return original(origem, destino, relato)

    monkeypatch.setattr(mod, "mover", mover_e_cair)
    with pytest.raises(OSError, match="disco cheio"):
        _executar(mod, monkeypatch, tmp_path)
    assert (tmp_path / "output" / "campaigns").exists() and (tmp_path / "resultados" / "simio.db").exists()
    monkeypatch.setattr(mod, "mover", original)
    assert _executar(mod, monkeypatch, tmp_path) == 0
    _conferir_resultado(tmp_path, manifestos)


def test_artefato_alterado_eh_apontado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _montar(tmp_path)
    alvo = next((tmp_path / "output" / "campaigns").rglob("text.txt"))
    alvo.write_bytes(b"adulterado")
    assert _executar(_modulo(), monkeypatch, tmp_path) == 1
    saida = capsys.readouterr().out
    assert "texto-visivel.txt: SHA-256 difere do manifesto" in saida


def test_campanha_ja_no_layout_novo_dentro_de_output_vai_junto(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _montar(tmp_path)
    novo = "2026-11-26_09h00_bv_ampla"
    st = Armazenamento(tmp_path / "output" / "simio.db")
    try:
        st.con.execute(
            "INSERT INTO campanhas(id, marca, status, parametros_json, criado_em, atualizado_em)"
            " VALUES (?,?,?,?,?,?)",
            (novo, "bv", "CONCLUIDA", "{}", "2026-11-26T12:00:00+00:00", "2026-11-26T12:00:00+00:00"),
        )
    finally:
        st.fechar()
    pasta = caminhos.dir_campanha(tmp_path / "output", novo)
    (pasta / "dossies").mkdir(parents=True)
    (pasta / "relatorio.xlsx").write_bytes(b"planilha nova")
    (pasta / "dossies" / "x.html").write_text("<html></html>", encoding="utf-8")
    assert _executar(_modulo(), monkeypatch, tmp_path) == 0, capsys.readouterr().out
    assert "movidas sem renomear" in capsys.readouterr().out
    destino = caminhos.dir_campanha(tmp_path / "resultados", novo)
    assert (destino / "relatorio.xlsx").read_bytes() == b"planilha nova"
    assert (destino / "dossies" / "x.html").is_file()
    assert not (tmp_path / "output").exists()
