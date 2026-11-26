from __future__ import annotations

import base64
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import simio
from simio.agentes.descoberta import montar_hipoteses, montar_hipoteses_ameacas, montar_hipoteses_logo_antiga
from simio.agentes.identidade import CadastroCoban
from simio.agentes.inteligencia_dominio import carregar_inteligencia
from simio.agentes.triagem_ameacas import triar
from simio.configuracoes import Configuracoes
from simio.marcas import PerfilMarca, carregar_marca
from simio.nucleo import caminhos
from simio.nucleo.armazenamento import Armazenamento
from simio.nucleo.modelos import InteligenciaDominio
from simio.relatorios.dados import RegistroCandidata, carregar_registros
from simio.relatorios.excel import _host_pagina
from simio.relatorios.paginas import ambiente
from simio.relatorios.pdf import gerar_pdf
from simio.relatorios.rotulos import PALETA, num_br, rotulo

logger = logging.getLogger("simio.relatorios.executivo")

_SUFIXO_EMPRESA = re.compile(r",?\s+(Inc|LLC|Ltd|Ltda|S\.?A)\.?$", re.IGNORECASE)

# Informado pelo responsável pelo projeto; não dá para tirar do histórico do git.
TEMPO_DESENVOLVIMENTO = "2 dias"
# Média observada com gemini-3.8-flash (ver docs/implantacao.md). É estimativa, não fatura.
CUSTO_POR_PAGINA_USD = 0.03

GRAVIDADE = {"INDICIO_FORTE": 4, "REQUER_REVISAO": 3, "INCONCLUSIVA": 2, "SEM_INDICIO": 1}
NIVEIS = {"CRÍTICO": 5, "ALTO": 4, "MÉDIO": 3, "BAIXO": 2, "SEM AMEAÇA": 1}
ACHADO = ("INDICIO_FORTE", "REQUER_REVISAO")
REDES = {
    "instagram": "Instagram",
    "facebook": "Facebook",
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "linkedin": "LinkedIn",
    "kwai": "Kwai",
    "threads": "Threads",
    "t.me": "Telegram",
    "telegram": "Telegram",
    "wa.me": "WhatsApp",
    "whatsapp": "WhatsApp",
    "olx": "OLX",
    "mercadolivre": "Mercado Livre",
    "webmotors": "Webmotors",
}
SITE_PROPRIO = "Site próprio"
FONTES = {
    "grounding": "Grounding (Gemini)",
    "serper": "Serper (Google)",
    "typosquat": "Typosquatting",
    "crtsh": "crt.sh",
    "lens": "Google Lens",
    "brave": "Brave",
    "urlscan": "urlscan.io",
}
HIPOTESES = {
    "H1-identidade": "H1 · Marca no título ou na URL",
    "H2-produto": "H2 · Produtos (veículos, solar, consignado)",
    "H3-servico": "H3 · 2ª via, quitação, WhatsApp",
    "H4-linguagem-oficial": "H4 · Linguagem de canal oficial",
    "H5-estabelecimento": "H5 · Feirões e varredura por UF",
    "H6-plataforma": "H6 · Redes sociais e classificados",
    "H7-inteligencia-ameacas": "H7 · Inteligência de ameaças",
    "H8-semelhante": "H8 · Domínios parecidos",
    "H9-coban": "H9 · Nome de COBANs do cadastro",
    "H10-fraude": "H10 · Fraudes com a marca como isca",
    "H11-napista": "H11 · NaPista",
    "H12-logo-antiga": "H12 · Busca reversa da logo antiga",
    "H13-foco-logo-antiga": "H13 · Campanha de logo antiga",
    "H14-ameaca": "H14 · Campanha de ameaças",
}
NOMES_RITMO = {"ampla": "Varredura ampla", "logo-antiga": "Campanha de logo antiga", "ameacas": "Campanha de ameaças"}
IDENTIDADES = {
    "COBAN_CONFIRMADO": "COBAN confirmado no cadastro",
    "COBAN_POSSIVEL": "COBAN possível",
    "OUTRO_TERCEIRO": "Outro terceiro identificado",
    "IDENTIDADE_DESCONHECIDA": "Responsável não identificado",
}
MODOS = {"ampla": "Ampla", "logo-antiga": "Logo antiga", "ameacas": "Ameaças"}
COR = {"azul": "#1C3FAA", "cinza": "#98A2B3", **{t: f"#{c}" for t, (c, _) in PALETA.items()}}
COR_CLASSIFICACAO = {
    "INDICIO_FORTE": COR["crit"],
    "REQUER_REVISAO": COR["medio"],
    "INCONCLUSIVA": COR["cinza"],
    "SEM_INDICIO": COR["ok"],
}
COR_NIVEL = {"CRÍTICO": COR["crit"], "ALTO": COR["alto"], "MÉDIO": COR["medio"], "BAIXO": COR["cinza"]}


