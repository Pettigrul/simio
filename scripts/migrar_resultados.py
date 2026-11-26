#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

try:
    from simio.nucleo import caminhos
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from simio.nucleo import caminhos

ID_ANTIGO = re.compile(r"^simio-\d{8}-\d{6}-[0-9a-f]{4}$")
CAMINHO_ANTIGO = re.compile(r"^evidence/(?P<cid>[^/]+)/attempt-(?P<n>\d+)/(?P<arquivo>[^/]+)$")
TIPOS_ANTIGOS = {
    "viewport.png": "captura_viewport",
    "fullpage.png": "captura_completa",
    "page.html.txt": "html",
    "text.txt": "texto",
    "page.json": "dados_pagina",
}
NOMES_NOVOS = {antigo: caminhos.ARTEFATOS[tipo] for antigo, tipo in TIPOS_ANTIGOS.items()}
NOMES_NOVOS["manifest.json"] = caminhos.ARQUIVO_MANIFESTO
ARQUIVO_IDS = "ids_campanhas.json"
PULSO_VIVO_S = 300
NOTA_RENOMEACOES = (
    "Arquivos de evidência renomeados pela migração para a pasta resultados/, sem alteração de conteúdo. "
    "Os manifestos (manifesto.json) citam os nomes antigos; use esta lista para ligar cada nome antigo ao atual."
)


@dataclass
class Relato:
    simular: bool
    movidos: int = 0
    ja_no_lugar: int = 0
    linhas_evidencia: int = 0
    linhas_decisao: int = 0
    travas_removidas: int = 0
    hashes_conferidos: int = 0
    manifestos_conferidos: int = 0
    problemas: list[str] = field(default_factory=list)
    divergencias: list[str] = field(default_factory=list)
    deixados: list[str] = field(default_factory=list)
    plano: dict[str, dict[str, int]] = field(default_factory=dict)

    def contar(self, campanha: str, item: str, n: int = 1) -> None:
        self.plano.setdefault(campanha, {}).setdefault(item, 0)
        self.plano[campanha][item] += n


def sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as fh:
        for bloco in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def normalizar(caminho: str) -> str:
    return caminho.replace("\\", "/")


def novo_caminho(caminho: str, hosts: dict[str, str]) -> str | None:
    """Old artifact path (relative to the campaign folder) in the new layout; None when unknown."""
    caminho = normalizar(caminho)
    if caminho.startswith(caminhos.DIR_EVIDENCIAS + "/"):
        return caminho
    m = CAMINHO_ANTIGO.match(caminho)
    if not m or m["cid"] not in hosts:
        return None
    pasta = caminhos.dir_tentativa(hosts[m["cid"]], m["cid"], int(m["n"]))
    return str(pasta / NOMES_NOVOS.get(m["arquivo"], m["arquivo"]))


