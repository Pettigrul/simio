"""Explicit candidate state machine.

A plain transition table is enough for this pipeline; no workflow framework is needed.
"""

from __future__ import annotations

from simio.core.models import CandidateStatus as S


class InvalidTransition(RuntimeError):
    pass


TRANSITIONS: dict[S, frozenset[S]] = {
    S.DISCOVERED: frozenset({S.QUEUED, S.FAILED}),
    S.QUEUED: frozenset({S.COLLECTING, S.FAILED}),
    S.COLLECTING: frozenset({S.COLLECTED, S.QUEUED, S.FAILED}),
    S.COLLECTED: frozenset({S.ANALYZING, S.FAILED}),
    S.ANALYZING: frozenset({S.REVIEWING, S.COMPLETED, S.INCONCLUSIVE, S.COLLECTED, S.FAILED}),
    # REVIEWING -> QUEUED is the (bounded) "request new collection" path.
    S.REVIEWING: frozenset({S.COMPLETED, S.INCONCLUSIVE, S.QUEUED, S.COLLECTED, S.FAILED}),
    S.COMPLETED: frozenset(),
    S.INCONCLUSIVE: frozenset(),
    # FAILED can be re-queued explicitly by `simio resume --retry-failed`.
    S.FAILED: frozenset({S.QUEUED}),
}

TERMINAL = frozenset({S.COMPLETED, S.INCONCLUSIVE, S.FAILED})
IN_FLIGHT = frozenset({S.COLLECTING, S.ANALYZING, S.REVIEWING})

# Where an interrupted (crashed / budget-stopped) in-flight task resumes from.
RESUME_FROM: dict[S, S] = {
    S.COLLECTING: S.QUEUED,
    S.ANALYZING: S.COLLECTED,
    S.REVIEWING: S.COLLECTED,
}


def check_transition(current: S, target: S) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(f"{current} -> {target} is not allowed")
