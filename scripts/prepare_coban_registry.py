"""Prepare the authorised-correspondent (COBAN) registry used by the Identity agent.

Reads the operator's export (``.xlsx`` or ``.csv`` with columns ``cnpj``, ``nmpessoa``,
``dtcadastro``, ``login_parceiro``) and writes the CSV expected by ``CobanRegistry``:

* one row per CNPJ (duplicates from multiple partner logins are merged);
* the institution's own entities are removed (CNPJ roots from the brand profile, plus any row
  whose name is an institution name, e.g. internal accounts such as "COMISSAO PARCERIA");
* invalid CNPJs are dropped; mojibake in names (``COM√ÇRCIO``) is repaired when possible.

The output contains partner data and MUST stay on the execution machine (``data/`` is
git-ignored). Usage::

    python scripts/prepare_coban_registry.py Todos_Cobans_Ativos.xlsx \
        --out data/coban_registry_bv.csv --brand bv --reference-date 2026-10-06
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from simio.agents.identity import cnpj_is_valid, normalize_cnpj  # noqa: E402
from simio.brands import fold, load_brand  # noqa: E402

FIELDS = [
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
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def repair_name(raw: object) -> str:
    """Trim, collapse spaces and undo UTF-8-read-as-MacRoman mojibake (``COM√ÇRCIO`` -> ``COMÉRCIO``)."""
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if _MOJIBAKE.search(name):
        with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
            name = name.encode("mac_roman").decode("utf-8")
    if name.replace(".", "").isdigit():  # a bare number is not a name
        return ""
    return name


def _name_quality(name: str) -> tuple[int, int, int]:
    return (0 if ("?" in name or _MOJIBAKE.search(name)) else 1, 0 if name.isupper() else 1, len(name))


@dataclass
class Entry:
    cnpj: str
    names: Counter[str] = field(default_factory=Counter)
    dates: list[str] = field(default_factory=list)
    logins: list[str] = field(default_factory=list)

    def row(self, source: str, reference_date: str) -> dict[str, str]:
        ranked = sorted(self.names, key=lambda n: (_name_quality(n), self.names[n]), reverse=True)
        best = ranked[0] if ranked else ""
        alts = [n for n in ranked[1:] if fold(n) != fold(best)]
        dates = sorted(self.dates)
        return {
            "cnpj": self.cnpj,
            "nome": best,
            "nomes_alternativos": " | ".join(alts),
            "data_cadastro_inicial": dates[0] if dates else "",
            "data_cadastro_recente": dates[-1] if dates else "",
            "qtd_cadastros": str(len(self.logins) or len(self.dates) or 1),
            "logins_parceiro": " | ".join(sorted(set(self.logins))),
            "fonte": source,
            "data_referencia": reference_date,
        }


def read_rows(path: Path) -> list[dict[str, object]]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        ws = openpyxl.load_workbook(path, read_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [str(h or "").strip().lower() for h in next(it)]
        return [dict(zip(header, r, strict=False)) for r in it]
    text = path.read_text(encoding="utf-8-sig")
    delim = ";" if text.splitlines()[0].count(";") > text.splitlines()[0].count(",") else ","
    reader = csv.DictReader(text.splitlines(), delimiter=delim)
    return [{(k or "").strip().lower(): v for k, v in r.items()} for r in reader]


def prepare(
    rows: list[dict[str, object]], brand_id: str
) -> tuple[dict[str, Entry], Counter[str], list[tuple[str, str]]]:
    brand = load_brand(brand_id, None)
    roots = {normalize_cnpj(c)[:8] for c in brand.institution_cnpjs}
    inst_names = {fold(n).strip(" .") for n in brand.institution_names}
    stats: Counter[str] = Counter()
    removed: list[tuple[str, str]] = []
    entries: dict[str, Entry] = {}
    for r in rows:
        stats["linhas_lidas"] += 1
        cnpj = normalize_cnpj(str(r.get("cnpj") or ""))
        name = repair_name(r.get("nmpessoa") or r.get("nome") or r.get("name"))
        if len(cnpj) != 14 or not cnpj_is_valid(cnpj):
            stats["removidas_cnpj_invalido"] += 1
            continue
        folded = fold(name).strip(" .")
        if cnpj[:8] in roots or any(folded == n or folded.startswith(n + " ") for n in inst_names if len(n) > 6):
            stats["removidas_entidade_da_instituicao"] += 1
            removed.append((cnpj, name))
            continue
        e = entries.setdefault(cnpj, Entry(cnpj))
        if name:
            e.names[name] += 1
        if m := _DATE.search(str(r.get("dtcadastro") or "")):
            e.dates.append(m.group(0))
        if r.get("login_parceiro") not in (None, ""):
            e.logins.append(str(r["login_parceiro"]))
    stats["cnpjs_unicos"] = len(entries)
    kept = stats["linhas_lidas"] - stats["removidas_cnpj_invalido"] - stats["removidas_entidade_da_instituicao"]
    stats["linhas_duplicadas_mescladas"] = kept - len(entries)
    stats["cnpjs_com_nomes_divergentes"] = sum(1 for e in entries.values() if len({fold(n) for n in e.names}) > 1)
    return entries, stats, removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "coban_registry_bv.csv")
    ap.add_argument("--brand", default="bv")
    ap.add_argument("--reference-date", default="", help="data de referência da extração (AAAA-MM-DD)")
    args = ap.parse_args()

    ref = args.reference_date
    hints = [args.source.stem]
    if args.source.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        hints.append(openpyxl.load_workbook(args.source, read_only=True).worksheets[0].title)
    for hint in hints if not ref else []:
        if m := re.search(r"(20\d{2})(\d{2})(\d{2})", hint):
            ref = "-".join(m.groups())
            break
    entries, stats, removed = prepare(read_rows(args.source), args.brand)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";")
        w.writeheader()
        for cnpj in sorted(entries):
            w.writerow(entries[cnpj].row(args.source.name, ref))
    for k, v in stats.items():
        print(f"{k}: {v}")
    print("entidades da instituição removidas:")
    for (cnpj, name), n in Counter(removed).most_common():
        print(f"  {cnpj}  {name}  ({n} linha(s))")
    print(f"gravado: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
