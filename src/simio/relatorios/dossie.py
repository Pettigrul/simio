from __future__ import annotations

from pathlib import Path

from simio.agentes.inteligencia_dominio import resumir as resumir_inteligencia
from simio.nucleo import caminhos
from simio.nucleo.modelos import InteligenciaDominio
from simio.nucleo.urls import hostname_de
from simio.relatorios.dados import RegistroCandidata
from simio.relatorios.paginas import agora_br, ambiente, logo_marca

# Dossiês são arquivos locais sem script; o conteúdo da página investigada sempre sai escapado.


def renderizar_dossie(
    rec: RegistroCandidata,
    dir_campanha: Path,
    caminho_dossie: Path,
    intel: dict[str, str] | None = None,
    id_marca: str = "bv",
) -> str:
    ev = rec.evidencia
    capturas = []
    if ev:
        for tipo, rotulo in (
            ("captura_viewport", "Área visível ao abrir a página (E-SHOT-VIEWPORT)"),
            ("captura_completa", "Página inteira (E-SHOT-FULL)"),
        ):
            a = ev.artefato(tipo)
            if a:
                capturas.append(
                    {"src": caminhos.link(caminho_dossie, dir_campanha / a.caminho), "rotulo": rotulo, "sha": a.sha256}
                )
    rel = caminhos.relatorios(dir_campanha)
    return ambiente.get_template("dossie.html.j2").render(
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
        uri_logo=logo_marca(id_marca),
        nav=[("Relatório da campanha", caminhos.link(caminho_dossie, rel.html))],
        gerado_em=agora_br(),
    )


def gravar_dossies(
    registros: list[RegistroCandidata],
    dir_campanha: Path,
    todas_candidatas: bool = False,
    inteligencia_dominio: dict[str, InteligenciaDominio] | None = None,
    id_marca: str = "bv",
) -> dict[str, Path]:
    """Grava os dossiês das candidatas prioritárias (ou de todas). Devolve {id da candidata: arquivo}."""
    caminhos.relatorios(dir_campanha).dossies.mkdir(parents=True, exist_ok=True)
    gravados: dict[str, Path] = {}
    for rec in registros:
        if not (todas_candidatas or rec.eh_prioritaria):
            continue
        caminho = caminhos.arquivo_dossie(dir_campanha, rec.candidata.hostname, rec.candidata.id)
        host = hostname_de((rec.evidencia.url_final if rec.evidencia else None) or rec.candidata.url)
        item = (inteligencia_dominio or {}).get(host)
        caminho.write_text(
            renderizar_dossie(rec, dir_campanha, caminho, resumir_inteligencia(item) if item else None, id_marca),
            encoding="utf-8",
        )
        gravados[rec.candidata.id] = caminho
    return gravados
