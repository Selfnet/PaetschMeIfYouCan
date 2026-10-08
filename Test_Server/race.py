import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum

from board import GameBoard
from gamemodes import BUILTIN_MODES, EvaluationContext, ModeRegistry, validate_snapshot

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
    mode_id: str
    player_number: int
    name: str
    duration_ms: int
    leaderboard_slot: int = 1


@dataclass(frozen=True)
class RaceOrigin:
    race_id: str
    mode_id: str
    leaderboard_slot: int = 1


@dataclass(frozen=True)
class BrowseOrigin:
    epoch: int
    phase: Phase
    mode_id: str
    race_id: str | None
    leaderboard_slot: int = 1


@dataclass(frozen=True)
class StateChange:
    changed: bool
    action: str | None = None
    submissions: tuple[ScoreSubmission, ...] = ()
    origin: RaceOrigin | None = None
    diagnostic: str | None = None


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
        registry: ModeRegistry = BUILTIN_MODES,
        leaderboard_slot: int = 1,
    ) -> None:
        self._clock_ns = clock_ns
        self._race_id_factory = race_id_factory
        self._lock = threading.RLock()
        self.registry = registry
        self.selected_mode_id = "full-field"
        if type(leaderboard_slot) is not int or not 1 <= leaderboard_slot <= 9:
            raise ValueError("leaderboard_slot must be an integer between 1 and 9")
        self.leaderboard_slot = leaderboard_slot
        self.registry.get(self.selected_mode_id)
        self.boards = {1: GameBoard(), 2: GameBoard()}
        self.context = EvaluationContext(verify=True)
        self.epoch = 0
        self.revision = 0
        self.presentations = {1: None, 2: None}
        self._reset_unlocked()

    def _change_unlocked(self, action, *, submissions=(), origin=None, diagnostic=None):
        self.revision += 1
        return StateChange(True, action, submissions, origin, diagnostic)

    def _preview_unlocked(self):
        try:
            mode = self.registry.get(self.selected_mode_id)
            staged = {
                number: validate_snapshot(mode.preview(board.snapshot(), self.context))
                for number, board in self.boards.items()
            }
        except Exception as error:  # noqa: BLE001 - mode failures must remain recoverable
            self.mode_error = "Mode preview failed. Select another mode or reset."
            self.presentations = {1: None, 2: None}
            return f"{type(error).__name__}: {error}"
        self.presentations = staged
        self.mode_error = None
        return None

    def _reset_unlocked(self):
        self.epoch += 1
        self.phase = Phase.READY
        self.race_id: str | None = None
        self.started_ns: int | None = None
        self.players = {1: PlayerRaceState(), 2: PlayerRaceState()}
        self.result_order: list[int] = []
        self.tie = False
        self.name_fields = {1: NameFieldState(), 2: NameFieldState()}
        self.active_name_player: int | None = None
        self.deadline_ns: int | None = None
        self.sessions = {}
        self.race_mode_id = None
        self.race_leaderboard_slot = None
        self.mode_error = None
        self.persistence_status = "idle"
        self.persistence_error: str | None = None
        return self._preview_unlocked()

    def _failure_unlocked(self, error, now_ns):
        for player in self.players.values():
            if player.completed_ns is None and player.manual_stopped_ns is None:
                player.manual_stopped_ns = now_ns
        self.phase = Phase.STOPPED
        self.epoch += 1
        self.deadline_ns = None
        self.sessions = {}
        self.result_order = []
        self.mode_error = "Mode failed. Reset to try again or select another mode."
        return self._change_unlocked(
            "mode_error", diagnostic=f"{type(error).__name__}: {error}"
        )

    def _start_unlocked(self, now_ns) -> StateChange:
        if self.mode_error is not None:
            return StateChange(False)
        self.epoch += 1
        self.phase = Phase.RACING
        self.race_id = self._race_id_factory()
        self.race_mode_id = self.selected_mode_id
        self.race_leaderboard_slot = self.leaderboard_slot
        self.started_ns = now_ns
        try:
            mode = self.registry.get(self.race_mode_id)
            sessions = {number: mode.new_session() for number in (1, 2)}
            if sessions[1] is sessions[2]:
                raise ValueError("mode factory returned a shared session")
            staged = {
                number: validate_snapshot(
                    session.initialize(self.boards[number].snapshot(), self.context, 0)
                )
                for number, session in sessions.items()
            }
        except Exception as error:  # noqa: BLE001 - mode failures must remain recoverable
            return self._failure_unlocked(error, now_ns)
        self.sessions = sessions
        return self._publish_evaluations_unlocked(staged, now_ns, "started")

    def _publish_evaluations_unlocked(self, staged, now_ns, action=None):
        changed = action is not None
        for number, result in staged.items():
            changed |= self.presentations[number] != result
            self.presentations[number] = result
            if result.complete:
                self.players[number].completed_ns = now_ns
                self.players[number].manual_stopped_ns = None
                action = action or "player_completed"
                changed = True
        if all(player.completed_ns is not None for player in self.players.values()):
            return self._enter_name_entry_unlocked(now_ns)
        return (
            self._change_unlocked(action or "progress")
            if changed
            else StateChange(False)
        )

    def _evaluate_unlocked(self, method, now_ns, numbers=(1, 2), *, published=False):
        try:
            elapsed_ns = max(0, now_ns - self.started_ns)
            staged = {
                number: validate_snapshot(
                    getattr(self.sessions[number], method)(
                        self.boards[number].snapshot(), self.context, elapsed_ns
                    )
                )
                for number in numbers
                if self.players[number].completed_ns is None
            }
        except Exception as error:  # noqa: BLE001 - mode failures must remain recoverable
            return self._failure_unlocked(error, now_ns)
        return self._publish_evaluations_unlocked(
            staged, now_ns, "updated" if published else None
        )

    def start(self) -> StateChange:
        with self._lock:
            if self.phase != Phase.READY:
                return StateChange(False)
            return self._start_unlocked(self._clock_ns())

    def space(self) -> StateChange:
        with self._lock:
            now_ns = self._clock_ns()
            if self.phase == Phase.READY:
                return self._start_unlocked(now_ns)
            if self.phase == Phase.RACING:
                for player in self.players.values():
                    if player.completed_ns is None and player.manual_stopped_ns is None:
                        player.manual_stopped_ns = now_ns
                self.phase = Phase.STOPPED
                self.epoch += 1
                self.deadline_ns = None
                return self._change_unlocked("stopped")
            if self.phase == Phase.STOPPED:
                diagnostic = self._reset_unlocked()
                return self._change_unlocked("reset", diagnostic=diagnostic)
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
                self.epoch += 1
                return self._change_unlocked("stopped")
            return self._change_unlocked("player_stopped")

    def observe_board(self, player_number: int, values: object) -> StateChange:
        with self._lock:
            if type(player_number) is not int or player_number not in self.boards:
                return StateChange(False)
            before = self.boards[player_number].snapshot()
            try:
                observation = self.boards[player_number].update(values)
            except ValueError:
                return StateChange(False)
            now_ns = self._clock_ns()
            if self.phase == Phase.READY:
                previous = self.presentations.copy()
                previous_error = self.mode_error
                diagnostic = self._preview_unlocked()
                if (
                    observation != before
                    or previous != self.presentations
                    or previous_error != self.mode_error
                ):
                    return self._change_unlocked("board", diagnostic=diagnostic)
                return StateChange(False)
            if self.phase == Phase.RACING:
                return self._evaluate_unlocked(
                    "observe", now_ns, (player_number,), published=observation != before
                )
            return (
                self._change_unlocked("board")
                if observation != before
                else StateChange(False)
            )

    def select_mode(self, mode_id) -> StateChange:
        with self._lock:
            if self.phase != Phase.READY:
                return StateChange(False)
            try:
                self.registry.get(mode_id)
            except ValueError:
                return StateChange(False)
            self.selected_mode_id = mode_id
            self.epoch += 1
            diagnostic = self._preview_unlocked()
            return self._change_unlocked("mode_selected", diagnostic=diagnostic)

    def select_leaderboard_slot(self, leaderboard_slot) -> StateChange:
        with self._lock:
            if (
                self.phase != Phase.READY
                or type(leaderboard_slot) is not int
                or not 1 <= leaderboard_slot <= 9
            ):
                return StateChange(False)
            if leaderboard_slot == self.leaderboard_slot:
                return StateChange(False, action="leaderboard_slot_selected")
            self.leaderboard_slot = leaderboard_slot
            self.epoch += 1
            return self._change_unlocked("leaderboard_slot_selected")

    def set_verification(self, verify) -> StateChange:
        with self._lock:
            if type(verify) is not bool or verify == self.context.verify:
                return StateChange(False)
            self.context = EvaluationContext(verify=verify)
            now_ns = self._clock_ns()
            if self.phase == Phase.READY:
                diagnostic = self._preview_unlocked()
                return self._change_unlocked("verification", diagnostic=diagnostic)
            if self.phase == Phase.RACING:
                return self._evaluate_unlocked("observe", now_ns, published=True)
            return self._change_unlocked("verification")

    def toggle_verification(self) -> StateChange:
        with self._lock:
            return self.set_verification(not self.context.verify)

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

    def _enter_name_entry_unlocked(self, now_ns) -> StateChange:
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
        self.epoch += 1
        return self._change_unlocked("name_entry")

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
            now_ns = self._clock_ns()
            expired = self._expire_if_due_unlocked(now_ns)
            if expired is not None:
                return expired
            if not self._valid_name_event_unlocked(race_id, player_number, draft):
                return StateChange(False)
            self.name_fields[player_number].draft = draft
            self.deadline_ns = now_ns + NAME_TIMEOUT_NS
            return self._change_unlocked("name_draft")

    def submit_name(
        self, race_id: object, player_number: object, draft: object
    ) -> StateChange:
        with self._lock:
            now_ns = self._clock_ns()
            expired = self._expire_if_due_unlocked(now_ns)
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
                self.deadline_ns = now_ns + NAME_TIMEOUT_NS
                return self._change_unlocked("next_name")
            return self._enter_leaderboard_unlocked(now_ns)

    def _enter_leaderboard_unlocked(self, now_ns) -> StateChange:
        if self.race_id is None:
            return StateChange(False)
        submissions = tuple(
            ScoreSubmission(
                race_id=self.race_id,
                mode_id=self.race_mode_id,
                player_number=player_number,
                name=self.name_fields[player_number].draft,
                duration_ms=self._duration_ms_unlocked(player_number, now_ns),
                leaderboard_slot=self.race_leaderboard_slot,
            )
            for player_number in self.result_order
            if self.name_fields[player_number].resolved
            and self.name_fields[player_number].draft
        )
        self.phase = Phase.LEADERBOARD
        self.epoch += 1
        self.active_name_player = None
        self.deadline_ns = now_ns + LEADERBOARD_TIMEOUT_NS
        self.persistence_status = "pending"
        self.persistence_error = None
        return self._change_unlocked(
            "leaderboard",
            submissions=submissions,
            origin=RaceOrigin(
                race_id=self.race_id,
                mode_id=self.race_mode_id,
                leaderboard_slot=self.race_leaderboard_slot,
            ),
        )

    def tick(self) -> StateChange:
        with self._lock:
            now_ns = self._clock_ns()
            expired = self._expire_if_due_unlocked(now_ns)
            if expired is not None:
                return expired
            if self.phase == Phase.RACING:
                return self._evaluate_unlocked("tick", now_ns)
            return StateChange(False)

    def _expire_if_due_unlocked(self, now_ns) -> StateChange | None:
        if self.deadline_ns is None or now_ns < self.deadline_ns:
            return None
        if self.phase == Phase.NAME_ENTRY:
            for field in self.name_fields.values():
                if not field.resolved:
                    field.draft = ""
                    field.resolved = True
            return self._enter_leaderboard_unlocked(now_ns)
        if self.phase == Phase.LEADERBOARD:
            diagnostic = self._reset_unlocked()
            return self._change_unlocked("reset", diagnostic=diagnostic)
        return None

    def set_persistence(self, origin: RaceOrigin, error: str | None) -> StateChange:
        with self._lock:
            expired = self._expire_if_due_unlocked(self._clock_ns())
            if expired is not None:
                return expired
            if (
                type(origin) is not RaceOrigin
                or self.phase != Phase.LEADERBOARD
                or origin.race_id != self.race_id
                or origin.mode_id != self.race_mode_id
                or origin.leaderboard_slot != self.race_leaderboard_slot
            ):
                return StateChange(False)
            self.persistence_error = error
            self.persistence_status = "saved" if error is None else "error"
            return self._change_unlocked("persistence", origin=origin)

    def leaderboard_activity(self, race_id) -> StateChange:
        with self._lock:
            now_ns = self._clock_ns()
            expired = self._expire_if_due_unlocked(now_ns)
            if expired is not None:
                return expired
            if self.phase != Phase.LEADERBOARD or race_id != self.race_id:
                return StateChange(False)
            self.deadline_ns = now_ns + LEADERBOARD_TIMEOUT_NS
            return self._change_unlocked("leaderboard_activity")

    def _browse_origin_unlocked(self):
        return BrowseOrigin(
            epoch=self.epoch,
            phase=self.phase,
            mode_id=self.selected_mode_id,
            race_id=self.race_id if self.phase == Phase.LEADERBOARD else None,
            leaderboard_slot=(
                self.race_leaderboard_slot
                if self.phase == Phase.LEADERBOARD
                else self.leaderboard_slot
            ),
        )

    def capture_browse(
        self, mode_id, race_id, leaderboard_slot=1
    ) -> BrowseOrigin | None:
        """Capture eligibility; recheck after the outside-lock page read."""
        with self._lock:
            if self.phase not in (Phase.READY, Phase.STOPPED, Phase.LEADERBOARD):
                return None
            if self.phase == Phase.LEADERBOARD and self.persistence_status == "pending":
                return None
            origin = self._browse_origin_unlocked()
            if (
                mode_id != origin.mode_id
                or race_id != origin.race_id
                or type(leaderboard_slot) is not int
                or leaderboard_slot != origin.leaderboard_slot
            ):
                return None
            return origin

    def browse_is_current(self, origin) -> tuple[bool, StateChange]:
        """Return eligibility and any expiry transition for runtime publication."""
        with self._lock:
            expired = self._expire_if_due_unlocked(self._clock_ns())
            current = (
                type(origin) is BrowseOrigin
                and self.phase in (Phase.READY, Phase.STOPPED, Phase.LEADERBOARD)
                and (
                    self.phase != Phase.LEADERBOARD
                    or self.persistence_status in ("saved", "error")
                )
                and origin == self._browse_origin_unlocked()
            )
            return current, expired or StateChange(False)

    def dismiss_leaderboard(self, race_id: object) -> StateChange:
        with self._lock:
            if self.phase != Phase.LEADERBOARD or race_id != self.race_id:
                return StateChange(False)
            diagnostic = self._reset_unlocked()
            return self._change_unlocked("reset", diagnostic=diagnostic)

    def reset(self) -> StateChange:
        with self._lock:
            diagnostic = self._reset_unlocked()
            return self._change_unlocked("reset", diagnostic=diagnostic)

    def _remaining_seconds_unlocked(self, now_ns: int) -> int | None:
        if self.deadline_ns is None:
            return None
        return math.ceil(max(0, self.deadline_ns - now_ns) / 1_000_000_000)

    def clock_payload(self) -> dict[str, object]:
        with self._lock:
            now_ns = self._clock_ns()
            return {
                "revision": self.revision,
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
                    "presentation": (
                        asdict(self.presentations[player_number])
                        if self.presentations[player_number] is not None
                        else None
                    ),
                }
                for player_number, player in self.players.items()
            }
            return {
                "revision": self.revision,
                "mode": next(
                    asdict(mode)
                    for mode in self.registry.metadata()
                    if mode.id == self.selected_mode_id
                ),
                "available_modes": [asdict(mode) for mode in self.registry.metadata()],
                "browse_epoch": self.epoch,
                "leaderboard_slot": self.leaderboard_slot,
                "mode_error": self.mode_error,
                "persistence_status": self.persistence_status,
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
                "persistence_error": self.persistence_error,
            }
