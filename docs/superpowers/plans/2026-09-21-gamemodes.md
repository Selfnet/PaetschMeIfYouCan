# Gamemodes Implementation Plan

Status: implemented and deployed on `feat/gamemodes` through `194a6cb`. This is the historical implementation plan; checkbox records are not a current work queue. [The authoring guide](../../gamemode-authoring.md) describes the implemented API, and [the README](../../../README.md) records the important name-entry keyboard contract and test prerequisites. Local `.superpowers/sdd/` acceptance reports are not versioned; automated coverage lives in `Test_Server/tests/`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add extensible field gamemodes, a keyboard-accessible selector, and per-mode score browsing while retaining the familiar kiosk UI and race flow.

**Architecture:** Move validated raw observations and physical layout into a pure board module, and gameplay into independent per-player mode sessions. Keep authoritative state transitions under the existing race controller lock, with persistence and Socket.IO work outside it. Render generic server-owned presentations in the existing template; keep dialog focus and paginated leaderboard browsing local to each browser.

**Tech Stack:** Python >=3.11, Flask, Flask-SocketIO threading mode, SQLite, pytest, vanilla JavaScript/CSS, existing local Socket.IO client. Browser acceptance uses the existing deterministic harness and agent-browser; no frontend framework or production dependency is needed.

## Global Constraints

- Work happens on `feat/gamemodes` in the canonical checkout's `.worktrees/gamemodes/` directory. Deployment is not part of this work.
- Preserve current working behavior outside the changes explicitly described here, including simultaneous name-entry support, ties, per-player diagnostic stops, and result timing.
- Both players use the same selected mode.
- Full Field: `full-field`, 48 ports, indices 0-47. Half Field: `half-field`, 24 ports, indices 0-23. Quarter Field: `quarter-field`, 12 ports, indices 0-11.
- Half and Quarter Field are single-region objectives, not left-to-right sequences.
- There is no unplugging prerequisite. Pre-patched ports count.
- Server startup selects Full Field. Selection survives every reset path but not server restarts.
- Board updates must contain exactly 48 integer byte values; reject booleans, out-of-range values, malformed text, and partial frames.
- Preserve millisecond floor timing and existing manual-stop behavior.
- This version promises no sub-second scheduling for time-driven modes.
- Fetch bounded pages on demand, initially 50 rows per page. Use boundary `0` for an empty snapshot.
- The post-race leaderboard retains a server-owned 60-second deadline. The idle leaderboard remains browser-local with its existing 60-second deadline.
- Keep rank, name, time, and locally formatted creation timestamp columns, and current-race highlighting wherever those rows occur.
- Keep all 48 physical positions visible in the existing two-row arrangement.
- No firmware modifications, host LED writes, plugin discovery, new production modes beyond the three field modes, leaderboard administration, or unfinished-race recovery.
- User clarification: preserve all existing UI look and feel not explicitly changed. The selector must look like the current UI, not a redesign. Add the suggested compact bottom bar for current-mode information and contextual shortcuts.
- This document is a plan, not authorization to deploy, push, or commit. During execution, make logical commits at task boundaries only when authorized; otherwise retain those boundaries for review.

---

## Sources and Scope

- [Gamemodes design](../specs/2026-09-21-gamemodes-design.md): authoritative gameplay, concurrency, UI, persistence, and acceptance requirements. Read before implementation; the user has now requested planning, not implementation.
- [Post-race design](../specs/2026-08-28-post-race-leaderboard-design.md): historical lifecycle and persistence context. Its all-48 rule, top-10 limit, fixed leaderboard deadline, and older name-entry details do not override current code or this spec.
- [Idle leaderboard design](../specs/2026-08-30-idle-leaderboard-design.md): local-view ownership and existing dismissal behavior, not the new pagination contract.
- `Test_Server/templates/index.html`: authoritative visual baseline. In particular, preserve `.race-stage`, `.stage-header`, `.player-panel`, `.clock-display`, board geometry, and result cards.
- `Test_Server/tests/browser_harness.py`: local fake clock and temporary database, with no serial threads. Use for acceptance; never exercise these scenarios on the deployed PC.

All paths below are relative to `/Users/amtmann/workspace/paetsch-me-if-you-can/PaetschMeIfYouCan/.worktrees/gamemodes`. Run Python commands from its `Test_Server` directory. The parent workspace's `just check` targets the canonical checkout, not this worktree; it is not evidence for this implementation.

## File Boundaries

| File | Responsibility |
| --- | --- |
| Create `Test_Server/board.py` | Immutable raw observations, expected identities, coordinate helpers, validated board cache |
| Create `Test_Server/gamemodes.py` | Frozen presentation/context values, session protocol, explicit registry, output validation, one parameterized field-mode implementation |
| Modify `Test_Server/race.py` | Own boards, selection, verification context, sessions, lifecycle, origin tokens, deadline validation |
| Modify `Test_Server/leaderboard.py` | Transactional migration, explicit score mode, snapshot boundary, ranked bounded pages |
| Modify `Test_Server/app.py` | Decode serial, transport raw observations, selection/page/activity events, outside-lock persistence |
| Modify `Test_Server/templates/index.html` | Existing styling plus mode bar/dialog, generic board rendering, local paginated browsing |
| Create `Test_Server/tests/test_board.py`, `test_gamemodes.py` | Pure-domain behavior and malformed contracts |
| Create `Test_Server/tests/mode_fixtures.py` | Non-production staged/timed and failing sessions shared by tests/harness |
| Modify `Test_Server/tests/test_race.py`, `test_app.py`, `test_leaderboard.py` | Lifecycle, races between operations, migration, targeted transport |
| Modify `Test_Server/tests/browser_harness.py` | Raw observations, multi-page fixtures, deterministic failure/delay controls |
| Create `docs/gamemode-authoring.md`; modify `README.md` | Contributor API and annotated entry link |

Do not change `Test_Server/templates/control.html`, firmware, deployment helpers, dependency lockfiles, or global CSS merely for consistency. Keep the current single-template frontend organization; do not add a build tool as part of this feature.

## Shared Contracts

These names are the interfaces between tasks. Frozen dataclasses contain only frozen nested values; serialization to fresh JSON dictionaries happens at the transport boundary.

