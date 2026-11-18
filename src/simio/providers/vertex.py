"""Gemini on Vertex AI via the official ``google-genai`` SDK.

Authentication uses Application Default Credentials (``gcloud auth application-default login``
or a service account configured by the organisation). No credentials are read or stored by
SIMIO itself, and no unofficial session extraction (e.g. from Antigravity) is performed.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from pydantic import BaseModel

from simio.config import Settings
from simio.providers.llm import (
    GroundedResult,
    GroundedSource,
    ImagePart,
    LLMUnavailable,
    Part,
    TransientLLMError,
    Usage,
)

log = logging.getLogger(__name__)

TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}


def _usage(resp: Any) -> Usage:
    um = getattr(resp, "usage_metadata", None)
    if um is None:
        return Usage()
    p = int(getattr(um, "prompt_token_count", 0) or 0)
    o = int(getattr(um, "candidates_token_count", 0) or 0)
    t = int(getattr(um, "total_token_count", 0) or 0) or p + o
    return Usage(prompt_tokens=p, output_tokens=o, total_tokens=t)


def build_client(settings: Settings) -> Any:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover
        raise LLMUnavailable("google-genai is not installed") from exc
    if not settings.gcp_project:
        raise LLMUnavailable("SIMIO_GCP_PROJECT is not set")
    kwargs: dict[str, Any] = {
        "project": settings.gcp_project,
        "location": settings.gcp_location,
        "http_options": types.HttpOptions(timeout=int(settings.llm_timeout_s * 1000)),
    }
    params = inspect.signature(genai.Client.__init__).parameters
    # google-genai >= 3 names the Vertex backend "enterprise"; older releases use "vertexai".
    if "enterprise" in params:
        kwargs["enterprise"] = True
    else:
        kwargs["vertexai"] = True
    return genai.Client(**kwargs)


class VertexProvider:
    name = "vertex"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client
        self._use_json_schema: bool | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = build_client(self.settings)
        return self._client

    def _translate(self, exc: Exception) -> Exception:
        from google.genai import errors

        if isinstance(exc, errors.APIError):
            code = getattr(exc, "code", None)
            if code in TRANSIENT_CODES:
                return TransientLLMError(f"{code}: {getattr(exc, 'message', exc)}")
            if code in (401, 403):
                return LLMUnavailable(f"{code}: permission/auth error: {getattr(exc, 'message', exc)}")
            if code == 404:
                return LLMUnavailable(f"404: model or endpoint not found: {getattr(exc, 'message', exc)}")
            return exc
        name = type(exc).__name__.lower()
        if "timeout" in name or "connect" in name:
            return TransientLLMError(f"{type(exc).__name__}: {exc}")
        if "defaultcredentials" in name or "refresherror" in name:
            return LLMUnavailable(f"credentials error: {exc}")
        return exc

    @staticmethod
    def _contents(parts: list[Part]) -> list[Any]:
        from google.genai import types

        out: list[Any] = []
        for p in parts:
            if isinstance(p, ImagePart):
                if p.label:
                    out.append(types.Part.from_text(text=f"[imagem: {p.label}]"))
                out.append(types.Part.from_bytes(data=p.data, mime_type=p.mime_type))
            else:
                out.append(types.Part.from_text(text=p))
        return [types.Content(role="user", parts=out)]

    async def generate_text(
        self,
        *,
        model: str,
        system: str,
        parts: list[Part],
        json_schema: dict | None,
        pydantic_schema: type[BaseModel] | None,
    ) -> tuple[str, Usage]:
        from google.genai import errors, types

        cfg: dict[str, Any] = {
            "system_instruction": system,
            "temperature": self.settings.llm_temperature,
            "max_output_tokens": self.settings.llm_max_output_tokens,
        }
        if json_schema is not None:
            cfg["response_mime_type"] = "application/json"
            supports_json_schema = "response_json_schema" in types.GenerateContentConfig.model_fields
            if supports_json_schema and self._use_json_schema is not False:
                cfg["response_json_schema"] = json_schema
            elif pydantic_schema is not None:
                cfg["response_schema"] = pydantic_schema
        try:
            resp = await self.client.aio.models.generate_content(
                model=model,
                contents=self._contents(parts),
                config=types.GenerateContentConfig(**cfg),
            )
        except errors.ClientError as exc:
            # Some model versions reject parts of JSON Schema; fall back to SDK schema conversion.
            if exc.code == 400 and "response_json_schema" in cfg and pydantic_schema is not None:
                log.warning(
                    "response_json_schema rejected (%s); falling back to response_schema", getattr(exc, "message", exc)
                )
                self._use_json_schema = False
                cfg.pop("response_json_schema")
                cfg["response_schema"] = pydantic_schema
                try:
                    resp = await self.client.aio.models.generate_content(
                        model=model,
                        contents=self._contents(parts),
                        config=types.GenerateContentConfig(**cfg),
                    )
                except Exception as exc2:
                    raise self._translate(exc2) from exc2
            else:
                raise self._translate(exc) from exc
        except Exception as exc:
            raise self._translate(exc) from exc
        text = getattr(resp, "text", None) or ""
        if not text:
            reason = None
            if getattr(resp, "candidates", None):
                reason = getattr(resp.candidates[0], "finish_reason", None)
            raise TransientLLMError(f"empty response (finish_reason={reason})")
        return text, _usage(resp)

    async def grounded_search(self, *, model: str, prompt: str, exclude_domains: list[str]) -> GroundedResult:
        from google.genai import types

        search_kwargs: dict[str, Any] = {}
        if exclude_domains and "exclude_domains" in types.GoogleSearch.model_fields:
            search_kwargs["exclude_domains"] = exclude_domains
        cfg = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch(**search_kwargs))],
            temperature=1.0,
            max_output_tokens=self.settings.llm_max_output_tokens,
        )
        try:
            resp = await self.client.aio.models.generate_content(model=model, contents=prompt, config=cfg)
        except Exception as exc:
            raise self._translate(exc) from exc
        return parse_grounded_response(resp, model)


def parse_grounded_response(resp: Any, model: str) -> GroundedResult:
    """Extract sources ONLY from grounding metadata (never from free model text)."""
    sources: list[GroundedSource] = []
    queries: list[str] = []
    entry_html: str | None = None
    cands = getattr(resp, "candidates", None) or []
    gm = getattr(cands[0], "grounding_metadata", None) if cands else None
    if gm is not None:
        queries = list(getattr(gm, "web_search_queries", None) or [])
        sep = getattr(gm, "search_entry_point", None)
        entry_html = getattr(sep, "rendered_content", None) if sep else None
        chunks = list(getattr(gm, "grounding_chunks", None) or [])
        support_map: dict[int, list[str]] = {}
        for sup in getattr(gm, "grounding_supports", None) or []:
            seg = getattr(sup, "segment", None)
            seg_text = (getattr(seg, "text", None) or "").strip()
            for idx in getattr(sup, "grounding_chunk_indices", None) or []:
                if seg_text:
                    support_map.setdefault(int(idx), []).append(seg_text[:500])
        for i, ch in enumerate(chunks):
            web = getattr(ch, "web", None)
            uri = getattr(web, "uri", None) if web else None
            if not uri:
                continue
            sources.append(
                GroundedSource(
                    uri=uri,
                    title=getattr(web, "title", None),
                    domain=getattr(web, "domain", None),
                    support_texts=support_map.get(i, []),
                    chunk_index=i,
                )
            )
    return GroundedResult(
        text=getattr(resp, "text", None) or "",
        sources=sources,
        web_search_queries=queries,
        search_entry_point_html=entry_html,
        usage=_usage(resp),
        model=model,
    )
