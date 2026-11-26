from __future__ import annotations

import base64
import io
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
from simio.relatorios.dados_logo import LOGO_BV_AZUL_BASE64
from simio.relatorios.paginas import data_br
from simio.relatorios.rotulos import (
    COR_MARCA,
    PALETA,
    dados_pedidos,
    descrever_cadastro,
    descrever_chave_entidade,
    num_br,
    plural,
    resumir_consultas,
    rotulo,
    rotulo_fonte,
    tom,
)
from simio.relatorios.textos_antigos import traduzir

FONTE = "Calibri"
PREENCHIMENTO_CABECALHO = PatternFill("solid", fgColor=COR_MARCA)
FONTE_CABECALHO = Font(name=FONTE, size=11, color="FFFFFF", bold=True)
FONTE_DADOS = Font(name=FONTE, size=10, color="101828")
FONTE_LINK = Font(name=FONTE, size=10, color=COR_MARCA, underline="single")
# Mesma paleta dos relatórios HTML: a célula da classificação/nível recebe o fundo e a cor do tom.
PREENCHIMENTOS = {t: PatternFill("solid", fgColor=fundo) for t, (_cor, fundo) in PALETA.items()}
FONTES_TOM = {t: Font(name=FONTE, size=10, color=cor, bold=True) for t, (cor, _fundo) in PALETA.items()}

BORDA_FINA = Border(
    left=Side(style="thin", color="E4E7EC"),
    right=Side(style="thin", color="E4E7EC"),
    top=Side(style="thin", color="E4E7EC"),
    bottom=Side(style="thin", color="E4E7EC"),
)

CLASSIFICACOES = ("INDICIO_FORTE", "REQUER_REVISAO", "INCONCLUSIVA", "SEM_INDICIO")
_ILEGAIS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
MAX_CELULA = 32000

ABA_RESUMO = "Resumo"
ABA_CANDIDATAS = "Candidatas"
ABA_INDICIOS_FORTES = "Indícios fortes"
ABA_LOGO_ANTIGA = "Logo antiga"
ABA_AMEACAS = "Ameaças"
ABA_DOMINIOS = "Domínios"
ABA_IDENTIDADE = "Responsáveis"
ABA_ENTIDADES = "Entidades"
ABA_EVIDENCIAS = "Evidências"
ABA_REGISTRO = "Registro da investigação"
ABA_ERROS = "Erros e limitações"
ABA_GLOSSARIO = "Glossário"


@dataclass(frozen=True)
class _Link:
    """Célula com caminho relativo que abre o arquivo (o Excel resolve a partir da pasta da planilha)."""

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
        # Conteúdo da página investigada não pode virar fórmula na planilha.
        if v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            v = "'" + v
    return v


def _contagem(contagens: dict[str, int]) -> str:
    return ", ".join(f"{rotulo(k)}: {n}" for k, n in sorted(contagens.items(), key=lambda kv: -kv[1]))


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
    ws.row_dimensions[1].height = 30
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
            t = tom(r[coluna_preenchimento].value)
            if t:
                r[coluna_preenchimento].fill = PREENCHIMENTOS[t]
                r[coluna_preenchimento].font = FONTES_TOM[t]
    for i in range(1, len(cabecalhos) + 1):
        ws.column_dimensions[get_column_letter(i)].width = (larguras or {}).get(i, 22)
    ws.freeze_panes = "A2"
    if linhas:
        ws.auto_filter.ref = ws.dimensions


_PARAMETROS = {
    "marca": "Marca",
    "foco": "Foco",
    "limite": "Limite de páginas analisadas",
    "fontes": "Fontes pedidas",
    "offline": "Modo offline",
    "revisar_todas": "Revisão crítica de todas as páginas",
    "modelo_analise": "Modelo de análise",
    "modelo_revisao": "Modelo de revisão",
    "modelo_descoberta": "Modelo de descoberta",
    "paralelismo": "Páginas em paralelo",
    "max_requisicoes_llm": "Máximo de chamadas ao modelo",
    "max_tokens_llm": "Máximo de tokens",
    "max_consultas_busca": "Máximo de consultas de busca",
    "urls_importadas": "URLs importadas",
    "id_anterior": "Identificador anterior",
}


def _valor_parametro(v: Any) -> str:
    if v is None or v == "" or v == []:
        return "—"
    if isinstance(v, bool):
        return "sim" if v else "não"
    if isinstance(v, (int, float)):
        return num_br(v)
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _parametros(parametros: dict[str, Any] | None) -> str:
    return "\n".join(f"{_PARAMETROS.get(k, k)}: {_valor_parametro(v)}" for k, v in (parametros or {}).items())


