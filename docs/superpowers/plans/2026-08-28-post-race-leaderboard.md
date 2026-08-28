# Post-Race Results and Leaderboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add server-authoritative race results, sequential optional name entry, and a persistent SQLite top-10 leaderboard to the two-player kiosk.

**Architecture:** Extract the locked race lifecycle into `race.py` and SQLite access into `leaderboard.py`; keep Flask routes, Socket.IO events, serial input, persistence coordination, and background updates in `app.py`. The browser renders one authoritative `game_state` snapshot, while the existing periodic clock updates keep the live race display smooth.

**Tech Stack:** Python 3.11+, Flask 3.1, Flask-SocketIO 5.5+, built-in `sqlite3`, vanilla HTML/CSS/JavaScript, pytest, and agent-browser for acceptance checks.

## Global Constraints

- The approved behavior source is `docs/superpowers/specs/2026-08-28-post-race-leaderboard-design.md`; implementation must not silently reinterpret it.
- Keep the server authoritative for phase, race ID, timing, result order, active name, deadlines, and leaderboard contents.
- Use `time.monotonic_ns()` and floor durations with `(completed_ns - started_ns) // 1_000_000`.
- Only exactly 48 integer cell states, all equal to `2`, complete a player; the first valid completion wins.
- The state flow is `ready -> racing -> name_entry -> leaderboard -> ready`, with the non-result branch `racing -> stopped -> ready`.
- Display the exact result copy `you were able to success`.
- Accept at most 12 printable Unicode code points; strip surrounding whitespace only when resolving a name.
- Name-entry idle timeout is 120 seconds; leaderboard timeout is 60 seconds.
- Space is normal text during `name_entry`, dismisses `leaderboard`, and controls start/stop/reset only in `ready`, `racing`, and `stopped`.
- Use competition ranks (`1, 1, 3`), order equal times by `created_at` then `id`, and return at most 10 rows.
- Default the database to `Path.home() / ".local/share/patchmeifyoucan/leaderboard.sqlite3"`; allow `LEADERBOARD_DB` to override it.
- Database failures may show `Scores could not be saved` but must never trap the kiosk.
- Tests must not start serial threads, access `/dev/ttyACM*`, write to the real leaderboard path, or depend on wall-clock sleeps.
- Match the existing poster-like kiosk styling and retain both 48-cell board displays and the `/control` page controls.
- Do not deploy to the game PC until the user explicitly approves deployment.

## File Map

- Create `Test_Server/leaderboard.py`: schema setup, validation, atomic idempotent insertion, ranked top-10 queries, and default path resolution.
- Create `Test_Server/race.py`: locked state machine, monotonic timing, manual stops, verified completion, name entry, deadlines, result snapshots, and reset behavior.
- Modify `Test_Server/app.py`: application factory, route and Socket.IO adapters, serial integration, persistence coordination, and background state/clock updates.
- Modify `Test_Server/templates/index.html`: phase-specific result, name-entry, and leaderboard views plus keyboard and reconnect behavior.
- Modify `Test_Server/pyproject.toml` and `Test_Server/uv.lock`: add pytest as a development dependency.
- Create `Test_Server/tests/conftest.py`: mutable monotonic clock and app fixtures.
- Create `Test_Server/tests/test_leaderboard.py`: SQLite behavior tests.
- Create `Test_Server/tests/test_race.py`: state-machine behavior tests.
- Create `Test_Server/tests/test_app.py`: HTTP and Socket.IO integration tests.
- Create `Test_Server/tests/browser_harness.py`: local-only deterministic controls for browser acceptance without serial hardware.

---

### Task 1: Persistent Leaderboard Store

**Files:**
- Create: `Test_Server/leaderboard.py`
- Create: `Test_Server/tests/test_leaderboard.py`
- Modify: `Test_Server/pyproject.toml`
- Modify: `Test_Server/uv.lock`

**Interfaces:**
- Produces: `NewLeaderboardEntry(race_id: str, player_number: int, name: str, duration_ms: int)`.
- Produces: `RankedLeaderboardEntry(id: int, race_id: str, player_number: int, name: str, duration_ms: int, created_at: str, rank: int)`.
- Produces: `default_database_path() -> Path`.
- Produces: `LeaderboardStore(path: Path, now: Callable[[], datetime] | None = None)` with `insert_entries(entries) -> set[int]` and `top_entries(limit: int = 10) -> list[RankedLeaderboardEntry]`.

- [ ] **Step 1: Add the test dependency**

Run from `Test_Server`:

```bash
uv add --dev "pytest>=8.4"
```

Expected: `pyproject.toml` gains a `dev` dependency group and `uv.lock` resolves pytest without changing runtime dependencies.

- [ ] **Step 2: Write failing persistence tests**

Create `tests/test_leaderboard.py` with concrete tests for initialization, restart persistence, idempotency, validation, ordering, truncation, and competition ranks:

```python
from datetime import UTC, datetime, timedelta

import pytest

from pathlib import Path

from leaderboard import LeaderboardStore, NewLeaderboardEntry, default_database_path


class AdvancingUtcClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(microseconds=1)
        return value


def entry(race_id: str, player: int, name: str, duration_ms: int) -> NewLeaderboardEntry:
    return NewLeaderboardEntry(race_id, player, name, duration_ms)


def test_default_path_uses_service_home_and_environment_override(tmp_path, monkeypatch):
    monkeypatch.delenv("LEADERBOARD_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_database_path() == tmp_path / ".local/share/patchmeifyoucan/leaderboard.sqlite3"

    override = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("LEADERBOARD_DB", str(override))
    assert default_database_path() == override


def test_store_initializes_and_reopens_existing_database(tmp_path):
    path = tmp_path / "scores" / "leaderboard.sqlite3"
    clock = AdvancingUtcClock()
    first = LeaderboardStore(path, now=clock)
    inserted = first.insert_entries([entry("race-1", 1, "Ada", 1234)])

    second = LeaderboardStore(path, now=clock)

    assert len(inserted) == 1
    assert [(row.name, row.duration_ms) for row in second.top_entries()] == [("Ada", 1234)]


def test_insert_is_atomic_and_duplicate_race_player_is_idempotent(tmp_path):
    store = LeaderboardStore(tmp_path / "scores.sqlite3", now=AdvancingUtcClock())
    submissions = [
        entry("race-1", 1, "Ada", 1000),
        entry("race-1", 2, "Grace", 1100),
    ]

    assert store.insert_entries([]) == set()
    first_ids = store.insert_entries(submissions)
    retry_ids = store.insert_entries(submissions)

    assert retry_ids == first_ids
    assert [row.name for row in store.top_entries()] == ["Ada", "Grace"]


@pytest.mark.parametrize(
    "submission",
    [
        entry("", 1, "Ada", 1),
        entry("race", 0, "Ada", 1),
        entry("race", 3, "Ada", 1),
        entry("race", 1, "", 1),
        entry("race", 1, " " * 2, 1),
        entry("race", 1, "A" * 13, 1),
        entry("race", 1, "Ada\n", 1),
        entry("race", 1, "Ada", -1),
        entry("race", 1, "Ada", True),
    ],
)
def test_insert_rejects_invalid_rows_without_partial_writes(tmp_path, submission):
    store = LeaderboardStore(tmp_path / "scores.sqlite3", now=AdvancingUtcClock())

    with pytest.raises(ValueError):
        store.insert_entries([entry("valid", 1, "Valid", 10), submission])

    assert store.top_entries() == []


def test_top_ten_uses_stable_order_and_competition_ranks(tmp_path):
    store = LeaderboardStore(tmp_path / "scores.sqlite3", now=AdvancingUtcClock())
    store.insert_entries(
        [entry(f"race-{index}", 1, f"P{index}", duration) for index, duration in enumerate(
            [100, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100]
        )]
    )

    rows = store.top_entries()

    assert len(rows) == 10
    assert [(row.rank, row.name, row.duration_ms) for row in rows[:3]] == [
        (1, "P0", 100),
        (1, "P1", 100),
        (3, "P2", 200),
    ]
    assert rows[-1].duration_ms == 900
```

