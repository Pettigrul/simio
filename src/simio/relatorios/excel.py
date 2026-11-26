"""Consolidated Excel workbook (openpyxl) aligned with Banco BV Design System."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.drawing.image import Image as OpenpyxlImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from simio.agentes.descoberta import FOCO_AMEACAS
from simio.agentes.inteligencia_dominio import resumir as resumir_inteligencia
from simio.agentes.triagem_ameacas import VisaoAmeaca, triar
from simio.marcas import PerfilMarca, carregar_marca
from simio.nucleo import caminhos
from simio.nucleo.armazenamento import Armazenamento
from simio.nucleo.modelos import InteligenciaDominio
from simio.nucleo.urls import hostname_de
from simio.relatorios.dados import RegistroCandidata

# Banco BV Design System Theme Colors
# --bv-blue-dark: #001A6B
# --bv-blue-primary: #0B5FFF
# --bv-bg-light: #F5F5F5 / #EFF4FB
PREENCHIMENTO_CABECALHO = PatternFill("solid", fgColor="001A6B")
FONTE_CABECALHO = Font(name="Inter", size=11, color="FFFFFF", bold=True)
FONTE_DADOS = Font(name="Inter", size=10, color="272833")
FONTE_LINK = Font(name="Inter", size=10, color="0B5FFF", underline="single")

# Status and Classification Fills with high legibility and soft tint
PREENCHIMENTOS = {
    "INDICIO_FORTE": PatternFill("solid", fgColor="FCE8E6"),  # Soft red
    "REQUER_REVISAO": PatternFill("solid", fgColor="FEF3D6"),    # Soft amber
    "INCONCLUSIVA": PatternFill("solid", fgColor="E7E7ED"),       # Soft gray
    "SEM_INDICIO": PatternFill("solid", fgColor="E6F4EA"),      # Soft green
    # Threat levels (threat triage)
    "CRÍTICO": PatternFill("solid", fgColor="F9D2CE"),
    "ALTO": PatternFill("solid", fgColor="FCE8E6"),
    "MÉDIO": PatternFill("solid", fgColor="FEF3D6"),
    "BAIXO": PatternFill("solid", fgColor="E7E7ED"),
}

FONTES_STATUS = {
    "INDICIO_FORTE": Font(name="Inter", size=10, color="B42318", bold=True),
    "REQUER_REVISAO": Font(name="Inter", size=10, color="B54708", bold=True),
    "INCONCLUSIVA": Font(name="Inter", size=10, color="475467", bold=True),
    "SEM_INDICIO": Font(name="Inter", size=10, color="067647", bold=True),
    "CRÍTICO": Font(name="Inter", size=10, color="912018", bold=True),
    "ALTO": Font(name="Inter", size=10, color="B42318", bold=True),
    "MÉDIO": Font(name="Inter", size=10, color="B54708", bold=True),
    "BAIXO": Font(name="Inter", size=10, color="475467", bold=True),
}

BORDA_FINA = Border(
    left=Side(style="thin", color="E7E7ED"),
    right=Side(style="thin", color="E7E7ED"),
    top=Side(style="thin", color="E7E7ED"),
    bottom=Side(style="thin", color="E7E7ED"),
)

_ILEGAIS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
MAX_CELULA = 32000


@dataclass(frozen=True)
class _Link:
    """Cell shown as a relative path that opens the file (resolved by Excel from the workbook's folder)."""

    alvo: str


def _link(planilha: Path, alvo: Path | None) -> _Link | str:
    return _Link(caminhos.link(planilha, alvo)) if alvo else ""


def _celula(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, _Link):
        v = v.alvo
    if isinstance(v, (list, tuple, set)):
        v = "\n".join(str(x) for x in v)
    elif isinstance(v, dict):
        v = json.dumps(v, ensure_ascii=False)
    if isinstance(v, str):
        v = _ILEGAIS.sub("", v)[:MAX_CELULA]
        # Neutralise spreadsheet formula injection from untrusted page content.
        if v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            v = "'" + v
    return v


def _aba(
    wb: Workbook,
    titulo: str,
    cabecalhos: list[str],
    linhas: list[list[Any]],
    larguras: dict[int, int] | None = None,
    coluna_preenchimento: int | None = None,
) -> None:
    ws = wb.create_sheet(titulo)
    ws.append(cabecalhos)
    ws.row_dimensions[1].height = 28
    for c in ws[1]:
        c.fill, c.font = PREENCHIMENTO_CABECALHO, FONTE_CABECALHO
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="left")
        c.border = BORDA_FINA
    for linha in linhas:
        ws.append([_celula(v) for v in linha])
        for coluna, v in enumerate(linha, start=1):
            if isinstance(v, _Link):
                ws.cell(row=ws.max_row, column=coluna).hyperlink = v.alvo
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.font = FONTE_LINK if c.hyperlink else FONTE_DADOS
            c.border = BORDA_FINA
            c.alignment = Alignment(wrap_text=True, vertical="top")
        if coluna_preenchimento is not None:
            val = str(r[coluna_preenchimento].value)
            preenchimento = PREENCHIMENTOS.get(val)
            if preenchimento:
                r[coluna_preenchimento].fill = preenchimento
            fonte_s = FONTES_STATUS.get(val)
            if fonte_s:
                r[coluna_preenchimento].font = fonte_s
    for i in range(1, len(cabecalhos) + 1):
        ws.column_dimensions[get_column_letter(i)].width = (larguras or {}).get(i, 22)
    ws.freeze_panes = "A2"
    if linhas:
        ws.auto_filter.ref = ws.dimensions


