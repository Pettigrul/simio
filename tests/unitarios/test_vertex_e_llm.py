from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from google.genai import errors

from simio.configuracoes import Configuracoes
from simio.nucleo.modelos import CasoCenario, ClassificacaoFinal, Prioridade, SaidaAnaliseMarca
from simio.provedores.llm import (
    ErroTransitorioLLM,
    GatewayLLM,
    LLMIndisponivel,
    Orcamento,
    ParteImagem,
    SaidaLLMInvalida,
    remover_cercas,
)
from simio.provedores.mock import ProvedorMock
from simio.provedores.vertex import ProvedorVertex, montar_cliente

VALIDO = SaidaAnaliseMarca(
    caso=CasoCenario.B_COMUNICACAO_AMBIGUA,
    classificacao=ClassificacaoFinal.REQUER_REVISAO,
    prioridade=Prioridade.MEDIA,
)


class ModelosFalsos:
    def __init__(self, respostas: list) -> None:
        self.respostas = respostas
        self.chamadas: list[dict] = []

    async def generate_content(self, *, model, contents, config):
        self.chamadas.append({"model": model, "contents": contents, "config": config})
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def cliente_falso(respostas: list) -> tuple[NS, ModelosFalsos]:
    modelos = ModelosFalsos(respostas)
    return NS(aio=NS(models=modelos)), modelos


def resposta_falsa(texto: str) -> NS:
    return NS(
        text=texto,
        candidates=[NS(finish_reason="STOP")],
        usage_metadata=NS(prompt_token_count=11, candidates_token_count=7, total_token_count=18),
    )


def erro_api(codigo: int) -> errors.APIError:
    cls = errors.ClientError if codigo < 500 else errors.ServerError
    return cls(codigo, {"error": {"code": codigo, "message": f"err {codigo}", "status": "X"}})


def test_montar_cliente_exige_projeto() -> None:
    with pytest.raises(LLMIndisponivel):
        montar_cliente(Configuracoes(projeto_gcp=None))


def test_montar_cliente_usa_backend_vertex(monkeypatch) -> None:
    capturado = {}

    class C:
        def __init__(self, **kw):
            capturado.update(kw)

    from google import genai

    monkeypatch.setattr(genai, "Client", C)
    montar_cliente(Configuracoes(projeto_gcp="proj-x", local_gcp="us-central1", llm_timeout_s=5))
    assert capturado["project"] == "proj-x" and capturado["location"] == "us-central1"
    assert capturado.get("enterprise") is True or capturado.get("vertexai") is True
    assert capturado["http_options"].timeout == 5000
    assert "api_key" not in capturado


async def test_chamada_estruturada_sem_ferramentas_e_envia_imagens() -> None:
    cliente, modelos = cliente_falso([resposta_falsa(VALIDO.model_dump_json())])
    prov = ProvedorVertex(Configuracoes(projeto_gcp="p"), cliente=cliente)
    _texto, uso = await prov.gerar_texto(
        modelo="m",
        sistema="sys",
        partes=["a", ParteImagem(b"\x89PNG", rotulo="shot")],
        esquema_json=SaidaAnaliseMarca.model_json_schema(),
        esquema_pydantic=SaidaAnaliseMarca,
    )
    cfg = modelos.chamadas[0]["config"]
    # Chamada de análise nunca recebe ferramentas (nem busca nem function calling), para o conteúdo
    # da página não conseguir disparar ação externa ou vazar dados pelo modelo.
    assert not cfg.tools
    assert cfg.response_mime_type == "application/json"
    partes = modelos.chamadas[0]["contents"][0].parts
    assert any(getattr(p, "inline_data", None) for p in partes)
    assert uso.tokens_total == 18


