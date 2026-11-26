from __future__ import annotations

import contextlib
from pathlib import Path

from PIL import Image

from simio.agentes.descoberta import FOCO_LOGO_ANTIGA, AgenteDescoberta
from simio.agentes.visual import pistas_logo_antiga
from simio.nucleo import caminhos
from simio.nucleo.modelos import (
    Artefato,
    AvaliacaoMarca,
    CasoCenario,
    DadosPagina,
    DecisaoFinal,
    Evidencia,
    InfoImagem,
    Prioridade,
    SaidaAnaliseMarca,
    SaidaRevisao,
    StatusColeta,
)
from simio.nucleo.modelos import ClassificacaoFinal as C
from simio.nucleo.orquestrador import OpcoesInvestigacao, Orquestrador
from simio.nucleo.urls import hostname_de
from simio.provedores.busca import importar_texto
from simio.provedores.mock import ProvedorMock
from simio.seguranca.politica_url import PoliticaUrl
from tests.auxiliares import MARCA

LOGO = Image.open(MARCA.caminhos_logo_antiga()[0]).convert("RGB")


class _Nomeado:
    def __init__(self, nome: str, consultas: list[str] | None = None) -> None:
        self.nome = nome
        self._q = consultas or []

    def disponivel(self) -> tuple[bool, str]:
        return True, self.nome

    def consultas(self) -> list[str]:
        return self._q

    async def executar(self, hipotese, consulta):  # pragma: no cover
        raise AssertionError


def test_plano_focado_usa_so_hipoteses_de_logo_e_pula_conectores_de_dominio() -> None:
    cons = [_Nomeado("serper"), _Nomeado("lens", ["https://img.example/bv.png"]), _Nomeado("typosquat", ["x"])]
    agente = AgenteDescoberta(MARCA, cons, PoliticaUrl(), max_consultas=500, foco=FOCO_LOGO_ANTIGA)
    planejar = agente.planejar(cons)
    ids = {p.hipotese.id.split(":")[0] for p in planejar}
    assert ids == {"H13-foco-logo-antiga", "H12-logo-antiga"}
    assert not any(p.conector == "typosquat" for p in planejar)
    assert any("typosquat: não usado em campanha com foco" in d for d in agente.diagnosticos)
    assert sum(p.conector == "serper" for p in planejar) >= 40  # temáticas + uma por UF


def _ev_com(imagens: list[InfoImagem]) -> Evidencia:
    return Evidencia(
        id_candidata="C1", tentativa=1, status=StatusColeta.OK, url_original="https://a.example/", pagina=DadosPagina(imagens=imagens)
    )


def test_pistas_de_logo_por_nome_de_arquivo_ou_alt_curto() -> None:
    def img(src: str, alt: str = "") -> InfoImagem:
        return InfoImagem(src=src, alt=alt, largura=60, altura=40)

    resultado = [
        img("https://static.wixstatic.com/media/x~mv2.png/v1/fill/w_144/logo-bv-financeira-512.png"),
        img("https://loja.example/images/financial/bv.png"),
        img("https://loja.example/img/parceiro-3.png", alt="BV"),
        img("https://loja.example/img/p.png", alt="logo BV Financeira"),
    ]
    falta = [
        img("https://loja.example/img/bvx-banner.png"),
        img("https://loja.example/img/ilustracao.png", alt="Por que contratar o crédito da BV financeira com a gente"),
        InfoImagem(src="https://loja.example/bv.png", alt="", largura=8, altura=8),  # invisible pixel
    ]
    assert len(pistas_logo_antiga(_ev_com(resultado))) == 4
    assert pistas_logo_antiga(_ev_com(falta)) == []


class _ColetorFalso:
    # /logo mostra a logo antiga, /hint tem imagem bv.png sem logo visível, /plain não tem nenhuma das duas.

    async def coletar(self, id_candidata: str, url: str, tentativa: int, dir_campanha: Path) -> Evidencia:
        rel = caminhos.dir_tentativa(hostname_de(url), id_candidata, tentativa)
        (dir_campanha / rel).mkdir(parents=True, exist_ok=True)
        tela = Image.new("RGB", (1366, 900), "white")
        imagens: list[InfoImagem] = []
        if url.endswith("/logo"):
            tela.paste(LOGO.resize((110, 58)), (200, 150))
            imagens.append(InfoImagem(src=f"{url}/img/parceiro.png", alt="", largura=110, altura=58, pagina_x=200, pagina_y=150))
        elif url.endswith("/hint"):
            imagens.append(InfoImagem(src=f"{url}/img/bv.png", alt="", largura=60, altura=40, pagina_x=10, pagina_y=10))
        tela.save(dir_campanha / rel / caminhos.ARTEFATOS["captura_viewport"])
        return Evidencia(
            id_candidata=id_candidata,
            tentativa=tentativa,
            status=StatusColeta.OK,
            url_original=url,
            url_final=url,
            status_http=200,
            pagina=DadosPagina(titulo="Loja", texto_visivel="Financiamento", imagens=imagens),
            artefatos=[
                Artefato(tipo="captura_viewport", caminho=str(rel / caminhos.ARTEFATOS["captura_viewport"]), sha256="x", tamanho_bytes=1)
            ],
        )


async def test_campanha_focada_envia_ao_modelo_so_paginas_com_logo(configuracoes, armazenamento) -> None:
    configuracoes.modelo_analise = "m"
    chamadas: list[str] = []

    def analista(sy, partes):
        chamadas.append("marca")
        return SaidaAnaliseMarca(
            caso=CasoCenario.B_COMUNICACAO_AMBIGUA, classificacao=C.REQUER_REVISAO, prioridade=Prioridade.MEDIA
        )

    def revisor(sy, partes):
        return SaidaRevisao(
            decisao="CONFIRMAR", classificacao_revisada=C.REQUER_REVISAO, prioridade_revisada=Prioridade.MEDIA
        )

    prov = ProvedorMock({"SaidaAnaliseMarca": analista, "SaidaRevisao": revisor})

    @contextlib.asynccontextmanager
    async def fabrica():
        yield _ColetorFalso()

    urls = importar_texto("https://logo.example/logo\nhttps://hint.example/hint\nhttps://plain.example/plain")
    orq = Orquestrador(configuracoes, armazenamento, provedor=prov, fabrica_coletor=fabrica)
    s = await orq.investigar(OpcoesInvestigacao(importadas=urls, fontes=[], foco=FOCO_LOGO_ANTIGA))
    assert s.status == "CONCLUIDA" and chamadas == ["marca", "marca"]

    por_host = {c.hostname: c for c in armazenamento.listar_candidatas(s.id_campanha)}
    logo = armazenamento.obter_avaliacao(por_host["logo.example"].id, "final", DecisaoFinal)
    assert logo.identidade_visual_antiga and logo.prioridade == Prioridade.ALTA
    simples = armazenamento.obter_avaliacao(por_host["plain.example"].id, "final", DecisaoFinal)
    assert simples.classificacao == C.SEM_INDICIO and not simples.identidade_visual_antiga
    assert any("campanha focada" in lim for lim in simples.limitacoes)
    marca = armazenamento.obter_avaliacao(por_host["plain.example"].id, "marca", AvaliacaoMarca)
    assert marca.analisador == f"foco:{FOCO_LOGO_ANTIGA}"
    assert armazenamento.obter_campanha(s.id_campanha)["parametros"]["foco"] == FOCO_LOGO_ANTIGA
