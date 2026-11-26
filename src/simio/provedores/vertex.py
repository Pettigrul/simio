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

from simio.configuracoes import Configuracoes
from simio.provedores.llm import (
    ErroTransitorioLLM,
    FonteGrounding,
    LLMIndisponivel,
    Parte,
    ParteImagem,
    ResultadoGrounding,
    Uso,
)

log = logging.getLogger(__name__)

CODIGOS_TRANSITORIOS = {408, 429, 500, 502, 503, 504}


def _uso(resposta: Any) -> Uso:
    um = getattr(resposta, "usage_metadata", None)
    if um is None:
        return Uso()
    p = int(getattr(um, "prompt_token_count", 0) or 0)
    o = int(getattr(um, "candidates_token_count", 0) or 0)
    t = int(getattr(um, "total_token_count", 0) or 0) or p + o
    return Uso(tokens_prompt=p, tokens_saida=o, tokens_total=t)


def montar_cliente(configuracoes: Configuracoes) -> Any:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover
        raise LLMIndisponivel("google-genai is not installed") from exc
    if not configuracoes.projeto_gcp:
        raise LLMIndisponivel("SIMIO_PROJETO_GCP is not set")
    kwargs: dict[str, Any] = {
        "project": configuracoes.projeto_gcp,
        "location": configuracoes.local_gcp,
        "http_options": types.HttpOptions(timeout=int(configuracoes.llm_timeout_s * 1000)),
    }
    parametros = inspect.signature(genai.Client.__init__).parameters
    # google-genai >= 3 names the Vertex backend "enterprise"; older releases use "vertexai".
    if "enterprise" in parametros:
        kwargs["enterprise"] = True
    else:
        kwargs["vertexai"] = True
    return genai.Client(**kwargs)


class ProvedorVertex:
    nome = "vertex"

    def __init__(self, configuracoes: Configuracoes, cliente: Any | None = None) -> None:
        self.configuracoes = configuracoes
        self._cliente = cliente
        self._usar_json_schema: bool | None = None

    @property
    def cliente(self) -> Any:
        if self._cliente is None:
            self._cliente = montar_cliente(self.configuracoes)
        return self._cliente

    def _traduzir_erro(self, exc: Exception) -> Exception:
        from google.genai import errors

        if isinstance(exc, errors.APIError):
            codigo = getattr(exc, "code", None)
            if codigo in CODIGOS_TRANSITORIOS:
                return ErroTransitorioLLM(f"{codigo}: {getattr(exc, 'message', exc)}")
            if codigo in (401, 403):
                return LLMIndisponivel(f"{codigo}: permission/auth error: {getattr(exc, 'message', exc)}")
            if codigo == 404:
                return LLMIndisponivel(f"404: model or endpoint not found: {getattr(exc, 'message', exc)}")
            return exc
        nome = type(exc).__name__.lower()
        if "timeout" in nome or "connect" in nome:
            return ErroTransitorioLLM(f"{type(exc).__name__}: {exc}")
        if "defaultcredentials" in nome or "refresherror" in nome:
            return LLMIndisponivel(f"credentials error: {exc}")
        return exc

    @staticmethod
    def _conteudos(partes: list[Parte]) -> list[Any]:
        from google.genai import types

        saida: list[Any] = []
        for p in partes:
            if isinstance(p, ParteImagem):
                if p.rotulo:
                    saida.append(types.Part.from_text(text=f"[imagem: {p.rotulo}]"))
                saida.append(types.Part.from_bytes(data=p.dados, mime_type=p.tipo_mime))
            else:
                saida.append(types.Part.from_text(text=p))
        return [types.Content(role="user", parts=saida)]

    async def gerar_texto(
        self,
        *,
        modelo: str,
        sistema: str,
        partes: list[Parte],
        esquema_json: dict | None,
        esquema_pydantic: type[BaseModel] | None,
    ) -> tuple[str, Uso]:
        from google.genai import errors, types

        cfg: dict[str, Any] = {
            "system_instruction": sistema,
            "temperature": self.configuracoes.llm_temperatura,
            "max_output_tokens": self.configuracoes.llm_max_tokens_saida,
        }
        if esquema_json is not None:
            cfg["response_mime_type"] = "application/json"
            suporta_json_schema = "response_json_schema" in types.GenerateContentConfig.model_fields
            if suporta_json_schema and self._usar_json_schema is not False:
                cfg["response_json_schema"] = esquema_json
            elif esquema_pydantic is not None:
                cfg["response_schema"] = esquema_pydantic
        try:
            resposta = await self.cliente.aio.models.generate_content(
                model=modelo,
                contents=self._conteudos(partes),
                config=types.GenerateContentConfig(**cfg),
            )
        except errors.ClientError as exc:
            # Some model versions reject parts of JSON Schema; fall back to SDK schema conversion.
            if exc.code == 400 and "response_json_schema" in cfg and esquema_pydantic is not None:
                log.warning(
                    "response_json_schema rejected (%s); falling back to response_schema", getattr(exc, "message", exc)
                )
                self._usar_json_schema = False
                cfg.pop("response_json_schema")
                cfg["response_schema"] = esquema_pydantic
                try:
                    resposta = await self.cliente.aio.models.generate_content(
                        model=modelo,
                        contents=self._conteudos(partes),
                        config=types.GenerateContentConfig(**cfg),
                    )
                except Exception as exc2:
                    raise self._traduzir_erro(exc2) from exc2
            else:
                raise self._traduzir_erro(exc) from exc
        except Exception as exc:
            raise self._traduzir_erro(exc) from exc
        texto = getattr(resposta, "text", None) or ""
        if not texto:
            motivo = None
            if getattr(resposta, "candidates", None):
                motivo = getattr(resposta.candidates[0], "finish_reason", None)
            raise ErroTransitorioLLM(f"empty response (finish_reason={motivo})")
        return texto, _uso(resposta)

    async def busca_grounding(self, *, modelo: str, prompt: str, dominios_excluidos: list[str]) -> ResultadoGrounding:
        from google.genai import types

        kwargs_busca: dict[str, Any] = {}
        if dominios_excluidos and "exclude_domains" in types.GoogleSearch.model_fields:
            kwargs_busca["exclude_domains"] = dominios_excluidos
        cfg = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch(**kwargs_busca))],
            temperature=1.0,
            max_output_tokens=self.configuracoes.llm_max_tokens_saida,
        )
        try:
            resposta = await self.cliente.aio.models.generate_content(model=modelo, contents=prompt, config=cfg)
        except Exception as exc:
            raise self._traduzir_erro(exc) from exc
        return interpretar_resposta_grounding(resposta, modelo)