def _evidencia_principal(rec: RegistroCandidata, n: int = 6) -> str:
    if not rec.final:
        return ""
    por_ref = {i.ref: i for i in rec.catalogo}
    return "\n".join(f"[{r}] {por_ref[r].conteudo[:220]}" for r in rec.final.evidencias_chave[:n] if r in por_ref)


GLOSSARIO: list[list[str]] = [
    ["SEM_INDICIO", "Sem indício de confusão com a instituição no material coletado (não atesta conformidade)"],
    ["REQUER_REVISAO", "Requer análise humana: indícios de ambiguidade ou de apresentação como canal da instituição"],
    ["INDICIO_FORTE", "Forte indício de que o consumidor pode crer tratar diretamente com a instituição"],
    ["INCONCLUSIVA", "Evidência insuficiente (coleta falhou, conteúdo bloqueado ou contraditório)"],
    ["A_MENCAO_COMERCIAL_LEGITIMA", "Cenário A — menção comercial com condição de correspondente clara"],
    ["B_COMUNICACAO_AMBIGUA", "Cenário B — uso destacado da marca sem transparência suficiente"],
    ["C_POSSIVEL_CANAL_OFICIAL", "Cenário C — possível apresentação como canal oficial da instituição"],
    ["D_PERSONIFICACAO_FORTE", "Cenário D — forte indício de impersonação"],
    ["E_EVIDENCIA_INSUFICIENTE", "Cenário E — evidência insuficiente para avaliar a comunicação"],
    ["NAO_RELEVANTE", "A página não usa a marca de forma relevante"],
    ["COBAN_CONFIRMADO", "CNPJ do responsável consta de cadastro autoritativo de correspondentes"],
    ["COBAN_POSSIVEL", "Indícios de vínculo (declaração, nome) sem confirmação em cadastro autoritativo"],
    ["OUTRO_TERCEIRO", "Empresa identificada sem indício de relação de correspondente"],
    ["IDENTIDADE_DESCONHECIDA", "Responsável pela página não identificado"],
    ["HIPOTESE_NAO_CONFORMIDADE", "Hipótese de não conformidade (correspondente confirmado) — exige revisão"],
    ["HIPOTESE_CONDICIONADA_VINCULO", "Hipótese aplicável somente se confirmado o vínculo de correspondente"],
    ["NAO_APLICAVEL_SALVO_COBAN", "Responsável não identificado como correspondente; possível tema de marca/fraude"],
    ["SEM_INDICIO_OBSERVADO", "Nenhum indício observado para o requisito no material coletado"],
    ["NAO_AVALIAVEL", "Não avaliável com a evidência disponível"],
    ["PENDENTE / NAO_EXIGIDA", "Status da revisão humana (registrar a decisão da auditoria nos papéis de trabalho)"],
    ["E-…", "Referências ao catálogo de evidências (ver aba Evidence Index e dossiês)"],
    [
        "Confiança",
        "Extração = leitura das evidências; atribuição = vínculo página→responsável. Nenhuma é "
        "probabilidade de irregularidade.",
    ],
]


