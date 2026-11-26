from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")
MODO_AMPLA = "ampla"

DIR_SAIDA_PADRAO = "resultados"
ARQUIVO_BANCO = "simio.db"
DIR_REGISTROS = "registros"
ARQUIVO_LOG = "simio.jsonl"
DIR_CAMPANHAS = "campanhas"
ARQUIVO_PONTUACAO = "pontuacao.toml"

PLANILHA = "relatorio.xlsx"
RELATORIO_HTML = "relatorio.html"
RELATORIO_PDF = "relatorio.pdf"
RELATORIO_AMEACAS = "ameacas.html"
ARQUIVO_DOMINIOS = "dominios.json"
DIR_DOSSIES = "dossies"
DIR_LOGO_ANTIGA = "logo-antiga"
DIR_EVIDENCIAS = "evidencias"
DIR_ANTERIORES = "anteriores"
ARQUIVO_RENOMEACOES = "renomeacoes.json"
PREFIXO_TENTATIVA = "tentativa-"
ARQUIVO_MANIFESTO = "manifesto.json"

# Tipo do artefato -> nome do arquivo na pasta da tentativa (o banco e o código usam o tipo).
ARTEFATOS = {
    "captura_viewport": "tela-inicial.png",
    "captura_completa": "pagina-inteira.png",
    "html": "codigo-fonte.html.txt",
    "texto": "texto-visivel.txt",
    "dados_pagina": "dados-da-pagina.json",
}

# Arquivos que o relatório grava na raiz da campanha; os da geração anterior vão antes para anteriores/.
ARQUIVOS_RELATORIO = (PLANILHA, RELATORIO_HTML, RELATORIO_PDF, RELATORIO_AMEACAS)
DIRS_RELATORIO = (DIR_DOSSIES, DIR_LOGO_ANTIGA)

_MAX_HOST = 60
_NAO_SEGURO = re.compile(r"[^a-z0-9.-]+")


def carimbo(momento: datetime | None = None) -> str:
    # Horário de Brasília; datetime sem fuso é tratado como UTC.
    momento = momento or datetime.now(UTC)
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=UTC)
    return momento.astimezone(FUSO_BRASILIA).strftime("%Y-%m-%d_%Hh%M")


def _segmento(texto: str, padrao: str) -> str:
    limpo = _NAO_SEGURO.sub("-", (texto or "").lower()).strip(".-")
    return limpo or padrao


def modo_campanha(foco: str | None) -> str:
    return _segmento(foco or MODO_AMPLA, MODO_AMPLA)


def id_campanha(momento: datetime, marca: str, foco: str | None) -> str:
    # O armazenamento acrescenta -2, -3... se o id já existir.
    return f"{carimbo(momento)}_{_segmento(marca, 'marca')}_{modo_campanha(foco)}"


def com_sufixo(base: str, n: int) -> str:
    return base if n <= 1 else f"{base}-{n}"


def host_seguro(hostname: str) -> str:
    host = (hostname or "").strip().lower().rstrip(".")
    host = host.removeprefix("www.")
    with contextlib.suppress(UnicodeError):
        host = host.encode("idna").decode("ascii")
    return _segmento(host, "sem-host")[:_MAX_HOST].rstrip(".-") or "sem-host"


def id_curto(id_candidata: str) -> str:
    corpo = id_candidata[1:] if id_candidata.startswith("C") and len(id_candidata) > 1 else id_candidata
    return _segmento(corpo[:8], "x")


def chave_candidata(hostname: str, id_candidata: str) -> str:
    # Nomeia o dossiê, o recorte da logo e a pasta de evidências da candidata.
    return f"{host_seguro(hostname)}_{id_curto(id_candidata)}"


def arquivo_banco(dir_saida: Path) -> Path:
    return dir_saida / ARQUIVO_BANCO


def arquivo_log(dir_saida: Path) -> Path:
    return dir_saida / DIR_REGISTROS / ARQUIVO_LOG


def arquivo_pontuacao(dir_saida: Path) -> Path:
    novo = dir_saida / ARQUIVO_PONTUACAO
    antigo = dir_saida / "scoring.toml"
    return antigo if not novo.exists() and antigo.exists() else novo


def dir_campanhas(dir_saida: Path) -> Path:
    return dir_saida / DIR_CAMPANHAS


def dir_campanha(dir_saida: Path, id_campanha: str) -> Path:
    return dir_saida / DIR_CAMPANHAS / id_campanha


def arquivo_dominios(dir_campanha: Path) -> Path:
    return dir_campanha / ARQUIVO_DOMINIOS


@dataclass(frozen=True)
class Relatorios:
    planilha: Path
    html: Path
    pdf: Path
    ameacas: Path
    dossies: Path
    logo_antiga: Path


def relatorios(dir_campanha: Path) -> Relatorios:
    return Relatorios(
        planilha=dir_campanha / PLANILHA,
        html=dir_campanha / RELATORIO_HTML,
        pdf=dir_campanha / RELATORIO_PDF,
        ameacas=dir_campanha / RELATORIO_AMEACAS,
        dossies=dir_campanha / DIR_DOSSIES,
        logo_antiga=dir_campanha / DIR_LOGO_ANTIGA,
    )


def arquivo_dossie(dir_campanha: Path, hostname: str, id_candidata: str) -> Path:
    return dir_campanha / DIR_DOSSIES / f"{chave_candidata(hostname, id_candidata)}.html"


def arquivo_logo_antiga(dir_campanha: Path, hostname: str, id_candidata: str) -> Path:
    return dir_campanha / DIR_LOGO_ANTIGA / f"{chave_candidata(hostname, id_candidata)}.png"


def dir_tentativa(hostname: str, id_candidata: str, tentativa: int) -> PurePosixPath:
    # Relativo à pasta da campanha e sempre com "/", porque vai para o banco.
    return PurePosixPath(DIR_EVIDENCIAS, chave_candidata(hostname, id_candidata), f"{PREFIXO_TENTATIVA}{tentativa}")


def arquivo_renomeacoes(dir_campanha: Path) -> Path:
    return dir_campanha / DIR_EVIDENCIAS / ARQUIVO_RENOMEACOES


def dir_anteriores(dir_campanha: Path) -> Path:
    return dir_campanha / DIR_ANTERIORES


def dir_anterior_livre(dir_campanha: Path, nome: str) -> Path:
    base = dir_anteriores(dir_campanha)
    n = 1
    while (base / com_sufixo(nome, n)).exists():
        n += 1
    return base / com_sufixo(nome, n)


def link(origem: Path, alvo: Path) -> str:
    # Link relativo, com "/", do jeito que vai no HTML e na planilha.
    return Path(os.path.relpath(alvo, origem.parent)).as_posix()
