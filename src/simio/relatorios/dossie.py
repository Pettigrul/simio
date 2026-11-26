"""Per-candidate HTML dossiers (local files, no scripts, untrusted content always escaped)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from simio.agentes.inteligencia_dominio import resumir as resumir_inteligencia
from simio.nucleo.modelos import InteligenciaDominio
from simio.nucleo.urls import hostname_de
from simio.relatorios.dados import RegistroCandidata
from simio.relatorios.dados_logo import LOGO_BV_BRANCA_DATA_URI

_env = Environment(
    loader=PackageLoader("simio.relatorios", "templates"),
    autoescape=select_autoescape(["html", "j2"], default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def renderizar_dossie(
    rec: RegistroCandidata, dir_saida: Path, caminho_dossie: Path, intel: dict[str, str] | None = None
) -> str:
    ev = rec.evidencia
    capturas = []
    if ev:
        for tipo, rotulo in (
            ("captura_viewport", "Área visível inicial (E-SHOT-VIEWPORT)"),
            ("captura_completa", "Página completa (E-SHOT-FULL)"),
        ):
            a = ev.artefato(tipo)
            if a:
                alvo = dir_saida / "campaigns" / rec.candidata.id_campanha / a.caminho
                rel = os.path.relpath(alvo, caminho_dossie.parent)
                capturas.append({"src": Path(rel).as_posix(), "rotulo": rotulo, "sha": a.sha256})
    return _env.get_template("dossie.html.j2").render(
        c=rec.candidata,
        f=rec.final,
        b=rec.marca,
        idn=rec.identidade,
        comp=rec.conformidade,
        rv=rec.revisao,
        ev=ev,
        catalogo=rec.catalogo,
        resultados=rec.resultados,
        transicoes=rec.transicoes,
        capturas=capturas,
        di=intel,
        uri_logo_bv_branca=LOGO_BV_BRANCA_DATA_URI,
        gerado_em=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def gravar_dossies(
    registros: list[RegistroCandidata],
    dir_saida: Path,
    dir_relatorio: Path,
    todas_candidatas: bool = False,
    inteligencia_dominio: dict[str, InteligenciaDominio] | None = None,
) -> dict[str, str]:
    """Write dossiers for priority candidates (or all). Returns {candidate_id: path relative to
    output_dir}."""
    ddir = dir_relatorio / "dossiers"
    ddir.mkdir(parents=True, exist_ok=True)
    gravados: dict[str, str] = {}
    for rec in registros:
        if not (todas_candidatas or rec.eh_prioritaria):
            continue
        caminho = ddir / f"{rec.candidata.id}.html"
        host = hostname_de((rec.evidencia.url_final if rec.evidencia else None) or rec.candidata.url)
        item = (inteligencia_dominio or {}).get(host)
        caminho.write_text(
            renderizar_dossie(rec, dir_saida, caminho, resumir_inteligencia(item) if item else None), encoding="utf-8"
        )
        gravados[rec.candidata.id] = Path(os.path.relpath(caminho, dir_saida)).as_posix()
    return gravados
