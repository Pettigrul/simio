"""Deterministic in-process LLM provider used by tests (no network, no credentials)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from simio.providers.llm import GroundedResult, GroundedSource, Part, Usage

Responder = Callable[[str, list[Part]], Any]


@dataclass
class MockCall:
    kind: str
    model: str
    system: str
    parts: list[Part]
    schema: str | None = None


@dataclass
class MockProvider:
    """Responders are keyed by the Pydantic schema class name (e.g. ``BrandAnalysisOutput``).

    A responder receives (system, parts) and returns a dict / BaseModel / JSON string, or
    raises an exception to simulate provider failures.
    """

    responders: dict[str, Responder] = field(default_factory=dict)
    grounded: Callable[[str], GroundedResult] | None = None
    name: str = "mock"
    calls: list[MockCall] = field(default_factory=list)

    async def generate_text(
        self,
        *,
        model: str,
        system: str,
        parts: list[Part],
        json_schema: dict | None,
        pydantic_schema: type[BaseModel] | None,
    ) -> tuple[str, Usage]:
        key = pydantic_schema.__name__ if pydantic_schema else "text"
        self.calls.append(MockCall("generate", model, system, list(parts), key))
        responder = self.responders.get(key)
        if responder is None:
            raise RuntimeError(f"MockProvider has no responder for {key}")
        out = responder(system, parts)
        if isinstance(out, BaseModel):
            text = out.model_dump_json()
        elif isinstance(out, str):
            text = out
        else:
            text = json.dumps(out, ensure_ascii=False)
        return text, Usage(prompt_tokens=100, output_tokens=50, total_tokens=150)

    async def grounded_search(self, *, model: str, prompt: str, exclude_domains: list[str]) -> GroundedResult:
        self.calls.append(MockCall("grounded", model, "", [prompt]))
        if self.grounded is None:
            raise RuntimeError("MockProvider has no grounded responder")
        return self.grounded(prompt)


def grounded_result(urls: list[tuple[str, str]], queries: list[str] | None = None) -> GroundedResult:
    """Helper: build a GroundedResult from (uri, title) pairs."""
    return GroundedResult(
        text="(mock)",
        sources=[
            GroundedSource(uri=u, title=t, domain=None, support_texts=[f"menção a {t}"], chunk_index=i)
            for i, (u, t) in enumerate(urls)
        ],
        web_search_queries=queries or [],
        search_entry_point_html=None,
        usage=Usage(prompt_tokens=50, output_tokens=20, total_tokens=70),
        model="mock-model",
    )