```python
# board.py
NUM_PORTS = 48
OPEN_ID = 255
# EXPECTED_IDS: tuple[int, ...], moved unchanged from app.py
# coordinates(index: int) -> tuple[int, int] returns (column, row), zero-based
# port_index(column: int, row: int) -> int
# BoardObservation(identities: tuple[int, ...] | None)
# GameBoard.snapshot() -> BoardObservation
# GameBoard.update(values: object) -> BoardObservation  # raises ValueError

# gamemodes.py
# EvaluationContext(verify: bool)
# PortPresentation(required: bool, feedback: str)
# Progress(current: int, total: int, unit: str)
# LedState(yellow: bool, green: bool)
# ModeSnapshot(objective: str, stage: str | None, progress: Progress | None,
#              ports: tuple[PortPresentation, ...], complete: bool,
#              led_intent: tuple[LedState, ...] | None = None)
# ModeSession.initialize(observation, context, elapsed_ns) -> ModeSnapshot
# ModeSession.observe(observation, context, elapsed_ns) -> ModeSnapshot
# ModeSession.tick(observation, context, elapsed_ns) -> ModeSnapshot
# GameMode(id, label, description, required_port_count, session_factory, preview)
# GameMode.new_session() -> ModeSession
# GameMode.preview(observation, context) -> ModeSnapshot
# ModeRegistry(definitions: Sequence[GameMode])
# ModeRegistry.get(mode_id: str) -> GameMode
# ModeRegistry.metadata() -> tuple[ModeMetadata, ...]
# validate_snapshot(snapshot: object) -> ModeSnapshot  # raises ValueError
# BUILTIN_MODES: ModeRegistry, registry order full/half/quarter
```

`ModeMetadata` is a frozen value with `id`, `label`, `description`, and `required_port_count: int | None`. Session methods receive actual elapsed nanoseconds from the race start. Field modes ignore elapsed time; timed fixtures do not. `preview` is a pure callable, not a retained session.

Controller API additions: `observe_board(player_number, values)`, `select_mode(mode_id)`, `set_verification(verify)`, `leaderboard_activity(race_id)`, `capture_browse(mode_id, race_id)`, `browse_is_current(origin)`, and `set_persistence(origin, error)`. All public mutations return `StateChange`. Keep existing start/space/reset/manual-stop/name APIs. Inject `registry=BUILTIN_MODES` into `RaceStateMachine` and `create_app` for fixtures.

`RaceOrigin(race_id: str, mode_id: str)` accompanies every leaderboard transition, including empty submissions. Add `mode_id: str` to `ScoreSubmission` and `NewLeaderboardEntry`; use keyword construction to avoid positional mistakes. `StateChange` gains `origin: RaceOrigin | None` and `diagnostic: str | None`. Diagnostics are formatted exception details collected during evaluation; runtime logs them after lock release.

`BrowseOrigin(epoch: int, phase: Phase, mode_id: str, race_id: str | None)` captures eligibility under the controller lock. Increment `epoch` on start, reset, selection, and entry into a new view-owning phase, not on ordinary ticks/observations. It invalidates slow reads even across ready -> racing -> reset -> ready. `browse_is_current` processes expiry and checks every origin field; runtime must publish an expiry transition if that check expires the race.

The shared `game_state` payload retains existing timing/name fields and adds:

```json
{
  "revision": 1,
  "mode": {"id": "quarter-field", "label": "Quarter Field", "description": "Patch the leftmost six columns.", "required_port_count": 12},
  "available_modes": [],
  "mode_error": null,
  "persistence_status": "idle",
  "players": {"1": {"presentation": {"objective": "Patch the leftmost six columns.", "stage": null, "progress": {"current": 0, "total": 12, "unit": "correct"}, "ports": [], "complete": false, "led_intent": null}}}
}
```

The abbreviated arrays in this payload example are filled with registry metadata and exactly 48 port presentations in production. Include player 2 and existing player clock fields. `persistence_status` is `idle`, `pending`, `saved`, or `error`. Remove shared `leaderboard` row collections. Increment a monotonic controller `revision` for published state changes; browsers ignore older revisions so outside-lock emits cannot roll back UI state. Reconnect establishes a new revision baseline to support server restart.

### Visual Contract

Preserve the paper background, ridged blue frame, black outlines, offset shadows, existing blue/red/yellow/green/pink palette, condensed bold headings, monospaced timers, humorous existing copy, and current animations. Do not modernize the UI into neutral cards, add a new icon/font package, or reformat unrelated CSS.

Extend `.idle-actions` into an in-flow `.mode-bar`, not a fixed overlay. At 1920x1080 it is a compact single row: current mode/count and a short description on the left, existing yellow button treatment and contextual key hints on the right. Keep at least 44px control targets. Wrap the bar at narrow widths; do not reduce the number of columns or obscure port numbers to make room.

Ready: mode control with `M: mode`, existing `L: leaderboard`, and `Space: start`. Racing: read-only current mode and `Space: stop`. Stopped: read-only current mode, `L: leaderboard`, and `Space: reset`. Name entry and leaderboards retain their own footer layout and gain a compact mode label; do not add duplicate shortcut bars. Selector hints are `Arrows: browse`, `Enter: select`, `Esc / M: cancel`. Never advertise unavailable actions.

The selector is a native `<dialog>` with a label, ordinary full-width option buttons, and a visible close button. Reuse existing paper/yellow surfaces, 4px black borders, 5px offset shadows, and font variables. No new menu animation. Selected marker and visible focus outline are distinct; selection highlighting must not substitute for focus. Dialog options show label and description; the bar shows the authoritative selection, not the locally focused option.

## Task 1: Raw Board Model and Validation

**Files:** Create `Test_Server/board.py`, `Test_Server/tests/test_board.py`.

**Consumes:** Existing `EXPECTED_IDS` generation in `app.py` and canonical grid mapping in `index.html`.
**Produces:** `GameBoard`, `BoardObservation`, `EXPECTED_IDS`, `OPEN_ID`, `NUM_PORTS`, coordinate helpers.

- [ ] Add these focused failing tests, then parameterize invalid input over lengths 47/49, booleans, floats, strings, -1, and 256. Check both coordinate boundaries and every valid round trip.

```python
import pytest
from board import GameBoard, EXPECTED_IDS, coordinates, port_index

def test_invalid_frame_keeps_previous_raw_observation():
    board = GameBoard()
    assert board.snapshot().identities is None
    accepted = board.update(EXPECTED_IDS)
    with pytest.raises(ValueError):
        board.update([True] * 48)
    assert board.snapshot() == accepted
    assert accepted.identities == EXPECTED_IDS

def test_layout_alternates_top_and_bottom():
    for index in range(48):
        assert coordinates(index) == (index // 2, index % 2)
        assert port_index(*coordinates(index)) == index
```

- [ ] Run `uv run pytest tests/test_board.py -q`; expect import failure before implementation.
- [ ] Implement the cache and strict validation. Move the expected-ID expression without changing its values; freeze it as a tuple. Coordinate helpers reject non-integer/bool and out-of-range arguments with `ValueError`.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class BoardObservation:
    identities: tuple[int, ...] | None

