"""Report generation entry point."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from simio.agentes.inteligencia_dominio import carregar_inteligencia
from simio.agentes.visual import recortar_correspondencia
from simio.configuracoes import Configuracoes
from simio.marcas import carregar_marca
from simio.nucleo.armazenamento import Armazenamento
from simio.relatorios.dados import RegistroCandidata, carregar_registros
from simio.relatorios.dossie import gravar_dossies
from simio.relatorios.excel import gravar_planilha, linhas_ameacas
from simio.relatorios.html_ameacas import gravar_html_ameacas
from simio.relatorios.html_campanha import gravar_html_campanha
from simio.relatorios.pdf import gerar_pdf_campanha


@dataclass
class ResultadoRelatorios:
    planilha: Path
    relatorio_html: Path
    relatorio_pdf: Path | None
    dossies: dict[str, str]
    dir_relatorio: Path
    relatorio_ameacas: Path | None = None


def gravar_recortes_logo_antiga(
    registros: list[RegistroCandidata], dir_campanha: Path, dir_relatorio: Path
) -> dict[str, str]:
    """PNG cut-outs of the best legacy-logo match of each page (``<report>/logo-antiga/``)."""
    saida: dict[str, str] = {}
    for r in registros:
        correspondencias = r.marca.correspondencias_visuais if r.marca else []
        if not correspondencias or r.evidencia is None:
            continue
        melhor = max(correspondencias, key=lambda m: m.pontuacao)
        try:
            recorte = recortar_correspondencia(r.evidencia, dir_campanha, melhor)
        except OSError:
            continue
        if recorte is None:
            continue
        caminho = dir_relatorio / "logo-antiga" / f"{r.candidata.id}-{r.candidata.dominio_registravel}.png"
        caminho.parent.mkdir(parents=True, exist_ok=True)
        recorte.save(caminho)
        saida[r.candidata.id] = str(caminho)
    return saida


def gerar_relatorios(
    configuracoes: Configuracoes, armazenamento: Armazenamento, id_campanha: str, todos_dossies: bool = False
) -> ResultadoRelatorios:
    camp = armazenamento.obter_campanha(id_campanha)
    if camp is None:
        raise KeyError(f"campaign not found: {id_campanha}")
    marca = carregar_marca(camp["marca"], configuracoes.arquivo_marca)
    registros = carregar_registros(armazenamento, id_campanha, marca)
    dir_relatorio = configuracoes.dir_saida / "reports" / id_campanha
    intel = carregar_inteligencia(configuracoes.dir_saida, id_campanha)
    dossies = gravar_dossies(
        registros, configuracoes.dir_saida, dir_relatorio, todas_candidatas=todos_dossies, inteligencia_dominio=intel
    )
    carimbo = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    # 1. Excel Workbook (.xlsx)
    caminho_planilha = dir_relatorio / f"simio-{id_campanha}-{carimbo}.xlsx"
    recortes = gravar_recortes_logo_antiga(
        registros, configuracoes.dir_saida / "campaigns" / id_campanha, dir_relatorio
    )
    planilha = gravar_planilha(caminho_planilha, armazenamento, id_campanha, registros, dossies, recortes, intel)

    # 2. Consolidated Campaign HTML Report (.html)
    caminho_relatorio_html = dir_relatorio / f"simio-{id_campanha}-{carimbo}.html"
    relatorio_html = gravar_html_campanha(caminho_relatorio_html, registros, armazenamento, id_campanha, dossies)

    # 3. Consolidated Campaign PDF Report (.pdf)
    caminho_relatorio_pdf = dir_relatorio / f"simio-{id_campanha}-{carimbo}.pdf"
    relatorio_pdf = gerar_pdf_campanha(relatorio_html, caminho_relatorio_pdf)

    # 4. Threat-intelligence briefing (.html), when the campaign has threats
    foco = (camp.get("parametros") or {}).get("foco")
    linhas = linhas_ameacas(registros, marca, foco, intel)
    relatorio_ameacas = None
    if linhas:
        defensivos = sorted(
            (h, i.rdap.titular or "")
            for h, i in intel.items()
            if i.rdap and not i.plataforma and (
                (i.rdap.documento_titular and i.rdap.documento_titular in marca.cnpjs_instituicao)
                or (i.rdap.titular and marca.eh_nome_instituicao(i.rdap.titular))
            )
        )
        relatorio_ameacas = gravar_html_ameacas(
            dir_relatorio / f"simio-{id_campanha}-{carimbo}-ameacas.html",
            linhas,
            id_campanha,
            configuracoes.dir_saida,
            dossies,
            defensivos,
        )

    return ResultadoRelatorios(
        planilha=planilha,
        relatorio_html=relatorio_html,
        relatorio_pdf=relatorio_pdf,
        dossies=dossies,
        dir_relatorio=dir_relatorio,
        relatorio_ameacas=relatorio_ameacas,
    )
