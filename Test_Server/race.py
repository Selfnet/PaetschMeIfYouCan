import math
import threading
import time
import uuid
from collections.abc import Callable, Sequence
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


def _valid_draft(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 12
        and all(character.isprintable() for character in value)
    )


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

    def _valid_name_event_unlocked(
        self, race_id: object, player_number: object, draft: object
    ) -> bool:
        return (
            self.phase == Phase.NAME_ENTRY
            and race_id == self.race_id
            and type(player_number) is int
            and player_number in self.name_fields
            and not self.name_fields[player_number].resolved
            and _valid_draft(draft)
        )

    def set_name_draft(
        self, race_id: object, player_number: object, draft: object
    ) -> StateChange:
        with self._lock:
            expired = self._expire_if_due_unlocked()
            if expired is not None:
                return expired
            if not self._valid_name_event_unlocked(race_id, player_number, draft):
                return StateChange(False)
            self.name_fields[player_number].draft = draft
            self.deadline_ns = self._clock_ns() + NAME_TIMEOUT_NS
            return StateChange(True, "name_draft")

    def submit_name(
        self, race_id: object, player_number: object, draft: object
    ) -> StateChange:
        with self._lock:
            expired = self._expire_if_due_unlocked()
            if expired is not None:
                return expired
            if not self._valid_name_event_unlocked(race_id, player_number, draft):
                return StateChange(False)
            field = self.name_fields[player_number]
            field.draft = draft.strip()
            field.resolved = True
            unresolved_players = [
                candidate
                for candidate in self.result_order
                if not self.name_fields[candidate].resolved
            ]
            if unresolved_players:
                self.active_name_player = unresolved_players[0]
                self.deadline_ns = self._clock_ns() + NAME_TIMEOUT_NS
                return StateChange(True, "next_name")
            return self._enter_leaderboard_unlocked()

    def _enter_leaderboard_unlocked(self) -> StateChange:
        if self.race_id is None:
            return StateChange(False)
        now_ns = self._clock_ns()
        submissions = tuple(
            ScoreSubmission(
                race_id=self.race_id,
                player_number=player_number,
                name=self.name_fields[player_number].draft,
                duration_ms=self._duration_ms_unlocked(player_number, now_ns),
            )
            for player_number in self.result_order
            if self.name_fields[player_number].resolved
            and self.name_fields[player_number].draft
        )
        self.phase = Phase.LEADERBOARD
        self.active_name_player = None
        self.deadline_ns = now_ns + LEADERBOARD_TIMEOUT_NS
        self.leaderboard_rows = []
        self.persistence_error = None
        return StateChange(True, "leaderboard", submissions)

    def tick(self) -> StateChange:
        with self._lock:
            return self._expire_if_due_unlocked() or StateChange(False)

    def _expire_if_due_unlocked(self) -> StateChange | None:
        if self.deadline_ns is None or self._clock_ns() < self.deadline_ns:
            return None
        if self.phase == Phase.NAME_ENTRY:
            for field in self.name_fields.values():
                if not field.resolved:
                    field.draft = ""
                    field.resolved = True
            return self._enter_leaderboard_unlocked()
        if self.phase == Phase.LEADERBOARD:
            self._reset_unlocked()
            return StateChange(True, "reset")
        return None

    def set_leaderboard(
        self,
        race_id: object,
        rows: Sequence[dict[str, object]],
        highlighted_ids: set[int],
        persistence_error: str | None,
    ) -> StateChange:
        with self._lock:
            if self.phase != Phase.LEADERBOARD or race_id != self.race_id:
                return StateChange(False)
            self.leaderboard_rows = [
                {**row, "current_race": row.get("id") in highlighted_ids}
                for row in rows
            ]
            self.persistence_error = persistence_error
            return StateChange(True, "leaderboard_updated")

    def dismiss_leaderboard(self, race_id: object) -> StateChange:
        with self._lock:
            if self.phase != Phase.LEADERBOARD or race_id != self.race_id:
                return StateChange(False)
            self._reset_unlocked()
            return StateChange(True, "reset")

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