class GameBoard:
    def __init__(self):
        self._observation = BoardObservation(None)

    def snapshot(self):
        return self._observation

    def update(self, values):
        if not isinstance(values, (list, tuple)) or len(values) != 48:
            raise ValueError("expected 48 byte identities")
        if any(type(value) is not int or not 0 <= value <= 255 for value in values):
            raise ValueError("expected 48 byte identities")
        self._observation = BoardObservation(tuple(values))
        return self._observation
```

- [ ] Run `uv run pytest tests/test_board.py -q`; expect all tests passing. Check input-list mutation cannot mutate a published observation. Review this isolated domain change before continuing.

## Task 2: Session Contract, Registry, and Field Modes

**Files:** Create `Test_Server/gamemodes.py`, `Test_Server/tests/test_gamemodes.py`, `Test_Server/tests/mode_fixtures.py`.

**Consumes:** Task 1 immutable observations and constants.
**Produces:** All `gamemodes.py` contracts above, three registered modes, test-only staged/timed sessions.

- [ ] Write parameterized tests for `(full-field, 48)`, `(half-field, 24)`, `(quarter-field, 12)`. Include the following positive completion test plus required-open/wrong, arbitrary inactive identities, bypass, unknown board, and repeated pure-preview tests.

```python
import pytest
from board import GameBoard, EXPECTED_IDS
from gamemodes import BUILTIN_MODES, EvaluationContext

@pytest.mark.parametrize("mode_id,count", [("full-field", 48), ("half-field", 24), ("quarter-field", 12)])
def test_field_targets_and_prepatched_completion(mode_id, count):
    mode = BUILTIN_MODES.get(mode_id)
    board = GameBoard()
    observation = board.update(EXPECTED_IDS[:count] + (255,) * (48 - count))
    result = mode.new_session().initialize(observation, EvaluationContext(True), 0)
    assert result.complete
    assert result.progress.current == result.progress.total == count
    assert tuple(i for i, port in enumerate(result.ports) if port.required) == tuple(range(count))
```

- [ ] Run `uv run pytest tests/test_gamemodes.py -q`; expect missing module/API failures.
- [ ] Define frozen nested dataclasses and a `Protocol` for the three session methods. Implement one field evaluator shared by the factory's sessions and the pure preview. Use this feedback rule for every port, including inactive ones; only target members contribute to progress/completion.

```python
def feedback(value, expected, context):
    if value == 255:
        return "open"
    if not context.verify or value == expected:
        return "correct"
    return "wrong"

def evaluate_field(observation, context, count, objective):
    identities = observation.identities
    ports = tuple(
        PortPresentation(
            required=index < count,
            feedback="unknown" if identities is None else feedback(identities[index], EXPECTED_IDS[index], context),
        )
        for index in range(48)
    )
    correct = sum(port.required and port.feedback == "correct" for port in ports)
    return ModeSnapshot(objective, None, Progress(correct, count, "correct"), ports, identities is not None and correct == count)
```

- [ ] Implement `validate_snapshot` as the only acceptance gate for mode output: exact `ModeSnapshot` and nested value types; nonempty objective; optional string stage; optional `Progress` with exact integer current >=0 and total >0 and nonempty unit; exactly 48 ports with bool membership and one of `open/wrong/correct/unknown`; bool completion; optional tuple of exactly 48 `LedState` values with exact bool channels. Do not impose `current <= total`, which the spec does not require. Reject mutable nested lists. Distinguish `None` LED intent from an all-off frame.
- [ ] Validate registry definitions at construction: nonempty stable ID/label/description, unique ID, callable factory/preview, and optional positive integer port count. Unknown lookups reject cleanly. No discovery or imports of Flask, serial, SQLite, or clocks. Use descriptions `Patch all 24 columns.`, `Patch the leftmost 12 columns.`, and `Patch the leftmost six columns.`; factories always return a fresh object.
- [ ] Implement a test-only `StagedSession`: initialization starts stage `Patch` targeting port 0; correct raw identity at port 0 enters `Wait`, targeting port 1 and recording the elapsed time; ticks complete after 2_000_000_000 ns in `Wait` even with no further observations. Build snapshots with `dataclasses.replace` over a valid field preview. Assert two instances advance independently, input remains immutable, and preview never changes an instance. Add failing-factory, failing-preview, failing-observe/tick, and malformed-output fixtures.

```python
def test_led_absence_and_all_off_are_distinct(valid_snapshot):
    from dataclasses import replace
    from gamemodes import LedState, validate_snapshot
    assert validate_snapshot(valid_snapshot).led_intent is None
    explicit = replace(valid_snapshot, led_intent=(LedState(False, False),) * 48)
    assert validate_snapshot(explicit).led_intent == (LedState(False, False),) * 48
```

Define `valid_snapshot` as a pytest fixture returning Full Field's preview of `GameBoard().snapshot()` with `EvaluationContext(True)`.

- [ ] Run `uv run pytest tests/test_board.py tests/test_gamemodes.py -q`; expect pass. Review the contributor-facing API before wiring transport.

## Task 3: Authoritative Mode Lifecycle

**Files:** Modify `Test_Server/race.py`, `Test_Server/tests/test_race.py`; use `Test_Server/tests/mode_fixtures.py`.

**Consumes:** Board/session API from Tasks 1-2.
**Produces:** Controller additions, origin values, per-player presentations, mode metadata and error state, immutable completed-race submissions.

- [ ] Replace test `VERIFIED = [2] * 48` with `EXPECTED_IDS` and `observe_cells` calls with `observe_board`. Preserve existing name-entry/timing regression tests; update equality assertions to include presentations without weakening their existing clock assertions. Add this start contract:

```python
def test_prepatched_quarter_field_finishes_both_at_zero(clock):
    from board import EXPECTED_IDS
    machine = RaceStateMachine(clock_ns=clock)
    machine.select_mode("quarter-field")
    values = EXPECTED_IDS[:12] + (255,) * 36
    machine.observe_board(1, values)
    machine.observe_board(2, values)
    machine.start()
    state = machine.snapshot()
    assert state["phase"] == "name_entry"
    assert state["tie"] is True
    assert [state["players"][str(p)]["duration_ms"] for p in (1, 2)] == [0, 0]
    machine.reset()
    assert machine.snapshot()["mode"]["id"] == "quarter-field"