def _decisao_registro(decisao: str) -> str:
    # "final:SEM_INDICIO" e códigos soltos (REBAIXAR, IDENTIDADE_DESCONHECIDA) viram rótulo; o resto fica.
    if decisao.startswith("final:"):
        return f"final: {rotulo(decisao[len('final:') :])}"
    return rotulo(decisao)


_CAPTURAS = {"E-SHOT-VIEWPORT": "área visível", "E-SHOT-FULL": "página inteira"}


def _evidencia_principal(rec: RegistroCandidata, n: int = 6) -> str:
    if not rec.final:
        return ""
    por_ref = {i.ref: i for i in rec.catalogo}
    return "\n".join(f"[{r}] {por_ref[r].conteudo[:220]}" for r in rec.final.evidencias_chave[:n] if r in por_ref)


_SIGNIFICADOS: list[tuple[str, str]] = [
    ("SEM_INDICIO", "Sem indício de confusão com a instituição no material coletado (não atesta conformidade)"),
    ("REQUER_REVISAO", "Precisa de análise humana: há ambiguidade ou apresentação como canal da instituição"),
    ("INDICIO_FORTE", "Forte indício de que o consumidor pode achar que trata diretamente com a instituição"),
    ("INCONCLUSIVA", "Evidência insuficiente (coleta falhou, conteúdo bloqueado ou contraditório)"),
    ("A_MENCAO_COMERCIAL_LEGITIMA", "Menção comercial com a condição de correspondente clara"),
    ("B_COMUNICACAO_AMBIGUA", "Uso destacado da marca sem transparência suficiente"),
    ("C_POSSIVEL_CANAL_OFICIAL", "Possível apresentação como canal oficial da instituição"),
    ("D_PERSONIFICACAO_FORTE", "Forte indício de que a página se passa pela instituição"),
    ("E_EVIDENCIA_INSUFICIENTE", "Evidência insuficiente para avaliar a comunicação"),
    ("NAO_RELEVANTE", "A página não usa a marca de forma relevante"),
    ("COBAN_CONFIRMADO", "CNPJ do responsável consta de cadastro oficial de correspondentes"),
    ("COBAN_POSSIVEL", "Indícios de vínculo (declaração, nome) sem confirmação em cadastro oficial"),
    ("OUTRO_TERCEIRO", "Empresa identificada sem indício de relação de correspondente"),
    ("IDENTIDADE_DESCONHECIDA", "Responsável pela página não identificado"),
    ("HIPOTESE_NAO_CONFORMIDADE", "Hipótese de não conformidade (correspondente confirmado); exige revisão"),
    ("HIPOTESE_CONDICIONADA_VINCULO", "Hipótese que só vale se o vínculo de correspondente for confirmado"),
    ("NAO_APLICAVEL_SALVO_COBAN", "Responsável não identificado como correspondente; possível tema de marca ou fraude"),
    ("SEM_INDICIO_OBSERVADO", "Nenhum indício observado para o requisito no material coletado"),
    ("NAO_AVALIAVEL", "Não dá para avaliar com a evidência disponível"),
]
GLOSSARIO: list[list[str]] = [
    *([rotulo(codigo), texto] for codigo, texto in _SIGNIFICADOS),
    ["Pendente / Não exigida", "Situação da revisão humana (registrar a decisão da auditoria nos papéis de trabalho)"],
    ["E-…", f"Referências ao catálogo de evidências (aba {ABA_EVIDENCIAS} e dossiês)"],
    [
        "Confiança",
        "Extração = leitura das evidências; atribuição = vínculo entre a página e o responsável. Nenhuma das duas "
        "é probabilidade de irregularidade.",
    ],
]


_ALTURA_LINHA_RECORTE = 64


