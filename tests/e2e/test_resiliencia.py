"""Architecture-review regressions: crash isolation, resume semantics, idempotency, budgets."""

from __future__ import annotations

import asyncio

import pytest

from simio.agentes import evidencia as modulo_evidencia
from simio.nucleo.armazenamento import ModificacaoConcorrente
from simio.nucleo.modelos import (
    CasoCenario,
    Prioridade,
    RegistroConsulta,
    ResultadoDescoberta,
    SaidaAnaliseMarca,
    SaidaRevisao,
)
from simio.nucleo.modelos import ClassificacaoFinal as C
from simio.nucleo.modelos import StatusCandidata as S
from simio.nucleo.orquestrador import CampanhaOcupada, OpcoesInvestigacao, Orquestrador
from simio.provedores.busca import importar_texto
from simio.provedores.llm import Orcamento, OrcamentoEsgotado, Uso
from simio.provedores.mock import ProvedorMock
from simio.relatorios.dados import carregar_registros
from tests.auxiliares import MARCA

pytestmark = pytest.mark.browser

FORTE = SaidaAnaliseMarca(
    caso=CasoCenario.B_COMUNICACAO_AMBIGUA, classificacao=C.REQUER_REVISAO, prioridade=Prioridade.MEDIA
)


async def test_erro_inesperado_fica_isolado_em_uma_candidata(configuracoes, armazenamento, site, monkeypatch) -> None:
    configuracoes.offline = True
    real = modulo_evidencia.calcular_sinais

    def explodir(ev, marca, catalogo):
        if ev.url_original.endswith("ambiguous.html"):
            raise ValueError("poison page")
        return real(ev, marca, catalogo)

    monkeypatch.setattr("simio.nucleo.orquestrador.calcular_sinais", explodir)
    urls = importar_texto(f"{site}/ambiguous.html\n{site}/legit_partner.html\n{site}/lookalike.html")
    s = await Orquestrador(configuracoes, armazenamento).investigar(OpcoesInvestigacao(importadas=urls))
    por_url = {c.url.rsplit("/", 1)[-1]: c for c in armazenamento.listar_candidatas(s.id_campanha)}
    assert por_url["ambiguous.html"].status == S.FALHOU
    assert por_url["legit_partner.html"].status == S.CONCLUIDA and por_url["lookalike.html"].status == S.CONCLUIDA
    assert s.status == "CONCLUIDA"


async def test_candidata_venenosa_nao_bloqueia_retomada(configuracoes, armazenamento, site) -> None:
    configuracoes.offline = True
    cid = armazenamento.criar_campanha("bv", {"marca": "bv", "limite": 5, "fontes": [], "offline": True})
    armazenamento.salvar_checkpoint(cid, "descoberta_concluida", True)
    cand, _ = armazenamento.upsert_candidata(
        cid, url=f"{site}/ambiguous.html", url_normalizada="n", hostname="127.0.0.1", dominio_registravel="127.0.0.1"
    )
    armazenamento.transicionar(cand, S.NA_FILA)
    armazenamento.transicionar(cand, S.COLETANDO)
    for _ in range(configuracoes.max_tentativas):
        armazenamento.incrementar(cand, "tentativas")  # crashed/hung the process max_attempts times
    s = await Orquestrador(configuracoes, armazenamento).retomar(cid)
    assert armazenamento.obter_candidata(cand).status == S.FALHOU
    assert s.status == "CONCLUIDA"


async def test_retomar_mantem_modo_offline_e_modelo(configuracoes, armazenamento, site) -> None:
    configuracoes.offline = True
    urls = importar_texto(f"{site}/ambiguous.html")
    s1 = await Orquestrador(configuracoes, armazenamento).investigar(OpcoesInvestigacao(importadas=urls, limite=0))
    assert s1.contagens == {"DESCOBERTA": 1}
    # Operator now has Vertex configured: resume must NOT silently start sending pages to the model.
    chamadas: list[str] = []
    prov = ProvedorMock({"SaidaAnaliseMarca": lambda sy, p: chamadas.append("x") or FORTE})
    configuracoes.offline = False
    configuracoes.modelo_analise = "other-model"
    orq = Orquestrador(configuracoes, armazenamento, provedor=prov)
    await orq.retomar(s1.id_campanha)
    assert orq.configuracoes.offline is True and chamadas == []


async def test_trava_da_campanha_impede_processamento_concorrente(configuracoes, armazenamento) -> None:
    cid = armazenamento.criar_campanha("bv", {"marca": "bv", "limite": 1, "fontes": []})
    assert armazenamento.adquirir_trava(cid, "other-host:1:abcd") is None
    with pytest.raises(CampanhaOcupada):
        await Orquestrador(configuracoes, armazenamento).retomar(cid)
    armazenamento.liberar_trava(cid, "other-host:1:abcd")


def test_transicao_compare_and_set(armazenamento) -> None:
    cid = armazenamento.criar_campanha("bv", {})
    c, _ = armazenamento.upsert_candidata(
        cid, url="https://a.example/", url_normalizada="a", hostname="a.example", dominio_registravel="a.example"
    )
    armazenamento.transicionar(c, S.NA_FILA)
    armazenamento.transicionar(c, S.COLETANDO, esperado=S.NA_FILA)
    with pytest.raises(ModificacaoConcorrente):
        armazenamento.transicionar(c, S.COLETADA, esperado=S.NA_FILA)