- [ ] **Step 3: Run the tests to verify the missing module failure**

Run from `Test_Server`:

```bash
uv run pytest tests/test_leaderboard.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'leaderboard'`.

- [ ] **Step 4: Implement the SQLite store**

Create `leaderboard.py` with these exact public records and schema:

```python
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class NewLeaderboardEntry:
    race_id: str
    player_number: int
    name: str
    duration_ms: int


@dataclass(frozen=True)
class RankedLeaderboardEntry:
    id: int
    race_id: str
    player_number: int
    name: str
    duration_ms: int
    created_at: str
    rank: int


def default_database_path() -> Path:
    configured = os.environ.get("LEADERBOARD_DB")
    return Path(configured).expanduser() if configured else Path.home() / ".local/share/patchmeifyoucan/leaderboard.sqlite3"


class LeaderboardStore:
    def __init__(self, path: Path, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self.now = now or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def insert_entries(self, entries: Sequence[NewLeaderboardEntry]) -> set[int]:
        validated = tuple(self._validate(entry) for entry in entries)
        if not validated:
            return set()
        with self._connect() as connection:
            for item in validated:
                connection.execute(
                    """INSERT INTO leaderboard_entries
                       (race_id, player_number, name, duration_ms, created_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(race_id, player_number) DO NOTHING""",
                    (item.race_id, item.player_number, item.name, item.duration_ms, self.now().isoformat()),
                )
            return {
                row[0]
                for item in validated
                for row in connection.execute(
                    "SELECT id FROM leaderboard_entries WHERE race_id = ? AND player_number = ?",
                    (item.race_id, item.player_number),
                )
            }

    def top_entries(self, limit: int = 10) -> list[RankedLeaderboardEntry]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, race_id, player_number, name, duration_ms, created_at, rank
                   FROM (
                       SELECT id, race_id, player_number, name, duration_ms, created_at,
                              RANK() OVER (ORDER BY duration_ms) AS rank
                       FROM leaderboard_entries
                   )
                   ORDER BY duration_ms, created_at, id
                   LIMIT ?""",
                (limit,),
            )
            return [RankedLeaderboardEntry(*row) for row in rows]

    @staticmethod
    def _validate(entry: NewLeaderboardEntry) -> NewLeaderboardEntry:
        if not entry.race_id:
            raise ValueError("race_id is required")
        if entry.player_number not in (1, 2):
            raise ValueError("player_number must be 1 or 2")
        if not entry.name or entry.name != entry.name.strip() or len(entry.name) > 12:
            raise ValueError("name must contain 1 to 12 trimmed characters")
        if not all(character.isprintable() for character in entry.name):
            raise ValueError("name must be printable")
        if not isinstance(entry.duration_ms, int) or isinstance(entry.duration_ms, bool) or entry.duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative integer")
        return entry


SCHEMA = """
CREATE TABLE IF NOT EXISTS leaderboard_entries (
    id INTEGER PRIMARY KEY,
    race_id TEXT NOT NULL,
    player_number INTEGER NOT NULL CHECK (player_number IN (1, 2)),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 12),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (race_id, player_number)
);
CREATE INDEX IF NOT EXISTS leaderboard_time_idx
    ON leaderboard_entries (duration_ms, created_at, id);
"""
```

Keep validation before opening the write transaction so a bad row cannot commit earlier rows. Let `sqlite3.Error` propagate for `app.py` to handle.

- [ ] **Step 5: Run the store tests**

Run from `Test_Server`:

```bash
uv run pytest tests/test_leaderboard.py -q
```

Expected: all leaderboard tests pass.

- [ ] **Step 6: Commit the store**

```bash
git add Test_Server/pyproject.toml Test_Server/uv.lock Test_Server/leaderboard.py Test_Server/tests/test_leaderboard.py
git commit -m "feat(server): add persistent leaderboard store"
```

---

### Task 2: Core Race State and Timing

**Files:**
- Create: `Test_Server/race.py`
- Create: `Test_Server/tests/conftest.py`
- Create: `Test_Server/tests/test_race.py`

**Interfaces:**
- Consumes: monotonic `Callable[[], int]` returning nanoseconds.
- Produces: `Phase`, `PlayerRaceState`, `StateChange`, and `RaceStateMachine`.
- Produces methods: `start()`, `space()`, `manual_stop(player_number)`, `observe_cells(player_number, states)`, `reset()`, `clock_payload()`, and `snapshot()`.
- Produces invariant: every mutating method holds one internal `threading.RLock` and returns `StateChange(changed, action, submissions)`.

- [ ] **Step 1: Add a deterministic monotonic clock fixture**

Create `tests/conftest.py`:

```python
import pytest


class MutableClock:
    def __init__(self) -> None:
        self.nanoseconds = 0

    def __call__(self) -> int:
        return self.nanoseconds

    def advance_ms(self, milliseconds: int) -> None:
        self.nanoseconds += milliseconds * 1_000_000

    def advance_ns(self, nanoseconds: int) -> None:
        self.nanoseconds += nanoseconds


@pytest.fixture
def clock():
    return MutableClock()
```

- [ ] **Step 2: Write failing timing and completion tests**

Create `tests/test_race.py` with the core cases:

```python
import pytest

from race import Phase, RaceStateMachine


VERIFIED = [2] * 48


def started_race(clock):
    machine = RaceStateMachine(clock_ns=clock, race_id_factory=lambda: "race-1")
    assert machine.start().action == "started"
    return machine


def test_start_and_global_space_follow_non_result_branch(clock):
    machine = RaceStateMachine(clock_ns=clock, race_id_factory=lambda: "race-1")

    assert machine.snapshot()["phase"] == Phase.READY
    assert machine.space().action == "started"
    clock.advance_ms(1250)
    assert machine.space().action == "stopped"
    assert machine.snapshot()["phase"] == Phase.STOPPED
    assert machine.snapshot()["result_order"] == []
    assert machine.space().action == "reset"
    assert machine.snapshot()["phase"] == Phase.READY


def test_manual_stops_freeze_display_without_completing_players(clock):
    machine = started_race(clock)
    clock.advance_ms(1000)
    assert machine.manual_stop(1).changed
    clock.advance_ms(500)

    state = machine.snapshot()
    assert state["phase"] == Phase.RACING
    assert state["players"]["1"] == {
        "duration_ms": 1000,
        "completed": False,
        "manual_stopped": True,
        "clock_active": False,
    }
    assert machine.manual_stop(2).action == "stopped"
    assert machine.snapshot()["phase"] == Phase.STOPPED


@pytest.mark.parametrize(
    "states",
    [[2] * 47, [2] * 49, [2] * 47 + [0], [2] * 47 + [1], [True] * 48, "2" * 48],
)
def test_only_exactly_48_integer_verified_cells_complete(clock, states):
    machine = started_race(clock)

    assert not machine.observe_cells(1, states).changed
    assert not machine.snapshot()["players"]["1"]["completed"]


def test_first_valid_completion_is_idempotent_and_uses_floor_milliseconds(clock):
    machine = started_race(clock)
    clock.advance_ns(1_234_999_999)
    assert machine.observe_cells(1, VERIFIED).changed
    clock.advance_ms(500)
    assert not machine.observe_cells(1, VERIFIED).changed

    assert machine.snapshot()["players"]["1"]["duration_ms"] == 1234


def test_manual_stop_can_be_replaced_by_later_verified_completion(clock):
    machine = started_race(clock)
    clock.advance_ms(500)
    machine.manual_stop(1)
    clock.advance_ms(700)
    machine.observe_cells(1, VERIFIED)

    player = machine.snapshot()["players"]["1"]
    assert player["duration_ms"] == 1200
    assert player["completed"] is True
    assert player["manual_stopped"] is False


def test_second_completion_enters_name_entry_with_winner_order(clock):
    machine = started_race(clock)
    clock.advance_ms(900)
    machine.observe_cells(2, VERIFIED)
    clock.advance_ms(100)
    transition = machine.observe_cells(1, VERIFIED)

    state = machine.snapshot()
    assert transition.action == "name_entry"
    assert state["phase"] == Phase.NAME_ENTRY
    assert state["result_order"] == [2, 1]
    assert state["tie"] is False


def test_equal_floor_milliseconds_are_a_tie_and_prompt_player_one_first(clock):
    machine = started_race(clock)
    clock.advance_ns(1_000_100_000)
    machine.observe_cells(2, VERIFIED)
    clock.advance_ns(700_000)
    machine.observe_cells(1, VERIFIED)

    state = machine.snapshot()
    assert state["tie"] is True
    assert state["result_order"] == [1, 2]
    assert state["name_entry"]["active_player"] == 1
```