@dataclass
class ResultadoExecutivo:
    html: Path
    pdf: Path | None


@dataclass
class Pagina:
    """Uma URL somada entre campanhas, guardando a classificação e o nível de ameaça mais graves."""

    url: str
    host: str
    dominio: str
    campanhas: set[str] = field(default_factory=set)
    modos: set[str] = field(default_factory=set)
    classificacao: str | None = None
    caso: str | None = None
    prioridade: str | None = None
    identidade: str | None = None
    logo: bool = False
    fonte_logo: set[str] = field(default_factory=set)
    nivel: str | None = None
    categorias: set[str] = field(default_factory=set)
    credenciais: set[str] = field(default_factory=set)
    encaminhamento: set[str] = field(default_factory=set)
    conformidade: set[tuple[str, str]] = field(default_factory=set)
    hipoteses: set[str] = field(default_factory=set)
    intel: InteligenciaDominio | None = None

    @property
    def analisada(self) -> bool:
        return self.classificacao is not None

    @property
    def achado(self) -> bool:
        return self.classificacao in ACHADO

    @property
    def canal(self) -> str:
        for chave, nome in REDES.items():
            if chave in self.host:
                return nome
        return SITE_PROPRIO

    def somar(self, r: RegistroCandidata, nivel: str, categorias: list[str], eh_ameaca: bool) -> None:
        f = r.final
        if f is None:
            return
        cls = f.classificacao.value
        if self.classificacao is None or GRAVIDADE.get(cls, 0) > GRAVIDADE.get(self.classificacao, 0):
            self.classificacao, self.caso = cls, f.caso.value
            self.prioridade, self.identidade = f.prioridade.value, f.classe_identidade.value
        if f.identidade_visual_antiga:
            self.logo = True
            self.fonte_logo.add(f.fonte_identidade_visual_antiga or "")
        self.credenciais.update(f.dados_sensiveis_solicitados)
        self.encaminhamento.update(f.encaminhamento)
        if r.conformidade:
            self.conformidade.update((a.id_requisito, a.status.value) for a in r.conformidade.achados)
        if self.nivel is None or NIVEIS[nivel] > NIVEIS[self.nivel]:
            self.nivel = nivel
        if eh_ameaca:
            self.categorias.update(categorias)


def _horas(campanha: dict[str, Any]) -> float:
    fim = campanha.get("finalizado_em") or campanha.get("atualizado_em")
    try:
        return (datetime.fromisoformat(fim) - datetime.fromisoformat(campanha["criado_em"])).total_seconds() / 3600
    except (TypeError, ValueError):
        return 0.0


def _contar(itens) -> list[tuple[str, int]]:
    return Counter(itens).most_common()


def _fonte_logo(p: Pagina) -> str:
    texto = " ".join(p.fonte_logo)
    if "modelo" in texto and "compara" in texto:
        return "ambas"
    return "imagem" if "compara" in texto else "modelo"


