from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from simio.nucleo import caminhos
from simio.nucleo.armazenamento import Armazenamento
from simio.nucleo.modelos import ClassificacaoFinal
from simio.nucleo.urls import hostname_de
from simio.relatorios.dados import RegistroCandidata
from simio.relatorios.paginas import agora_br, ambiente, data_br, logo_marca

# Ordem das tabelas da segunda parte do relatório; as fechadas viram <details> (abertas no PDF).
GRUPOS_TABELA = (
    ("REQUER_REVISAO", "Requer revisão", False),
    ("INCONCLUSIVA", "Inconclusivas", True),
    ("SEM_INDICIO", "Sem indício", True),
    ("", "Não analisadas", True),
)


def _em_destaque(rec: RegistroCandidata) -> bool:
    """Achados que ganham cartão com captura; o resto vai para as tabelas."""
    f = rec.final
    if f is None:
        return False
    return (
        f.classificacao == ClassificacaoFinal.INDICIO_FORTE
        or (f.classificacao == ClassificacaoFinal.REQUER_REVISAO and f.prioridade.value == "ALTA")
        or f.identidade_visual_antiga
        or bool(f.dados_sensiveis_solicitados)
    )


def _linha(rec: RegistroCandidata, caminho_html: Path, dir_campanha: Path, dossies: dict[str, Path]) -> dict[str, Any]:
    f, ev, idn = rec.final, rec.evidencia, rec.identidade
    dossie = dossies.get(rec.candidata.id)
    captura = ev.artefato("captura_viewport") if ev else None
    url = (ev.url_final if ev else None) or rec.candidata.url
    return {
        "id": rec.candidata.id,
        "host": hostname_de(url) or rec.candidata.hostname,
        "url": url,
        "classificacao": f.classificacao.value if f else "",
        "andamento": "" if f else rec.candidata.status.value,
        "prioridade": f.prioridade.value if f else "",
        "caso": f.caso.value if f else "",
        "identidade": idn.classificacao.value if idn else "",
        "nome_entidade": (idn.nome_exibicao or "") if idn else "",
        "revisao_critica": rec.revisao.decisao.value if rec.revisao else "",
        "revisao_humana": f.status_revisao_humana if f else "",
        "justificativa": (f.justificativa if f else rec.candidata.ultimo_erro) or "",
        "logo_antiga": bool(f and f.identidade_visual_antiga),
        "dados_sensiveis": ", ".join(f.dados_sensiveis_solicitados) if f else "",
        "captura": caminhos.link(caminho_html, dir_campanha / captura.caminho) if captura else None,
        "dossie": caminhos.link(caminho_html, dossie) if dossie else "",
    }


def renderizar_relatorio_campanha(
    registros: list[RegistroCandidata],
    armazenamento: Armazenamento,
    id_campanha: str,
    dossies: dict[str, Path],
    caminho_html: Path,
    nome_marca: str = "",
    relatorio_ameacas: Path | None = None,
) -> str:
    camp = armazenamento.obter_campanha(id_campanha) or {}
    id_marca = camp.get("marca") or "bv"
    dir_campanha = caminho_html.parent
    contagens = Counter((r.final.classificacao.value if r.final else "") for r in registros)
    destaques = [_linha(r, caminho_html, dir_campanha, dossies) for r in registros if _em_destaque(r)]
    restantes = [_linha(r, caminho_html, dir_campanha, dossies) for r in registros if not _em_destaque(r)]
    grupos = []
    for cls, titulo, fechado in GRUPOS_TABELA:
        linhas = [x for x in restantes if x["classificacao"] == cls]
        if linhas:
            grupos.append({"titulo": titulo, "fechado": fechado, "linhas": linhas})

    disc = armazenamento.obter_checkpoint(id_campanha, "descoberta", {}) or {}
    requisicoes, tokens = armazenamento.totais_uso(id_campanha)
    rel = caminhos.relatorios(dir_campanha)
    nav = []
    if relatorio_ameacas is not None:
        nav.append(("Ameaças", caminhos.link(caminho_html, relatorio_ameacas)))
    nav.append(("Planilha", caminhos.link(caminho_html, rel.planilha)))
    inicio, fim = camp.get("criado_em"), camp.get("finalizado_em")
    return ambiente.get_template("relatorio_campanha.html.j2").render(
        id_campanha=id_campanha,
        nome_marca=nome_marca or id_marca.upper(),
        uri_logo=logo_marca(id_marca),
        nav=nav,
        status_campanha=camp.get("status") or "",
        foco=(camp.get("parametros") or {}).get("foco") or "",
        periodo=" a ".join(x for x in (data_br(inicio), data_br(fim)) if x),
        gerado_em=agora_br(),
        total=len(registros),
        contagens=contagens,
        revisoes_pendentes=sum(1 for r in registros if r.final and r.final.exige_revisao_humana),
        destaques=destaques,
        grupos=grupos,
        fontes_usadas=disc.get("fontes_usadas", []),
        requisicoes_llm=requisicoes,
        tokens_llm=tokens,
    )


def gravar_html_campanha(
    arquivo_relatorio: Path,
    registros: list[RegistroCandidata],
    armazenamento: Armazenamento,
    id_campanha: str,
    dossies: dict[str, Path],
    nome_marca: str = "",
    relatorio_ameacas: Path | None = None,
) -> Path:
    arquivo_relatorio.parent.mkdir(parents=True, exist_ok=True)
    conteudo = renderizar_relatorio_campanha(
        registros, armazenamento, id_campanha, dossies, arquivo_relatorio, nome_marca, relatorio_ameacas
    )
    arquivo_relatorio.write_text(conteudo, encoding="utf-8")
    return arquivo_relatorio