```

- [ ] Run `uv run pytest tests/test_race.py -q`; expect missing controller API failures.
- [ ] Initialize registry, selected ID, both `GameBoard`s, verification context, epoch, and revision once in `__init__`, outside `_reset_unlocked`. Reset only race-owned state and rebuild pure previews from retained observations. Save `race_mode_id` at start; never derive completed-race mode from later selection.
- [ ] Refactor internal transitions to receive one captured `now_ns`. In each start transition, create and initialize both sessions using the same `started_ns` and elapsed zero; validate both results before publishing completion. Stage output in local values first so a failure for player 2 cannot leave a partially accepted race. Enter name entry once after evaluating both players.

```python
now_ns = self._clock_ns()
elapsed_ns = max(0, now_ns - self.started_ns)
result = validate_snapshot(session.observe(observation, self.context, elapsed_ns))
```

Use that evaluation pattern inside the lock only; reuse it for initialize/tick with the appropriate method. Do not call a mode from `snapshot()`; snapshots copy already-validated presentation data. Call previews only on ready observation/selection/context changes or reset.
- [ ] Implement `observe_board` with validation and cache publication under the same lock as session delivery. Invalid inputs do not change the board or reach a session. Ready observations evaluate preview. Racing observations evaluate unfinished sessions even if individually stopped. Other phases retain physical observations without advancing sessions. Completed sessions keep their final presentation. On global stop, freeze presentations; reset reveals fresh previews of current physical observations.
- [ ] Evaluate unfinished sessions during racing `tick()` using actual elapsed time; return `changed=True` when objective, stage, targets, progress, feedback, or LED intent changes, even without a phase change. Latch first completion. Keep displayed diagnostic-stop duration frozen, but use original elapsed time for evaluation and allow completion to replace that stop. Stop evaluation on global stop or when both players have diagnostic stops. Preserve the existing case where one player is complete and the other individually stopped: the shared phase remains racing and the unfinished session can still complete.
- [ ] Keep verification state under the controller lock. `set_verification(bool)` updates context and reevaluates ready previews or unfinished racing sessions at one captured time; completed results remain latched. Runtime will no longer read a separate mutable verification flag.
- [ ] Assign `revision` under the controller lock on every accepted published mutation, including partial progress, physical observation publication, selection, verification, persistence status, and deadline refresh. Snapshot reads and clock-only countdown broadcasts do not increment it. Include current revision in both `game_state` and `clock_update`. Add tests for monotonicity across reset and equal revisions on repeated read-only snapshots.
- [ ] Catch factory, initialization, evaluation, and validation exceptions. A ready preview failure sets `mode_error`, blocks start, and leaves other selections usable. An active failure sets `Phase.STOPPED`, freezes clocks, discards sessions/submissions, and publishes a short recoverable error with reset available. Put formatted diagnostic details in the transition for outside-lock logging; do not expose tracebacks in UI. Reset clears the race error, then reports any new preview failure.
- [ ] Test all rejection/reset paths: unknown IDs and non-ready selection; explicit reset, stopped Space reset, dismissal, and automatic expiry preserve selection; startup returns to Full Field. Test factory freshness, stages, tick-only completion, exceptions on every method, unknown initial boards, individual/global stops, clock floor, and latching after unplugging. Use `threading.Event` barriers, not sleeps, to order observe/start/reset in both directions; each delivered observation belongs to the session current when its lock is acquired.
- [ ] Run `uv run pytest tests/test_race.py tests/test_gamemodes.py -q`; expect pass. Review locking and error paths independently before transport integration.

## Task 4: Per-Mode Migration and Snapshot Pagination

**Files:** Modify `Test_Server/leaderboard.py`, `Test_Server/tests/test_leaderboard.py`.

**Consumes:** Explicit immutable `mode_id` on submissions.
**Produces:** `LeaderboardStore.snapshot_boundary(mode_id) -> int`, `page_entries(mode_id, boundary, offset, limit=50) -> LeaderboardPage`, and `LeaderboardPage(rows: tuple[RankedLeaderboardEntry, ...], next_offset: int | None)`.

- [ ] Add a legacy-schema database fixture using literal pre-mode DDL and rows, not the new `SCHEMA`. Assert migration preserves IDs, names, times, timestamps, uniqueness, and assigns `full-field`; reopen twice. Include insertion failure and forced migration rollback tests.
- [ ] Add a 121-row mode dataset with ties across row 50, another mode with faster times, and concurrent inserts after the captured boundary. Assert ranks across pages equal ranks from the complete original snapshot, every ID occurs exactly once, and an empty boundary remains empty after inserts.

```python
def test_empty_snapshot_stays_empty(store):
    boundary = store.snapshot_boundary("quarter-field")
    assert boundary == 0
    store.insert_entries([NewLeaderboardEntry(race_id="later", player_number=1, name="Ada", duration_ms=1, mode_id="quarter-field")])
    page = store.page_entries("quarter-field", boundary, 0)
    assert page.rows == ()
    assert page.next_offset is None