- [ ] **Step 3: Run the tests to verify the missing module failure**

Run from `Test_Server`:

```bash
uv run pytest tests/test_race.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'race'`.

- [ ] **Step 4: Implement the core race model**

Create `race.py` with this public model:

```python
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
import math
import threading
import time
import uuid


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
```

Implement the initial state and core methods as follows (Task 3 adds the post-race mutators):

```python
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
            if all(item.manual_stopped_ns is not None for item in self.players.values()):
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
        self.result_order = [1, 2] if self.tie else sorted((1, 2), key=lambda item: durations[item])
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
                    str(player_number): self._duration_ms_unlocked(player_number, now_ns)
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
            players = {}
            for player_number, player in self.players.items():
                players[str(player_number)] = {
                    "duration_ms": self._duration_ms_unlocked(player_number, now_ns),
                    "completed": player.completed_ns is not None,
                    "manual_stopped": player.manual_stopped_ns is not None,
                    "clock_active": (
                        self.phase == Phase.RACING
                        and player.completed_ns is None
                        and player.manual_stopped_ns is None
                    ),
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
```

- [ ] **Step 5: Run the race tests**

Run from `Test_Server`:

```bash
uv run pytest tests/test_race.py -q
```

Expected: all core race tests pass.

- [ ] **Step 6: Commit the core state machine**

```bash
git add Test_Server/race.py Test_Server/tests/conftest.py Test_Server/tests/test_race.py
git commit -m "feat(server): add authoritative race state"
```

---

### Task 3: Name Entry, Deadlines, and Leaderboard State

**Files:**
- Modify: `Test_Server/race.py`
- Modify: `Test_Server/tests/test_race.py`

**Interfaces:**
- Extends `RaceStateMachine` with `set_name_draft(race_id, player_number, draft)`, `submit_name(...)`, `tick()`, `set_leaderboard(...)`, and `dismiss_leaderboard(race_id)`.
- Produces finalized `StateChange.submissions` only once, at the transition from `name_entry` to `leaderboard`.
- Consumes ranked rows as JSON-compatible dictionaries from `app.py`; `race.py` remains independent of SQLite.

- [ ] **Step 1: Add failing post-race tests**

Append tests that use this helper:

```python
def completed_race(clock):
    machine = started_race(clock)
    clock.advance_ms(1000)
    machine.observe_cells(1, VERIFIED)
    clock.advance_ms(250)
    machine.observe_cells(2, VERIFIED)
    return machine
```

Add these cases:

```python
def test_name_entry_is_sequential_and_trims_confirmed_names(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]

    assert machine.set_name_draft(race_id, 1, "  Ada  ").changed
    first = machine.submit_name(race_id, 1, "  Ada  ")
    assert first.action == "next_name"
    assert machine.snapshot()["name_entry"]["active_player"] == 2

    final = machine.submit_name(race_id, 2, "Grace")
    assert final.action == "leaderboard"
    assert [(item.player_number, item.name, item.duration_ms) for item in final.submissions] == [
        (1, "Ada", 1000),
        (2, "Grace", 1250),
    ]


def test_empty_enter_skips_and_repeated_enter_finishes_without_rows(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]

    assert machine.submit_name(race_id, 1, "").action == "next_name"
    final = machine.submit_name(race_id, 2, "   ")

    assert final.action == "leaderboard"
    assert final.submissions == ()
    assert machine.snapshot()["phase"] == Phase.LEADERBOARD


@pytest.mark.parametrize("draft", ["A" * 13, "Ada\n", 123, None])
def test_invalid_drafts_do_not_change_state_or_extend_deadline(clock, draft):
    machine = completed_race(clock)
    before = machine.snapshot()
    clock.advance_ms(5000)

    assert not machine.set_name_draft(before["race_id"], 1, draft).changed
    after = machine.snapshot()
    assert after["name_entry"]["drafts"] == before["name_entry"]["drafts"]
    assert after["remaining_seconds"] == 115


def test_stale_race_or_inactive_player_is_ignored(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]

    assert not machine.set_name_draft("old-race", 1, "Ada").changed
    assert not machine.submit_name(race_id, 2, "Grace").changed
    assert machine.snapshot()["name_entry"]["active_player"] == 1


def test_printable_unicode_draft_is_accepted_by_codepoint_count(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    draft = "Zoë 🚀"

    assert machine.set_name_draft(race_id, 1, draft).changed
    assert machine.snapshot()["name_entry"]["drafts"]["1"] == draft


def test_accepted_draft_and_first_confirmation_restart_idle_deadline(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    clock.advance_ms(119_000)
    machine.set_name_draft(race_id, 1, "Ada")
    assert machine.snapshot()["remaining_seconds"] == 120
    clock.advance_ms(119_000)
    machine.submit_name(race_id, 1, "Ada")
    assert machine.snapshot()["remaining_seconds"] == 120


def test_second_player_timeout_preserves_confirmed_first_player(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    machine.submit_name(race_id, 1, "Ada")
    machine.set_name_draft(race_id, 2, "Unconfirmed")
    clock.advance_ms(120_000)

    expired = machine.tick()

    assert expired.action == "leaderboard"
    assert [(item.player_number, item.name) for item in expired.submissions] == [(1, "Ada")]


def test_name_timeout_skips_active_and_unresolved_players(clock):
    machine = completed_race(clock)
    clock.advance_ms(120_000)

    expired = machine.tick()

    assert expired.submissions == ()
    assert machine.snapshot()["phase"] == Phase.LEADERBOARD
    assert machine.snapshot()["remaining_seconds"] == 60


def test_leaderboard_rows_highlights_error_and_dismissal(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    transition = machine.submit_name(race_id, 1, "Ada")
    assert transition.action == "next_name"
    machine.submit_name(race_id, 2, "")
    machine.set_leaderboard(
        race_id,
        [{"id": 4, "rank": 1, "name": "Ada", "duration_ms": 1000}],
        highlighted_ids={4},
        persistence_error="Scores could not be saved",
    )

    state = machine.snapshot()
    assert state["leaderboard"][0]["current_race"] is True
    assert state["persistence_error"] == "Scores could not be saved"
    assert machine.dismiss_leaderboard("old-race").changed is False
    assert machine.dismiss_leaderboard(race_id).action == "reset"


def test_leaderboard_deadline_resets_to_ready(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    machine.submit_name(race_id, 1, "")
    machine.submit_name(race_id, 2, "")
    clock.advance_ms(60_000)

    assert machine.tick().action == "reset"
    assert machine.snapshot()["phase"] == Phase.READY


def test_reset_cancels_post_race_state(clock):
    machine = completed_race(clock)
    machine.reset()
    clock.advance_ms(120_000)

    assert not machine.tick().changed
    assert machine.snapshot()["race_id"] is None
    assert machine.snapshot()["phase"] == Phase.READY
```

- [ ] **Step 2: Run only the new behavior to verify failure**

Run from `Test_Server`:

```bash
uv run pytest tests/test_race.py -q
```