ABA_LOGO_ANTIGA = "Logo antiga"
_ALTURA_LINHA_RECORTE = 64


def _aba_logo_antiga(
    wb: Workbook,
    planilha: Path,
    registros: list[RegistroCandidata],
    caminhos_dossies: dict[str, Path],
    recortes: dict[str, Path],
) -> None:
    """One row per page where the discontinued logo was found (model and/or image comparison)."""
    linhas: list[list[Any]] = []
    imagens: list[tuple[int, Path]] = []
    for r in registros:
        f, b, idn = r.final, r.marca, r.identidade
        if f is None or not f.identidade_visual_antiga:
            continue
        correspondencias = b.correspondencias_visuais if b else []
        melhor = max(correspondencias, key=lambda m: m.pontuacao) if correspondencias else None
        reg = idn.correspondencia_cadastro.registro if idn and idn.correspondencia_cadastro else {}
        linhas.append(
            [
                "",
                r.candidata.dominio_registravel,
                r.candidata.url,
                f.classificacao.value,
                f.prioridade.value,
                f.fonte_identidade_visual_antiga,
                "sim" if b and b.logo_antiga_isenta else "não",
                melhor.pontuacao if melhor else "",
                (f"{melhor.ref_captura} x={melhor.bbox[0]} y={melhor.bbox[1]} {melhor.bbox[2]}x{melhor.bbox[3]}px")
                if melhor
                else "",
                ("sim" if melhor.acima_dobra else "não") if melhor else "",
                "\n".join(dict.fromkeys(m.src_imagem for m in correspondencias if m.src_imagem)),
                idn.classificacao.value if idn else "",
                f"{reg.get('nome', '')} ({reg.get('cnpj', '')})" if reg else "",
                _link(planilha, recortes.get(r.candidata.id)),
                _link(planilha, caminhos_dossies.get(r.candidata.id)),
            ]
        )
        if r.candidata.id in recortes:
            imagens.append((len(linhas) + 1, recortes[r.candidata.id]))
    cabecalhos = [
        "Recorte",
        "Domínio",
        "URL",
        "Classificação",
        "Prioridade",
        "Origem da detecção",
        "Site de referência (não eleva)",
        "Similaridade (comparação de imagem)",
        "Onde (print e posição)",
        "Acima da dobra",
        "Imagem na página",
        "Identidade atribuída",
        "Cadastro de correspondentes",
        "Arquivo do recorte",
        "Dossiê",
    ]
    _aba(
        wb,
        ABA_LOGO_ANTIGA,
        cabecalhos,
        linhas,
        {1: 24, 3: 60, 6: 30, 9: 34, 11: 60, 13: 40, 14: 50},
        coluna_preenchimento=3,
    )
    ws = wb[ABA_LOGO_ANTIGA]
    for linha, recorte in imagens:
        try:
            img = OpenpyxlImage(str(recorte))
            escala = min(160 / img.width, (_ALTURA_LINHA_RECORTE * 1.3) / img.height, 1.0)
            img.width, img.height = int(img.width * escala), int(img.height * escala)
            ws.row_dimensions[linha].height = _ALTURA_LINHA_RECORTE
            ws.add_image(img, f"A{linha}")
        except Exception:  # noqa: S112 - a missing/broken crop must not break the report
            continue