```

- [ ] Run `uv run pytest tests/test_leaderboard.py -q`; expect missing mode/page interface failures.
- [ ] Replace initialization through `executescript` with explicit transaction statements: `BEGIN IMMEDIATE`, create table if missing, inspect `PRAGMA table_info(leaderboard_entries)`, conditionally add `mode_id TEXT NOT NULL DEFAULT 'full-field'`, then create index `(mode_id, duration_ms, created_at, id)`. Roll back on any exception. Do not rebuild IDs or drop the existing uniqueness constraint. Fresh schemas include `mode_id`; normal inserts always pass it explicitly.
- [ ] Validate new entry `mode_id` as a nonempty string without requiring current registry membership: durable old scoring identities must remain readable. Preserve existing score validation and idempotent `(race_id, player_number)` insertion semantics. Query `COALESCE(MAX(id), 0)` with the mode filter for opening boundaries.
- [ ] Execute the following parameterized query with `limit + 1` rows to derive `next_offset`; return at most `limit`. Validate boundary/offset with exact integer types and values >=0, limit with exact integer type and 1-50. Reject bools and invalid modes before SQL.

```sql
SELECT id, race_id, player_number, name, duration_ms, created_at, mode_id, rank
FROM (
    SELECT id, race_id, player_number, name, duration_ms, created_at, mode_id,
           RANK() OVER (ORDER BY duration_ms) AS rank
    FROM leaderboard_entries
    WHERE mode_id = ? AND id <= ?
)
ORDER BY duration_ms, created_at, id
LIMIT ? OFFSET ?
```

Define the dataclass field order to match this projection or map named SQLite row fields explicitly. Ranking uses only duration; deterministic display order also includes creation time and ID. Remove `top_entries` after all callers migrate in Task 5; do not retain an unfiltered fallback.
- [ ] Run `uv run pytest tests/test_leaderboard.py -q`; expect pass including repeat initialization, tie boundaries, offset beyond end, and invalid bounds. Review migration transaction behavior before wiring the real app.

## Task 5: Raw Transport, Persistence Origins, and Page Events

**Files:** Modify `Test_Server/app.py`, `Test_Server/race.py`, `Test_Server/tests/test_app.py`, `Test_Server/tests/test_race.py`, `Test_Server/tests/browser_harness.py`.

**Consumes:** Tasks 1-4 APIs.
**Produces:** Mode-aware transport and immutable operation origins, with no I/O inside the controller lock.

- [ ] Update app fixtures and fake stores to explicit `mode_id`, raw `EXPECTED_IDS`, and the new store methods. Replace assertions on `game_state.leaderboard` with targeted page response assertions. Write two-client tests for selection broadcast, reconnect presentation, and requester-only pages before handlers exist.
- [ ] Define page protocol using the existing event names to keep transport changes local:

```javascript
// Request opening page. Subsequent pages reuse the returned boundary.
socket.emit("request_leaderboard", {
    request_id: 1, mode_id: "quarter-field", race_id: null,
    boundary: null, offset: 0
});
// Targeted response shape:
const pageResponse = {
    request_id: 1, mode_id: "quarter-field", race_id: null,
    boundary: 123, offset: 0, next_offset: 50,
    rows: [], error: null
};
```

`race_id=null` is an idle view allowed in ready/stopped; a post-race request must match the active leaderboard race/mode and wait until `persistence_status` is `saved` or `error`. This prevents a snapshot opening before the race's inserts finish. An error response echoes correlation fields and the requested offset; initial boundary may remain null if boundary acquisition failed, allowing retry from opening. Page size is fixed at 50 at the socket boundary.
- [ ] Run `uv run pytest tests/test_app.py -q`; expect failing transport assertions.
- [ ] Extract `decode_serial_frame(line: bytes) -> tuple[int, ...]` in `app.py`: strict ASCII, exactly 48 hex byte tokens, no signs or Python integer prefixes, token grammar `[0-9A-Fa-f]{1,2}`, reverse once. Pass decoded raw values to `runtime.observe_board`, which delegates directly to the controller. Remove runtime board cache and precomputed-state reduction; keep any compatibility presentation event derived from controller snapshots only until all browser consumers switch in Task 6. Never preserve an authoritative second cache.

```python
def observe_board(self, player_number, values):
    self.handle_change(self.race.observe_board(player_number, values))
