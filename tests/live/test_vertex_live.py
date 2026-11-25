"""Real Vertex AI / Google Search Grounding checks.

Opt-in only (SIMIO_LIVE_TESTS=1) and meant for the authorised corporate environment with
SIMIO_GCP_PROJECT, SIMIO_GCP_LOCATION and SIMIO_ANALYSIS_MODEL configured and ADC available.
They send no investigation data: only fixed, synthetic prompts.
"""

from __future__ import annotations

import pytest

from simio.config import Settings
from simio.core.models import BrandAnalysisOutput
from simio.providers.vertex import VertexProvider

pytestmark = pytest.mark.live


@pytest.fixture
def live_settings() -> Settings:
    s = Settings()
    if not s.vertex_configured():
        pytest.skip("SIMIO_GCP_PROJECT / SIMIO_ANALYSIS_MODEL not configured")
    return s


async def test_live_structured_output(live_settings: Settings) -> None:
    prov = VertexProvider(live_settings)
    text, usage = await prov.generate_text(
        model=live_settings.analysis_model or "",
        system="Classifique a página sintética.",
        parts=["Página fictícia: 'Loja Exemplo Ltda, correspondente bancário do Banco Exemplo'. Responda no esquema."],
        json_schema=BrandAnalysisOutput.model_json_schema(),
        pydantic_schema=BrandAnalysisOutput,
    )
    BrandAnalysisOutput.model_validate_json(text)
    assert usage.total_tokens > 0


async def test_live_google_search_grounding(live_settings: Settings) -> None:
    prov = VertexProvider(live_settings)
    res = await prov.grounded_search(
        model=live_settings.effective_discovery_model or "",
        prompt="site oficial do Banco Central do Brasil",
        exclude_domains=[],
    )
    assert res.sources, "grounding returned no sources (check model/region support)"
    assert res.web_search_queries
