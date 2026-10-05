from dataclasses import replace
from functools import partial

from board import EXPECTED_IDS, BoardObservation
from gamemodes import (
    BUILTIN_MODES,
    EvaluationContext,
    GameMode,
    ModeSnapshot,
    Progress,
)

WAIT_NS = 2_000_000_000


def _staged_snapshot(observation, context, stage, waited_ns=0):
    base = BUILTIN_MODES.get("full-field").preview(observation, context)
    target = 0 if stage == "Patch" else 1
    ports = tuple(
        replace(port, required=index == target) for index, port in enumerate(base.ports)
    )
    return replace(
        base,
        objective="Patch port 0, then wait two seconds.",
        stage=stage,
        progress=(
            Progress(int(ports[0].feedback == "correct"), 1, "correct")
            if stage == "Patch"
            else Progress(waited_ns, WAIT_NS, "ns")
        ),
        ports=ports,
        complete=stage == "Wait" and waited_ns >= WAIT_NS,
    )


def staged_preview(observation, context):
    return _staged_snapshot(observation, context, "Patch")


class StagedSession:
    def __init__(self):
        self._wait_started_ns: int | None = None

    def initialize(
        self, observation: BoardObservation, context: EvaluationContext, elapsed_ns: int
    ) -> ModeSnapshot:
        self._wait_started_ns = None
        return self.observe(observation, context, elapsed_ns)

    def observe(
        self, observation: BoardObservation, context: EvaluationContext, elapsed_ns: int
    ) -> ModeSnapshot:
        if (
            self._wait_started_ns is None
            and observation.identities is not None
            and observation.identities[0] == EXPECTED_IDS[0]
        ):
            self._wait_started_ns = elapsed_ns
        if self._wait_started_ns is None:
            return staged_preview(observation, context)
        return _staged_snapshot(
            observation, context, "Wait", max(0, elapsed_ns - self._wait_started_ns)
        )

    def tick(
        self, observation: BoardObservation, context: EvaluationContext, elapsed_ns: int
    ) -> ModeSnapshot:
        return self.observe(observation, context, elapsed_ns)


STAGED_MODE = GameMode(
    "staged", "Staged", "Patch, then wait.", None, StagedSession, staged_preview
)


def _fail_factory():
    raise RuntimeError("factory failed")


def _fail_preview(observation, context):
    raise RuntimeError("preview failed")


FAILING_FACTORY_MODE = replace(
    STAGED_MODE, id="failing-factory", session_factory=_fail_factory
)
FAILING_PREVIEW_MODE = replace(STAGED_MODE, id="failing-preview", preview=_fail_preview)


class FailingSession:
    def __init__(self, operation: str, malformed: bool = False):
        self.operation = operation
        self.malformed = malformed

    def _evaluate(self, operation, observation, context):
        snapshot = staged_preview(observation, context)
        if operation != self.operation:
            return snapshot
        if self.malformed:
            return replace(snapshot, ports=list(snapshot.ports))
        raise RuntimeError(f"{operation} failed")

    def initialize(self, observation, context, elapsed_ns):
        return self._evaluate("initialize", observation, context)

    def observe(self, observation, context, elapsed_ns):
        return self._evaluate("observe", observation, context)

    def tick(self, observation, context, elapsed_ns):
        return self._evaluate("tick", observation, context)


def failing_mode(operation: str, *, malformed: bool = False) -> GameMode:
    return replace(
        STAGED_MODE,
        id=f"{'malformed' if malformed else 'failing'}-{operation}",
        session_factory=partial(FailingSession, operation, malformed),
    )