ABA_AMEACAS = "Ameaças"


ABA_DOMINIOS = "Domínios"


def _host_pagina(r: RegistroCandidata) -> str:
    return hostname_de((r.evidencia.url_final if r.evidencia else None) or r.candidata.url) or r.candidata.hostname


def _aba_dominios(wb: Workbook, registros: list[RegistroCandidata], intel: dict[str, InteligenciaDominio]) -> None:
    """Registration (RDAP/WHOIS), DNS and hosting of every flagged host, with the takedown contacts."""
    pior: dict[str, tuple[str, str]] = {}
    posicao = {"INDICIO_FORTE": 0, "REQUER_REVISAO": 1}
    for r in registros:
        if r.final is None:
            continue
        host = _host_pagina(r)
        cur = (r.final.classificacao.value, r.final.prioridade.value)
        if host in intel and (host not in pior or posicao.get(cur[0], 9) < posicao.get(pior[host][0], 9)):
            pior[host] = cur
    linhas = []
    for host, item in sorted(intel.items(), key=lambda kv: (posicao.get(pior.get(kv[0], ("",))[0], 9), kv[0])):
        s = resumir_inteligencia(item)
        cls, prio = pior.get(host, ("", ""))
        linhas.append(
            [host, item.dominio_registrado, cls, prio, s["plataforma"], s["criado"], s["registrador"], s["titular"],
             s["resolve"], ", ".join(item.a[:4]), s["hospedagem"], s["ns"], s["mx"], s["abuso"], s["notas"]]
        )
    cabecalhos = [
        "Host", "Domínio registrado", "Classificação", "Prioridade", "Plataforma (dono só via denúncia)",
        "Criado em (idade)", "Registrador", "Titular", "Resolve hoje", "IPs", "Hospedagem (rede do IP)",
        "Servidores DNS (NS)", "E-mail (MX)", "Contatos de abuso (takedown)", "Observações",
    ]
    _aba(
        wb,
        ABA_DOMINIOS,
        cabecalhos,
        linhas,
        {1: 40, 2: 26, 8: 40, 11: 40, 12: 36, 14: 44, 15: 70},
        coluna_preenchimento=2,
    )


def linhas_ameacas(
    registros: list[RegistroCandidata],
    marca: PerfilMarca,
    foco: str | None,
    intel: dict[str, InteligenciaDominio] | None = None,
) -> list[tuple[VisaoAmeaca, RegistroCandidata, InteligenciaDominio | None]]:
    """Pages in the threat view, most dangerous first. In a threat campaign every level is listed; in
    other campaigns only CRÍTICO/ALTO pages and pages asking for credentials."""
    saida = []
    for r in registros:
        if r.final is None:
            continue
        item = (intel or {}).get(_host_pagina(r))
        t = triar(r, marca, item)
        manter = t.eh_ameaca if foco == FOCO_AMEACAS else (t.nivel in ("CRÍTICO", "ALTO") or bool(
            r.final.dados_sensiveis_solicitados and t.eh_ameaca))
        if manter:
            saida.append((t, r, item))
    saida.sort(key=lambda x: (-x[0].pontuacao, x[1].candidata.url))
    return saida


