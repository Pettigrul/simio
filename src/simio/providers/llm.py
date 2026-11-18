"""Provider-neutral LLM interface, budget enforcement and retry policy."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from simio.core.storage import Storage

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)


class BudgetExceeded(RuntimeError):
    """The campaign's LLM request or token budget is exhausted."""


class LLMUnavailable(RuntimeError):
    """The provider is not configured / not reachable (non-transient)."""


class TransientLLMError(RuntimeError):
    """Retryable provider error (rate limit, 5xx, timeout)."""


class LLMOutputInvalid(RuntimeError):
    """The model output did not satisfy the contract even after a repair attempt."""


@dataclass
class ImagePart:
    data: bytes
    mime_type: str = "image/png"
    label: str = ""


Part = str | ImagePart


@dataclass
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class GroundedSource:
    uri: str
    title: str | None = None
    domain: str | None = None
    support_texts: list[str] = field(default_factory=list)
    chunk_index: int = 0


@dataclass
class GroundedResult:
    text: str
    sources: list[GroundedSource]
    web_search_queries: list[str]
    search_entry_point_html: str | None
    usage: Usage
    model: str


class LLMProvider(Protocol):
    name: str

    async def generate_text(
        self,
        *,
        model: str,
        system: str,
        parts: list[Part],
        json_schema: dict | None,
        pydantic_schema: type[BaseModel] | None,
    ) -> tuple[str, Usage]: ...

    async def grounded_search(self, *, model: str, prompt: str, exclude_domains: list[str]) -> GroundedResult: ...


def strip_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    return m.group(1) if m else t


class Budget:
    """Per-campaign request/token budget persisted in SQLite (survives resume)."""

    def __init__(self, storage: Storage, campaign_id: str, max_requests: int, max_tokens: int):
        self.storage = storage
        self.campaign_id = campaign_id
        self.max_requests = max_requests
        self.max_tokens = max_tokens
        self._reserved = 0
        self._reserved_tokens = 0
        self._lock = asyncio.Lock()

    def used(self) -> tuple[int, int]:
        return self.storage.usage_totals(self.campaign_id)

    async def reserve(self, est_tokens: int = 0) -> None:
        """Reserve one request and an *estimated* token cost before calling, so concurrent workers
        cannot overshoot the token cap (the estimate is settled on commit/release)."""
        async with self._lock:
            requests, tokens = self.used()
            if requests + self._reserved >= self.max_requests:
                raise BudgetExceeded(f"LLM request budget exhausted ({requests}/{self.max_requests})")
            if tokens + self._reserved_tokens + est_tokens > self.max_tokens:
                raise BudgetExceeded(
                    f"LLM token budget insufficient ({tokens} used + {self._reserved_tokens} reserved + "
                    f"~{est_tokens} estimated > {self.max_tokens})"
                )
            self._reserved += 1
            self._reserved_tokens += est_tokens

    async def commit(self, agent: str, model: str, usage: Usage, est_tokens: int = 0) -> None:
        async with self._lock:
            self._reserved = max(0, self._reserved - 1)
            self._reserved_tokens = max(0, self._reserved_tokens - est_tokens)
            self.storage.record_usage(
                self.campaign_id,
                agent,
                model,
                usage.prompt_tokens,
                usage.output_tokens,
                usage.total_tokens or usage.prompt_tokens + usage.output_tokens,
            )

    async def release(self, est_tokens: int = 0) -> None:
        async with self._lock:
            self._reserved = max(0, self._reserved - 1)
            self._reserved_tokens = max(0, self._reserved_tokens - est_tokens)


def estimate_tokens(parts: list[Part] | str, max_output_tokens: int) -> int:
    """Conservative pre-call estimate: ~3 chars/token for text, a fixed cost per image, plus the
    maximum output allowed."""
    if isinstance(parts, str):
        parts = [parts]
    text = sum(len(p) for p in parts if isinstance(p, str))
    images = sum(1 for p in parts if not isinstance(p, str))
    return text // 3 + images * 1800 + max_output_tokens


class LLMGateway:
    """Single entry point used by agents: budget + retries + schema validation + logging."""

    def __init__(
        self,
        provider: LLMProvider,
        budget: Budget,
        *,
        max_retries: int = 3,
        base_delay_s: float = 2.0,
        concurrency: int = 4,
        max_output_tokens: int = 4096,
    ) -> None:
        self.max_output_tokens = max_output_tokens
        self.provider = provider
        self.budget = budget
        self.max_retries = max_retries
        self.base_delay_s = base_delay_s
        self._sem = asyncio.Semaphore(concurrency)

    async def _call(self, agent: str, model: str, fn, est: int = 0):  # type: ignore[no-untyped-def]
        attempt = 0
        while True:
            await self.budget.reserve(est)
            try:
                async with self._sem:
                    result = await fn()
            except TransientLLMError as exc:
                await self.budget.commit(agent, model, Usage(), est)  # a failed call still counts
                attempt += 1
                if attempt > self.max_retries:
                    raise
                delay = self.base_delay_s * (2 ** (attempt - 1)) + random.uniform(0, 1)  # noqa: S311
                log.warning("transient LLM error (%s), retry %d in %.1fs", exc, attempt, delay)
                await asyncio.sleep(delay)
                continue
            except BaseException:
                await self.budget.release(est)
                raise
            usage = result[1] if isinstance(result, tuple) else result.usage
            await self.budget.commit(agent, model, usage, est)
            return result

    async def structured(self, *, agent: str, model: str, system: str, parts: list[Part], schema: type[M]) -> M:
        json_schema = schema.model_json_schema()

        async def go(p: list[Part]) -> tuple[str, Usage]:
            return await self.provider.generate_text(
                model=model,
                system=system,
                parts=p,
                json_schema=json_schema,
                pydantic_schema=schema,
            )

        text, _ = await self._call(
            agent, model, lambda: go(parts), estimate_tokens([system, *parts], self.max_output_tokens)
        )
        try:
            return schema.model_validate_json(strip_fences(text))
        except ValidationError as exc:
            log.warning("%s: invalid structured output, attempting one repair", agent)
            repair = (
                "A resposta anterior não respeitou o esquema JSON exigido. Erros de validação:\n"
                f"{_short_errors(exc)}\n"
                "Retorne SOMENTE um objeto JSON válido conforme o esquema. Resposta anterior:\n"
                f"{text[:6000]}"
            )
            text2, _ = await self._call(
                agent,
                model,
                lambda: go([*parts, repair]),
                estimate_tokens([system, *parts, repair], self.max_output_tokens),
            )
            try:
                return schema.model_validate_json(strip_fences(text2))
            except ValidationError as exc2:
                raise LLMOutputInvalid(f"{agent}: {_short_errors(exc2)}") from exc2

    async def grounded_search(
        self, *, agent: str, model: str, prompt: str, exclude_domains: list[str]
    ) -> GroundedResult:
        return await self._call(
            agent,
            model,
            lambda: self.provider.grounded_search(model=model, prompt=prompt, exclude_domains=exclude_domains),
            estimate_tokens(prompt, self.max_output_tokens),
        )


def _short_errors(exc: ValidationError) -> str:
    try:
        errs = exc.errors()[:10]
        return json.dumps([{"loc": e.get("loc"), "msg": e.get("msg")} for e in errs], ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover
        return str(exc)[:2000]
