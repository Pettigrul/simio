from __future__ import annotations

from collections import Counter
from pathlib import Path

from simio.agentes.inteligencia_dominio import resumir as resumir_inteligencia
from simio.agentes.triagem_ameacas import NIVEIS, VisaoAmeaca
from simio.nucleo import caminhos
from simio.nucleo.modelos import InteligenciaDominio
from simio.relatorios.dados import RegistroCandidata
from simio.relatorios.paginas import agora_br, ambiente, logo_marca
from simio.relatorios.rotulos import dados_pedidos


def gravar_html_ameacas(
    caminho: Path,
    linhas: list[tuple[VisaoAmeaca, RegistroCandidata, InteligenciaDominio | None]],
    id_campanha: str,
    dir_campanha: Path,
    caminhos_dossies: dict[str, Path],
    defensivos: list[tuple[str, str]] | None = None,
    id_marca: str = "bv",
    nome_marca: str = "",
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
                "url": (ev.url_final if ev else None) or r.candidata.url,
                "dominio": r.candidata.dominio_registravel,
                "intel": resumir_inteligencia(item) if item else None,
                "sensiveis": dados_pedidos(f.dados_sensiveis_solicitados) if f else "",
                "classificacao": f.classificacao.value if f else "",
                "justificativa": (f.justificativa or "")[:700] if f else "",
                "captura": captura,
                "dossie": caminhos.link(caminho, dossie) if dossie else None,
                "cid": r.candidata.id,
            }
        )
    niveis = Counter(c["nivel"] for c in fichas)
    categorias = Counter(cat for c in fichas for cat in c["categorias"])
    rel = caminhos.relatorios(dir_campanha)
    html = ambiente.get_template("relatorio_ameacas.html.j2").render(
        id_campanha=id_campanha,
        nome_marca=nome_marca or id_marca.upper(),
        uri_logo=logo_marca(id_marca),
        nav=[
            ("Relatório da campanha", caminhos.link(caminho, rel.html)),
            ("Planilha", caminhos.link(caminho, rel.planilha)),
        ],
        gerado_em=agora_br(),
        fichas=fichas,
        niveis=[(nv, niveis.get(nv, 0)) for nv in NIVEIS[:-1]],
        categorias=categorias.most_common(),
        defensivos=defensivos or [],
    )
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(html, encoding="utf-8")
    return caminho
