"""Threat-intelligence briefing (HTML): what impersonates the bank or endangers its customers, with the
facts behind each level, the domain intelligence and the takedown contacts.

URLs of suspicious pages are *defanged* (``hxxps://exemplo[.]com``) so nobody opens them by accident."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from simio.agentes.inteligencia_dominio import resumir as resumir_inteligencia
from simio.agentes.triagem_ameacas import NIVEIS, VisaoAmeaca
from simio.nucleo import caminhos
from simio.nucleo.modelos import InteligenciaDominio
from simio.relatorios.dados import RegistroCandidata

_env = Environment(
    loader=PackageLoader("simio.relatorios", "templates"),
    autoescape=select_autoescape(["html", "j2"], default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def desarmar_url(url: str) -> str:
    saida = re.sub(r"^http", "hxxp", url, flags=re.IGNORECASE)
    fim_host = saida.find("/", saida.find("://") + 3) if "://" in saida else -1
    host = saida[: fim_host if fim_host > 0 else len(saida)]
    return host.replace(".", "[.]") + (saida[fim_host:] if fim_host > 0 else "")


def gravar_html_ameacas(
    caminho: Path,
    linhas: list[tuple[VisaoAmeaca, RegistroCandidata, InteligenciaDominio | None]],
    id_campanha: str,
    dir_campanha: Path,
    caminhos_dossies: dict[str, Path],
    defensivos: list[tuple[str, str]] | None = None,
) -> Path:
    fichas = []
    for t, r, item in linhas:
        ev, f = r.evidencia, r.final
        captura = None
        if ev:
            a = ev.artefato("captura_viewport")
            if a:
                captura = caminhos.link(caminho, dir_campanha / a.caminho)
        dossie = caminhos_dossies.get(r.candidata.id)
        fichas.append(
            {
                "nivel": t.nivel,
                "pontuacao": t.pontuacao,
                "categorias": t.categorias,
                "fatos": t.fatos,
                "url": desarmar_url((ev.url_final if ev else None) or r.candidata.url),
                "dominio": r.candidata.dominio_registravel,
                "intel": resumir_inteligencia(item) if item else None,
                "sensiveis": ", ".join(f.dados_sensiveis_solicitados) if f else "",
                "classificacao": f.classificacao.value if f else "",
                "justificativa": (f.justificativa or "")[:700] if f else "",
                "captura": captura,
                "dossie": caminhos.link(caminho, dossie) if dossie else None,
                "cid": r.candidata.id,
            }
        )
    niveis = Counter(c["nivel"] for c in fichas)
    categorias = Counter(cat for c in fichas for cat in c["categorias"])
    html = _env.get_template("relatorio_ameacas.html.j2").render(
        id_campanha=id_campanha,
        gerado_em=datetime.now(UTC).strftime("%d/%m/%Y %H:%M UTC"),
        fichas=fichas,
        niveis=[(nv, niveis.get(nv, 0)) for nv in NIVEIS[:-1]],
        categorias=categorias.most_common(),
        defensivos=defensivos or [],
    )
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(html, encoding="utf-8")
    return caminho