def levantar(configuracoes: Configuracoes, armazenamento: Armazenamento, marca: PerfilMarca) -> dict[str, Any]:
    paginas: dict[str, Pagina] = {}
    campanhas = []
    consultas_fonte: Counter[str] = Counter()
    resultados_fonte: Counter[str] = Counter()
    coletas = 0
    for camp in reversed(armazenamento.listar_campanhas(limite=100_000)):
        cid = camp["id"]
        completa = armazenamento.obter_campanha(cid) or {}
        parametros = completa.get("parametros") or {}
        modo = caminhos.modo_campanha(parametros.get("foco"))
        registros = carregar_registros(armazenamento, cid, marca)
        intel = carregar_inteligencia(configuracoes.dir_saida, cid)
        chamadas, tokens = armazenamento.totais_uso(cid)
        for q in armazenamento.listar_consultas(cid):
            if not q["fonte"].startswith("importacao"):
                consultas_fonte[q["fonte"]] += 1
        niveis: Counter[str] = Counter()
        for r in registros:
            coletas += len(r.todas_evidencias)
            for res in r.resultados:
                if not res.fonte.startswith("importacao"):
                    resultados_fonte[res.fonte] += 1
            if marca.eh_oficial(r.candidata.hostname):
                continue
            p = paginas.setdefault(
                r.candidata.url_normalizada,
                Pagina(
                    url=r.candidata.url_normalizada, host=r.candidata.hostname, dominio=r.candidata.dominio_registravel
                ),
            )
            p.campanhas.add(cid)
            p.modos.add(modo)
            p.hipoteses.update(res.id_hipotese.split(":")[0] for res in r.resultados if res.id_hipotese)
            item = intel.get(_host_pagina(r))
            p.intel = p.intel or item
            if r.final:
                t = triar(r, marca, item)
                niveis[t.nivel] += 1
                p.somar(r, t.nivel, t.categorias, t.eh_ameaca)
        finais = [r.final for r in registros if r.final]
        campanhas.append(
            {
                "id": cid,
                "modo": modo,
                "status": camp["status"],
                "horas": _horas(completa),
                "urls": len(registros),
                "analisadas": len(finais),
                "forte": sum(f.classificacao.value == "INDICIO_FORTE" for f in finais),
                "revisao": sum(f.classificacao.value == "REQUER_REVISAO" for f in finais),
                "logo": sum(f.identidade_visual_antiga for f in finais),
                "ameacas": niveis.get("CRÍTICO", 0) + niveis.get("ALTO", 0),
                "chamadas": chamadas,
                "tokens": tokens,
                "url_set": {r.candidata.url_normalizada for r in registros},
            }
        )

    todas = list(paginas.values())
    analisadas = [p for p in todas if p.analisada]
    achados = [p for p in analisadas if p.achado]
    coban = [p for p in achados if p.identidade in ("COBAN_CONFIRMADO", "COBAN_POSSIVEL")]
    logo = [p for p in analisadas if p.logo]
    ameacas = [p for p in analisadas if p.nivel in ("CRÍTICO", "ALTO", "MÉDIO", "BAIXO")]
    crit_alto = [p for p in ameacas if p.nivel in ("CRÍTICO", "ALTO")]
    hosts_intel = {p.intel.hostname: p.intel for p in achados + ameacas if p.intel}

    cadastro = 0
    if configuracoes.caminho_cadastro_coban and configuracoes.caminho_cadastro_coban.is_file():
        cadastro = len(CadastroCoban.carregar(configuracoes.caminho_cadastro_coban).registros)

    dir_campanhas = caminhos.dir_campanhas(configuracoes.dir_saida)
    evidencias = [a for a in dir_campanhas.glob(f"*/{caminhos.DIR_EVIDENCIAS}/*/*/*") if a.is_file()]
    return {
        "paginas": todas,
        "analisadas": analisadas,
        "achados": achados,
        "coban": coban,
        "logo": logo,
        "ameacas": ameacas,
        "crit_alto": crit_alto,
        "intel": list(hosts_intel.values()),
        "campanhas": campanhas,
        "consultas_fonte": consultas_fonte,
        "resultados_fonte": resultados_fonte,
        "coletas": coletas,
        "cadastro_coban": cadastro,
        "evidencias": len(evidencias),
        "capturas": sum(a.suffix == ".png" for a in evidencias),
        "gb_evidencias": sum(a.stat().st_size for a in evidencias) / 1e9,
        "dossies": len(list(dir_campanhas.glob(f"*/{caminhos.DIR_DOSSIES}/*.html"))),
        "recortes": len(list(dir_campanhas.glob(f"*/{caminhos.DIR_LOGO_ANTIGA}/*.png"))),
    }