Expected: failures report missing `set_name_draft`, `submit_name`, `tick`, `set_leaderboard`, and `dismiss_leaderboard` methods.

- [ ] **Step 3: Implement name validation and resolution**

Add this module helper before `RaceStateMachine`:

```python
def _valid_draft(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 12
        and all(character.isprintable() for character in value)
    )
```

Then add these methods inside `RaceStateMachine`:

```python
    def _valid_name_event_unlocked(
        self,
        race_id: object,
        player_number: object,
        draft: object,
    ) -> bool:
        return (
            self.phase == Phase.NAME_ENTRY
            and race_id == self.race_id
            and type(player_number) is int
            and player_number == self.active_name_player
            and _valid_draft(draft)
        )

    def set_name_draft(
        self,
        race_id: object,
        player_number: object,
        draft: object,
    ) -> StateChange:
        with self._lock:
            if not self._valid_name_event_unlocked(race_id, player_number, draft):
                return StateChange(False)
            self.name_fields[player_number].draft = draft
            self.deadline_ns = self._clock_ns() + NAME_TIMEOUT_NS
            return StateChange(True, "name_draft")

    def submit_name(
        self,
        race_id: object,
        player_number: object,
        draft: object,
    ) -> StateChange:
        with self._lock:
            if not self._valid_name_event_unlocked(race_id, player_number, draft):
                return StateChange(False)
            field = self.name_fields[player_number]
            field.draft = draft.strip()
            field.resolved = True
            current_index = self.result_order.index(player_number)
            if current_index + 1 < len(self.result_order):
                self.active_name_player = self.result_order[current_index + 1]
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
```

Use `math.ceil(max(0, deadline_ns - now_ns) / 1_000_000_000)` for `remaining_seconds`, so new phases display `120` and `60` rather than one second less.

- [ ] **Step 4: Implement deadline, rows, and dismissal methods**

Add the remaining methods:

```python
    def tick(self) -> StateChange:
        with self._lock:
            if self.deadline_ns is None or self._clock_ns() < self.deadline_ns:
                return StateChange(False)
            if self.phase == Phase.NAME_ENTRY:
                for field in self.name_fields.values():
                    if not field.resolved:
                        field.draft = ""
                        field.resolved = True
                return self._enter_leaderboard_unlocked()
            if self.phase == Phase.LEADERBOARD:
                self._reset_unlocked()
                return StateChange(True, "reset")
            return StateChange(False)

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
            rendered_rows = []
            for row in rows:
                rendered = dict(row)
                rendered["current_race"] = rendered.get("id") in highlighted_ids
                rendered_rows.append(rendered)
            self.leaderboard_rows = rendered_rows
            self.persistence_error = persistence_error
            return StateChange(True, "leaderboard_updated")

    def dismiss_leaderboard(self, race_id: object) -> StateChange:
        with self._lock:
            if self.phase != Phase.LEADERBOARD or race_id != self.race_id:
                return StateChange(False)
            self._reset_unlocked()
            return StateChange(True, "reset")
```

The `snapshot()` written in Task 2 already exposes the name fields, remaining seconds, leaderboard rows, and persistence error required by these methods.

- [ ] **Step 5: Run all state-machine tests**

Run from `Test_Server`:

```bash
uv run pytest tests/test_race.py -q
```

Expected: all core and post-race state tests pass without sleeping.

- [ ] **Step 6: Commit the post-race transitions**

```bash
git add Test_Server/race.py Test_Server/tests/test_race.py
git commit -m "feat(server): add post-race name flow"
```

---

### Task 4: Flask, Socket.IO, Serial, and Persistence Integration

**Files:**
- Modify: `Test_Server/app.py`
- Create: `Test_Server/tests/test_app.py`
- Create: `Test_Server/tests/browser_harness.py`

**Interfaces:**
- Consumes: `RaceStateMachine`, `LeaderboardStore`, `NewLeaderboardEntry`, and ranked rows.
- Produces: `create_app(config=None, *, clock_ns=time.monotonic_ns, store=None, race_id_factory=None) -> tuple[Flask, SocketIO]`.
- Produces: `GameRuntime.observe_cells`, `emit_state`, `handle_change`, and `tick` as the single coordination boundary used by routes, Socket.IO handlers, serial threads, and the browser harness.
- Exposes: `app.extensions["game_runtime"]` for integration tests and the local-only browser harness.

- [ ] **Step 1: Write integration fixtures and failing connection tests**

Create `tests/test_app.py` with a factory that never starts hardware or background loops:

```python
import sqlite3

import pytest

from app import create_app
from leaderboard import LeaderboardStore
from race import Phase


VERIFIED = [2] * 48


@pytest.fixture
def app_bundle(tmp_path, clock):
    store = LeaderboardStore(tmp_path / "leaderboard.sqlite3")
    app, socketio = create_app(
        {
            "TESTING": True,
            "START_SERIAL_ON_CONNECT": False,
            "START_BACKGROUND_TASKS": False,
        },
        clock_ns=clock,
        store=store,
        race_id_factory=lambda: "race-1",
    )
    return app, socketio, app.extensions["game_runtime"]


def latest_event(client, name):
    events = [event for event in client.get_received() if event["name"] == name]
    assert events
    return events[-1]["args"][0]


def finish_race(runtime, clock):
    runtime.handle_change(runtime.race.start())
    clock.advance_ms(1000)
    runtime.observe_cells(1, VERIFIED)
    clock.advance_ms(100)
    runtime.observe_cells(2, VERIFIED)


def test_connect_receives_ready_game_state_without_starting_serial(app_bundle):
    app, socketio, runtime = app_bundle

    client = socketio.test_client(app)

    assert latest_event(client, "game_state")["phase"] == Phase.READY
    assert runtime.serial_threads is None


def test_state_changes_are_synchronized_across_clients(app_bundle):
    app, socketio, runtime = app_bundle
    first = socketio.test_client(app)
    second = socketio.test_client(app)
    first.get_received()
    second.get_received()

    app.test_client().get("/start-clock")

    assert latest_event(first, "game_state")["phase"] == Phase.RACING
    assert latest_event(second, "game_state")["phase"] == Phase.RACING
```

- [ ] **Step 2: Add failing event, reconnect, and error-path tests**

Append:

```python
@pytest.mark.parametrize("target_phase", [Phase.READY, Phase.RACING, Phase.NAME_ENTRY, Phase.LEADERBOARD])
def test_reconnect_receives_complete_current_phase(app_bundle, clock, target_phase):
    app, socketio, runtime = app_bundle
    if target_phase != Phase.READY:
        runtime.handle_change(runtime.race.start())
    if target_phase in (Phase.NAME_ENTRY, Phase.LEADERBOARD):
        clock.advance_ms(1000)
        runtime.observe_cells(1, VERIFIED)
        runtime.observe_cells(2, VERIFIED)
    if target_phase == Phase.LEADERBOARD:
        race_id = runtime.race.snapshot()["race_id"]
        runtime.handle_change(runtime.race.submit_name(race_id, 1, ""))
        runtime.handle_change(runtime.race.submit_name(race_id, 2, ""))

    client = socketio.test_client(app)

    assert latest_event(client, "game_state")["phase"] == target_phase


def test_stale_name_event_is_rejected_without_broadcast(app_bundle, clock):
    app, socketio, runtime = app_bundle
    finish_race(runtime, clock)
    client = socketio.test_client(app)
    client.get_received()

    client.emit("name_draft", {"race_id": "old", "player_number": 1, "draft": "Ada"})

    assert client.get_received() == []
    assert runtime.race.snapshot()["name_entry"]["drafts"]["1"] == ""


def test_submitting_names_persists_and_highlights_current_rows(app_bundle, clock):
    app, socketio, runtime = app_bundle
    finish_race(runtime, clock)
    client = socketio.test_client(app)
    race_id = runtime.race.snapshot()["race_id"]
    client.get_received()

    client.emit("submit_name", {"race_id": race_id, "player_number": 1, "draft": "Ada"})
    client.emit("submit_name", {"race_id": race_id, "player_number": 2, "draft": "Grace"})

    state = latest_event(client, "game_state")
    assert state["phase"] == Phase.LEADERBOARD
    assert [row["name"] for row in state["leaderboard"]] == ["Ada", "Grace"]
    assert all(row["current_race"] for row in state["leaderboard"])


class FailingStore:
    def insert_entries(self, entries):
        raise sqlite3.OperationalError("write failed")

    def top_entries(self):
        return []


def test_database_failure_still_enters_dismissible_leaderboard(tmp_path, clock):
    app, socketio = create_app(
        {"TESTING": True, "START_SERIAL_ON_CONNECT": False, "START_BACKGROUND_TASKS": False},
        clock_ns=clock,
        store=FailingStore(),
        race_id_factory=lambda: "race-1",
    )
    runtime = app.extensions["game_runtime"]
    finish_race(runtime, clock)
    client = socketio.test_client(app)
    race_id = runtime.race.snapshot()["race_id"]
    client.get_received()

    client.emit("submit_name", {"race_id": race_id, "player_number": 1, "draft": "Ada"})
    client.emit("submit_name", {"race_id": race_id, "player_number": 2, "draft": ""})

    state = latest_event(client, "game_state")
    assert state["phase"] == Phase.LEADERBOARD
    assert state["persistence_error"] == "Scores could not be saved"
    client.emit("dismiss_leaderboard", {"race_id": race_id})
    assert latest_event(client, "game_state")["phase"] == Phase.READY
```