# ---- database -------------------------------------------------------------------------------
def abrir_banco(caminho: Path, simular: bool) -> sqlite3.Connection:
    if simular:
        con = sqlite3.connect(f"{caminho.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    else:
        con = sqlite3.connect(caminho, isolation_level=None)
    con.row_factory = sqlite3.Row
    versao = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    if versao != 3:
        raise SystemExit(f"Banco {caminho} está no esquema v{versao}; esta migração espera o v3.")
    return con


def mapa_ids(con: sqlite3.Connection, existente: dict[str, str]) -> dict[str, str]:
    mapa = dict(existente)
    linhas = con.execute("SELECT id, marca, parametros_json, criado_em FROM campanhas ORDER BY criado_em, rowid")
    linhas = linhas.fetchall()
    ocupados = {r["id"] for r in linhas if r["id"] not in mapa} | set(mapa.values())
    for r in linhas:
        if r["id"] in mapa or r["id"] in mapa.values() or not ID_ANTIGO.match(r["id"]):
            continue
        foco = (json.loads(r["parametros_json"] or "{}") or {}).get("foco")
        base = caminhos.id_campanha(datetime.fromisoformat(r["criado_em"]), r["marca"], foco)
        n = 1
        while caminhos.com_sufixo(base, n) in ocupados:
            n += 1
        mapa[r["id"]] = caminhos.com_sufixo(base, n)
        ocupados.add(mapa[r["id"]])
    return mapa


def colunas_campanha(con: sqlite3.Connection) -> list[str]:
    tabelas = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return [t for t in tabelas if any(c[1] == "id_campanha" for c in con.execute(f"PRAGMA table_info({t})"))]


def verificar_travas(con: sqlite3.Connection) -> None:
    agora = datetime.now(UTC)
    for r in con.execute("SELECT id_campanha, dono, pulso FROM travas_campanha"):
        idade = (agora - datetime.fromisoformat(r["pulso"])).total_seconds()
        if idade < PULSO_VIVO_S:
            raise SystemExit(
                f"A campanha {r['id_campanha']} parece estar em execução ({r['dono']}, sinal há {idade:.0f} s). "
                "Espere terminar antes de migrar."
            )


def reescrever_evidencia(dados: dict, hosts: dict[str, str], relato: Relato) -> bool:
    mudou = False
    for art in dados.get("artefatos") or []:
        novo = novo_caminho(art.get("caminho") or "", hosts)
        if novo is None:
            relato.problemas.append(f"caminho de artefato não reconhecido: {art.get('caminho')}")
        elif novo != art["caminho"]:
            art["caminho"], mudou = novo, True
    if dados.get("caminho_manifesto"):
        novo = novo_caminho(dados["caminho_manifesto"], hosts)
        if novo is None:
            relato.problemas.append(f"caminho de manifesto não reconhecido: {dados['caminho_manifesto']}")
        elif novo != dados["caminho_manifesto"]:
            dados["caminho_manifesto"], mudou = novo, True
    return mudou


def migrar_banco(con: sqlite3.Connection, mapa: dict[str, str], hosts: dict[str, str], relato: Relato) -> None:
    existentes = {r[0] for r in con.execute("SELECT id FROM campanhas")}
    pendentes = {a: n for a, n in mapa.items() if a in existentes}
    tabelas = [t for t in colunas_campanha(con) if t != "campanhas"]
    relato.travas_removidas = con.execute("SELECT COUNT(*) FROM travas_campanha").fetchone()[0]

    evidencias = []
    for r in con.execute("SELECT id, caminho_manifesto, dados_json FROM evidencias"):
        dados = json.loads(r["dados_json"])
        novo_manifesto = novo_caminho(r["caminho_manifesto"], hosts) if r["caminho_manifesto"] else None
        if reescrever_evidencia(dados, hosts, relato) or (novo_manifesto and novo_manifesto != r["caminho_manifesto"]):
            evidencias.append((novo_manifesto or r["caminho_manifesto"], dados, r["id"]))
    decisoes = []
    for r in con.execute("SELECT id, detalhe_json FROM decisoes_agentes WHERE detalhe_json LIKE '%manifesto%'"):
        detalhe = json.loads(r["detalhe_json"])
        if isinstance(detalhe, dict) and isinstance(detalhe.get("manifesto"), str):
            novo = novo_caminho(detalhe["manifesto"], hosts)
            if novo and novo != detalhe["manifesto"]:
                detalhe["manifesto"] = novo
                decisoes.append((detalhe, r["id"]))
    relato.linhas_evidencia, relato.linhas_decisao = len(evidencias), len(decisoes)
    if relato.simular:
        return

    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM travas_campanha")
        for antigo, novo in pendentes.items():
            con.execute(
                "UPDATE campanhas SET id=?, parametros_json=json_set(parametros_json, '$.id_anterior', ?) WHERE id=?",
                (novo, antigo, antigo),
            )
            for t in tabelas:
                con.execute(f"UPDATE {t} SET id_campanha=? WHERE id_campanha=?", (novo, antigo))  # noqa: S608
        for manifesto, dados, id_linha in evidencias:
            con.execute(
                "UPDATE evidencias SET caminho_manifesto=?, dados_json=? WHERE id=?",
                (manifesto, json.dumps(dados, ensure_ascii=False, separators=(",", ":")), id_linha),
            )
        for detalhe, id_linha in decisoes:
            con.execute(
                "UPDATE decisoes_agentes SET detalhe_json=? WHERE id=?",
                (json.dumps(detalhe, ensure_ascii=False), id_linha),
            )
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA foreign_keys=ON")
    violacoes = con.execute("PRAGMA foreign_key_check").fetchall()
    if violacoes:
        exemplos = [tuple(v) for v in violacoes[:5]]
        raise SystemExit(f"A migração deixou {len(violacoes)} referência(s) quebrada(s): {exemplos}")


def mover_banco(origem: Path, destino: Path, relato: Relato) -> None:
    con = sqlite3.connect(origem)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    destino.parent.mkdir(parents=True, exist_ok=True)
    for sufixo in ("", "-wal", "-shm"):
        a = origem.with_name(origem.name + sufixo)
        if a.exists():
            mover(a, destino.with_name(destino.name + sufixo), relato)


# ---- files ----------------------------------------------------------------------------------
def mover(origem: Path, destino: Path, relato: Relato) -> bool:
    """Rename (copy then delete only across file systems). Never overwrites a different file."""
    if relato.simular:
        return True
    if destino.exists():
        if destino.is_file() and origem.is_file() and sha256(destino) == sha256(origem):
            origem.unlink()  # an earlier run copied it and stopped before deleting the original
            relato.ja_no_lugar += 1
            return True
        relato.problemas.append(f"destino já existe com outro conteúdo, origem mantida: {origem} -> {destino}")
        return False
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        origem.rename(destino)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copy2(origem, destino)
        if sha256(destino) != sha256(origem):
            destino.unlink()
            relato.problemas.append(f"cópia divergente, origem mantida: {origem}")
            return False
        origem.unlink()
    relato.movidos += 1
    return True


def plano_evidencias(pasta_antiga: Path, hosts: dict[str, str], relato: Relato) -> list[tuple[str, str]]:
    plano = []
    raiz = pasta_antiga / "evidence"
    if not raiz.is_dir():
        return plano
    for arq in sorted(p for p in raiz.rglob("*") if p.is_file()):
        rel = arq.relative_to(pasta_antiga).as_posix()
        novo = novo_caminho(rel, hosts)
        if novo is None:
            relato.deixados.append(f"{arq} (candidata desconhecida ou nome fora do padrão)")
            continue
        plano.append((rel, novo))
    return plano


def gravar_renomeacoes(pasta_nova: Path, plano: list[tuple[str, str]]) -> None:
    arquivo = caminhos.arquivo_renomeacoes(pasta_nova)
    atuais: dict[str, str] = {}
    if arquivo.exists():
        atuais = {r["antigo"]: r["novo"] for r in json.loads(arquivo.read_text(encoding="utf-8"))["arquivos"]}
    atuais.update(dict(plano))
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    conteudo = {
        "observacao": NOTA_RENOMEACOES,
        "arquivos": [{"antigo": a, "novo": n} for a, n in sorted(atuais.items())],
    }
    arquivo.write_text(json.dumps(conteudo, ensure_ascii=False, indent=1), encoding="utf-8")


def migrar_campanha(origem: Path, destino: Path, antigo: str, novo: str, hosts: dict[str, str], relato: Relato) -> None:
    pasta_antiga = origem / "campaigns" / antigo
    pasta_nova = caminhos.dir_campanha(destino, novo)
    if pasta_antiga.is_dir():
        plano = plano_evidencias(pasta_antiga, hosts, relato)
        relato.contar(novo, "evidencias", len(plano))
        if plano and not relato.simular:
            gravar_renomeacoes(pasta_nova, plano)
        for rel, novo_rel in plano:
            mover(pasta_antiga / rel, pasta_nova / novo_rel, relato)
        intel = pasta_antiga / "domain-intel.json"
        if intel.exists():
            relato.contar(novo, "dominios")
            mover(intel, caminhos.arquivo_dominios(pasta_nova), relato)
    pasta_rel = origem / "reports" / antigo
    if pasta_rel.is_dir():
        legado = caminhos.dir_anteriores(pasta_nova) / "legado"
        for arq in sorted(p for p in pasta_rel.rglob("*") if p.is_file()):
            relato.contar(novo, "relatorios_antigos")
            mover(arq, legado / arq.relative_to(pasta_rel), relato)


def remover_vazias(raiz: Path) -> None:
    if not raiz.is_dir():
        return
    for pasta in sorted((p for p in raiz.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(pasta.iterdir()):
            pasta.rmdir()
    if not any(raiz.iterdir()):
        raiz.rmdir()


# ---- verification ---------------------------------------------------------------------------
def _entradas_manifesto(manifesto: dict) -> list[tuple[str, str]]:
    itens = manifesto.get("artefatos") or manifesto.get("artifacts") or []
    return [(normalizar(i.get("caminho") or i.get("path") or ""), i.get("sha256") or "") for i in itens]


def verificar(destino: Path, con: sqlite3.Connection, mapa: dict[str, str], relato: Relato) -> None:
    sha_manifesto = {r[0]: r[1] for r in con.execute("SELECT caminho_manifesto, sha256_manifesto FROM evidencias")}
    for novo in sorted(set(mapa.values())):
        pasta = caminhos.dir_campanha(destino, novo)
        arq_ren = caminhos.arquivo_renomeacoes(pasta)
        renomeados = {}
        if arq_ren.exists():
            renomeados = {r["antigo"]: r["novo"] for r in json.loads(arq_ren.read_text(encoding="utf-8"))["arquivos"]}
        padrao = f"*/{caminhos.PREFIXO_TENTATIVA}*/{caminhos.ARQUIVO_MANIFESTO}"
        for manifesto in sorted((pasta / caminhos.DIR_EVIDENCIAS).glob(padrao)):
            rel_manifesto = manifesto.relative_to(pasta).as_posix()
            esperado = sha_manifesto.get(rel_manifesto)
            if esperado is not None:
                relato.manifestos_conferidos += 1
                if sha256(manifesto) != esperado:
                    relato.divergencias.append(f"{novo}/{rel_manifesto}: SHA-256 do manifesto difere do banco")
            for caminho, hash_esperado in _entradas_manifesto(json.loads(manifesto.read_text(encoding="utf-8"))):
                atual = renomeados.get(caminho, caminho)
                arq = pasta / atual
                if not arq.is_file():
                    relato.divergencias.append(f"{novo}/{atual}: arquivo citado no manifesto não encontrado")
                    continue
                relato.hashes_conferidos += 1
                if sha256(arq) != hash_esperado:
                    relato.divergencias.append(f"{novo}/{atual}: SHA-256 difere do manifesto")
    linhas = con.execute(
        "SELECT c.id_campanha, e.dados_json FROM evidencias e JOIN candidatas c ON c.id=e.id_candidata"
    ).fetchall()
    for r in linhas:
        dados = json.loads(r["dados_json"])
        for caminho in [a["caminho"] for a in dados.get("artefatos") or []] + [dados.get("caminho_manifesto")]:
            if caminho and not (caminhos.dir_campanha(destino, r["id_campanha"]) / caminho).is_file():
                relato.divergencias.append(f"{r['id_campanha']}/{caminho}: citado no banco e não encontrado")


# ---- main -----------------------------------------------------------------------------------
def sugestoes_relatorio(con: sqlite3.Connection, origem: Path, destino: Path, mapa: dict[str, str]) -> list[str]:
    saida = []
    for antigo, novo in sorted(mapa.items(), key=lambda x: x[1]):
        pasta_legado = caminhos.dir_anteriores(caminhos.dir_campanha(destino, novo)) / "legado"
        pasta_antiga = origem / "reports" / antigo
        pasta = pasta_legado if pasta_legado.is_dir() else pasta_antiga
        if not pasta.is_dir():
            continue
        dossies = len(list((pasta / "dossiers").glob("*.html")))
        id_atual = novo if con.execute("SELECT 1 FROM campanhas WHERE id=?", (novo,)).fetchone() else antigo
        candidatas = con.execute("SELECT COUNT(*) FROM candidatas WHERE id_campanha=?", (id_atual,)).fetchone()[0]
        todos = " --todos-dossies" if candidatas and dossies >= candidatas else ""
        saida.append(f"simio relatorio -c {novo}{todos} -o {destino}")
    return saida


def principal() -> int:
    ap = argparse.ArgumentParser(description="Move as campanhas gravadas em output/ para o layout de resultados/.")
    ap.add_argument("--origem", type=Path, default=Path("output"))
    ap.add_argument("--destino", type=Path, default=Path(caminhos.DIR_SAIDA_PADRAO))
    ap.add_argument("--simular", action="store_true", help="Mostra o plano sem alterar nada")
    args = ap.parse_args()
    origem, destino, simular = args.origem, args.destino, args.simular
    relato = Relato(simular=simular)

    db_origem, db_destino = caminhos.arquivo_banco(origem), caminhos.arquivo_banco(destino)
    if db_origem.exists() and db_destino.exists():
        raise SystemExit(f"Há banco em {db_origem} e em {db_destino}; deixe só um antes de migrar.")
    if not db_origem.exists() and not db_destino.exists():
        raise SystemExit(f"Nenhum banco encontrado em {db_origem} nem em {db_destino}.")

    arq_ids = destino / ARQUIVO_IDS
    existente = json.loads(arq_ids.read_text(encoding="utf-8")) if arq_ids.exists() else {}
    con = abrir_banco(db_destino if db_destino.exists() else db_origem, simular)
    verificar_travas(con)
    mapa = mapa_ids(con, existente)
    hosts = {r[0]: r[1] for r in con.execute("SELECT id, hostname FROM candidatas")}

    if not simular:
        destino.mkdir(parents=True, exist_ok=True)
        arq_ids.write_text(json.dumps(mapa, ensure_ascii=False, indent=1), encoding="utf-8")
        if db_origem.exists():
            con.close()
            mover_banco(db_origem, db_destino, relato)
            con = abrir_banco(db_destino, simular)
    migrar_banco(con, mapa, hosts, relato)

    for antigo, novo in sorted(mapa.items()):
        migrar_campanha(origem, destino, antigo, novo, hosts, relato)
    log_antigo = origem / "logs" / caminhos.ARQUIVO_LOG
    if log_antigo.exists():
        relato.contar("(raiz)", "registro")
        mover(log_antigo, caminhos.arquivo_log(destino), relato)

    pastas_conhecidas = set(mapa)
    for sub in ("campaigns", "reports"):
        for p in sorted((origem / sub).glob("*")) if (origem / sub).is_dir() else []:
            if p.name not in pastas_conhecidas:
                relato.deixados.append(f"{p} (campanha fora do banco)")

    integridade = "não verificada (simulação)"
    if not simular:
        remover_vazias(origem)
        if origem.exists():
            relato.deixados += [str(p) for p in sorted(origem.rglob("*")) if p.is_file()]
        verificar(destino, con, mapa, relato)
        integridade = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk = len(con.execute("PRAGMA foreign_key_check").fetchall())
        integridade = f"{integridade}; referências quebradas: {fk}"

    print("Campanhas (id antigo -> id novo):")
    for antigo, novo in sorted(mapa.items(), key=lambda x: x[1]):
        itens = relato.plano.get(novo, {})
        detalhe = ", ".join(f"{k}: {v}" for k, v in sorted(itens.items())) or "sem arquivos"
        print(f"  {antigo} -> {novo}  ({detalhe})")
    print()
    verbo = "a atualizar" if simular else "atualizadas"
    print(
        f"Banco: {relato.linhas_evidencia} evidências e {relato.linhas_decisao} decisões {verbo}; "
        f"{relato.travas_removidas} trava(s) de campanha {'a remover' if simular else 'removida(s)'}."
    )
    if simular:
        total = sum(v for itens in relato.plano.values() for v in itens.values())
        print(f"Arquivos a mover: {total}. Nada foi alterado (simulação).")
    else:
        print(f"Arquivos movidos: {relato.movidos} (já estavam no destino: {relato.ja_no_lugar}).")
        print(
            f"Conferência: {relato.hashes_conferidos} artefatos e {relato.manifestos_conferidos} manifestos; "
            f"{len(relato.divergencias)} divergência(s)."
        )
    print(f"Integridade do banco: {integridade}.")
    for titulo, itens in (
        ("Divergências", relato.divergencias),
        ("Problemas", relato.problemas),
        ("Deixados na origem", relato.deixados),
    ):
        if itens:
            print(f"\n{titulo} ({len(itens)}):")
            for i in itens[:50]:
                print(f"  {i}")
            if len(itens) > 50:
                print(f"  ... e mais {len(itens) - 50}")
    comandos = sugestoes_relatorio(con, origem, destino, mapa)
    if comandos:
        print("\nPara gerar os relatórios no novo layout:")
        for c in comandos:
            print(f"  {c}")
    con.close()
    return 1 if relato.divergencias or relato.problemas else 0


if __name__ == "__main__":
    sys.exit(principal())
