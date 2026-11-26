from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from simio.nucleo.armazenamento import Armazenamento

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)


class OrcamentoEsgotado(RuntimeError):
    """Acabou o orçamento de requisições ou de tokens do LLM da campanha."""


class LLMIndisponivel(RuntimeError):
    """Provedor não configurado ou inacessível (erro que não passa tentando de novo)."""


class ErroTransitorioLLM(RuntimeError):
    """Erro que vale tentar de novo (limite de taxa, 5xx, timeout)."""


class SaidaLLMInvalida(RuntimeError):
    """A saída do modelo não respeitou o esquema nem depois da tentativa de reparo."""


@dataclass
class ParteImagem:
    dados: bytes
    tipo_mime: str = "image/png"
    rotulo: str = ""


Parte = str | ParteImagem


@dataclass
class Uso:
    tokens_prompt: int = 0
    tokens_saida: int = 0
    tokens_total: int = 0


@dataclass
class FonteGrounding:
    uri: str
    titulo: str | None = None
    dominio: str | None = None
    textos_suporte: list[str] = field(default_factory=list)
    indice_trecho: int = 0


@dataclass
class ResultadoGrounding:
    texto: str
    fontes: list[FonteGrounding]
    consultas_busca_web: list[str]
    html_ponto_entrada_busca: str | None
    uso: Uso
    modelo: str


class ProvedorLLM(Protocol):
    nome: str

    async def gerar_texto(
        self,
        *,
        modelo: str,
        sistema: str,
        partes: list[Parte],
        esquema_json: dict | None,
        esquema_pydantic: type[BaseModel] | None,
    ) -> tuple[str, Uso]: ...

    async def busca_grounding(
        self, *, modelo: str, prompt: str, dominios_excluidos: list[str]
    ) -> ResultadoGrounding: ...


def remover_cercas(texto: str) -> str:
    t = texto.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    return m.group(1) if m else t


class Orcamento:
    # Fica no SQLite para valer também depois de `simio retomar`.

    def __init__(self, armazenamento: Armazenamento, id_campanha: str, max_requisicoes: int, max_tokens: int):
        self.armazenamento = armazenamento
        self.id_campanha = id_campanha
        self.max_requisicoes = max_requisicoes
        self.max_tokens = max_tokens
        self._reservadas = 0
        self._tokens_reservados = 0
        self._trava = asyncio.Lock()

    def usado(self) -> tuple[int, int]:
        return self.armazenamento.totais_uso(self.id_campanha)

    async def reservar(self, tokens_est: int = 0) -> None:
        # Reserva antes da chamada uma requisição e o custo estimado de tokens, para que workers em
        # paralelo não estourem o teto. A estimativa é acertada em efetivar/liberar.
        async with self._trava:
            requisicoes, tokens = self.usado()
            if requisicoes + self._reservadas >= self.max_requisicoes:
                raise OrcamentoEsgotado(
                    f"orçamento de requisições ao LLM esgotado ({requisicoes}/{self.max_requisicoes})"
                )
            if tokens + self._tokens_reservados + tokens_est > self.max_tokens:
                raise OrcamentoEsgotado(
                    f"orçamento de tokens do LLM insuficiente ({tokens} usados + {self._tokens_reservados} "
                    f"reservados + ~{tokens_est} estimados > {self.max_tokens})"
                )
            self._reservadas += 1
            self._tokens_reservados += tokens_est

    async def efetivar(self, agente: str, modelo: str, uso: Uso, tokens_est: int = 0) -> None:
        async with self._trava:
            self._reservadas = max(0, self._reservadas - 1)
            self._tokens_reservados = max(0, self._tokens_reservados - tokens_est)
            self.armazenamento.registrar_uso(
                self.id_campanha,
                agente,
                modelo,
                uso.tokens_prompt,
                uso.tokens_saida,
                uso.tokens_total or uso.tokens_prompt + uso.tokens_saida,
            )

    async def liberar(self, tokens_est: int = 0) -> None:
        async with self._trava:
            self._reservadas = max(0, self._reservadas - 1)
            self._tokens_reservados = max(0, self._tokens_reservados - tokens_est)


