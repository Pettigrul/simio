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
    # REVISANDO -> NA_FILA é o pedido de nova coleta do revisor (limitado por max_recoletas).
    S.REVISANDO: frozenset({S.CONCLUIDA, S.INCONCLUSIVA, S.NA_FILA, S.COLETADA, S.FALHOU}),
    S.CONCLUIDA: frozenset(),
    S.INCONCLUSIVA: frozenset(),
    # FALHOU só volta para a fila com `simio retomar --repetir-falhas`.
    S.FALHOU: frozenset({S.NA_FILA}),
}

TERMINAIS = frozenset({S.CONCLUIDA, S.INCONCLUSIVA, S.FALHOU})
EM_ANDAMENTO = frozenset({S.COLETANDO, S.ANALISANDO, S.REVISANDO})

# De onde recomeça uma etapa interrompida (queda do processo ou limite de uso).
RETOMAR_DE: dict[S, S] = {
    S.COLETANDO: S.NA_FILA,
    S.ANALISANDO: S.COLETADA,
    S.REVISANDO: S.COLETADA,
}


def verificar_transicao(atual: S, alvo: S) -> None:
    if alvo not in TRANSICOES[atual]:
        raise TransicaoInvalida(f"transição não permitida: {atual} -> {alvo}")
