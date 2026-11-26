"""Consolidated HTML campaign report aligned with Banco BV Design System."""

from __future__ import annotations

import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from simio.nucleo.armazenamento import Armazenamento
from simio.relatorios.dados import RegistroCandidata
from simio.relatorios.dados_logo import LOGO_BV_BRANCA_DATA_URI

_env = Environment(
    loader=PackageLoader("simio.relatorios", "templates"),
    autoescape=select_autoescape(["html", "j2"], default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)

MAPA_ROTULOS = {
    "INDICIO_FORTE": "Forte Indício",
    "REQUER_REVISAO": "Requer Análise",
    "INCONCLUSIVA": "Inconclusivo",
    "SEM_INDICIO": "Sem Indício",
}


def renderizar_relatorio_campanha(
    registros: list[RegistroCandidata],
    armazenamento: Armazenamento,
    id_campanha: str,
    dossies: dict[str, str],
    caminho_html_relatorio: Path,
) -> str:
    camp = armazenamento.obter_campanha(id_campanha) or {}
    id_marca = camp.get("marca", "bv")
    status_camp = camp.get("status", "CONCLUIDA")
    
    # Execution date formatted in Brazilian standard
    agora = datetime.now(UTC)
    executada_em = agora.strftime("%d/%m/%Y %H:%M:%S UTC")

    contagens = Counter()
    revisoes_pendentes = 0
    linhas_candidatas = []

    for rec in registros:
        f = rec.final
        status_c = f.classificacao.value if f else "INCONCLUSIVA"
        contagens[status_c] += 1

        if f and f.exige_revisao_humana:
            revisoes_pendentes += 1

        dossie_rel = ""
        if rec.candidata.id in dossies:
            # relative link from report_html_path to dossier file
            alvo = caminho_html_relatorio.parent / dossies[rec.candidata.id]
            dossie_rel = Path(os.path.relpath(alvo, caminho_html_relatorio.parent)).as_posix()

        status_revisao = f.status_revisao_humana if f else "NAO_EXIGIDA"
        if rec.revisao:
            status_revisao = f"{rec.revisao.decisao.value} ({status_revisao})"

        linhas_candidatas.append({
            "id": rec.candidata.id,
            "dominio": rec.candidata.dominio_registravel or rec.candidata.hostname,
            "url": (rec.evidencia.url_final if rec.evidencia else None) or rec.candidata.url,
            "classificacao": status_c,
            "rotulo_classificacao": MAPA_ROTULOS.get(status_c, status_c),
            "eh_prioritaria": rec.eh_prioritaria,
            "tipo_entidade": rec.identidade.classificacao.value if rec.identidade else "DESCONHECIDO",
            "nome_entidade": (rec.identidade.nome_exibicao or rec.identidade.chave_entidade) if rec.identidade else "",
            "cenario_marca": rec.marca.caso.value if rec.marca else "",
            "status_revisao": status_revisao,
            "resumo": f.justificativa if f else "",
            "logo_antiga": bool(f and f.identidade_visual_antiga),
            "dados_sensiveis": ", ".join(f.dados_sensiveis_solicitados) if f else "",
            "dossie_rel": dossie_rel,
        })

    # Sort candidates by priority & classification (STRONG_INDICATION first)
    ordem = {"INDICIO_FORTE": 0, "REQUER_REVISAO": 1, "INCONCLUSIVA": 2, "SEM_INDICIO": 3}
    linhas_candidatas.sort(key=lambda r: (0 if r["eh_prioritaria"] else 1, ordem.get(r["classificacao"], 9)))

    disc = armazenamento.obter_checkpoint(id_campanha, "descoberta", {}) or {}
    fontes_usadas = disc.get("fontes_usadas", [])
    reqs_llm, toks_llm = armazenamento.totais_uso(id_campanha)

    return _env.get_template("relatorio_campanha.html.j2").render(
        id_campanha=id_campanha,
        id_marca=id_marca,
        status_campanha=status_camp,
        executada_em=executada_em,
        total_candidatas=len(registros),
        contagens=contagens,
        revisoes_pendentes=revisoes_pendentes,
        candidatas=linhas_candidatas,
        fontes_usadas=fontes_usadas,
        requisicoes_llm=reqs_llm,
        tokens_llm=toks_llm,
        uri_logo_bv_branca=LOGO_BV_BRANCA_DATA_URI,
    )


def gravar_html_campanha(
    arquivo_relatorio: Path,
    registros: list[RegistroCandidata],
    armazenamento: Armazenamento,
    id_campanha: str,
    dossies: dict[str, str],
) -> Path:
    arquivo_relatorio.parent.mkdir(parents=True, exist_ok=True)
    conteudo = renderizar_relatorio_campanha(registros, armazenamento, id_campanha, dossies, arquivo_relatorio)
    arquivo_relatorio.write_text(conteudo, encoding="utf-8")
    return arquivo_relatorio