async def test_config_grounding_habilita_so_google_search() -> None:
    gm = NS(web_search_queries=["q"], search_entry_point=None, grounding_chunks=[], grounding_supports=[])
    cliente, modelos = cliente_falso([NS(text="t", candidates=[NS(grounding_metadata=gm)], usage_metadata=None)])
    prov = ProvedorVertex(Configuracoes(projeto_gcp="p"), cliente=cliente)
    res = await prov.busca_grounding(modelo="m", prompt="p", dominios_excluidos=["bv.com.br"])
    ferramentas = modelos.chamadas[0]["config"].tools
    assert len(ferramentas) == 1 and ferramentas[0].google_search is not None
    assert res.consultas_busca_web == ["q"] and res.fontes == []


@pytest.mark.parametrize(
    ("codigo", "exc"), [(429, ErroTransitorioLLM), (503, ErroTransitorioLLM), (403, LLMIndisponivel), (404, LLMIndisponivel)]
)
async def test_traducao_de_erros(codigo: int, exc: type) -> None:
    cliente, _ = cliente_falso([erro_api(codigo)])
    prov = ProvedorVertex(Configuracoes(projeto_gcp="p"), cliente=cliente)
    with pytest.raises(exc):
        await prov.gerar_texto(modelo="m", sistema="s", partes=["x"], esquema_json=None, esquema_pydantic=None)


async def test_json_schema_rejeitado_recorre_a_response_schema() -> None:
    cliente, modelos = cliente_falso([erro_api(400), resposta_falsa(VALIDO.model_dump_json())])
    prov = ProvedorVertex(Configuracoes(projeto_gcp="p"), cliente=cliente)
    await prov.gerar_texto(
        modelo="m", sistema="s", partes=["x"], esquema_json={"type": "object"}, esquema_pydantic=SaidaAnaliseMarca
    )
    assert modelos.chamadas[1]["config"].response_schema is not None


async def test_gateway_repete_erro_transitorio_e_depois_sucede(armazenamento) -> None:
    cid = armazenamento.criar_campanha("bv", {})
    seq = [ErroTransitorioLLM("429"), VALIDO]

    def respondedor(sistema, partes):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    gw = GatewayLLM(
        ProvedorMock({"SaidaAnaliseMarca": respondedor}),
        Orcamento(armazenamento, cid, 10, 10_000),
        max_retentativas=2,
        atraso_base_s=0,
    )
    saida = await gw.estruturada(agente="t", modelo="m", sistema="s", partes=["x"], esquema=SaidaAnaliseMarca)
    assert saida.caso == CasoCenario.B_COMUNICACAO_AMBIGUA
    assert armazenamento.totais_uso(cid)[0] == 2  # tentativa que falhou também conta no orçamento


async def test_gateway_repara_json_invalido_uma_vez(armazenamento) -> None:
    cid = armazenamento.criar_campanha("bv", {})
    saidas = ['{"caso": "B_COMUNICACAO_AMBIGUA"}', "```json\n" + VALIDO.model_dump_json() + "\n```"]
    prov = ProvedorMock({"SaidaAnaliseMarca": lambda s, p: saidas.pop(0)})
    gw = GatewayLLM(prov, Orcamento(armazenamento, cid, 10, 10_000), max_retentativas=0, atraso_base_s=0)
    saida = await gw.estruturada(agente="t", modelo="m", sistema="s", partes=["x"], esquema=SaidaAnaliseMarca)
    assert saida.classificacao == ClassificacaoFinal.REQUER_REVISAO
    assert "não respeitou o esquema" in prov.chamadas[1].partes[-1]


async def test_gateway_desiste_apos_reparo(armazenamento) -> None:
    cid = armazenamento.criar_campanha("bv", {})
    gw = GatewayLLM(
        ProvedorMock({"SaidaAnaliseMarca": lambda s, p: "not json"}),
        Orcamento(armazenamento, cid, 10, 10_000),
        max_retentativas=0,
        atraso_base_s=0,
    )
    with pytest.raises(SaidaLLMInvalida):
        await gw.estruturada(agente="t", modelo="m", sistema="s", partes=["x"], esquema=SaidaAnaliseMarca)


def test_remover_cercas() -> None:
    assert remover_cercas('```json\n{"a":1}\n```') == '{"a":1}'
    assert remover_cercas('{"a":1}') == '{"a":1}'