def _aba_ameacas(
    wb: Workbook,
    planilha: Path,
    linhas_entrada: list[tuple[VisaoAmeaca, RegistroCandidata, InteligenciaDominio | None]],
    caminhos_dossies: dict[str, Path],
) -> bool:
    """Threat-intelligence view: level, category and the facts behind it, with the IOCs and takedown data."""
    linhas: list[list[Any]] = []
    for t, r, item in linhas_entrada:
        f, ev = r.final, r.evidencia
        assert f is not None
        url_final = (ev.url_final if ev else None) or r.candidata.url
        acao = next((i.conteudo.split(": ", 1)[-1] for i in r.catalogo if i.ref == "E-FORM-ACTION"), "")
        di = resumir_inteligencia(item) if item else {}
        evento_registro = " · ".join(x for x in (di.get("criado"), di.get("registrador"), di.get("titular")) if x)
        linhas.append(
            [
                t.nivel,
                t.pontuacao,
                "; ".join(t.categorias),
                "\n".join(t.fatos),
                url_final,
                r.candidata.dominio_registravel,
                di.get("plataforma", ""),
                evento_registro,
                di.get("resolve", ""),
                di.get("hospedagem", "") or ((ev.ip_servidor or "") if ev else ""),
                di.get("abuso", ""),
                ", ".join(f.dados_sensiveis_solicitados),
                acao,
                "\n".join(ev.cadeia_redirecionamentos[:6]) if ev else "",
                f.classificacao.value,
                f.caso.value,
                (f.justificativa or "")[:600],
                ", ".join(sorted({h.fonte for h in r.resultados})),
                "\n".join(sorted({h.id_hipotese for h in r.resultados if h.id_hipotese}))[:500],
                _link(planilha, caminhos_dossies.get(r.candidata.id)),
            ]
        )
    if not linhas:
        return False
    cabecalhos = [
        "Nível de ameaça",
        "Pontos",
        "Categoria",
        "Fatos que levaram ao nível",
        "URL",
        "Domínio",
        "Plataforma (dono só via denúncia)",
        "Registro do domínio (criação · registrador · titular)",
        "Resolve hoje",
        "Hospedagem",
        "Contatos de abuso (takedown)",
        "Dados sensíveis pedidos",
        "Formulário envia para",
        "Redirecionamentos",
        "Classificação SIMIO (conformidade)",
        "Caso (analisador)",
        "Justificativa do analisador",
        "Fontes",
        "Hipóteses",
        "Dossiê",
    ]
    larguras = {3: 34, 4: 60, 5: 50, 6: 26, 8: 44, 10: 36, 11: 40, 14: 40, 17: 70, 19: 34, 20: 40}
    _aba(wb, ABA_AMEACAS, cabecalhos, linhas, larguras, coluna_preenchimento=0)
    return True


