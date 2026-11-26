"""Deterministic in-process LLM provider used by tests (no network, no credentials)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from simio.provedores.llm import FonteGrounding, Parte, ResultadoGrounding, Uso

Respondedor = Callable[[str, list[Parte]], Any]


@dataclass
class ChamadaMock:
    tipo: str
    modelo: str
    sistema: str
    partes: list[Parte]
    esquema: str | None = None


@dataclass
class ProvedorMock:
    """Responders are keyed by the Pydantic schema class name (e.g. ``SaidaAnaliseMarca``).

    A responder receives (system, parts) and returns a dict / BaseModel / JSON string, or
    raises an exception to simulate provider failures.
    """

    respondedores: dict[str, Respondedor] = field(default_factory=dict)
    com_grounding: Callable[[str], ResultadoGrounding] | None = None
    nome: str = "mock"
    chamadas: list[ChamadaMock] = field(default_factory=list)

    async def gerar_texto(
        self,
        *,
        modelo: str,
        sistema: str,
        partes: list[Parte],
        esquema_json: dict | None,
        esquema_pydantic: type[BaseModel] | None,
    ) -> tuple[str, Uso]:
        chave = esquema_pydantic.__name__ if esquema_pydantic else "text"
        self.chamadas.append(ChamadaMock("gerar", modelo, sistema, list(partes), chave))
        respondedor = self.respondedores.get(chave)
        if respondedor is None:
            raise RuntimeError(f"MockProvider has no responder for {chave}")
        saida = respondedor(sistema, partes)
        if isinstance(saida, BaseModel):
            texto = saida.model_dump_json()
        elif isinstance(saida, str):
            texto = saida
        else:
            texto = json.dumps(saida, ensure_ascii=False)
        return texto, Uso(tokens_prompt=100, tokens_saida=50, tokens_total=150)

    async def busca_grounding(self, *, modelo: str, prompt: str, dominios_excluidos: list[str]) -> ResultadoGrounding:
        self.chamadas.append(ChamadaMock("grounding", modelo, "", [prompt]))
        if self.com_grounding is None:
            raise RuntimeError("MockProvider has no grounded responder")
        return self.com_grounding(prompt)


def resultado_grounding(urls: list[tuple[str, str]], consultas: list[str] | None = None) -> ResultadoGrounding:
    """Helper: build a GroundedResult from (uri, title) pairs."""
    return ResultadoGrounding(
        texto="(mock)",
        fontes=[
            FonteGrounding(uri=u, titulo=t, dominio=None, textos_suporte=[f"menção a {t}"], indice_trecho=i)
            for i, (u, t) in enumerate(urls)
        ],
        consultas_busca_web=consultas or [],
        html_ponto_entrada_busca=None,
        uso=Uso(tokens_prompt=50, tokens_saida=20, tokens_total=70),
        modelo="mock-model",
    )
