from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from board import EXPECTED_IDS, NUM_PORTS, OPEN_ID, BoardObservation


@dataclass(frozen=True)
class EvaluationContext:
    verify: bool


@dataclass(frozen=True)
class PortPresentation:
    required: bool
    feedback: str


@dataclass(frozen=True)
class Progress:
    current: int
    total: int
    unit: str


@dataclass(frozen=True)
class LedState:
    yellow: bool
    green: bool


@dataclass(frozen=True)
class ModeSnapshot:
    objective: str
    stage: str | None
    progress: Progress | None
    ports: tuple[PortPresentation, ...]
    complete: bool
    led_intent: tuple[LedState, ...] | None = None


class ModeSession(Protocol):
    def initialize(
        self, observation: BoardObservation, context: EvaluationContext, elapsed_ns: int
    ) -> ModeSnapshot: ...

    def observe(
        self, observation: BoardObservation, context: EvaluationContext, elapsed_ns: int
    ) -> ModeSnapshot: ...

    def tick(
        self, observation: BoardObservation, context: EvaluationContext, elapsed_ns: int
    ) -> ModeSnapshot: ...


@dataclass(frozen=True)
class ModeMetadata:
    id: str
    label: str
    description: str
    required_port_count: int | None


@dataclass(frozen=True)
class GameMode:
    id: str
    label: str
    description: str
    required_port_count: int | None
    session_factory: Callable[[], ModeSession]
    preview: Callable[[BoardObservation, EvaluationContext], ModeSnapshot]

    def new_session(self) -> ModeSession:
        return self.session_factory()


def _nonempty_text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def validate_snapshot(snapshot: object) -> ModeSnapshot:
    if type(snapshot) is not ModeSnapshot:
        raise ValueError("expected ModeSnapshot")
    if not _nonempty_text(snapshot.objective):
        raise ValueError("expected nonempty objective")
    if snapshot.stage is not None and type(snapshot.stage) is not str:
        raise ValueError("expected optional string stage")
    if snapshot.progress is not None:
        progress = snapshot.progress
        if (
            type(progress) is not Progress
            or type(progress.current) is not int
            or progress.current < 0
            or type(progress.total) is not int
            or progress.total <= 0
            or not _nonempty_text(progress.unit)
        ):
            raise ValueError("expected valid Progress")
    if type(snapshot.ports) is not tuple or len(snapshot.ports) != NUM_PORTS:
        raise ValueError("expected tuple of 48 port presentations")
    for port in snapshot.ports:
        if (
            type(port) is not PortPresentation
            or type(port.required) is not bool
            or type(port.feedback) is not str
            or port.feedback not in ("open", "wrong", "correct", "unknown")
        ):
            raise ValueError("expected valid PortPresentation")
    if type(snapshot.complete) is not bool:
        raise ValueError("expected boolean completion")
    if snapshot.led_intent is not None:
        if (
            type(snapshot.led_intent) is not tuple
            or len(snapshot.led_intent) != NUM_PORTS
        ):
            raise ValueError("expected optional tuple of 48 LED states")
        for led in snapshot.led_intent:
            if (
                type(led) is not LedState
                or type(led.yellow) is not bool
                or type(led.green) is not bool
            ):
                raise ValueError("expected valid LedState")
    return snapshot


class ModeRegistry:
    def __init__(self, definitions: Sequence[GameMode]):
        self._modes: dict[str, GameMode] = {}
        for mode in definitions:
            if type(mode) is not GameMode:
                raise ValueError("expected GameMode definition")
            if not all(
                _nonempty_text(value)
                for value in (mode.id, mode.label, mode.description)
            ):
                raise ValueError("expected nonempty mode ID, label, and description")
            if mode.id in self._modes:
                raise ValueError(f"duplicate mode ID: {mode.id}")
            if not callable(mode.session_factory) or not callable(mode.preview):
                raise ValueError("expected callable session factory and preview")  # noqa: TRY004 - registry rejection contract
            count = mode.required_port_count
            if count is not None and (type(count) is not int or count <= 0):
                raise ValueError("expected optional positive integer port count")
            self._modes[mode.id] = mode

    def get(self, mode_id: str) -> GameMode:
        if type(mode_id) is not str or mode_id not in self._modes:
            raise ValueError("unknown mode ID")
        return self._modes[mode_id]

    def metadata(self) -> tuple[ModeMetadata, ...]:
        return tuple(
            ModeMetadata(
                mode.id, mode.label, mode.description, mode.required_port_count
            )
            for mode in self._modes.values()
        )


def _feedback(value: int, expected: int, context: EvaluationContext) -> str:
    if value == OPEN_ID:
        return "open"
    if not context.verify or value == expected:
        return "correct"
    return "wrong"


def _evaluate_field(
    observation: BoardObservation,
    context: EvaluationContext,
    count: int,
    objective: str,
) -> ModeSnapshot:
    identities = observation.identities
    ports = tuple(
        PortPresentation(
            required=index < count,
            feedback=(
                "unknown"
                if identities is None
                else _feedback(identities[index], EXPECTED_IDS[index], context)
            ),
        )
        for index in range(NUM_PORTS)
    )
    correct = sum(port.required and port.feedback == "correct" for port in ports)
    return ModeSnapshot(
        objective,
        None,
        Progress(correct, count, "correct"),
        ports,
        identities is not None and correct == count,
    )


class _FieldSession:
    def __init__(self, count: int, objective: str):
        self._count = count
        self._objective = objective

    def initialize(
        self, observation: BoardObservation, context: EvaluationContext, elapsed_ns: int
    ) -> ModeSnapshot:
        return _evaluate_field(observation, context, self._count, self._objective)

    def observe(
        self, observation: BoardObservation, context: EvaluationContext, elapsed_ns: int
    ) -> ModeSnapshot:
        return _evaluate_field(observation, context, self._count, self._objective)

    def tick(
        self, observation: BoardObservation, context: EvaluationContext, elapsed_ns: int
    ) -> ModeSnapshot:
        return _evaluate_field(observation, context, self._count, self._objective)


BUILTIN_MODES = ModeRegistry(
    tuple(
        GameMode(
            mode_id,
            label,
            description,
            count,
            partial(_FieldSession, count, description),
            partial(_evaluate_field, count=count, objective=description),
        )
        for mode_id, label, description, count in (
            ("full-field", "Full Field", "Patch all 24 columns.", 48),
            ("half-field", "Half Field", "Patch the leftmost 12 columns.", 24),
            ("quarter-field", "Quarter Field", "Patch the leftmost six columns.", 12),
        )
    )
)