- [ ] **Step 3: Run integration tests to verify the factory is missing**

Run from `Test_Server`:

```bash
uv run pytest tests/test_app.py -q
```

Expected: import or fixture setup fails because `create_app` and `GameRuntime` do not exist yet.

- [ ] **Step 4: Refactor `app.py` around an application factory**

Retain the existing routes and serial decoder, but replace timer globals with this ownership structure:

```python
class GameRuntime:
    def __init__(self, socketio, race, store, logger):
        self.socketio = socketio
        self.race = race
        self.store = store
        self.logger = logger
        self.cell_states = {1: [0] * NUM_CELLS, 2: [0] * NUM_CELLS}
        self.verify = True
        self.serial_threads = None
        self.stop_serial = False
        self._serial_lock = threading.Lock()

    def observe_cells(self, player_number, states):
        if isinstance(states, (list, tuple)):
            self.cell_states[player_number] = list(states)
            self.socketio.emit("table_update", {"switch_id": player_number, "states": list(states)})
        self.handle_change(self.race.observe_cells(player_number, states))

    def handle_change(self, change):
        if change.action == "leaderboard":
            self._persist_and_load(change.submissions)
        if change.changed:
            self.emit_state()

    def _persist_and_load(self, submissions):
        highlighted_ids = set()
        persistence_error = None
        try:
            highlighted_ids = self.store.insert_entries([
                NewLeaderboardEntry(item.race_id, item.player_number, item.name, item.duration_ms)
                for item in submissions
            ])
        except (sqlite3.Error, OSError, ValueError):
            self.logger.exception("Could not save leaderboard scores")
            persistence_error = "Scores could not be saved"
        try:
            rows = [asdict(row) for row in self.store.top_entries()]
        except (sqlite3.Error, OSError):
            self.logger.exception("Could not load leaderboard scores")
            rows = []
            persistence_error = "Scores could not be saved"
        race_id = self.race.snapshot()["race_id"]
        self.race.set_leaderboard(race_id, rows, highlighted_ids, persistence_error)

    def emit_state(self):
        self.socketio.emit("game_state", self.race.snapshot())

    def tick(self):
        self.handle_change(self.race.tick())

    def start_serial_once(self):
        with self._serial_lock:
            if self.serial_threads is not None:
                return
            self.stop_serial = False
            self.serial_threads = [
                threading.Thread(target=serial_loop, args=(self, serial_id), daemon=True)
                for serial_id in (0, 1)
            ]
            for thread in self.serial_threads:
                thread.start()

    def restart_serial(self):
        with self._serial_lock:
            threads = self.serial_threads or []
            self.stop_serial = True
        for thread in threads:
            thread.join()
        with self._serial_lock:
            self.serial_threads = None
        self.start_serial_once()
```

Create each `SocketIO` inside `create_app` so repeated pytest factories do not accumulate handlers:

```python
def create_app(config=None, *, clock_ns=time.monotonic_ns, store=None, race_id_factory=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY="secret",
        START_SERIAL_ON_CONNECT=True,
        START_BACKGROUND_TASKS=True,
    )
    if config:
        app.config.update(config)
    socketio = SocketIO(app, async_mode="threading")
    leaderboard = store if store is not None else LeaderboardStore(default_database_path())
    kwargs = {"clock_ns": clock_ns}
    if race_id_factory is not None:
        kwargs["race_id_factory"] = race_id_factory
    runtime = GameRuntime(socketio, RaceStateMachine(**kwargs), leaderboard, app.logger)
    app.extensions["game_runtime"] = runtime
    register_routes(app, socketio, runtime)
    register_socket_events(app, socketio, runtime)
    if app.config["START_BACKGROUND_TASKS"]:
        socketio.start_background_task(background_loop, socketio, runtime)
    return app, socketio
```

Under `if __name__ == "__main__":`, call `create_app()` and retain `host="0.0.0.0"`, `debug=True`, and `allow_unsafe_werkzeug=True` for deployed compatibility.

- [ ] **Step 5: Adapt routes, Socket.IO events, and the background loop**

Import `asdict`, `request`, `sqlite3`, `time`, and the new race/leaderboard types at module scope. Replace the global-dependent serial function with the runtime-aware decoder used by `GameRuntime.start_serial_once()`:

```python
PANEL_IDS = [int(value, 2) for value in (
    "1110", "0110", "1010", "0010", "1100", "0100", "1000", "0000"
)]
EXPECTED_IDS = [
    panel | int(side, 2)
    for outside, underside in (
        ("00000001", "01000000"),
        ("10000000", "01000001"),
        ("10000001", "11000000"),
    )
    for panel in PANEL_IDS
    for side in (outside, underside)
]


def parse_cell_state(value, expected, verify):
    if verify:
        if value == expected:
            return 2
        return 1 if value != 255 else 0
    return 2 if value != 255 else 0


def serial_loop(runtime, serial_id):
    player_number = serial_id + 1
    try:
        with serial.Serial(
            port=f"/dev/ttyACM{serial_id}",
            baudrate=9600,
            parity=serial.PARITY_ODD,
            stopbits=serial.STOPBITS_TWO,
            bytesize=serial.SEVENBITS,
            timeout=5,
        ) as connection:
            runtime.logger.info("Started serial reader %s", serial_id)
            while not runtime.stop_serial:
                line = connection.readline()
                if not line:
                    continue
                try:
                    tokens = line.decode("ascii").strip().split()
                    tokens.reverse()
                    values = [int(token, 16) for token in tokens]
                except (UnicodeDecodeError, ValueError):
                    runtime.logger.warning("Ignored malformed serial update from reader %s", serial_id)
                    continue
                if len(values) != NUM_CELLS:
                    runtime.logger.warning(
                        "Ignored serial update with %s cells from reader %s",
                        len(values),
                        serial_id,
                    )
                    continue
                states = [
                    parse_cell_state(value, expected, runtime.verify)
                    for value, expected in zip(values, EXPECTED_IDS, strict=True)
                ]
                runtime.observe_cells(player_number, states)
    except serial.SerialException:
        runtime.logger.exception("Serial reader %s stopped", serial_id)
```

This rejects short and long hardware messages before `zip`, so neither can become an accidental 48-cell completion. It also removes `player1_done`, `player2_done`, `stop_serial`, `verify`, and direct Socket.IO emissions from the serial function; all mutable ownership is on `GameRuntime` or `RaceStateMachine`.