def _aba_logo_antiga(
    wb: Workbook,
    planilha: Path,
    registros: list[RegistroCandidata],
    caminhos_dossies: dict[str, Path],
    recortes: dict[str, Path],
) -> None:
    """Uma linha por página onde a logo descontinuada apareceu (pelo modelo e/ou pela comparação de imagem)."""
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
                rotulo(f.classificacao),
                rotulo(f.prioridade),
                f.fonte_identidade_visual_antiga,
                "sim" if b and b.logo_antiga_isenta else "não",
                melhor.pontuacao if melhor else "",
                (
                    f"{_CAPTURAS.get(melhor.ref_captura, melhor.ref_captura)}, posição {melhor.bbox[0]}, "
                    f"{melhor.bbox[1]}, {melhor.bbox[2]}×{melhor.bbox[3]} px"
                )
                if melhor
                else "",
                ("sim" if melhor.acima_dobra else "não") if melhor else "",
                "\n".join(dict.fromkeys(m.src_imagem for m in correspondencias if m.src_imagem)),
                rotulo(idn.classificacao) if idn else "",
                descrever_cadastro(idn.correspondencia_cadastro) if reg and idn else "",
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
        "Como foi detectada",
        "Site que só cataloga a logo (não muda a classificação)",
        "Similaridade (comparação de imagem)",
        "Onde (captura e posição)",
        "Acima da dobra",
        "Imagem na página",
        "Responsável",
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
        except Exception:  # noqa: S112 - recorte ausente ou corrompido não pode derrubar o relatório
            continue


def _host_pagina(r: RegistroCandidata) -> str:
    return hostname_de((r.evidencia.url_final if r.evidencia else None) or r.candidata.url) or r.candidata.hostname