def gravar_planilha(
    caminho: Path,
    armazenamento: Armazenamento,
    id_campanha: str,
    registros: list[RegistroCandidata],
    caminhos_dossies: dict[str, Path],
    recortes_logo: dict[str, Path] | None = None,
    inteligencia_dominio: dict[str, InteligenciaDominio] | None = None,
) -> Path:
    """``caminho`` is the workbook at the campaign folder root: artifact paths are relative to it."""
    dir_campanha = caminho.parent
    camp = armazenamento.obter_campanha(id_campanha) or {}
    wb = Workbook()
    if wb.active is not None:
        wb.remove(wb.active)
    finais = [r.final for r in registros if r.final]
    contagem_cls = Counter(f.classificacao.value for f in finais)
    contagem_por_status = armazenamento.contagem_por_status(id_campanha)
    disc = armazenamento.obter_checkpoint(id_campanha, "descoberta", {}) or {}
    req, tok = armazenamento.totais_uso(id_campanha)
    analisadores = Counter(r.marca.analisador for r in registros if r.marca)

    resumo: list[list[Any]] = [
        ["Campanha", id_campanha],
        ["Marca", camp.get("marca")],
        ["Status da campanha", camp.get("status")],
        ["Início", camp.get("criado_em")],
        ["Relatório gerado em", datetime.now(UTC).isoformat(timespec="seconds")],
        ["Candidatos descobertos (únicos)", len(registros)],
        ["Status do pipeline", json.dumps(contagem_por_status, ensure_ascii=False)],
        ["INDICIO_FORTE", contagem_cls.get("INDICIO_FORTE", 0)],
        ["REQUER_REVISAO", contagem_cls.get("REQUER_REVISAO", 0)],
        ["INCONCLUSIVA", contagem_cls.get("INCONCLUSIVA", 0)],
        ["SEM_INDICIO", contagem_cls.get("SEM_INDICIO", 0)],
        ["Pendentes de revisão humana", sum(1 for f in finais if f.exige_revisao_humana)],
        ["Fontes de descoberta usadas", ", ".join(disc.get("fontes_usadas", []))],
        ["Consultas executadas", json.dumps(disc.get("consultas", {}), ensure_ascii=False)],
        ["Diagnóstico da descoberta", "\n".join(disc.get("diagnosticos", []))],
        ["Analisadores utilizados", json.dumps(dict(analisadores), ensure_ascii=False)],
        ["Requisições LLM / tokens", f"{req} / {tok}"],
        ["Parâmetros", json.dumps(camp.get("parametros", {}), ensure_ascii=False)],
        [
            "Aviso",
            "Classificações são indícios para apoio à auditoria; não constituem conclusão "
            "regulatória. Pontuações não representam probabilidade de irregularidade.",
        ],
    ]
    _aba(wb, "Executive Summary", ["Item", "Valor"], resumo, {1: 34, 2: 110})

    # Add Banco BV logo to Executive Summary if available
    exec_ws = wb["Executive Summary"]
    caminho_logo = Path("design-system/assets/bv-logo-blue.png")
    if caminho_logo.exists():
        try:
            img = OpenpyxlImage(str(caminho_logo))
            img.width = 120
            img.height = 89
            exec_ws.add_image(img, "D2")
        except Exception:
            pass

    cabecalhos_cand = [
        "ID",
        "Domínio",
        "URL",
        "URL final",
        "Origem da descoberta",
        "Consulta utilizada",
        "Identidade atribuída",
        "Categoria de relacionamento",
        "Confiança da atribuição",
        "Classificação",
        "Prioridade",
        "Identidade visual antiga (logo descontinuada)",
        "Dados sensíveis pedidos (senha, cartão, token...)",
        "Cenário",
        "Confiança da extração",
        "Suficiência da evidência",
        "Encaminhamento sugerido",
        "Mapeamento normativo (hipóteses — não conclusivo)",
        "Justificativa",
        "Principais evidências",
        "Limitações",
        "Revisão humana",
        "Decisão do revisor",
        "Screenshot (viewport)",
        "Screenshot (completo)",
        "Dossiê",
        "Data da coleta",
        "Status pipeline",
        "Score pré-coleta (ordem da fila de descoberta)",
    ]
    linhas = []
    for r in registros:
        c, f, ev, idn = r.candidata, r.final, r.evidencia, r.identidade
        vp = ev.artefato("captura_viewport") if ev else None
        fp = ev.artefato("captura_completa") if ev else None
        linhas.append(
            [
                c.id,
                c.dominio_registravel,
                c.url,
                ev.url_final if ev else "",
                ", ".join(sorted({h.fonte for h in r.resultados})),
                "\n".join(sorted({h.consulta for h in r.resultados if h.consulta}))[:2000],
                (idn.nome_exibicao or idn.chave_entidade) if idn else "",
                idn.classificacao.value if idn else "",
                idn.confianca_atribuicao.value if idn else "",
                f.classificacao.value if f else "",
                f.prioridade.value if f else "",
                (f"SIM ({f.fonte_identidade_visual_antiga})" if f.identidade_visual_antiga else "não") if f else "",
                ", ".join(f.dados_sensiveis_solicitados) if f else "",
                f.caso.value if f else "",
                f.confianca_extracao.value if f else "",
                f.suficiencia_evidencia.value if f else "",
                "\n".join(f.encaminhamento) if f else "",
                "\n".join(f.resumo_conformidade) if f else "",
                f.justificativa if f else (c.ultimo_erro or ""),
                _evidencia_principal(r),
                "\n".join(f.limitacoes) if f else "",
                f.status_revisao_humana if f else "",
                r.revisao.decisao.value if r.revisao else "",
                _link(caminho, dir_campanha / vp.caminho if vp else None),
                _link(caminho, dir_campanha / fp.caminho if fp else None),
                _link(caminho, caminhos_dossies.get(c.id)),
                ev.coletada_em.isoformat(timespec="seconds") if ev else "",
                c.status.value,
                round(c.pontuacao_prioridade, 1),
            ]
        )
    larguras = {1: 13, 2: 24, 3: 40, 4: 40, 6: 40, 16: 34, 17: 40, 18: 60, 19: 60, 20: 50}
    _aba(wb, "Candidates", cabecalhos_cand, linhas, larguras, coluna_preenchimento=9)
    forte = [
        linha
        for linha, r in zip(linhas, registros, strict=True)
        if r.final and r.final.classificacao.value == "INDICIO_FORTE"
    ]
    _aba(wb, "Strong Indications", cabecalhos_cand, forte, larguras, coluna_preenchimento=9)
    if any(r.final and r.final.identidade_visual_antiga for r in registros):
        _aba_logo_antiga(wb, caminho, registros, caminhos_dossies, recortes_logo or {})
        wb.move_sheet(ABA_LOGO_ANTIGA, offset=-(len(wb.sheetnames) - 2))
    foco = (camp.get("parametros") or {}).get("foco")
    perfil_marca = carregar_marca(camp.get("marca") or "bv")
    # In a threat campaign the threat view comes first; in the COBAN sweep it follows the compliance sheets.
    ameacas_primeiro = foco == FOCO_AMEACAS
    if (
        _aba_ameacas(
            wb, caminho, linhas_ameacas(registros, perfil_marca, foco, inteligencia_dominio), caminhos_dossies
        )
        and ameacas_primeiro
    ):
        wb.move_sheet(ABA_AMEACAS, offset=-(len(wb.sheetnames) - 2))
    if inteligencia_dominio:
        _aba_dominios(wb, registros, inteligencia_dominio)
        if ameacas_primeiro:
            wb.move_sheet(ABA_DOMINIOS, offset=-(len(wb.sheetnames) - 3))

    linhas_id = []
    for r in registros:
        idn = r.identidade
        if not idn:
            continue
        fatos = "\n".join(
            f"{f.tipo}: {f.valor} [{f.ref_evidencia}]" + ("" if f.valido in (None, True) else " (inválido)")
            for f in idn.fatos
        )
        linhas_id.append(
            [
                r.candidata.id,
                r.candidata.dominio_registravel,
                idn.chave_entidade,
                idn.nome_exibicao,
                idn.classificacao.value,
                idn.confianca_atribuicao.value,
                ", ".join(idn.cnpjs_responsavel),
                ", ".join(idn.cnpjs_instituicao_referenciados),
                fatos,
                "\n".join(idn.inferencias),
                json.dumps(idn.correspondencia_cadastro.model_dump(), ensure_ascii=False)
                if idn.correspondencia_cadastro
                else "",
                idn.fundamentacao,
                "\n".join(idn.limitacoes),
            ]
        )
    _aba(
        wb,
        "Identity Attribution",
        [
            "ID",
            "Domínio",
            "Chave da entidade",
            "Nome",
            "Classificação",
            "Confiança",
            "CNPJs do responsável",
            "CNPJs da instituição citados",
            "Fatos observados (ref)",
            "Inferências",
            "Correspondência em cadastro",
            "Racional",
            "Limitações",
        ],
        linhas_id,
        {9: 60, 10: 50, 12: 50},
    )

    linhas_ent = [
        [e["chave_entidade"], e["nome_exibicao"], e["classificacao"], e["dominios"], e["ids_candidatas"]]
        for e in armazenamento.entidades(id_campanha)
    ]
    if linhas_ent:
        _aba(wb, "Entities", ["Chave", "Nome", "Classificação", "Domínios", "Candidatos"], linhas_ent)

    linhas_ev = []
    for r in registros:
        for ev in r.todas_evidencias:
            for a in ev.artefatos:
                linhas_ev.append(
                    [
                        r.candidata.id,
                        ev.tentativa,
                        ev.status.value,
                        a.tipo,
                        _link(caminho, dir_campanha / a.caminho),
                        a.sha256,
                        a.tamanho_bytes,
                        ev.coletada_em.isoformat(timespec="seconds"),
                        _link(caminho, dir_campanha / ev.caminho_manifesto if ev.caminho_manifesto else None),
                        ev.sha256_manifesto,
                    ]
                )
            if not ev.artefatos:
                linhas_ev.append(
                    [
                        r.candidata.id,
                        ev.tentativa,
                        ev.status.value,
                        "(nenhum artefato)",
                        "",
                        "",
                        0,
                        ev.coletada_em.isoformat(timespec="seconds"),
                        _link(caminho, dir_campanha / ev.caminho_manifesto if ev.caminho_manifesto else None),
                        ev.sha256_manifesto,
                    ]
                )
    _aba(
        wb,
        "Evidence Index",
        [
            "ID",
            "Tentativa",
            "Status da coleta",
            "Tipo",
            "Caminho",
            "SHA-256",
            "Bytes",
            "Coletado em",
            "Manifesto",
            "SHA-256 do manifesto",
        ],
        linhas_ev,
        {5: 60, 6: 66, 9: 50, 10: 66},
    )

    linhas_log = [
        [d["em"], d["id_candidata"] or "", d["agente"], d["decisao"], d["detalhe_json"]]
        for d in armazenamento.decisoes(id_campanha)
    ]
    for q in armazenamento.listar_consultas(id_campanha):
        meta = json.loads(q["metadados_json"] or "{}")
        meta.pop("html_ponto_entrada_busca", None)
        linhas_log.append(
            [
                q["executada_em"],
                "",
                f"query:{q['fonte']}",
                q["status"],
                json.dumps(
                    {
                        "consulta": q["texto_consulta"],
                        "hipotese": q["id_hipotese"],
                        "retornos": q["qtd_resultados"],
                        "erro": q["erro"],
                        **meta,
                    },
                    ensure_ascii=False,
                ),
            ]
        )
    linhas_log.sort(key=lambda x: str(x[0]))
    _aba(wb, "Investigation Log", ["Quando", "Candidato", "Agente", "Decisão", "Detalhe"], linhas_log, {1: 22, 5: 120})

    linhas_erro = [
        [e["em"], e["id_candidata"] or "", e["etapa"], e["tipo_erro"], e["mensagem"]]
        for e in armazenamento.erros(id_campanha)
    ]
    for r in registros:
        if r.evidencia and (r.evidencia.erros or r.evidencia.nao_inspecionado):
            linhas_erro.append(
                [
                    r.evidencia.coletada_em.isoformat(timespec="seconds"),
                    r.candidata.id,
                    "coleta",
                    r.evidencia.status.value,
                    "\n".join([*r.evidencia.erros, *r.evidencia.nao_inspecionado]),
                ]
            )
        if r.candidata.status.value in ("DESCOBERTA",):
            linhas_erro.append(
                [
                    "",
                    r.candidata.id,
                    "queue",
                    "NOT_PROCESSED",
                    "candidato não processado (fora do limite --limite ou campanha pausada)",
                ]
            )
    for diag in disc.get("diagnosticos", []):
        linhas_erro.append(["", "", "descoberta", "DIAGNOSTIC", diag])
    _aba(wb, "Errors and Limitations", ["Quando", "Candidato", "Etapa", "Tipo", "Mensagem"], linhas_erro, {5: 120})
    _aba(wb, "Glossário", ["Termo", "Significado"], GLOSSARIO, {1: 36, 2: 120})

    caminho.parent.mkdir(parents=True, exist_ok=True)
    wb.save(caminho)
    return caminho