Implement the event registrations with these payload contracts:

```python
def register_socket_events(app, socketio, runtime):
    @socketio.on("connect")
    def handle_connect():
        socketio.emit("game_state", runtime.race.snapshot(), to=request.sid)
        for player_number in (1, 2):
            socketio.emit(
                "table_update",
                {"switch_id": player_number, "states": runtime.cell_states[player_number]},
                to=request.sid,
            )
        if app.config["START_SERIAL_ON_CONNECT"]:
            runtime.start_serial_once()

    @socketio.on("name_draft")
    def handle_name_draft(data):
        if not isinstance(data, dict):
            return
        runtime.handle_change(runtime.race.set_name_draft(
            data.get("race_id"), data.get("player_number"), data.get("draft")
        ))

    @socketio.on("submit_name")
    def handle_submit_name(data):
        if not isinstance(data, dict):
            return
        runtime.handle_change(runtime.race.submit_name(
            data.get("race_id"), data.get("player_number"), data.get("draft")
        ))

    @socketio.on("dismiss_leaderboard")
    def handle_dismiss(data):
        if not isinstance(data, dict):
            return
        runtime.handle_change(runtime.race.dismiss_leaderboard(data.get("race_id")))
```

Register all current HTTP controls through the runtime:

```python
def register_routes(app, socketio, runtime):
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/control")
    def control():
        return render_template("control.html")

    @app.get("/randomize")
    def randomize():
        for player_number in (1, 2):
            states = [random.choice((0, 1)) for _ in range(NUM_CELLS)]
            runtime.observe_cells(player_number, states)
        return jsonify(runtime.cell_states)

    @app.get("/start-clock")
    def start_clock():
        runtime.handle_change(runtime.race.start())
        return "Clock started"

    @app.get("/stop-clock1")
    def stop_clock1():
        runtime.handle_change(runtime.race.manual_stop(1))
        return "Clock stopped"

    @app.get("/stop-clock2")
    def stop_clock2():
        runtime.handle_change(runtime.race.manual_stop(2))
        return "Clock stopped"

    @app.get("/reset-clock")
    def reset_clock():
        runtime.handle_change(runtime.race.reset())
        return "Clock reset"

    @app.get("/space-clock")
    def space_clock():
        change = runtime.race.space()
        runtime.handle_change(change)
        return f"Clock {change.action or 'ignored'}"

    @app.get("/toggle-verify")
    def toggle_verify():
        runtime.verify = not runtime.verify
        return "Done"

    @app.get("/restart-serial")
    def restart_serial():
        runtime.restart_serial()
        return "Done"
```

`/space-clock` therefore returns `Clock ignored` during `name_entry` and `leaderboard`; leaderboard dismissal comes only from the race-ID-bearing Socket.IO event.

The background loop runs once per second:

```python
def background_loop(socketio, runtime):
    while True:
        runtime.tick()
        phase = runtime.race.snapshot()["phase"]
        if phase == Phase.RACING:
            socketio.emit("clock_update", runtime.race.clock_payload())
        elif phase in (Phase.NAME_ENTRY, Phase.LEADERBOARD):
            runtime.emit_state()
        socketio.sleep(1)
```

Pass every decoded serial state list through `runtime.observe_cells(serial_id + 1, states)`. Do not duplicate completion checks in `serial_loop`; the state machine owns exact-length and all-verified validation.

- [ ] **Step 6: Add a local-only browser harness**

Create `tests/browser_harness.py`. It imports production code but is never imported by `app.py`:

```python
import itertools
from pathlib import Path
import tempfile

from app import create_app
from leaderboard import LeaderboardStore


class BrowserClock:
    def __init__(self):
        self.nanoseconds = 0

    def __call__(self):
        return self.nanoseconds

    def advance_ms(self, milliseconds):
        self.nanoseconds += milliseconds * 1_000_000


clock = BrowserClock()
database = Path(tempfile.mkdtemp(prefix="patchme-browser-")) / "leaderboard.sqlite3"
race_ids = itertools.count(1)
app, socketio = create_app(
    {"START_SERIAL_ON_CONNECT": False, "START_BACKGROUND_TASKS": False},
    clock_ns=clock,
    store=LeaderboardStore(database),
    race_id_factory=lambda: f"browser-race-{next(race_ids)}",
)
runtime = app.extensions["game_runtime"]


@app.post("/__test__/complete/<int:player_number>/<int:duration_ms>")
def complete(player_number, duration_ms):
    current = runtime.race.clock_payload()["durations_ms"][str(player_number)]
    clock.advance_ms(max(0, duration_ms - current))
    runtime.observe_cells(player_number, [2] * 48)
    return "completed"


@app.post("/__test__/advance/<int:milliseconds>")
def advance(milliseconds):
    clock.advance_ms(milliseconds)
    runtime.tick()
    runtime.emit_state()
    return "advanced"


if __name__ == "__main__":
    socketio.run(app, host="127.0.0.1", port=5001, allow_unsafe_werkzeug=True)
```

The browser flow starts the race through `/space-clock`, completes Player 1 at 1000 ms, then completes Player 2 at 1250 ms. The harness database lives under the system temporary directory and cannot affect production scores.

- [ ] **Step 7: Run all backend tests**

Run from `Test_Server`:

```bash
uv run pytest -q
```

Expected: store, race, HTTP, Socket.IO, reconnect, and failure-path tests all pass; no serial-open errors appear.

- [ ] **Step 8: Commit the application integration**

```bash
git add Test_Server/app.py Test_Server/tests/test_app.py Test_Server/tests/browser_harness.py
git commit -m "feat(server): integrate post-race game state"
```

---

### Task 5: Phase-Aware Kiosk UI

**Files:**
- Modify: `Test_Server/templates/index.html`
- Modify: `Test_Server/tests/test_app.py`

**Interfaces:**
- Consumes: `game_state`, `clock_update`, and `table_update` server events.
- Emits: `name_draft`, `submit_name`, and `dismiss_leaderboard`, each with the current `race_id` where specified.
- Keeps: HTTP `/space-clock` for ready/racing/stopped Space behavior.

- [ ] **Step 1: Add a failing template contract test**

Append to `tests/test_app.py`:

```python
def test_index_contains_all_phase_views_and_exact_copy(app_bundle):
    app, _, _ = app_bundle

    html = app.test_client().get("/").get_data(as_text=True)

    assert 'id="raceView"' in html
    assert 'id="nameEntryView"' in html
    assert 'id="leaderboardView"' in html
    assert "you were able to success" in html
    assert html.count('data-max-codepoints="12"') == 2
    assert 'id="nameEntryCountdown"' in html
    assert 'id="leaderboardCountdown"' in html
    assert 'id="persistenceError"' in html
```

- [ ] **Step 2: Run the contract test to verify failure**

Run from `Test_Server`:

```bash
uv run pytest tests/test_app.py::test_index_contains_all_phase_views_and_exact_copy -q
```

Expected: fails because the three phase view IDs and post-race elements do not exist.

- [ ] **Step 3: Add result/name-entry and leaderboard markup**

Wrap the existing header and player panels in `<div id="raceView" class="phase-view">`. Add sibling views inside `.race-stage`:

