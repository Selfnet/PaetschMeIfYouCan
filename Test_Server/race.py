import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

NUM_CELLS = 48
NAME_TIMEOUT_NS = 120 * 1_000_000_000
LEADERBOARD_TIMEOUT_NS = 60 * 1_000_000_000


class Phase(StrEnum):
    READY = "ready"
    RACING = "racing"
    STOPPED = "stopped"
    NAME_ENTRY = "name_entry"
    LEADERBOARD = "leaderboard"


@dataclass
class PlayerRaceState:
    completed_ns: int | None = None
    manual_stopped_ns: int | None = None


@dataclass(frozen=True)
class ScoreSubmission:
    race_id: str
    player_number: int
    name: str
    duration_ms: int


@dataclass(frozen=True)
class StateChange:
    changed: bool
    action: str | None = None
    submissions: tuple[ScoreSubmission, ...] = ()


@dataclass
class NameFieldState:
    draft: str = ""
    resolved: bool = False


class RaceStateMachine:
    def __init__(
        self,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        race_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._clock_ns = clock_ns
        self._race_id_factory = race_id_factory
        self._lock = threading.RLock()
        self._reset_unlocked()

    def _reset_unlocked(self) -> None:
        self.phase = Phase.READY
        self.race_id: str | None = None
        self.started_ns: int | None = None
        self.players = {1: PlayerRaceState(), 2: PlayerRaceState()}
        self.result_order: list[int] = []
        self.tie = False
        self.name_fields = {1: NameFieldState(), 2: NameFieldState()}
        self.active_name_player: int | None = None
        self.deadline_ns: int | None = None
        self.leaderboard_rows: list[dict[str, object]] = []
        self.persistence_error: str | None = None

    def _start_unlocked(self) -> StateChange:
        self._reset_unlocked()
        self.phase = Phase.RACING
        self.race_id = self._race_id_factory()
        self.started_ns = self._clock_ns()
        return StateChange(True, "started")

    def start(self) -> StateChange:
        with self._lock:
            if self.phase != Phase.READY:
                return StateChange(False)
            return self._start_unlocked()

    def space(self) -> StateChange:
        with self._lock:
            if self.phase == Phase.READY:
                return self._start_unlocked()
            if self.phase == Phase.RACING:
                now_ns = self._clock_ns()
                for player in self.players.values():
                    if player.completed_ns is None and player.manual_stopped_ns is None:
                        player.manual_stopped_ns = now_ns
                self.phase = Phase.STOPPED
                self.deadline_ns = None
                return StateChange(True, "stopped")
            if self.phase == Phase.STOPPED:
                self._reset_unlocked()
                return StateChange(True, "reset")
            return StateChange(False)

    def manual_stop(self, player_number: int) -> StateChange:
        with self._lock:
            if (
                self.phase != Phase.RACING
                or type(player_number) is not int
                or player_number not in self.players
            ):
                return StateChange(False)
            player = self.players[player_number]
            if player.completed_ns is not None or player.manual_stopped_ns is not None:
                return StateChange(False)
            player.manual_stopped_ns = self._clock_ns()
            if all(
                item.manual_stopped_ns is not None for item in self.players.values()
            ):
                self.phase = Phase.STOPPED
                return StateChange(True, "stopped")
            return StateChange(True, "player_stopped")

    def observe_cells(self, player_number: int, states: object) -> StateChange:
        with self._lock:
            valid_states = (
                isinstance(states, (list, tuple))
                and len(states) == NUM_CELLS
                and all(type(state) is int and state == 2 for state in states)
            )
            if (
                self.phase != Phase.RACING
                or type(player_number) is not int
                or player_number not in self.players
                or not valid_states
            ):
                return StateChange(False)
            player = self.players[player_number]
            if player.completed_ns is not None:
                return StateChange(False)
            player.completed_ns = self._clock_ns()
            player.manual_stopped_ns = None
            if all(item.completed_ns is not None for item in self.players.values()):
                return self._enter_name_entry_unlocked()
            return StateChange(True, "player_completed")

    def _duration_ms_unlocked(self, player_number: int, now_ns: int) -> int:
        if self.started_ns is None:
            return 0
        player = self.players[player_number]
        endpoint_ns = player.completed_ns
        if endpoint_ns is None:
            endpoint_ns = player.manual_stopped_ns
        if endpoint_ns is None:
            endpoint_ns = now_ns
        return max(0, (endpoint_ns - self.started_ns) // 1_000_000)

    def _enter_name_entry_unlocked(self) -> StateChange:
        now_ns = self._clock_ns()
        durations = {
            player_number: self._duration_ms_unlocked(player_number, now_ns)
            for player_number in (1, 2)
        }
        self.tie = durations[1] == durations[2]
        self.result_order = (
            [1, 2]
            if self.tie
            else sorted((1, 2), key=lambda player_number: durations[player_number])
        )
        self.name_fields = {1: NameFieldState(), 2: NameFieldState()}
        self.active_name_player = self.result_order[0]
        self.deadline_ns = now_ns + NAME_TIMEOUT_NS
        self.phase = Phase.NAME_ENTRY
        return StateChange(True, "name_entry")

    def reset(self) -> StateChange:
        with self._lock:
            self._reset_unlocked()
            return StateChange(True, "reset")

    def _remaining_seconds_unlocked(self, now_ns: int) -> int | None:
        if self.deadline_ns is None:
            return None
        return math.ceil(max(0, self.deadline_ns - now_ns) / 1_000_000_000)

    def clock_payload(self) -> dict[str, object]:
        with self._lock:
            now_ns = self._clock_ns()
            return {
                "race_id": self.race_id,
                "phase": self.phase.value,
                "durations_ms": {
                    str(player_number): self._duration_ms_unlocked(
                        player_number, now_ns
                    )
                    for player_number in (1, 2)
                },
                "active_players": {
                    str(player_number): (
                        self.phase == Phase.RACING
                        and self.players[player_number].completed_ns is None
                        and self.players[player_number].manual_stopped_ns is None
                    )
                    for player_number in (1, 2)
                },
            }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            now_ns = self._clock_ns()
            players = {
                str(player_number): {
                    "duration_ms": self._duration_ms_unlocked(player_number, now_ns),
                    "completed": player.completed_ns is not None,
                    "manual_stopped": player.manual_stopped_ns is not None,
                    "clock_active": (
                        self.phase == Phase.RACING
                        and player.completed_ns is None
                        and player.manual_stopped_ns is None
                    ),
                }
                for player_number, player in self.players.items()
            }
            return {
                "phase": self.phase.value,
                "race_id": self.race_id,
                "players": players,
                "result_order": list(self.result_order),
                "tie": self.tie,
                "name_entry": {
                    "active_player": self.active_name_player,
                    "drafts": {
                        str(player_number): field.draft
                        for player_number, field in self.name_fields.items()
                    },
                    "resolved": {
                        str(player_number): field.resolved
                        for player_number, field in self.name_fields.items()
                    },
                },
                "remaining_seconds": self._remaining_seconds_unlocked(now_ns),
                "leaderboard": [dict(row) for row in self.leaderboard_rows],
                "persistence_error": self.persistence_error,
            }
