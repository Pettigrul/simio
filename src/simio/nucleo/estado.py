"""Explicit candidate state machine.

A plain transition table is enough for this pipeline; no workflow framework is needed.
"""

from __future__ import annotations

from simio.nucleo.modelos import StatusCandidata as S


class TransicaoInvalida(RuntimeError):
    pass


TRANSICOES: dict[S, frozenset[S]] = {
    S.DESCOBERTA: frozenset({S.NA_FILA, S.FALHOU}),
    S.NA_FILA: frozenset({S.COLETANDO, S.FALHOU}),
    S.COLETANDO: frozenset({S.COLETADA, S.NA_FILA, S.FALHOU}),
    S.COLETADA: frozenset({S.ANALISANDO, S.FALHOU}),
    S.ANALISANDO: frozenset({S.REVISANDO, S.CONCLUIDA, S.INCONCLUSIVA, S.COLETADA, S.FALHOU}),
    # REVIEWING -> QUEUED is the (bounded) "request new collection" path.
    S.REVISANDO: frozenset({S.CONCLUIDA, S.INCONCLUSIVA, S.NA_FILA, S.COLETADA, S.FALHOU}),
    S.CONCLUIDA: frozenset(),
    S.INCONCLUSIVA: frozenset(),
    # FAILED can be re-queued explicitly by `simio retomar --repetir-falhas`.
    S.FALHOU: frozenset({S.NA_FILA}),
}

TERMINAIS = frozenset({S.CONCLUIDA, S.INCONCLUSIVA, S.FALHOU})
EM_ANDAMENTO = frozenset({S.COLETANDO, S.ANALISANDO, S.REVISANDO})

# Where an interrupted (crashed / budget-stopped) in-flight task resumes from.
RETOMAR_DE: dict[S, S] = {
    S.COLETANDO: S.NA_FILA,
    S.ANALISANDO: S.COLETADA,
    S.REVISANDO: S.COLETADA,
}


def verificar_transicao(atual: S, alvo: S) -> None:
    if alvo not in TRANSICOES[atual]:
        raise TransicaoInvalida(f"{atual} -> {alvo} is not allowed")
