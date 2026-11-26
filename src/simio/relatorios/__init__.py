from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from openpyxl import load_workbook

from simio.agentes.inteligencia_dominio import carregar_inteligencia
from simio.agentes.visual import recortar_correspondencia
from simio.configuracoes import Configuracoes
from simio.marcas import carregar_marca
from simio.nucleo import caminhos
from simio.nucleo.armazenamento import Armazenamento
from simio.relatorios.dados import RegistroCandidata, carregar_registros
from simio.relatorios.dossie import gravar_dossies
from simio.relatorios.excel import gravar_planilha, linhas_ameacas
from simio.relatorios.html_ameacas import gravar_html_ameacas
from simio.relatorios.html_campanha import gravar_html_campanha
from simio.relatorios.pdf import gerar_pdf

log = logging.getLogger(__name__)


@dataclass
class ResultadoRelatorios:
    planilha: Path
    relatorio_html: Path
    relatorio_pdf: Path | None
    dossies: dict[str, Path]
    dir_campanha: Path
    relatorio_ameacas: Path | None = None
    anteriores: Path | None = None


_LINK_EVIDENCIA = re.compile(r'(?P<attr>\b(?:src|href)=")(?P<sobe>(?:\.\./)*)(?=evidencias/)')
_ALVO_EVIDENCIA = re.compile(rf"(?:\.\./)*{caminhos.DIR_EVIDENCIAS}/")


def _ajustar_links_planilha(planilha: Path) -> None:
    wb = load_workbook(planilha)
    mudou = False
    for ws in wb.worksheets:
        for linha in ws.iter_rows():
            for celula in linha:
                alvo = celula.hyperlink.target if celula.hyperlink else None
                if not alvo or not _ALVO_EVIDENCIA.match(alvo):
                    continue
                novo = "../../" + alvo
                if celula.value == alvo:
                    celula.value = novo
                celula.hyperlink.target = novo
                mudou = True
    if mudou:
        wb.save(planilha)


def arquivar_relatorios_anteriores(dir_campanha: Path) -> Path | None:
    """Move os relatórios da geração anterior (com dossiês e recortes) para ``anteriores/<carimbo>/``,
    com a hora em que aquela geração foi gravada."""
    presentes = [
        dir_campanha / nome
        for nome in (*caminhos.ARQUIVOS_RELATORIO, *caminhos.DIRS_RELATORIO)
        if (dir_campanha / nome).exists()
    ]
    if not presentes:
        return None
    momento = datetime.fromtimestamp(min(p.stat().st_mtime for p in presentes), UTC)
    destino = caminhos.dir_anterior_livre(dir_campanha, caminhos.carimbo(momento))
    destino.mkdir(parents=True)
    movidos: list[Path] = []
    for p in presentes:
        try:
            p.rename(destino / p.name)
        except OSError as exc:
            for m in reversed(movidos):
                (destino / m.name).rename(m)
            destino.rmdir()
            raise RuntimeError(
                f"Não foi possível mover {p} para {destino} ({exc}). "
                "Se o arquivo estiver aberto em outro programa, feche-o e gere o relatório de novo."
            ) from exc
        movidos.append(p)
    # As páginas arquivadas ficam duas pastas abaixo: os links para as evidências precisam subir mais.
    for html in [*destino.glob("*.html"), *destino.glob(f"{caminhos.DIR_DOSSIES}/*.html")]:
        texto = html.read_text(encoding="utf-8")
        novo = _LINK_EVIDENCIA.sub(r"\g<attr>../../\g<sobe>", texto)
        if novo != texto:
            html.write_text(novo, encoding="utf-8")
    planilha = destino / caminhos.PLANILHA
    if planilha.exists():
        try:
            _ajustar_links_planilha(planilha)
        except Exception as exc:  # planilha antiga ilegível não impede gerar a nova
            log.warning("links da planilha arquivada não ajustados (%s): %s", planilha, exc)
    return destino


def gravar_recortes_logo_antiga(registros: list[RegistroCandidata], dir_campanha: Path) -> dict[str, Path]:
    """Recorte em PNG da melhor correspondência com a logo antiga de cada página (``logo-antiga/<host>_<id>.png``)."""
    saida: dict[str, Path] = {}
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
        caminho = caminhos.arquivo_logo_antiga(dir_campanha, r.candidata.hostname, r.candidata.id)
        caminho.parent.mkdir(parents=True, exist_ok=True)
        recorte.save(caminho)
        saida[r.candidata.id] = caminho
    return saida


def gerar_relatorios(
    configuracoes: Configuracoes, armazenamento: Armazenamento, id_campanha: str, todos_dossies: bool = False
) -> ResultadoRelatorios:
    camp = armazenamento.obter_campanha(id_campanha)
    if camp is None:
        raise KeyError(f"campanha não encontrada: {id_campanha}")
    marca = carregar_marca(camp["marca"], configuracoes.arquivo_marca)
    registros = carregar_registros(armazenamento, id_campanha, marca)
    dir_campanha = caminhos.dir_campanha(configuracoes.dir_saida, id_campanha)
    arquivos = caminhos.relatorios(dir_campanha)
    intel = carregar_inteligencia(configuracoes.dir_saida, id_campanha)
    anteriores = arquivar_relatorios_anteriores(dir_campanha)
    dossies = gravar_dossies(
        registros, dir_campanha, todas_candidatas=todos_dossies, inteligencia_dominio=intel, id_marca=marca.id
    )
    recortes = gravar_recortes_logo_antiga(registros, dir_campanha)
    planilha = gravar_planilha(arquivos.planilha, armazenamento, id_campanha, registros, dossies, recortes, intel)

    # O relatório de ameaças sai antes do da campanha, que aponta para ele.
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
            arquivos.ameacas, linhas, id_campanha, dir_campanha, dossies, defensivos, marca.id, marca.nome_exibicao
        )
    relatorio_html = gravar_html_campanha(
        arquivos.html, registros, armazenamento, id_campanha, dossies, marca.nome_exibicao, relatorio_ameacas
    )
    relatorio_pdf = gerar_pdf(relatorio_html, arquivos.pdf)

    return ResultadoRelatorios(
        planilha=planilha,
        relatorio_html=relatorio_html,
        relatorio_pdf=relatorio_pdf,
        dossies=dossies,
        dir_campanha=dir_campanha,
        relatorio_ameacas=relatorio_ameacas,
        anteriores=anteriores,
    )