async def test_analise_de_marca_paga_eh_reaproveitada_apos_falha_do_revisor(configuracoes, armazenamento, site) -> None:
    configuracoes.modelo_analise = "m"
    configuracoes.max_tentativas = 3
    n = {"marca": 0, "revisao": 0}

    def analista(sy, p):
        n["marca"] += 1
        return FORTE

    def revisor(sy, p):
        n["revisao"] += 1
        if n["revisao"] == 1:
            return "not json"  # invalid twice (initial + repair) -> analysis retry
        return SaidaRevisao(
            decisao="CONFIRMAR",
            classificacao_revisada=C.REQUER_REVISAO,
            prioridade_revisada=Prioridade.MEDIA,
            resumo_raciocinio="ok",
        )

    prov = ProvedorMock({"SaidaAnaliseMarca": analista, "SaidaRevisao": revisor})
    s = await Orquestrador(configuracoes, armazenamento, provedor=prov).investigar(
        OpcoesInvestigacao(importadas=importar_texto(f"{site}/ambiguous.html"), fontes=[])
    )
    assert s.status == "CONCLUIDA"
    assert n["marca"] == 1, "brand analysis was bought twice"


async def test_orcamento_de_tokens_eh_teto_rigido(armazenamento) -> None:
    cid = armazenamento.criar_campanha("bv", {})
    orcamento = Orcamento(armazenamento, cid, max_requisicoes=100, max_tokens=10_000)

    async def chamada() -> None:
        await orcamento.reservar(4_000)
        await asyncio.sleep(0)
        await orcamento.efetivar("a", "m", Uso(tokens_prompt=3_000, tokens_saida=1_000, tokens_total=4_000), 4_000)

    retornos = await asyncio.gather(*(chamada() for _ in range(4)), return_exceptions=True)
    assert sum(isinstance(r, OrcamentoEsgotado) for r in retornos) == 2
    assert armazenamento.totais_uso(cid)[1] <= 10_000


class _BuscaContadora:
    nome = "brave"

    def __init__(self, urls: list[str], falhar_depois: int | None = None) -> None:
        self.urls, self.chamadas, self.falhar_depois = urls, [], falhar_depois

    def disponivel(self):
        return True, "brave: fake"

    async def executar(self, hipotese, consulta):
        if self.falhar_depois is not None and len(self.chamadas) >= self.falhar_depois:
            raise KeyboardInterrupt  # simulated Ctrl+C mid-discovery
        self.chamadas.append(consulta)
        resultados = [
            ResultadoDescoberta(fonte=self.nome, url_original=u, consulta=consulta, id_hipotese=hipotese.id) for u in self.urls
        ]
        return resultados, RegistroConsulta(fonte=self.nome, consulta=consulta, id_hipotese=hipotese.id, qtd_resultados=len(resultados))


async def test_descoberta_interrompida_retoma_sem_perder_importadas_nem_pagar_de_novo(configuracoes, armazenamento, site) -> None:
    configuracoes.offline = False
    configuracoes.max_consultas_busca = 4
    importadas = importar_texto(f"{site}/legit_partner.html")
    primeiro = _BuscaContadora([f"{site}/ambiguous.html"], falhar_depois=2)
    with pytest.raises(KeyboardInterrupt):
        await Orquestrador(configuracoes, armazenamento, conectores=[primeiro]).investigar(
            OpcoesInvestigacao(importadas=importadas, fontes=["brave"], limite=10)
        )
    cid = armazenamento.id_campanha_mais_recente()
    assert armazenamento.obter_campanha(cid)["status"] == "INTERROMPIDA"
    segundo = _BuscaContadora([f"{site}/lookalike.html"])
    await Orquestrador(configuracoes, armazenamento, conectores=[segundo]).retomar(cid)
    assert len(armazenamento.listar_consultas(cid)) == 1 + 4  # 1 import record + cap of 4 searches overall
    assert not set(primeiro.chamadas) & set(segundo.chamadas)
    urls = {c.url.rsplit("/", 1)[-1] for c in armazenamento.listar_candidatas(cid)}
    assert {"legit_partner.html", "ambiguous.html", "lookalike.html"} <= urls


async def test_relatorio_mostra_uma_rodada_consistente(configuracoes, armazenamento, site) -> None:
    configuracoes.offline = True
    s = await Orquestrador(configuracoes, armazenamento).investigar(OpcoesInvestigacao(importadas=importar_texto(f"{site}/ambiguous.html")))
    cand = armazenamento.listar_candidatas(s.id_campanha)[0]
    # A stale review from an older round must not be reported next to the current final.
    armazenamento.salvar_avaliacao(
        cand.id,
        "revisao",
        SaidaRevisao(
            decisao="RECOLETAR",
            classificacao_revisada=C.INCONCLUSIVA,
            prioridade_revisada=Prioridade.BAIXA,
            resumo_raciocinio="old",
        ),
        id_evidencia=999,
    )
    rec = carregar_registros(armazenamento, s.id_campanha, MARCA)[0]
    assert rec.revisao is None or rec.revisao.resumo_raciocinio != "old"
