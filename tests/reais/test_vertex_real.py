from __future__ import annotations

import pytest

from simio.configuracoes import Configuracoes
from simio.nucleo.modelos import SaidaAnaliseMarca
from simio.provedores.vertex import ProvedorVertex

# Só rodam com SIMIO_TESTES_REAIS=1, no ambiente autorizado (projeto GCP + ADC). Mandam apenas
# prompts sintéticos fixos, nenhum dado de investigação.
pytestmark = pytest.mark.live


@pytest.fixture
def configuracoes_reais() -> Configuracoes:
    s = Configuracoes()
    if not s.vertex_configurado():
        pytest.skip("SIMIO_PROJETO_GCP / SIMIO_MODELO_ANALISE não configurados")
    return s


async def test_real_saida_estruturada(configuracoes_reais: Configuracoes) -> None:
    prov = ProvedorVertex(configuracoes_reais)
    texto, uso = await prov.gerar_texto(
        modelo=configuracoes_reais.modelo_analise or "",
        sistema="Classifique a página sintética.",
        partes=["Página fictícia: 'Loja Exemplo Ltda, correspondente bancário do Banco Exemplo'. Responda no esquema."],
        esquema_json=SaidaAnaliseMarca.model_json_schema(),
        esquema_pydantic=SaidaAnaliseMarca,
    )
    SaidaAnaliseMarca.model_validate_json(texto)
    assert uso.tokens_total > 0


async def test_real_google_search_grounding(configuracoes_reais: Configuracoes) -> None:
    prov = ProvedorVertex(configuracoes_reais)
    res = await prov.busca_grounding(
        modelo=configuracoes_reais.modelo_descoberta_efetivo or "",
        prompt="site oficial do Banco Central do Brasil",
        dominios_excluidos=[],
    )
    assert res.fontes, "o grounding não devolveu fontes (confira se o modelo/região suporta)"
    assert res.consultas_busca_web