```

Update `/randomize` to generate actual open/correct/wrong raw identities and return current presentation state; `/toggle-verify` calls the locked context transition. Keep serial restart/start behavior unchanged. Test reversed wire order using a frame with distinct values, malformed ASCII/text, bad byte lengths/ranges, and partial frames. Mock serial writes and assert no host LED writes occur.
- [ ] Add `select_mode` Socket.IO event with `{mode_id}` and acknowledgement `{accepted, mode_id, error}`. On accepted selection broadcast state to all clients. On non-ready/unknown/malformed requests acknowledge rejection without changing state. Acknowledgement is needed so the dialog can close on confirmation of the already-selected mode, even if no state change occurs.
- [ ] Refactor `_persist_and_load` into `_persist(origin, submissions)`. Mark pending during the locked leaderboard transition; copy `RaceOrigin` regardless of whether submissions are empty. In `handle_change`, emit the leaderboard/pending snapshot before starting the potentially blocking insert, then insert mode-tagged entries outside the lock. Apply and emit `set_persistence(origin, error)` only after expiry processing and exact phase/race/mode checks. Do not capture `self.race.snapshot()["race_id"]` after a write and use it as the origin. Do not read rows as part of persistence. Highlight page rows by stored `race_id == origin.race_id`, not by current browser selection. Add a delayed-insert two-client test proving both see the pending leaderboard before the insert is released and reset remains responsive during that wait.
- [ ] Implement page request validation before reads: dict, positive safe integer request ID, exact nonnegative safe integer offset/boundary (not bool), registered requested mode matching selected/race mode, null boundary only with offset zero, string-or-null race ID matching the view. Cap values at JavaScript's safe integer maximum. Maintain at most one active browsing token per Socket.IO client `(request_id, origin, boundary)`; reject older request IDs and changed boundaries, require a newer ID for reopening, and clear on disconnect. Capture/recheck controller origin around outside-lock boundary/page reads; recheck the per-client token before targeted emit. Protect this small token map separately and never acquire its lock across a database call.
- [ ] Complete the server opening handshake explicitly: keep the opening token's boundary null while capturing the boundary and reading page zero outside locks. On successful read and origin recheck, atomically promote that same token to the captured integer boundary under the token-map lock only if its request ID/origin still match, then send page zero. On initial read failure leave the token boundary null for retry; never recapture a boundary for later pages. Add a Socket.IO test that opens, takes the returned boundary, successfully fetches offset 50 with that boundary, and rejects offset 100 with a changed boundary. Include empty boundary zero and request replacement during opening.
- [ ] Add `leaderboard_activity` event `{race_id}` and controller expiry-first deadline handling. Only the matching live leaderboard can refresh to `now + 60s`; either viewer may do so. Page reads, responses, persistence, reconnect, and broadcast never refresh it.

```python
def test_activity_cannot_revive_expired_leaderboard(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    machine.submit_name(race_id, 1, "")
    machine.submit_name(race_id, 2, "")
    clock.advance_ms(60_000)
    machine.leaderboard_activity(race_id)
    assert machine.snapshot()["phase"] == "ready"
```

- [ ] Add event-barrier tests that delay insert/page reads while another thread resets, starts a new race, changes mode, expires the old view, replaces the request, or disconnects. Verify completed writes retain original mode, but no late result modifies a replacement race or emits a stale page. A blocking fake store must allow concurrent `race.reset()` to finish, proving the lock is not held during I/O. Test unavailable initialization, insertion, boundary read, and page read independently; the next race must still start.
- [ ] Adapt the browser harness's completion endpoint to raw identities; add `POST /__test__/observe/<player>` accepting a JSON array and a reset fixture endpoint that resets both physical boards to unknown using a newly constructed test app/runtime or a test-only board reset helper. Do not place fixture endpoints in production `app.py`.
- [ ] Run `uv run pytest -q`; expect full backend suite passing. Review origin validation, lock ordering, and expiry processing before frontend work.

## Task 6: Generic Board Rendering and Matching Mode Bar

Acceptance evidence: `.superpowers/sdd/task-6-report.md` records matched-viewport screenshots, geometry, tests and review findings. Revision baselines reset before the server's initial state, which can precede the client connect callback.

**Files:** Modify `Test_Server/templates/index.html`, `Test_Server/tests/test_app.py`, `Test_Server/tests/browser_harness.py`.

**Consumes:** `game_state.mode`, registry metadata, player presentations, existing timers.
**Produces:** Mode-neutral live overview, mode identity on results, visible recoverable errors.

Browser checks in Tasks 6-9 are **manual/agent-driven acceptance**, not pytest browser regression tests. The repo has no browser test runner, and this plan does not silently introduce one. Record observed results and screenshot locations in task checkboxes during execution. Backend/domain coverage remains automated. Delegate browser operation to an `independent-execute` subagent which loads `agent-browser`; use a dedicated local target and a second labeled target for multi-viewer checks, not unrelated browser tabs.

After starting the harness in a managed process, the browser worker uses:

```bash
open -a "Brave Browser Beta" --args --remote-debugging-port=9222
agent-browser --cdp 9222 tab new --label gamemodes-a http://127.0.0.1:5001
agent-browser --cdp 9222 set viewport 1920 1080
agent-browser --cdp 9222 snapshot -i
agent-browser --cdp 9222 screenshot
agent-browser --cdp 9222 press m
agent-browser --cdp 9222 eval 'document.activeElement.dataset.modeId'
agent-browser --cdp 9222 press ArrowDown
agent-browser --cdp 9222 press Escape
```

Expected after Task 7: `m` opens the dialog and focuses the selected mode; Escape closes it without selecting the browsed option. Use page-local `eval --stdin` assertions for DOM identity and geometry; do not claim an automated suite from these commands. Capture page-local errors only, following the browser skill's shared-CDP privacy boundary. Stop the harness and close only the created test targets when acceptance is complete.

- [x] Start the local harness with `uv run python tests/browser_harness.py` and load agent-browser. Capture baseline screenshots at 1920x1080 and 1280x720 before edits, including ready, racing, name entry, and leaderboard. Keep screenshots outside tracked source. Record board row/column positions, fonts, and timer layout for comparison.
- [x] Manually check the browser after selecting Quarter Field through Socket.IO: each board has 48 cells, exactly 12 required, indices 0-11 required, and port 12 retains its number and raw feedback but is dimmed. Record the missing behavior before rendering changes. Test unknown before any observation and `M` as a literal name character.
- [x] Track `lastStateRevision` in the browser. Reset it to null on Socket.IO `connect`, invalidate old browsing requests, and accept the next full `game_state` as the reconnect baseline. After that, reject lower revisions but accept equal revisions for countdown refreshes. Ignore `clock_update` until baseline exists and unless race ID, phase, and revision match current state. Add a backend test capturing two snapshots and intentionally emitting newer then older; in browser acceptance, confirm presentation and selected mode cannot revert. Reconnect to a restarted harness to prove a lower fresh-server revision is accepted.
- [x] Change `updateBoard` to consume generic `presentation.ports`; preserve `createBoard` coordinate assignments, `fitBoardCells`, IDs, and existing open/wrong/correct CSS. Add only `.is-inactive` and `.is-unknown` styles. Keep readable numbers with an explicit muted foreground/background rather than fading the whole cell until illegible. Update accessible labels:

```javascript
cell.classList.toggle("is-inactive", !port.required);
cell.classList.toggle("is-unknown", port.feedback === "unknown");
cell.setAttribute("aria-label", `Port ${index}, ${port.required ? "required" : "not required"}, ${port.feedback}`);
```

Do not replay cell-pop on every periodic snapshot; compare previous semantic feedback. Target membership changes from keyboard selection update immediately without a new animation. Remove legacy `table_update` handling when controller presentations supply every state.
- [x] Render objective and optional stage in a small per-player line adjacent to the existing caption/stats, retaining existing humorous captions. Keep objectives visible in the short-height breakpoint where `.switch-caption` is hidden. Reuse the three stat boxes: current/total from server progress, per-second rate for a meaningful counter, remaining clamped to zero. Null progress displays `-` for counter/rate/remaining, not fabricated port counts. For cross-player summary/leader, compare only matching unit/total/stage; otherwise show `-` while preserving card dimensions. Never branch on a mode ID.
- [x] Extend the existing action region with a current-mode label/description, a ready-only `modeButton`, and contextual shortcut text. Keep the bar present in ready/racing/stopped so changing phase does not resize the boards just to hide controls. Reuse `.leaderboard-button` and its `kbd` treatment for the new button, while leaving the old leaderboard button behavior intact until Task 8. Add mode labels to name entry and leaderboard without altering result cards or name fields.

```css
.mode-bar { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.mode-bar__info { min-width: 0; font-family: var(--font-condensed); }
.mode-bar__actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.mode-control:focus-visible { outline: 4px solid var(--blue); outline-offset: 4px; }
```

- [x] Show `mode_error` using the existing error surface treatment with `role="alert"`; advertise reset for stopped errors, and leave ready mode selection available after preview errors. Starting a broken preview remains blocked server-side.
- [x] Re-run browser assertions, inspect 1920x1080 screenshots against baseline, then run `uv run pytest tests/test_app.py -q`. Confirm no unintended header, timer, typography, name-entry, or physical-grid changes. Do not run a whole-template formatter.

## Task 7: Local Modal Selection and Keyboard Ownership

Acceptance evidence: `.superpowers/sdd/task-7-report.md` records two-viewer keyboard/focus checks, acknowledgement generations, screenshots and review fixes.

**Files:** Modify `Test_Server/templates/index.html`, `Test_Server/tests/test_app.py`.

**Consumes:** Registry metadata, `select_mode` acknowledgement, current phase/mode.
**Produces:** Hidden-by-default accessible selection dialog and guarded shortcuts.

- [x] Record manual before/after browser checks for selected-option focus on open, arrows clamped at ends, Tab/Shift+Tab including close control, Escape/M cancel, Enter/click confirmation, and Space suppression. Use two dedicated browser targets to verify local visibility and authoritative shared selection.
- [x] Add dialog markup outside the phase views so `showModal()` makes background controls inert. Populate ordinary buttons once from metadata; update selected markers in place on external changes, never rebuild focused options on every state event.

```html
<dialog id="modeDialog" aria-labelledby="modeDialogTitle">
    <h2 id="modeDialogTitle">Game mode</h2>
    <div id="modeOptions"></div>
    <p id="modeSelectionError" role="status" hidden></p>
    <p>Arrows: browse | Enter: select | Esc / M: cancel</p>
    <button id="closeModeDialog" class="leaderboard-button" type="button">Close</button>
</dialog>
```

Style the dialog with existing `--paper`, `--ink`, `--yellow`, and font variables, 4px black border and offset shadow, max-height constrained to viewport with internal overflow. Ordinary option buttons include `data-mode-id`, text label/description, and `aria-pressed` for the selected marker. The native dialog supplies modality; explicitly constrain Tab at its first/last focusable control if browser behavior does not keep focus inside.
- [x] Implement `openModeSelector()`, `closeModeSelector({restoreFocus=true}={})`, and `confirmMode(modeId)`. Opening is allowed only in ready with no open/pending idle leaderboard. Opening focuses the selected button. Cancel never emits selection. Confirmation captures a local dialog generation; disable duplicate confirmations while pending, then close only if that generation is still current and acknowledgement accepted. On rejected selection show the error while ready or close on external phase change. Clicking the selected mode is still a successful confirmation.
- [x] Put text-entry checks and local-overlay ownership before global shortcuts. Preserve name-entry Enter/Tab behavior. Do not intercept modifier shortcuts or input/textarea/select/contenteditable typing. While modal is open, consume Space, M, Escape, arrows, and prevent L/V/S or race controls from reaching background handlers. Tab visits all option buttons and close; arrows visit options only without wrap.

```javascript
function moveModeFocus(step) {
    const options = Array.from(document.querySelectorAll("#modeOptions button"));
    const index = options.indexOf(document.activeElement);
    if (index < 0) return;
    options[Math.max(0, Math.min(options.length - 1, index + step))]?.focus();
}
```

- [x] On any authoritative phase change close the selector without restoring focus to a hidden mode button. Focus the first unresolved name field, leaderboard heading, or overview heading (`tabindex="-1"`) as appropriate. On external selection while still ready, update only marker and overview presentation, preserving locally browsed focus. On normal cancel/confirm restore mode-control focus.
- [x] Verify with browser: open M, move to Quarter, cancel and inspect unchanged mode; reopen and confirm; verify other client updates; open while another client starts; type `M` in both name fields; inspect focus outline separately from selected marker; verify no background HTTP control calls. Run `uv run pytest tests/test_app.py -q` after updating limited markup assertions. Behavioral browser checks, not string assertions alone, are the acceptance gate.

## Task 8: Local Page Browsing, Focus, and Activity

Acceptance evidence: `.superpowers/sdd/task-8-report.md` records pagination, genuine input, deadlines, final screenshots and review. It also explains the bounded fresh-snapshot recovery for the backend's unpublished browse epoch; this is not automatic invalidation detection.

**Files:** Modify `Test_Server/templates/index.html`, `Test_Server/tests/browser_harness.py`, `Test_Server/tests/test_app.py`.

**Consumes:** Targeted page protocol, mode/race metadata, persistence state, activity event.
**Produces:** All-score browsing with retained DOM/focus and separate idle/post-race deadlines.

- [x] Seed 121 scores per field mode in the harness, with ties crossing page boundaries. Add harness-only flags for failing/delaying the next page, and a release endpoint to unblock a delayed read using `threading.Event`. Exercise those flags through browser-driven HTTP calls. Seed faster scores ahead of a new completed race so its highlight appears beyond page one.
- [x] Before implementation, demonstrate failure to reach row 51 and failure to retain a row DOM node across periodic `game_state`. Then test the new browser state contract:

```javascript
let browsing = null;
// Created on open, not on each countdown broadcast:
function newBrowsingState(requestId, modeId, raceId) {
    return {
        requestId, modeId, raceId, boundary: null, nextOffset: 0,
        pendingOffset: null, rows: new Map(), navigationIntent: null,
        focusGeneration: 0
    };
}
```

- [x] Replace the `Top 10` title with `Leaderboard` plus the visible selected/completed mode label. Place a visible close button before a labeled `.leaderboard-scroll` container, keep native table markup and existing column styling, then load-more/retry control after rows. Scope height/layout changes to the leaderboard so the scroll area flexes within the existing view; use `min-height:0; overflow:auto` and retain the external countdown footer. Do not let the kiosk page scroll at 1920x1080.
- [x] Implement `openLeaderboard`, `requestNextPage`, `appendLeaderboardPage`, and `closeLeaderboard` around the browser-local token. Open immediately into a loading state and focus the heading. Post-race open waits to fetch until persistence settles; a countdown tick cannot reopen a locally closed/replaced request. On a fresh Socket.IO connection reset request/revision baselines and reopen eligible post-race browsing as a new snapshot without extending the server deadline.
- [x] Request only one page at a time. The opening handshake is special: while local boundary is null and pending offset is zero, accept a matching request/mode/race/offset response with a nonnegative safe integer boundary and atomically adopt that boundary, including zero, before appending. On an opening read error, retain a null boundary and allow offset-zero retry. Later pages require exact equality with the adopted boundary as well as matching request/mode/race/expected offset. Test opening success, empty boundary zero, retry after opening failure, and rejection of a changed later boundary. Store row identity by database ID, append each `<tr tabindex="0">` once with `tableRow.dataset.scoreId = String(row.id)`, and preserve existing DOM nodes. Use `textContent` for names and descriptions. Remove empty/loading rows only when real rows arrive. Countdown/state handlers update labels/errors/deadline only, never call `replaceChildren()` on loaded rows.
- [x] Implement keyboard navigation with native Tab order intact. Down on heading enters first loaded score; Up/Down moves to adjacent loaded rows. At the last row with more data, request a page and retain focus. Enter on load-more/retry requests a page. Capture `{sourceElement, focusGeneration, requestId}` as navigation intent; a `focusin` increments generation. Move focus to the first appended row only if source focus and generation still match and the same view is open. Cancel stale intent on close/navigation away; do not resurrect it if the user later returns to the same element. Repeated keys during pending load must not create duplicate requests.
- [x] Prefetch near the loaded end (within five rows or one viewport), without changing focus or deadlines. On failure retain existing rows and initiating focus, announce error, and enable retry. At final row Down stays in place; Tab can reach controls. Row focus uses a blue outline distinct from yellow current-race highlighting. Never jump automatically to current-race rows.
- [x] Close local browsing on mode changes and non-idle phases. Preserve L/Escape dismissal in both views and Space dismissal only post-race. Local view and pending local open suppress race Space; M cannot open behind either. Selector prevents opening leaderboard behind it. Normal dismissal/expiry restores overview focus; external phase changes focus the appropriate new phase.
- [x] Implement `noteLeaderboardActivity()` with a local deadline refresh for idle and `{race_id}` emission for post-race. Count valid keyboard navigation, load-more/retry activation, and user scrolling; do not count countdowns, page responses, reconnect, `focus()` alone, or `scrollIntoView()` alone. Track wheel/touch/pointer scrollbar/key input intent before scroll events, and suppress programmatic scroll accounting; `scroll.isTrusted` alone cannot distinguish these. Throttle continuous input notifications to one per second with a trailing notification while still open, and clear timers/intents on close. Navigation triggering scroll counts once. Check local expiry before refreshing so a delayed input cannot revive an expired view.
- [x] Verify real browser behaviors: row 121 reachable by arrows and Tab; ties/highlights beyond page one; retained row identity/scroll through countdowns; page failure/retry; delayed append after focus moves away/back cannot steal focus; no duplicate pending loads; empty boundary stays empty after inserts; mode/race changes drop old responses; both viewers extend shared post-race deadline; only active local viewer extends idle deadline. Use the harness fake server clock for server expiry. For local idle acceptance, temporarily replace page-local `Date.now` with a controllable offset via `eval --stdin`, move it beyond the deadline, and invoke the same expiry-check function used by the countdown timer; reload the dedicated target afterward. Ensure the timer calls that expiry-check function, not just a fixed timeout. Do not change production timeout constants for tests.
- [x] Run `uv run pytest -q` and compare leaderboard screenshots with baseline for unchanged table typography, borders, time formatting, and highlight treatment. Review browser token lifecycle and inactivity sources independently.

## Task 9: Contributor Guide and Final Acceptance

**Files:** Create `docs/gamemode-authoring.md`; modify `README.md`; finalize affected tests only.

**Consumes:** Verified board/session/registry contracts and working built-in implementation.
**Produces:** Contributor entry point and a checked acceptance record in this plan.

- [x] Write a concise guide with the actual API signatures from `gamemodes.py`, registry insertion location, canonical index/coordinate layout, open identity 255 and unknown observations, explicit verification context, factory freshness, pure previews, initialize/observe/tick lifecycle, nanosecond elapsed time and one-second scheduling limitation, frozen output validation, optional LED intent, and durable scoring IDs. Link to built-in field evaluator and `tests/mode_fixtures.py` as static and stateful examples; do not add a fourth production mode.
- [x] Link the guide from README with an annotation rather than duplicating it:

```markdown
## Development

[Adding gamemodes](docs/gamemode-authoring.md) explains the board/session contract, registry, validation, and scoring identities. Start there when adding a mode; firmware protocols and deployment are outside its scope.
```

- [x] Check contributor extensibility by registering the staged fixture through `create_app(registry=...)` only. Verify two browsers restore distinct per-player stages on reconnect without new routes, mode-ID frontend branches, or schema changes. Verify LED intent validation produces no serial write.
- [x] Run from the worktree's `Test_Server`:

```bash
uv run pytest -q
uvx ruff check board.py gamemodes.py race.py leaderboard.py app.py tests
uvx ruff format --check board.py gamemodes.py race.py leaderboard.py app.py tests
```

Expected: all tests pass and no new lint/format findings. Fix only touched code; report preexisting findings instead of drive-by formatting. For inline JavaScript, extract the script through a parser or browser DOM into a temporary file and run `node --check` on that file, then inspect browser console/errors. Do not apply a formatter to the entire legacy template.
- [x] Run final browser acceptance at 1920x1080, 1280x720, and a narrow viewport, with reduced-motion enabled as a separate check. Confirm no clipped cells, hidden controls, focus escape, console errors, or changed timer/result presentation. Explicitly compare before/after overview, dialog, name-entry, and leaderboard screenshots. Narrow layouts may use existing page scrolling; desktop leaderboard scrolling remains internal.
- [x] Perform an independent change review covering controller lock/I/O boundaries, exact score migration, delayed operations, full keyboard flow, and preservation of baseline UI. Fix blocking findings and rerun affected tests before claiming completion. Evaluate docs using `update-docs`; update the guide only where implementation differs from the planned contract. No deployment commands or remote database operations.

Acceptance completed on 2026-10-05: 296 tests pass, Ruff check/format clean,
inline JavaScript syntax and independent reviews pass. Local browser evidence
and remaining viewport limitations are in `.superpowers/sdd/layout-report.md`,
`final-fixes-report.md` and `result-layout-fixes-report.md`; these cover visual,
reconnect, staged-mode and result-screen acceptance, not live hardware racing.
The user separately authorized deployment, superseding this plan's original
remote-operation exclusion. Deployment evidence and rollback backup location
are in `.superpowers/sdd/deployment-report.md`.

## Coverage Map

| Spec requirement | Implementation and verification |
| --- | --- |
| Raw identities, exact frames, canonical layout, unknown initial state | Tasks 1, 5; board and serial decoder tests |
| Three field regions, ignored ports, bypass, pre-patched starts | Tasks 2-3; parameterized mode tests and zero-time ties |
| Independent sessions, stages, tick-only advancement, fresh reset | Tasks 2-3, 9; staged fixture and reconnect acceptance |
| Serialized observations/start/reset, first completion, manual/global stops | Task 3; event-barrier and clock tests |
| Invalid mode output, exceptions, recoverable error/no scores | Tasks 2-3, 6; validation and visible error acceptance |
| Optional LED intent and no hardware output | Tasks 2, 5, 9; frame validation and mocked serial writes |
| Selector visibility, keyboard/focus, multi-client selection | Task 7; real two-browser acceptance |
| Familiar UI, fixed physical positions, generic progress | Task 6; baseline screenshot and geometry comparison |
| Migration, per-mode ranks, bounded stable pages, empty snapshots | Task 4; real SQLite tests |
| Immutable score/page origins and late-operation rejection | Task 5; blocking store tests and per-client token checks |
| Browser page append/focus/retry and stale responses | Task 8; delayed browser fixtures and DOM identity assertions |
| Shared/local inactivity and expiry-first validation | Tasks 5, 8; fake clocks and explicit activity sources |
| Preserved simultaneous names, ties, timings, database fallback | Tasks 3, 5, 9; retained regression suite |
| New modes without transport/browser/schema edits | Tasks 2, 9; authoring guide and fixture-only integration |

## Execution Handoff

Implement tasks in order. Tasks 1-2 and 4 can be developed independently only if their published contracts remain identical; `race.py`, `app.py`, and `index.html` tasks should be sequential to avoid overlapping edits. Each task's tests and review gate precede the next task. Leave `.superpowers/` and the existing untracked spec untouched unless explicitly asked to update them.

Execution options: subagent-driven work with a fresh implementer/reviewer per task, or inline execution with checkpoints. This planning session does not start either option.