def _codigo() -> dict[str, int]:
    pacote = Path(simio.__file__).parent
    fontes = list(pacote.rglob("*.py"))
    saida = {"modulos": len(fontes), "linhas": sum(len(a.read_text(encoding="utf-8").splitlines()) for a in fontes)}
    testes = pacote.parents[1] / "tests"
    if testes.is_dir():
        saida["testes"] = sum(a.read_text(encoding="utf-8").count("def test_") for a in testes.rglob("test_*.py"))
    return saida


def _recortes(paginas: list[Pagina], dir_saida: Path, limite: int = 10) -> list[dict[str, str]]:
    """Recortes da logo antiga já gravados pelos relatórios das campanhas, um por domínio, os mais graves primeiro."""
    por_host = {}
    for arquivo in sorted(caminhos.dir_campanhas(dir_saida).glob(f"*/{caminhos.DIR_LOGO_ANTIGA}/*.png")):
        por_host.setdefault(arquivo.stem.rsplit("_", 1)[0], arquivo)
    vistos, saida = set(), []
    for p in sorted(paginas, key=lambda p: -GRAVIDADE.get(p.classificacao or "", 0)):
        if p.dominio in vistos or p.canal != SITE_PROPRIO or "refer" in " ".join(p.fonte_logo):
            continue
        arquivo = por_host.get(caminhos.host_seguro(p.host))
        if not arquivo:
            continue
        vistos.add(p.dominio)
        saida.append(
            {
                "host": p.host,
                "legenda": f"{rotulo(p.classificacao)} · {IDENTIDADES.get(p.identidade, p.identidade)}",
                "src": "data:image/png;base64," + base64.b64encode(arquivo.read_bytes()).decode("ascii"),
            }
        )
        if len(saida) >= limite:
            break
    return saida


def _exemplos(paginas: list[Pagina], limite: int = 12) -> list[Pagina]:
    vistos, saida = set(), []
    ordem = sorted(paginas, key=lambda p: (-GRAVIDADE.get(p.classificacao or "", 0), -NIVEIS.get(p.nivel or "", 0)))
    for p in ordem:
        if p.canal == SITE_PROPRIO and p.dominio not in vistos:
            vistos.add(p.dominio)
            saida.append(p)
    return saida[:limite]


def _sobreposicao(campanhas: list[dict[str, Any]]) -> dict[str, int] | None:
    amplas = sorted(
        (c for c in campanhas if c["modo"] == caminhos.MODO_AMPLA and c["analisadas"]), key=lambda c: -c["analisadas"]
    )[:2]
    if len(amplas) < 2:
        return None
    a, b = sorted(amplas, key=lambda c: c["id"])
    return {
        "a": len(a["url_set"]),
        "b": len(b["url_set"]),
        "comum": len(a["url_set"] & b["url_set"]),
        "id_a": a["id"],
        "id_b": b["id"],
    }


def _conformidade(paginas: list[Pagina]) -> dict[str, int]:
    return Counter(f"{req.removeprefix('CMN4935-')}|{st}" for p in paginas for req, st in p.conformidade)