```html
<section id="nameEntryView" class="phase-view post-race-view" aria-labelledby="resultTitle" hidden>
    <p class="eyebrow">Race complete</p>
    <h2 id="resultTitle" class="result-title">you were able to success</h2>
    <div id="resultCards" class="result-cards">
        <article class="result-card" data-result-slot="0">
            <span class="result-place"></span>
            <strong class="result-player"></strong>
            <output class="result-time"></output>
            <label>
                <span>Name for the leaderboard</span>
                <input id="nameInput1" class="name-input" type="text" data-max-codepoints="12" autocomplete="off" spellcheck="false">
            </label>
        </article>
        <article class="result-card" data-result-slot="1">
            <span class="result-place"></span>
            <strong class="result-player"></strong>
            <output class="result-time"></output>
            <label>
                <span>Name for the leaderboard</span>
                <input id="nameInput2" class="name-input" type="text" data-max-codepoints="12" autocomplete="off" spellcheck="false">
            </label>
        </article>
    </div>
    <p id="nameEntryCountdown" class="phase-footer" aria-live="polite"></p>
</section>

<section id="leaderboardView" class="phase-view post-race-view" aria-labelledby="leaderboardTitle" hidden>
    <p class="eyebrow">Fastest patchers</p>
    <h2 id="leaderboardTitle" class="result-title">Top 10</h2>
    <p id="persistenceError" class="persistence-error" role="status" hidden></p>
    <table class="leaderboard-table">
        <thead><tr><th>Rank</th><th>Name</th><th>Time</th></tr></thead>
        <tbody id="leaderboardRows"></tbody>
    </table>
    <p id="leaderboardCountdown" class="phase-footer" aria-live="polite"></p>
</section>
```

When rendering result order, associate each card with its actual player number and input; do not assume slot zero is Player 1. Both inputs remain visible, resolved inputs are disabled, and only the server-declared active player's input is enabled and focused.

- [ ] **Step 4: Add phase styles without disturbing board geometry**

Extend the existing CSS using the same black outlines, blue/red/yellow/pink/green palette, square cards, condensed display face, and monospace times. Include these structural rules:

```css
[hidden] { display: none !important; }
.phase-view { min-height: 0; }
#raceView { flex: 1 1 auto; display: flex; flex-direction: column; }
.post-race-view { flex: 1 1 auto; width: 100%; display: flex; flex-direction: column; justify-content: center; }
.result-title { margin: 0 0 clamp(18px, 3vh, 40px); font-size: clamp(2.5rem, 6vh, 5.5rem); line-height: 0.95; text-transform: uppercase; }
.result-cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: clamp(14px, 2vw, 32px); }
.result-card { min-width: 0; padding: clamp(16px, 2.5vw, 36px); border: 8px solid #000; background: #fff; box-shadow: 10px 10px 0 var(--blue); }
.result-place, .result-player, .result-time { display: block; }
.result-time { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.name-input { width: 100%; margin-top: 10px; border: 6px solid #000; padding: 12px; font: 900 clamp(1.4rem, 3vw, 3rem) var(--font-display); }
.result-card.is-active { background: var(--yellow); box-shadow: 12px 12px 0 var(--red); }
.result-card.is-resolved { opacity: 0.72; }
.leaderboard-table { width: 100%; border-collapse: collapse; background: #fff; font-size: clamp(1rem, 2.2vh, 2rem); }
.leaderboard-table th, .leaderboard-table td { border: 4px solid #000; padding: clamp(6px, 1vh, 14px); text-align: left; }
.leaderboard-table td:last-child { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.leaderboard-table tr.is-current-race { background: var(--yellow); }
.phase-footer { margin: auto 0 0; padding-top: 14px; font-family: var(--font-condensed); text-align: center; }
.persistence-error { border: 4px solid #000; padding: 8px 12px; color: #fff; background: var(--red); }
@media (max-width: 760px) { .result-cards { grid-template-columns: 1fr; } }
```

Inspect at 1920x1080 and the actual deployed viewport before changing size rules further. Do not hide the exact result copy at short viewport heights.

- [ ] **Step 5: Replace client-owned phase assumptions with `game_state` rendering**

Keep board painting, but maintain one client snapshot:

```javascript
let gameState = {
    phase: "ready",
    race_id: null,
    players: {},
    result_order: [],
    tie: false,
    name_entry: { active_player: null, drafts: {}, resolved: {} },
    remaining_seconds: null,
    leaderboard: [],
    persistence_error: null,
};

function formatDuration(durationMs) {
    const safe = Math.max(0, Number(durationMs) || 0);
    const minutes = Math.floor(safe / 60000).toString().padStart(2, "0");
    const seconds = Math.floor((safe % 60000) / 1000).toString().padStart(2, "0");
    const milliseconds = Math.floor(safe % 1000).toString().padStart(3, "0");
    return `${minutes}:${seconds}.${milliseconds}`;
}

function formatCountdown(seconds) {
    const safe = Math.max(0, Number(seconds) || 0);
    return `${Math.floor(safe / 60)}:${Math.floor(safe % 60).toString().padStart(2, "0")}`;
}

function showPhase(phase) {
    document.getElementById("raceView").hidden = !["ready", "racing", "stopped"].includes(phase);
    document.getElementById("nameEntryView").hidden = phase !== "name_entry";
    document.getElementById("leaderboardView").hidden = phase !== "leaderboard";
}

function renderLeaderboard(rows) {
    const body = document.getElementById("leaderboardRows");
    body.replaceChildren();
    rows.forEach((row) => {
        const tableRow = document.createElement("tr");
        tableRow.classList.toggle("is-current-race", Boolean(row.current_race));
        if (row.current_race) tableRow.setAttribute("aria-current", "true");
        [row.rank, row.name, formatDuration(row.duration_ms)].forEach((value) => {
            const cell = document.createElement("td");
            cell.textContent = String(value);
            tableRow.appendChild(cell);
        });
        body.appendChild(tableRow);
    });
}

function renderGameState(state) {
    const priorPhase = gameState.phase;
    const priorRaceId = gameState.race_id;
    const priorActivePlayer = gameState.name_entry.active_player;
    gameState = state;
    showPhase(state.phase);

    PLAYER_IDS.forEach((playerId) => {
        const player = state.players[String(playerId)];
        players[playerId].durationMs = player.duration_ms;
        clockState.activePlayers[playerId] = player.clock_active;
    });
    clockState.syncedAt = performance.now();
    updateStats();
    ensureClockAnimation();

    const resultCards = Array.from(document.querySelectorAll(".result-card"));
    resultCards.forEach((card, resultIndex) => {
        const playerNumber = state.result_order[resultIndex];
        if (!playerNumber) return;
        const key = String(playerNumber);
        const input = card.querySelector(".name-input");
        const isResolved = Boolean(state.name_entry.resolved[key]);
        const isActive = state.name_entry.active_player === playerNumber;
        card.dataset.playerNumber = key;
        input.dataset.playerNumber = key;
        card.querySelector(".result-place").textContent = state.tie
            ? "Tie"
            : resultIndex === 0 ? "Winner" : "Second place";
        card.querySelector(".result-player").textContent = `Player ${playerNumber}`;
        card.querySelector(".result-time").textContent = formatDuration(
            state.players[key].duration_ms,
        );
        if (document.activeElement !== input || isResolved) {
            input.value = state.name_entry.drafts[key] || "";
        }
        input.disabled = !isActive || isResolved;
        input.setAttribute("aria-label", `Player ${playerNumber} leaderboard name`);
        card.classList.toggle("is-active", isActive);
        card.classList.toggle("is-resolved", isResolved);
    });

    document.getElementById("nameEntryCountdown").textContent =
        `Leaderboard in ${formatCountdown(state.remaining_seconds)} - empty names are skipped`;
    renderLeaderboard(state.leaderboard);
    const persistenceError = document.getElementById("persistenceError");
    persistenceError.textContent = state.persistence_error || "";
    persistenceError.hidden = !state.persistence_error;
    document.getElementById("leaderboardCountdown").textContent =
        `Next race in ${formatCountdown(state.remaining_seconds)} - press Space to continue`;

    const activeChanged = (
        priorPhase !== state.phase
        || priorRaceId !== state.race_id
        || priorActivePlayer !== state.name_entry.active_player
    );
    if (state.phase === "name_entry" && activeChanged) {
        const activeInput = document.querySelector(".name-input:not(:disabled)");
        activeInput?.focus();
    }
}

socket.on("game_state", renderGameState);
```

Treat reconnect like a fresh connection; do not preserve a browser-only phase or deadline.

Replace the old seconds/milliseconds interpolation with duration-millisecond bases while retaining each player's `states` array:

