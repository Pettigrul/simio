from __future__ import annotations

import argparse
import contextlib
import csv
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from simio.agentes.identidade import cnpj_valido, normalizar_cnpj  # noqa: E402
from simio.marcas import carregar_marca, normalizar_texto  # noqa: E402

CAMPOS = [
    "cnpj",
    "nome",
    "nomes_alternativos",
    "data_cadastro_inicial",
    "data_cadastro_recente",
    "qtd_cadastros",
    "logins_parceiro",
    "fonte",
    "data_referencia",
]
_MOJIBAKE = re.compile(r"[√∫ƒ≈ŒœÀÃÕ]")
_DATA = re.compile(r"\d{4}-\d{2}-\d{2}")


def reparar_nome(bruto: object) -> str:
    # Desfaz UTF-8 lido como MacRoman: COM√ÇRCIO -> COMÉRCIO.
    nome = re.sub(r"\s+", " ", str(bruto or "")).strip()
    if _MOJIBAKE.search(nome):
        with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
            nome = nome.encode("mac_roman").decode("utf-8")
    if nome.replace(".", "").isdigit():  # número solto não é nome
        return ""
    return nome


def _qualidade_nome(nome: str) -> tuple[int, int, int]:
    return (0 if ("?" in nome or _MOJIBAKE.search(nome)) else 1, 0 if nome.isupper() else 1, len(nome))


@dataclass
class Entrada:
    cnpj: str
    nomes: Counter[str] = field(default_factory=Counter)
    datas: list[str] = field(default_factory=list)
    logins: list[str] = field(default_factory=list)

    def linha(self, fonte: str, data_referencia: str) -> dict[str, str]:
        ranqueados = sorted(self.nomes, key=lambda n: (_qualidade_nome(n), self.nomes[n]), reverse=True)
        melhor = ranqueados[0] if ranqueados else ""
        alts = [n for n in ranqueados[1:] if normalizar_texto(n) != normalizar_texto(melhor)]
        datas = sorted(self.datas)
        return {
            "cnpj": self.cnpj,
            "nome": melhor,
            "nomes_alternativos": " | ".join(alts),
            "data_cadastro_inicial": datas[0] if datas else "",
            "data_cadastro_recente": datas[-1] if datas else "",
            "qtd_cadastros": str(len(self.logins) or len(self.datas) or 1),
            "logins_parceiro": " | ".join(sorted(set(self.logins))),
            "fonte": fonte,
            "data_referencia": data_referencia,
        }


def ler_linhas(caminho: Path) -> list[dict[str, object]]:
    if caminho.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        ws = openpyxl.load_workbook(caminho, read_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        cabecalho = [str(h or "").strip().lower() for h in next(it)]
        return [dict(zip(cabecalho, r, strict=False)) for r in it]
    texto = caminho.read_text(encoding="utf-8-sig")
    delim = ";" if texto.splitlines()[0].count(";") > texto.splitlines()[0].count(",") else ","
    leitor = csv.DictReader(texto.splitlines(), delimiter=delim)
    return [{(k or "").strip().lower(): v for k, v in r.items()} for r in leitor]


def preparar(
    linhas: list[dict[str, object]], id_marca: str
) -> tuple[dict[str, Entrada], Counter[str], list[tuple[str, str]]]:
    marca = carregar_marca(id_marca, None)
    raizes = {normalizar_cnpj(c)[:8] for c in marca.cnpjs_instituicao}
    nomes_inst = {normalizar_texto(n).strip(" .") for n in marca.nomes_instituicao}
    estatisticas: Counter[str] = Counter()
    removidos: list[tuple[str, str]] = []
    entradas: dict[str, Entrada] = {}
    for r in linhas:
        estatisticas["linhas_lidas"] += 1
        cnpj = normalizar_cnpj(str(r.get("cnpj") or ""))
        nome = reparar_nome(r.get("nmpessoa") or r.get("nome") or r.get("name"))
        if len(cnpj) != 14 or not cnpj_valido(cnpj):
            estatisticas["removidas_cnpj_invalido"] += 1
            continue
        normalizado = normalizar_texto(nome).strip(" .")
        da_instituicao = any(
            normalizado == n or normalizado.startswith(n + " ") for n in nomes_inst if len(n) > 6
        )
        if cnpj[:8] in raizes or da_instituicao:
            estatisticas["removidas_entidade_da_instituicao"] += 1
            removidos.append((cnpj, nome))
            continue
        e = entradas.setdefault(cnpj, Entrada(cnpj))
        if nome:
            e.nomes[nome] += 1
        if m := _DATA.search(str(r.get("dtcadastro") or "")):
            e.datas.append(m.group(0))
        if r.get("login_parceiro") not in (None, ""):
            e.logins.append(str(r["login_parceiro"]))
    estatisticas["cnpjs_unicos"] = len(entradas)
    mantidos = (
        estatisticas["linhas_lidas"]
        - estatisticas["removidas_cnpj_invalido"]
        - estatisticas["removidas_entidade_da_instituicao"]
    )
    estatisticas["linhas_duplicadas_mescladas"] = mantidos - len(entradas)
    estatisticas["cnpjs_com_nomes_divergentes"] = sum(
        1 for e in entradas.values() if len({normalizar_texto(n) for n in e.nomes}) > 1
    )
    return entradas, estatisticas, removidos


def principal() -> int:
    ap = argparse.ArgumentParser(
        description="Gera o CSV de correspondentes (COBAN) usado pelo agente de identidade a partir da "
        "exportação do operador (colunas cnpj, nmpessoa, dtcadastro e login_parceiro). Junta as linhas "
        "do mesmo CNPJ, descarta CNPJ inválido e contas da própria instituição e conserta nomes com "
        "acentuação quebrada quando dá. O arquivo tem dados de parceiros e fica só nesta máquina.",
        epilog="exemplo: python scripts/preparar_cadastro_coban.py Todos_Cobans_Ativos.xlsx "
        "--saida data/coban_registry_bv.csv --data-referencia 2026-10-06",
    )
    ap.add_argument("origem", type=Path, help="exportação do operador (.xlsx ou .csv)")
    ap.add_argument("--saida", type=Path, default=RAIZ / "data" / "coban_registry_bv.csv", help="CSV de saída")
    ap.add_argument("--marca", default="bv", help="perfil de marca (padrão: bv)")
    ap.add_argument("--data-referencia", default="", help="data de referência da extração (AAAA-MM-DD)")
    args = ap.parse_args()

    ref = args.data_referencia
    pistas = [args.origem.stem]
    if args.origem.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        pistas.append(openpyxl.load_workbook(args.origem, read_only=True).worksheets[0].title)
    for pista in pistas if not ref else []:
        if m := re.search(r"(20\d{2})(\d{2})(\d{2})", pista):
            ref = "-".join(m.groups())
            break
    entradas, estatisticas, removidos = preparar(ler_linhas(args.origem), args.marca)
    args.saida.parent.mkdir(parents=True, exist_ok=True)
    with args.saida.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CAMPOS, delimiter=";")
        w.writeheader()
        for cnpj in sorted(entradas):
            w.writerow(entradas[cnpj].linha(args.origem.name, ref))
    for k, v in estatisticas.items():
        print(f"{k}: {v}")
    print("entidades da instituição removidas:")
    for (cnpj, nome), n in Counter(removidos).most_common():
        print(f"  {cnpj}  {nome}  ({n} linha(s))")
    print(f"gravado: {args.saida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