def estimar_tokens(partes: list[Parte] | str, max_tokens_saida: int) -> int:
    # Estimativa conservadora: ~3 caracteres por token, custo fixo por imagem e a saída máxima.
    if isinstance(partes, str):
        partes = [partes]
    texto = sum(len(p) for p in partes if isinstance(p, str))
    imagens = sum(1 for p in partes if not isinstance(p, str))
    return texto // 3 + imagens * 1800 + max_tokens_saida


class GatewayLLM:
    # Toda chamada dos agentes passa por aqui: orçamento, novas tentativas e validação do esquema.

    def __init__(
        self,
        provedor: ProvedorLLM,
        orcamento: Orcamento,
        *,
        max_retentativas: int = 3,
        atraso_base_s: float = 2.0,
        concorrencia: int = 4,
        max_tokens_saida: int = 4096,
    ) -> None:
        self.max_tokens_saida = max_tokens_saida
        self.provedor = provedor
        self.orcamento = orcamento
        self.max_retentativas = max_retentativas
        self.atraso_base_s = atraso_base_s
        self._semaforo = asyncio.Semaphore(concorrencia)

    async def _chamar(self, agente: str, modelo: str, fn, est: int = 0):  # type: ignore[no-untyped-def]
        tentativa = 0
        while True:
            await self.orcamento.reservar(est)
            try:
                async with self._semaforo:
                    retorno = await fn()
            except ErroTransitorioLLM as exc:
                await self.orcamento.efetivar(agente, modelo, Uso(), est)  # chamada que falhou também conta
                tentativa += 1
                if tentativa > self.max_retentativas:
                    raise
                atraso = self.atraso_base_s * (2 ** (tentativa - 1)) + random.uniform(0, 1)  # noqa: S311
                log.warning("erro transitório no LLM (%s), nova tentativa %d em %.1fs", exc, tentativa, atraso)
                await asyncio.sleep(atraso)
                continue
            except BaseException:
                await self.orcamento.liberar(est)
                raise
            uso = retorno[1] if isinstance(retorno, tuple) else retorno.uso
            await self.orcamento.efetivar(agente, modelo, uso, est)
            return retorno

    async def estruturada(self, *, agente: str, modelo: str, sistema: str, partes: list[Parte], esquema: type[M]) -> M:
        esquema_json = esquema.model_json_schema()

        async def chamar(p: list[Parte]) -> tuple[str, Uso]:
            return await self.provedor.gerar_texto(
                modelo=modelo,
                sistema=sistema,
                partes=p,
                esquema_json=esquema_json,
                esquema_pydantic=esquema,
            )

        texto, _ = await self._chamar(
            agente, modelo, lambda: chamar(partes), estimar_tokens([sistema, *partes], self.max_tokens_saida)
        )
        try:
            return esquema.model_validate_json(remover_cercas(texto))
        except ValidationError as exc:
            log.warning("%s: saída estruturada inválida, tentando um reparo", agente)
            reparo = (
                "A resposta anterior não respeitou o esquema JSON exigido. Erros de validação:\n"
                f"{_erros_resumidos(exc)}\n"
                "Retorne SOMENTE um objeto JSON válido conforme o esquema. Resposta anterior:\n"
                f"{texto[:6000]}"
            )
            texto2, _ = await self._chamar(
                agente,
                modelo,
                lambda: chamar([*partes, reparo]),
                estimar_tokens([sistema, *partes, reparo], self.max_tokens_saida),
            )
            try:
                return esquema.model_validate_json(remover_cercas(texto2))
            except ValidationError as exc2:
                raise SaidaLLMInvalida(f"{agente}: {_erros_resumidos(exc2)}") from exc2

    async def busca_grounding(
        self, *, agente: str, modelo: str, prompt: str, dominios_excluidos: list[str]
    ) -> ResultadoGrounding:
        return await self._chamar(
            agente,
            modelo,
            lambda: self.provedor.busca_grounding(modelo=modelo, prompt=prompt, dominios_excluidos=dominios_excluidos),
            estimar_tokens(prompt, self.max_tokens_saida),
        )


def _erros_resumidos(exc: ValidationError) -> str:
    try:
        erros = exc.errors()[:10]
        return json.dumps([{"loc": e.get("loc"), "msg": e.get("msg")} for e in erros], ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover
        return str(exc)[:2000]