def _aba_dominios(wb: Workbook, registros: list[RegistroCandidata], intel: dict[str, InteligenciaDominio]) -> None:
    """Registro (RDAP/WHOIS), DNS e hospedagem de cada host sinalizado, com os contatos para takedown."""
    pior: dict[str, tuple[str, str]] = {}
    posicao = {"INDICIO_FORTE": 0, "REQUER_REVISAO": 1}
    for r in registros:
        if r.final is None:
            continue
        host = _host_pagina(r)
        atual = (r.final.classificacao.value, r.final.prioridade.value)
        if host in intel and (host not in pior or posicao.get(atual[0], 9) < posicao.get(pior[host][0], 9)):
            pior[host] = atual
    linhas = []
    for host, item in sorted(intel.items(), key=lambda kv: (posicao.get(pior.get(kv[0], ("",))[0], 9), kv[0])):
        s = resumir_inteligencia(item)
        cls, prio = pior.get(host, ("", ""))
        linhas.append(
            [host, item.dominio_registrado, rotulo(cls), rotulo(prio), s["plataforma"], s["criado"], s["registrador"],
             s["titular"], s["resolve"], ", ".join(item.a[:4]), s["hospedagem"], s["ns"], s["mx"], s["abuso"],
             s["notas"]]
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
    """Páginas da visão de ameaças, da mais perigosa para a menos. Numa campanha de ameaças entram todos os
    níveis; nas demais, só CRÍTICO/ALTO e páginas que pedem credenciais."""
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
    """Nível, categoria e os fatos por trás de cada ameaça, com os indicadores (IOCs) e os dados para takedown."""
    linhas: list[list[Any]] = []
    for t, r, item in linhas_entrada:
        f, ev = r.final, r.evidencia
        assert f is not None
        url_final = (ev.url_final if ev else None) or r.candidata.url
        acao = next((i.conteudo.split(": ", 1)[-1] for i in r.catalogo if i.ref == "E-FORM-ACTION"), "")
        di = resumir_inteligencia(item) if item else {}
        registro_dominio = " · ".join(x for x in (di.get("criado"), di.get("registrador"), di.get("titular")) if x)
        linhas.append(
            [
                rotulo(t.nivel),
                t.pontuacao,
                "; ".join(t.categorias),
                "\n".join(t.fatos),
                url_final,
                r.candidata.dominio_registravel,
                di.get("plataforma", ""),
                registro_dominio,
                di.get("resolve", ""),
                di.get("hospedagem", "") or ((ev.ip_servidor or "") if ev else ""),
                di.get("abuso", ""),
                dados_pedidos(f.dados_sensiveis_solicitados),
                acao,
                "\n".join(ev.cadeia_redirecionamentos[:6]) if ev else "",
                rotulo(f.classificacao),
                rotulo(f.caso),
                (f.justificativa or "")[:600],
                ", ".join(sorted({rotulo_fonte(h.fonte) for h in r.resultados})),
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
        "Cenário (analisador)",
        "Justificativa do analisador",
        "Fontes",
        "Hipóteses",
        "Dossiê",
    ]
    larguras = {3: 34, 4: 60, 5: 50, 6: 26, 8: 44, 10: 36, 11: 40, 14: 40, 16: 30, 17: 70, 19: 34, 20: 40}
    _aba(wb, ABA_AMEACAS, cabecalhos, linhas, larguras, coluna_preenchimento=0)
    return True


def _logo_resumo(ws: Any, id_marca: str) -> None:
    if id_marca.lower() != "bv":
        return
    try:
        img = OpenpyxlImage(io.BytesIO(base64.b64decode(LOGO_BV_AZUL_BASE64)))
    except Exception:
        return
    escala = 64 / img.height
    img.width, img.height = int(img.width * escala), 64
    ws.add_image(img, "D2")


def gravar_planilha(
    caminho: Path,
    armazenamento: Armazenamento,
    id_campanha: str,
    registros: list[RegistroCandidata],
    caminhos_dossies: dict[str, Path],
    recortes_logo: dict[str, Path] | None = None,
    inteligencia_dominio: dict[str, InteligenciaDominio] | None = None,
) -> Path:
    """``caminho`` é a planilha na raiz da pasta da campanha: os caminhos dos arquivos são relativos a ela."""
    dir_campanha = caminho.parent
    camp = armazenamento.obter_campanha(id_campanha) or {}
    wb = Workbook()
    if wb.active is not None:
        wb.remove(wb.active)
    finais = [r.final for r in registros if r.final]
    contagem_cls = Counter(f.classificacao.value for f in finais)
    disc = armazenamento.obter_checkpoint(id_campanha, "descoberta", {}) or {}
    req, tok = armazenamento.totais_uso(id_campanha)
    analisadores = Counter(r.marca.analisador for r in registros if r.marca)
    # TODO: receber o perfil de gerar_relatorios; aqui ignora o arquivo de marca das configurações.
    perfil_marca = carregar_marca(camp.get("marca") or "bv")

    resumo: list[list[Any]] = [
        ["Campanha", id_campanha],
        ["Marca", perfil_marca.nome_exibicao],
        ["Situação da campanha", rotulo(camp.get("status"))],
        ["Início", data_br(camp.get("criado_em"))],
        ["Fim", data_br(camp.get("finalizado_em"))],
        ["Relatório gerado em", data_br(datetime.now(UTC))],
        ["Candidatas descobertas (únicas)", len(registros)],
        ["Andamento das candidatas", _contagem(armazenamento.contagem_por_status(id_campanha))],
        *([rotulo(c), contagem_cls.get(c, 0)] for c in CLASSIFICACOES),
        ["Aguardam revisão humana", sum(1 for f in finais if f.exige_revisao_humana)],
        ["Fontes de descoberta usadas", ", ".join(dict.fromkeys(map(rotulo_fonte, disc.get("fontes_usadas", []))))],
        ["Consultas executadas", resumir_consultas(disc.get("consultas"))],
        ["Diagnóstico da descoberta", "\n".join(traduzir(d) for d in disc.get("diagnosticos", []))],
        ["Analisadores usados", ", ".join(f"{k}: {n}" for k, n in analisadores.most_common())],
        ["Chamadas ao modelo / tokens", f"{plural(req, 'chamada', 'chamadas')} / {num_br(tok)} tokens"],
        ["Parâmetros", _parametros(camp.get("parametros"))],
        [
            "Observação",
            "As classificações são indícios para apoio à auditoria, não conclusão regulatória. "
            "Pontuações não representam probabilidade de irregularidade.",
        ],
    ]
    _aba(wb, ABA_RESUMO, ["Item", "Valor"], resumo, {1: 36, 2: 110}, coluna_preenchimento=None)
    ws_resumo = wb[ABA_RESUMO]
    for linha in ws_resumo.iter_rows(min_row=2):
        t = tom(linha[0].value)
        if t:
            linha[0].fill, linha[0].font = PREENCHIMENTOS[t], FONTES_TOM[t]
    _logo_resumo(ws_resumo, camp.get("marca") or "bv")

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
        "Logo antiga (descontinuada)",
        "Dados sensíveis pedidos (senha, cartão, token...)",
        "Cenário",
        "Confiança da extração",
        "Suficiência da evidência",
        "Encaminhamento sugerido",
        "Mapeamento normativo (hipóteses, não conclusivo)",
        "Justificativa",
        "Principais evidências",
        "Limitações",
        "Revisão humana",
        "Decisão do revisor",
        "Captura (área visível)",
        "Captura (página inteira)",
        "Dossiê",
        "Data da coleta",
        "Andamento",
        "Pontuação antes da coleta (ordem da fila)",
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
                ", ".join(sorted({rotulo_fonte(h.fonte) for h in r.resultados})),
                "\n".join(sorted({h.consulta for h in r.resultados if h.consulta}))[:2000],
                (idn.nome_exibicao or "") if idn else "",
                rotulo(idn.classificacao) if idn else "",
                rotulo(idn.confianca_atribuicao) if idn else "",
                rotulo(f.classificacao) if f else "",
                rotulo(f.prioridade) if f else "",
                (f"sim ({f.fonte_identidade_visual_antiga})" if f.identidade_visual_antiga else "não") if f else "",
                dados_pedidos(f.dados_sensiveis_solicitados) if f else "",
                rotulo(f.caso) if f else "",
                rotulo(f.confianca_extracao) if f else "",
                rotulo(f.suficiencia_evidencia) if f else "",
                "\n".join(f.encaminhamento) if f else "",
                "\n".join(f.resumo_conformidade) if f else "",
                f.justificativa if f else (c.ultimo_erro or ""),
                _evidencia_principal(r),
                "\n".join(f.limitacoes) if f else "",
                rotulo(f.status_revisao_humana) if f else "",
                rotulo(r.revisao.decisao) if r.revisao else "",
                _link(caminho, dir_campanha / vp.caminho if vp else None),
                _link(caminho, dir_campanha / fp.caminho if fp else None),
                _link(caminho, caminhos_dossies.get(c.id)),
                data_br(ev.coletada_em, segundos=True) if ev else "",
                rotulo(c.status),
                round(c.pontuacao_prioridade, 1),
            ]
        )
    larguras = {1: 13, 2: 24, 3: 40, 4: 40, 6: 40, 14: 28, 16: 34, 17: 40, 18: 60, 19: 60, 20: 50}
    _aba(wb, ABA_CANDIDATAS, cabecalhos_cand, linhas, larguras, coluna_preenchimento=9)
    forte = [
        linha
        for linha, r in zip(linhas, registros, strict=True)
        if r.final and r.final.classificacao.value == "INDICIO_FORTE"
    ]
    _aba(wb, ABA_INDICIOS_FORTES, cabecalhos_cand, forte, larguras, coluna_preenchimento=9)
    if any(r.final and r.final.identidade_visual_antiga for r in registros):
        _aba_logo_antiga(wb, caminho, registros, caminhos_dossies, recortes_logo or {})
        wb.move_sheet(ABA_LOGO_ANTIGA, offset=-(len(wb.sheetnames) - 2))
    foco = (camp.get("parametros") or {}).get("foco")
    # Na campanha de ameaças a aba de ameaças vem logo depois do resumo; na varredura ampla, depois das de conformidade.
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
            f"{rotulo(x.tipo)}: {x.valor} [{x.ref_evidencia}]" + ("" if x.valido in (None, True) else " (inválido)")
            for x in idn.fatos
        )
        linhas_id.append(
            [
                r.candidata.id,
                r.candidata.dominio_registravel,
                descrever_chave_entidade(idn.chave_entidade),
                idn.nome_exibicao,
                rotulo(idn.classificacao),
                rotulo(idn.confianca_atribuicao),
                ", ".join(idn.cnpjs_responsavel),
                ", ".join(idn.cnpjs_instituicao_referenciados),
                fatos,
                "\n".join(idn.inferencias),
                descrever_cadastro(idn.correspondencia_cadastro),
                idn.fundamentacao,
                "\n".join(idn.limitacoes),
            ]
        )
    _aba(
        wb,
        ABA_IDENTIDADE,
        [
            "ID",
            "Domínio",
            "Agrupada por",
            "Nome",
            "Categoria",
            "Confiança",
            "CNPJs do responsável",
            "CNPJs da instituição citados",
            "Fatos observados (ref.)",
            "Inferências",
            "Cadastro de correspondentes",
            "Fundamentação",
            "Limitações",
        ],
        linhas_id,
        {9: 60, 10: 50, 12: 50},
    )

    linhas_ent = [
        [
            descrever_chave_entidade(e["chave_entidade"]),
            e["nome_exibicao"],
            rotulo(e["classificacao"]),
            e["dominios"],
            e["ids_candidatas"],
        ]
        for e in armazenamento.entidades(id_campanha)
    ]
    if linhas_ent:
        _aba(wb, ABA_ENTIDADES, ["Agrupada por", "Nome", "Categoria", "Domínios", "Candidatas"], linhas_ent)

    linhas_ev = []
    for r in registros:
        for ev in r.todas_evidencias:
            for a in ev.artefatos:
                linhas_ev.append(
                    [
                        r.candidata.id,
                        ev.tentativa,
                        rotulo(ev.status),
                        rotulo(a.tipo),
                        _link(caminho, dir_campanha / a.caminho),
                        a.sha256,
                        a.tamanho_bytes,
                        data_br(ev.coletada_em, segundos=True),
                        _link(caminho, dir_campanha / ev.caminho_manifesto if ev.caminho_manifesto else None),
                        ev.sha256_manifesto,
                    ]
                )
            if not ev.artefatos:
                linhas_ev.append(
                    [
                        r.candidata.id,
                        ev.tentativa,
                        rotulo(ev.status),
                        "(nenhum arquivo)",
                        "",
                        "",
                        0,
                        data_br(ev.coletada_em, segundos=True),
                        _link(caminho, dir_campanha / ev.caminho_manifesto if ev.caminho_manifesto else None),
                        ev.sha256_manifesto,
                    ]
                )
    _aba(
        wb,
        ABA_EVIDENCIAS,
        [
            "ID",
            "Tentativa",
            "Situação da coleta",
            "Arquivo",
            "Caminho",
            "SHA-256",
            "Bytes",
            "Coletado em",
            "Manifesto",
            "SHA-256 do manifesto",
        ],
        linhas_ev,
        {4: 28, 5: 60, 6: 66, 9: 50, 10: 66},
    )

    # No registro bruto os valores do JSON são códigos: os antigos em inglês viram o código atual, não o rótulo.
    linhas_log = [
        [
            d["em"],
            d["id_candidata"] or "",
            d["agente"],
            _decisao_registro(d["decisao"]),
            traduzir(d["detalhe_json"], rotulos=False),
        ]
        for d in armazenamento.decisoes(id_campanha)
    ]
    for q in armazenamento.listar_consultas(id_campanha):
        meta = json.loads(q["metadados_json"] or "{}")
        meta.pop("html_ponto_entrada_busca", None)
        linhas_log.append(
            [
                q["executada_em"],
                "",
                f"consulta:{q['fonte']}",
                rotulo(q["status"]),
                json.dumps(
                    {
                        "consulta": q["texto_consulta"],
                        "hipotese": q["id_hipotese"],
                        "retornos": q["qtd_resultados"],
                        "erro": traduzir(q["erro"], rotulos=False) or q["erro"],
                        **meta,
                    },
                    ensure_ascii=False,
                ),
            ]
        )
    linhas_log.sort(key=lambda x: str(x[0]))
    for linha in linhas_log:
        linha[0] = data_br(linha[0], segundos=True)
    _aba(wb, ABA_REGISTRO, ["Quando", "Candidata", "Agente", "Decisão", "Detalhe"], linhas_log, {1: 22, 5: 120})

    linhas_erro = [
        [data_br(e["em"], segundos=True), e["id_candidata"] or "", e["etapa"], e["tipo_erro"], traduzir(e["mensagem"])]
        for e in armazenamento.erros(id_campanha)
    ]
    for r in registros:
        ev = r.evidencia
        if ev and ev.erros:
            # Coleta que falhou registra erro; nas que deram certo, o que sobrou é ocorrência.
            tipo = "ocorrência" if ev.status.value in ("OK", "PARCIAL") else "erro"
            linhas_erro.append(
                [data_br(ev.coletada_em, segundos=True), r.candidata.id, "coleta", tipo, "\n".join(ev.erros)]
            )
        if ev and ev.nao_inspecionado:
            linhas_erro.append(
                [
                    data_br(ev.coletada_em, segundos=True),
                    r.candidata.id,
                    "coleta",
                    "limitação",
                    "\n".join(ev.nao_inspecionado),
                ]
            )
        if r.candidata.status.value == "DESCOBERTA":
            linhas_erro.append(
                [
                    "",
                    r.candidata.id,
                    "fila",
                    "não analisada",
                    "candidata não analisada (limite de páginas atingido ou campanha interrompida)",
                ]
            )
    for diag in disc.get("diagnosticos", []):
        linhas_erro.append(["", "", "descoberta", "diagnóstico", traduzir(diag)])
    _aba(wb, ABA_ERROS, ["Quando", "Candidata", "Etapa", "Tipo", "Mensagem"], linhas_erro, {1: 22, 5: 120})
    _aba(wb, ABA_GLOSSARIO, ["Termo", "Significado"], GLOSSARIO, {1: 36, 2: 120})

    caminho.parent.mkdir(parents=True, exist_ok=True)
    wb.save(caminho)
    return caminho
