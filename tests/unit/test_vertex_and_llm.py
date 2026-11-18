"""Vertex provider and LLM gateway, exercised with a fake google-genai client (no network)."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from google.genai import errors

from simio.config import Settings
from simio.core.models import BrandAnalysisOutput, FinalClassification, Priority, ScenarioCase
from simio.providers.llm import (
    Budget,
    ImagePart,
    LLMGateway,
    LLMOutputInvalid,
    LLMUnavailable,
    TransientLLMError,
    strip_fences,
)
from simio.providers.mock import MockProvider
from simio.providers.vertex import VertexProvider, build_client

VALID = BrandAnalysisOutput(
    case=ScenarioCase.B_AMBIGUOUS_COMMUNICATION,
    classification=FinalClassification.REVIEW_REQUIRED,
    priority=Priority.MEDIUM,
)


class FakeModels:
    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def fake_client(responses: list) -> tuple[NS, FakeModels]:
    models = FakeModels(responses)
    return NS(aio=NS(models=models)), models


def resp(text: str) -> NS:
    return NS(
        text=text,
        candidates=[NS(finish_reason="STOP")],
        usage_metadata=NS(prompt_token_count=11, candidates_token_count=7, total_token_count=18),
    )


def api_error(code: int) -> errors.APIError:
    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, {"error": {"code": code, "message": f"err {code}", "status": "X"}})


def test_build_client_requires_project() -> None:
    with pytest.raises(LLMUnavailable):
        build_client(Settings(gcp_project=None))


def test_build_client_uses_vertex_backend(monkeypatch) -> None:
    captured = {}

    class C:
        def __init__(self, **kw):
            captured.update(kw)

    from google import genai

    monkeypatch.setattr(genai, "Client", C)
    build_client(Settings(gcp_project="proj-x", gcp_location="us-central1", llm_timeout_s=5))
    assert captured["project"] == "proj-x" and captured["location"] == "us-central1"
    assert captured.get("enterprise") is True or captured.get("vertexai") is True
    assert captured["http_options"].timeout == 5000
    assert "api_key" not in captured


async def test_structured_call_has_no_tools_and_sends_images() -> None:
    client, models = fake_client([resp(VALID.model_dump_json())])
    prov = VertexProvider(Settings(gcp_project="p"), client=client)
    _text, usage = await prov.generate_text(
        model="m",
        system="sys",
        parts=["a", ImagePart(b"\x89PNG", label="shot")],
        json_schema=BrandAnalysisOutput.model_json_schema(),
        pydantic_schema=BrandAnalysisOutput,
    )
    cfg = models.calls[0]["config"]
    # Analysis calls never get tools (no search, no function calling): page content cannot
    # trigger outbound actions or data exfiltration through the model.
    assert not cfg.tools
    assert cfg.response_mime_type == "application/json"
    parts = models.calls[0]["contents"][0].parts
    assert any(getattr(p, "inline_data", None) for p in parts)
    assert usage.total_tokens == 18


async def test_grounding_config_enables_only_google_search() -> None:
    gm = NS(web_search_queries=["q"], search_entry_point=None, grounding_chunks=[], grounding_supports=[])
    client, models = fake_client([NS(text="t", candidates=[NS(grounding_metadata=gm)], usage_metadata=None)])
    prov = VertexProvider(Settings(gcp_project="p"), client=client)
    res = await prov.grounded_search(model="m", prompt="p", exclude_domains=["bv.com.br"])
    tools = models.calls[0]["config"].tools
    assert len(tools) == 1 and tools[0].google_search is not None
    assert res.web_search_queries == ["q"] and res.sources == []


@pytest.mark.parametrize(
    ("code", "exc"), [(429, TransientLLMError), (503, TransientLLMError), (403, LLMUnavailable), (404, LLMUnavailable)]
)
async def test_error_translation(code: int, exc: type) -> None:
    client, _ = fake_client([api_error(code)])
    prov = VertexProvider(Settings(gcp_project="p"), client=client)
    with pytest.raises(exc):
        await prov.generate_text(model="m", system="s", parts=["x"], json_schema=None, pydantic_schema=None)


async def test_json_schema_rejected_falls_back_to_response_schema() -> None:
    client, models = fake_client([api_error(400), resp(VALID.model_dump_json())])
    prov = VertexProvider(Settings(gcp_project="p"), client=client)
    await prov.generate_text(
        model="m", system="s", parts=["x"], json_schema={"type": "object"}, pydantic_schema=BrandAnalysisOutput
    )
    assert models.calls[1]["config"].response_schema is not None


async def test_gateway_retries_transient_then_succeeds(storage) -> None:
    cid = storage.create_campaign("bv", {})
    seq = [TransientLLMError("429"), VALID]

    def responder(system, parts):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    gw = LLMGateway(
        MockProvider({"BrandAnalysisOutput": responder}),
        Budget(storage, cid, 10, 10_000),
        max_retries=2,
        base_delay_s=0,
    )
    out = await gw.structured(agent="t", model="m", system="s", parts=["x"], schema=BrandAnalysisOutput)
    assert out.case == ScenarioCase.B_AMBIGUOUS_COMMUNICATION
    assert storage.usage_totals(cid)[0] == 2  # failed attempts count towards the budget


async def test_gateway_repairs_invalid_json_once(storage) -> None:
    cid = storage.create_campaign("bv", {})
    outputs = ['{"case": "B_AMBIGUOUS_COMMUNICATION"}', "```json\n" + VALID.model_dump_json() + "\n```"]
    prov = MockProvider({"BrandAnalysisOutput": lambda s, p: outputs.pop(0)})
    gw = LLMGateway(prov, Budget(storage, cid, 10, 10_000), max_retries=0, base_delay_s=0)
    out = await gw.structured(agent="t", model="m", system="s", parts=["x"], schema=BrandAnalysisOutput)
    assert out.classification == FinalClassification.REVIEW_REQUIRED
    assert "não respeitou o esquema" in prov.calls[1].parts[-1]


async def test_gateway_gives_up_after_repair(storage) -> None:
    cid = storage.create_campaign("bv", {})
    gw = LLMGateway(
        MockProvider({"BrandAnalysisOutput": lambda s, p: "not json"}),
        Budget(storage, cid, 10, 10_000),
        max_retries=0,
        base_delay_s=0,
    )
    with pytest.raises(LLMOutputInvalid):
        await gw.structured(agent="t", model="m", system="s", parts=["x"], schema=BrandAnalysisOutput)


def test_strip_fences() -> None:
    assert strip_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert strip_fences('{"a":1}') == '{"a":1}'