def interpretar_resposta_grounding(resposta: Any, modelo: str) -> ResultadoGrounding:
    """Extract sources ONLY from grounding metadata (never from free model text)."""
    fontes: list[FonteGrounding] = []
    consultas: list[str] = []
    html_entrada: str | None = None
    cands = getattr(resposta, "candidates", None) or []
    gm = getattr(cands[0], "grounding_metadata", None) if cands else None
    if gm is not None:
        consultas = list(getattr(gm, "web_search_queries", None) or [])
        sep = getattr(gm, "search_entry_point", None)
        html_entrada = getattr(sep, "rendered_content", None) if sep else None
        fragmentos = list(getattr(gm, "grounding_chunks", None) or [])
        mapa_suporte: dict[int, list[str]] = {}
        for sup in getattr(gm, "grounding_supports", None) or []:
            seg = getattr(sup, "segment", None)
            texto_seg = (getattr(seg, "text", None) or "").strip()
            for idx in getattr(sup, "grounding_chunk_indices", None) or []:
                if texto_seg:
                    mapa_suporte.setdefault(int(idx), []).append(texto_seg[:500])
        for i, ch in enumerate(fragmentos):
            web = getattr(ch, "web", None)
            uri = getattr(web, "uri", None) if web else None
            if not uri:
                continue
            fontes.append(
                FonteGrounding(
                    uri=uri,
                    titulo=getattr(web, "title", None),
                    dominio=getattr(web, "domain", None),
                    textos_suporte=mapa_suporte.get(i, []),
                    indice_trecho=i,
                )
            )
    return ResultadoGrounding(
        texto=getattr(resposta, "text", None) or "",
        fontes=fontes,
        consultas_busca_web=consultas,
        html_ponto_entrada_busca=html_entrada,
        uso=_uso(resposta),
        modelo=modelo,
    )