def montar_contexto(
    configuracoes: Configuracoes, armazenamento: Armazenamento, marca: PerfilMarca, minutos_por_pagina: int = 10
) -> dict[str, Any]:
    d = levantar(configuracoes, armazenamento, marca)
    campanhas = d["campanhas"]
    executadas = [c for c in campanhas if c["analisadas"]]
    concluidas = [c for c in executadas if c["status"] == "CONCLUIDA"]
    maior = max(executadas, key=lambda c: c["analisadas"], default=None)
    horas_maquina = sum(c["horas"] for c in concluidas)
    analises = sum(c["analisadas"] for c in campanhas)
    achados, coban, logo = d["achados"], d["coban"], d["logo"]
    intel = d["intel"]
    idade = Counter(
        "ate180" if i.rdap.idade_dias <= 180 else "ate2anos" if i.rdap.idade_dias <= 730 else "antigo"
        for i in intel
        if i.rdap and i.rdap.idade_dias is not None
    )
    hospedagem = Counter(_SUFIXO_EMPRESA.sub("", i.ips[0].organizacao or i.ips[0].rede or "") for i in intel if i.ips)
    # Ritmo de cada modo medido na maior campanha concluída dele (as pequenas distorcem pelo tempo de partida).
    ritmo = {
        c["modo"]: c["analisadas"] / c["horas"]
        for c in sorted(concluidas, key=lambda c: c["analisadas"])
        if c["horas"] > 0.5
    }
    hipoteses = {
        "ampla": montar_hipoteses(marca),
        "logo": montar_hipoteses_logo_antiga(marca),
        "ameacas": montar_hipoteses_ameacas(marca),
    }
    return {
        "marca": marca,
        "data": datetime.now(caminhos.FUSO_BRASILIA).strftime("%d/%m/%Y"),
        "tempo_desenvolvimento": TEMPO_DESENVOLVIMENTO,
        "codigo": _codigo(),
        "hipoteses": {k: len(v) for k, v in hipoteses.items()},
        "dorks": {k: sum(len(h.consultas_palavras_chave) for h in v) for k, v in hipoteses.items()},
        "campanhas": campanhas,
        "executadas": executadas,
        "por_modo": Counter(c["modo"] for c in campanhas),
        "maior": maior,
        "ritmo": ritmo,
        "horas_maquina": horas_maquina,
        "minutos_por_pagina": minutos_por_pagina,
        "horas_manual": len(d["analisadas"]) * minutos_por_pagina / 60,
        "custo": analises * CUSTO_POR_PAGINA_USD,
        "custo_por_pagina": CUSTO_POR_PAGINA_USD,
        "consultas": sum(d["consultas_fonte"].values()),
        "consultas_fonte": [(FONTES.get(k, k), v) for k, v in d["consultas_fonte"].most_common()],
        "resultados": sum(d["resultados_fonte"].values()),
        "resultados_fonte": [(FONTES.get(k, k), v) for k, v in d["resultados_fonte"].most_common()],
        "urls": len(d["paginas"]),
        "hosts": len({p.host for p in d["paginas"]}),
        "dominios": len({p.dominio for p in d["paginas"]}),
        "analisadas": len(d["analisadas"]),
        "analises": analises,
        "coletas": d["coletas"],
        "chamadas": sum(c["chamadas"] for c in campanhas),
        "tokens": sum(c["tokens"] for c in campanhas),
        "evidencias": d["evidencias"],
        "capturas": d["capturas"],
        "gb_evidencias": d["gb_evidencias"],
        "dossies": d["dossies"],
        "recortes_total": d["recortes"],
        "cadastro_coban": d["cadastro_coban"],
        "classificacao": [
            (k, sum(p.classificacao == k for p in d["analisadas"]), COR_CLASSIFICACAO[k]) for k in GRAVIDADE
        ],
        "achados": {
            "total": len(achados),
            "forte": sum(p.classificacao == "INDICIO_FORTE" for p in achados),
            "revisao": sum(p.classificacao == "REQUER_REVISAO" for p in achados),
            "alta": sum(p.prioridade == "ALTA" for p in achados),
            "sites": len({p.dominio for p in achados if p.canal == SITE_PROPRIO}),
            "redes": sum(p.canal != SITE_PROPRIO for p in achados),
            "recorrentes": sum(len(p.campanhas) > 1 for p in achados),
            "caso": dict(_contar(p.caso for p in achados)),
            "identidade": _contar(p.identidade for p in achados),
            "canal": _contar(p.canal for p in achados),
            "encaminhamento": _contar(e for p in achados for e in p.encaminhamento),
            "hipoteses": [(HIPOTESES.get(k, k), v) for k, v in _contar(h for p in achados for h in p.hipoteses)],
            "conformidade": _conformidade(achados),
        },
        "coban": {
            "total": len(coban),
            "confirmado": sum(p.identidade == "COBAN_CONFIRMADO" for p in coban),
            "possivel": sum(p.identidade == "COBAN_POSSIVEL" for p in coban),
            "forte": sum(p.classificacao == "INDICIO_FORTE" for p in coban),
            "logo": sum(p.logo for p in coban),
            "sites": len({p.dominio for p in coban if p.canal == SITE_PROPRIO}),
            "caso": _contar(p.caso for p in coban),
            "conformidade": _conformidade(coban),
            "exemplos": _exemplos(coban, 10),
        },
        "logo": {
            "total": len(logo),
            "sites": len({p.dominio for p in logo if p.canal == SITE_PROPRIO}),
            "forte": sum(p.classificacao == "INDICIO_FORTE" for p in logo),
            "revisao": sum(p.classificacao == "REQUER_REVISAO" for p in logo),
            "coban": sum(p.identidade in ("COBAN_CONFIRMADO", "COBAN_POSSIVEL") for p in logo),
            "coban_confirmado": sum(p.identidade == "COBAN_CONFIRMADO" for p in logo),
            "fonte": Counter(_fonte_logo(p) for p in logo),
            "referencia": sum("refer" in " ".join(p.fonte_logo) for p in logo),
            "modos": [(MODOS.get(m, m), v) for m, v in _contar(m for p in logo for m in p.modos)],
            "exemplos": _exemplos([p for p in logo if p.achado], 10),
            "recortes": _recortes(logo, configuracoes.dir_saida),
        },
        "ameacas": {
            "nivel": [(n, sum(p.nivel == n for p in d["ameacas"]), COR_NIVEL[n]) for n in COR_NIVEL],
            "categorias": _contar(c for p in d["ameacas"] for c in p.categorias),
            "credenciais": sum(bool(p.credenciais) for p in d["analisadas"]),
            "sites": len({p.dominio for p in d["crit_alto"] if p.canal == SITE_PROPRIO}),
            "parecidos": sum("Domínio parecido com o oficial" in p.categorias for p in d["ameacas"]),
            "exemplos": _exemplos(d["crit_alto"], 12),
        },
        "intel": {
            "hosts": len(intel),
            "idade": idade,
            "hospedagem": [(k, v) for k, v in hospedagem.most_common() if k and not k.startswith("Company")][:7],
            "abuso": sum(bool((i.rdap and i.rdap.emails_abuso) or any(x.emails_abuso for x in i.ips)) for i in intel),
            "no_ar": sum(bool(i.a or i.aaaa) for i in intel),
            "pj": sum(bool(i.rdap and i.rdap.tipo_titular == "pessoa jurídica") for i in intel),
            "pf": sum(bool(i.rdap and i.rdap.tipo_titular.startswith("pessoa física")) for i in intel),
        },
        "ritmo_itens": [(NOMES_RITMO.get(m, m), round(v)) for m, v in ritmo.items()],
        "identidade_itens": [(IDENTIDADES.get(k, k), v) for k, v in _contar(p.identidade for p in achados)],
        "coban_caso": [(rotulo(k), v) for k, v in _contar(p.caso for p in coban)],
        "niveis_itens": [(n.capitalize(), sum(p.nivel == n for p in d["ameacas"]), c) for n, c in COR_NIVEL.items()],
        "sobreposicao": _sobreposicao(campanhas),
        "modos": MODOS,
        "identidades": IDENTIDADES,
    }


def _pct(parte: float, total: float) -> str:
    return num_br(100 * parte / total, 1) + "%" if total else "—"


def gravar_relatorio_executivo(
    configuracoes: Configuracoes,
    armazenamento: Armazenamento,
    destino: Path,
    id_marca: str = "bv",
    minutos_por_pagina: int = 10,
    com_pdf: bool = True,
) -> ResultadoExecutivo:
    marca = carregar_marca(id_marca, configuracoes.arquivo_marca)
    contexto = montar_contexto(configuracoes, armazenamento, marca, minutos_por_pagina)
    destino.mkdir(parents=True, exist_ok=True)
    html = destino / caminhos.RELATORIO_EXECUTIVO_HTML
    pagina = ambiente.get_template("relatorio_executivo.html.j2").render(**contexto, pct=_pct, cor=COR)
    html.write_text(pagina, encoding="utf-8")
    logger.info("Relatório executivo gravado em %s", html)
    pdf = gerar_pdf(html, destino / caminhos.RELATORIO_EXECUTIVO_PDF, paisagem=True) if com_pdf else None
    return ResultadoExecutivo(html=html, pdf=pdf)