```javascript
const clockState = {
    activePlayers: { 1: false, 2: false },
    syncedAt: performance.now(),
};

function playerClockIsActive(playerId) {
    return Boolean(clockState.activePlayers[playerId]);
}

function currentDurationMs(playerId) {
    const elapsed = playerClockIsActive(playerId)
        ? Math.max(0, performance.now() - clockState.syncedAt)
        : 0;
    return players[playerId].durationMs + elapsed;
}

function paintClocks() {
    PLAYER_IDS.forEach((playerId) => {
        document.getElementById(`clockDisplay${playerId}`).textContent =
            formatDuration(currentDurationMs(playerId));
    });
}

socket.on("clock_update", (data) => {
    if (data.race_id !== gameState.race_id) return;
    PLAYER_IDS.forEach((playerId) => {
        players[playerId].durationMs = data.durations_ms[String(playerId)];
        clockState.activePlayers[playerId] = data.active_players[String(playerId)];
    });
    clockState.syncedAt = performance.now();
    updateStats();
    ensureClockAnimation();
});
```

Initialize each `players[playerId]` with `durationMs: 0`. Update patch-rate calculations to pass `currentDurationMs(playerId) / 1000` instead of the removed `seconds` field, and remove `clockGeneration`, `milliseconds`, and all reads of `seconds1`, `seconds2`, `running`, `started`, or `generation`.

- [ ] **Step 6: Implement phase-specific keyboard and name events**

For each name input, resolve its current `data-player-number` assigned by `renderGameState`. On `input`, emit the full value:

```javascript
function emitNameDraft(input) {
    const codepoints = Array.from(input.value);
    if (codepoints.length > 12) {
        input.value = codepoints.slice(0, 12).join("");
    }
    socket.emit("name_draft", {
        race_id: gameState.race_id,
        player_number: Number(input.dataset.playerNumber),
        draft: input.value,
    });
}

document.querySelectorAll(".name-input").forEach((input) => {
    input.addEventListener("input", () => emitNameDraft(input));
});
```

Use one keydown handler with this order:

```javascript
document.addEventListener("keydown", (event) => {
    if (event.repeat) return;

    if (gameState.phase === "name_entry") {
        if (event.key === "Enter") {
            const active = document.querySelector(".name-input:not(:disabled)");
            if (!active) return;
            event.preventDefault();
            socket.emit("submit_name", {
                race_id: gameState.race_id,
                player_number: Number(active.dataset.playerNumber),
                draft: active.value,
            });
        }
        return;
    }

    if (event.code === "KeyV") {
        event.preventDefault();
        fetch("/toggle-verify");
        return;
    }

    if (event.code === "KeyS") {
        event.preventDefault();
        fetch("/restart-serial");
        return;
    }

    if (event.code !== "Space") return;
    event.preventDefault();

    if (gameState.phase === "leaderboard") {
        socket.emit("dismiss_leaderboard", { race_id: gameState.race_id });
        return;
    }

    if (["ready", "racing", "stopped"].includes(gameState.phase)) {
        advanceClockWithSpacebar();
    }
});
```

This early `name_entry` return is mandatory: Space reaches the focused text input and is never sent to `/space-clock`; `KeyV` and `KeyS` also cannot fire while entering a name.

- [ ] **Step 7: Run the automated suite**

Run from `Test_Server`:

```bash
uv run pytest -q
```

Expected: all tests pass, including the static phase contract and prior backend tests.

- [ ] **Step 8: Commit the kiosk UI**

```bash
git add Test_Server/templates/index.html Test_Server/tests/test_app.py
git commit -m "feat(ui): add results and leaderboard screens"
```

---

### Task 6: Full Verification and Browser Acceptance

**Files:**
- Verify only; change the smallest owning file if a check reveals a defect.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: evidence that automated behavior, the local kiosk workflow, responsive layout, and JavaScript console are clean.

- [ ] **Step 1: Run formatting, lint, syntax, and all tests**

Run from the repository root:

```bash
uvx ruff format --check Test_Server/app.py Test_Server/race.py Test_Server/leaderboard.py Test_Server/tests
uvx ruff check Test_Server/app.py Test_Server/race.py Test_Server/leaderboard.py Test_Server/tests
```

Run from the parent workspace (`/Users/amtmann/workspace/paetsch-me-if-you-can`):

```bash
just check
```

Then run from `Test_Server`:

```bash
uv run pytest -q
```

Expected: every command exits zero. If formatting changes are needed, run `uvx ruff format` on only these Python paths, rerun all four checks, and commit the correction with the owning task rather than making a drive-by cleanup.

- [ ] **Step 2: Start the deterministic local browser harness**

Run from `Test_Server` and keep the process active:

```bash
uv run python tests/browser_harness.py
```

Expected: the server listens on `http://127.0.0.1:5001` with no serial access attempts.

- [ ] **Step 3: Delegate browser control and exercise the full flow**

Use an `independent-execute` subagent that loads the `agent-browser` skill. Have it launch Brave Browser Beta with CDP port 9222, connect every command with `--cdp 9222`, and perform this exact workflow at 1920x1080:

1. Open `http://127.0.0.1:5001`, clear console/errors, and verify the ready dashboard has two 48-cell boards with no overlap.
2. Press Space and verify the racing phase and live clock.
3. Execute `fetch('/__test__/complete/1/1000', {method: 'POST'})`, then `fetch('/__test__/complete/2/1250', {method: 'POST'})` through `agent-browser eval --stdin`.
4. Verify exact visible text `you were able to success`, Winner Player 1 at `00:01.000`, Second place Player 2 at `00:01.250`, two visible fields, and focus on Player 1.
5. Type `Ada Lovelace` (12 characters), include an ordinary Space, press Enter, and verify focus advances to Player 2.
6. Press Enter on empty Player 2 and verify leaderboard shows one highlighted Ada Lovelace row at rank 1.
7. Press Space and verify a clean ready screen.
8. Run a second race with equal `1500` ms durations, verify both cards say `Tie`, and use repeated Enter to skip both names.
9. Verify the leaderboard still appears and the prior persisted row remains.
10. Dismiss to ready, start a third race, complete players at 2000 and 2100 ms, confirm `Grace` for the first result, type but do not confirm the second name, then exercise `POST /__test__/advance/120000`; verify Grace is saved and the unresolved player is skipped.
11. Exercise `POST /__test__/advance/60000` on the leaderboard and verify automatic return to ready.
12. Refresh once during `name_entry` and once during `leaderboard`; verify current phase, remaining time, drafts/rows, and focus recover from `game_state`.
13. Check `agent-browser errors` and `agent-browser console`; there must be no uncaught exception, failed fetch, overflow warning, or Socket.IO error.
14. Capture screenshots for name entry and leaderboard under `/var/folders/6t/wnyrmz114lv4_th3c0bjqy6w0000gp/T/opencode/` for inspection, then close the browser session.

Expected: all transitions and exact copy match the spec, keyboard focus is obvious, Space is phase-correct, and no text overlaps at 1920x1080.

- [ ] **Step 4: Check the narrow fallback without redesigning for mobile**

In the same delegated browser session, set viewport `760x900`, revisit result and leaderboard phases, and capture screenshots. Verify cards stack, names/times stay within their containers, the exact success message remains visible, and no page content requires horizontal scrolling. Restore 1920x1080 afterward.

- [ ] **Step 5: Review the complete diff and history**

Run from the repository root:

```bash
git status --short
git diff --check
git log --oneline -10
```

Expected: only intentional source/test/dependency changes remain, `git diff --check` is silent, and each task has one focused commit. Do not commit browser screenshots or a temporary SQLite database.

- [ ] **Step 6: Stop before deployment**

Report automated and browser verification evidence to the user and ask separately whether to deploy. Only after explicit approval should execution load `reviewing-changes`, run `just deploy` from the parent workspace, verify the service and sync state, and repeat the key kiosk checks at the actual deployed display size.
