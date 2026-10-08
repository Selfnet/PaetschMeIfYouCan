# Adding Gamemodes

A mode owns its objective, per-port feedback, progression, and completion. The
shared runtime owns board input, race timing, publication, and score persistence.
Add a definition and session, not a route, frontend mode-ID branch, or database
column. Firmware protocols and deployment are outside this guide.

## Board Input

[`board.py`](../Test_Server/board.py) defines the canonical board model and
coordinate helpers; use it rather than interpreting serial frames inside a mode.
`BoardObservation.identities` is either `None` (no valid observation yet) or an
immutable tuple of 48 byte identities. A malformed frame does not replace the
last valid observation. Unknown is not an all-open board; do not infer completion
from missing input. Identity `255` (`OPEN_ID`) means an open port.

Indices `0..47` are column-major across 24 columns and two rows:
`coordinates(index) == (index // 2, index % 2)` and
`port_index(column, row) == column * 2 + row`. Row 0 is the outside socket, row 1
the underside. Index 0/1 is the leftmost column; index 46/47 is the rightmost.
`EXPECTED_IDS` uses this same order. The adapter reverses wire order once, in
`decode_serial_frame`; neither sessions nor presentation code should reverse it.

## Definition And Lifecycle

[`gamemodes.py`](../Test_Server/gamemodes.py) is authoritative for the frozen
value types, protocol, and validator. The definition has these constructor fields:

```python
GameMode(
    id: str,
    label: str,
    description: str,
    required_port_count: int | None,
    session_factory: Callable[[], ModeSession],
    preview: Callable[[BoardObservation, EvaluationContext], ModeSnapshot],
)
```

IDs, labels, and descriptions must be nonempty. `required_port_count` is positive
or `None` for a mode without a fixed count; it is metadata, not a completion rule.
Both preview and session methods receive `EvaluationContext(verify: bool)`
explicitly. Define the mode's verification policy from that context, not globals.
For the built-in fields, verification requires matching `EXPECTED_IDS`; with it
off, any non-open identity is correct. Open remains open in either case.

Preview must be pure: return a ready-state presentation without creating,
advancing, or resetting a race session. Repeated preview calls must not change
future race behavior. `GameMode.new_session()` calls the factory with no arguments;
return a fresh object for every player on every start. Never return a singleton or
share mutable stage state between sessions or races. The runtime rejects the same
object returned for both players, but that check does not prove factory freshness.

Implement all three `ModeSession` methods with these signatures:

```python
def initialize(self, observation: BoardObservation,
               context: EvaluationContext, elapsed_ns: int) -> ModeSnapshot: ...
def observe(self, observation: BoardObservation,
            context: EvaluationContext, elapsed_ns: int) -> ModeSnapshot: ...
def tick(self, observation: BoardObservation,
         context: EvaluationContext, elapsed_ns: int) -> ModeSnapshot: ...
```

`initialize` receives the current board and elapsed time `0` at race start; it may
complete an already-satisfied objective immediately. During racing, `observe`
runs for valid board input (including repeated input) and verification changes.
`tick` evaluates time-dependent progress even without new input. Calls stop for
completed players. Reset discards sessions and returns to pure previews;
reconnect publishes cached presentations without reinitializing sessions.

Published `game_state.browse_epoch` identifies the runtime's leaderboard browse
generation. Browsers close local browsing when it changes, including same-mode
confirmation or a ready-state reset. Ordinary observations, progress ticks, and
persistence updates retain the generation; modes do not manage it. The lifecycle
and snapshot in [`race.py`](../Test_Server/race.py) define this transport contract,
not score storage or mode completion.

`elapsed_ns` is monotonic time since the **original shared race start**, not
displayed milliseconds or time since the last callback. A manually stopped
player's diagnostic display clock freezes, but its session still evaluates on
that original elapsed time while the race remains active. Store stage timestamps
in the session and subtract them from `elapsed_ns`; do not count callbacks.
[`race.py`](../Test_Server/race.py) owns these lifecycle and completion rules.
[`app.py`](../Test_Server/app.py) owns runtime scheduling and transport, not mode
logic: its background loop sleeps one second between ticks. Nanosecond precision
does not guarantee subsecond callbacks; completion is timestamped when evaluated.

## Output Contract

Return frozen `ModeSnapshot(objective, stage, progress, ports, complete,
led_intent=None)` values. Nested `Progress`, `PortPresentation`, and `LedState`
are frozen too. Validation requires the exact dataclass types, not subclasses or
dictionary lookalikes, and exact tuples rather than lists:

- `objective`: nonempty string; `stage`: string or `None`.
- `progress`: `Progress(current, total, unit)` or `None`; current is a nonnegative
  integer, total a positive integer, unit a nonempty string. Current may exceed
  total. Booleans are not integer counters.
- `ports`: exactly 48 `PortPresentation(required: bool, feedback: str)` entries
  in canonical order. Feedback is `open`, `wrong`, `correct`, or `unknown`.
  Required flags express the current objective; completion is the mode's own
  boolean, not a runtime count of required/correct ports.
- `led_intent`: `None` or exactly 48 `LedState(yellow: bool, green: bool)` entries.
  Absence differs from an explicit all-off tuple. This is optional intent only:
  the runtime validates and publishes it but **never writes it to serial**.

Every preview and lifecycle result passes `validate_snapshot` before publication.
An exception or invalid race output stops the race with a recoverable mode error;
invalid previews prevent starting until recovery. Keep mode code free of I/O.

## Registration And Scores

Insert production definitions in the explicit `BUILTIN_MODES = ModeRegistry(...)`
construction at the end of `gamemodes.py`. There is no plugin discovery; registry
order determines selector order and duplicate IDs are rejected. For isolated
tests, inject `ModeRegistry([...])` through `create_app(registry=...)`, which returns
`(app, socketio)`. Include `full-field`: it is the runtime's initial selected ID.
The staged fixture belongs in tests, not the production registry.

Treat a mode ID as a durable scoring identity. Scores capture the race's selected
ID and leaderboard queries filter by it. Changing labels does not migrate scores;
renaming/removing an ID leaves old rows stored, without a selector entry. Assign
a new ID when changed rules make old times incomparable; reuse an ID only when
that comparison remains valid. [`leaderboard.py`](../Test_Server/leaderboard.py)
owns storage and migration: legacy rows without a mode column are assigned
`full-field`, preserving their IDs and existing data. New modes need no schema
change; do not retag historical scores to a new ruleset.

Leaderboard slots are a separate event namespace, not mode IDs. The runtime
captures the slot alongside the mode at race start, and queries filter by both.
Modes must not select slots or handle persistence. The [leaderboard slot contract](../README.md#leaderboard-slots)
describes switching, restart persistence, migration, and rollback constraints;
it does not define mode rules or firmware behavior.

## Examples And Checks

- [`_evaluate_field` and `_FieldSession`](../Test_Server/gamemodes.py) show static
  objectives sharing a pure evaluator for previews and all lifecycle methods.
  Start here for fixed required-port sets; they do not illustrate stage state.
- [`StagedSession` and `STAGED_MODE`](../Test_Server/tests/mode_fixtures.py) show
  independent patch/wait stages, elapsed-time timestamps, and a pure initial
  preview. This is a test fixture, not a production verification-policy template:
  its stage transition deliberately checks the exact raw port-0 identity.
- [`test_gamemodes.py`](../Test_Server/tests/test_gamemodes.py) covers validation,
  freshness, pure previews, and staged ticks; [`test_race.py`](../Test_Server/tests/test_race.py)
  covers shared timing and failure recovery. [`test_app.py`](../Test_Server/tests/test_app.py)
  covers transport and cached reconnects, not physical browser or hardware tests.

Exercise unknown/open/wrong/correct input, verification changes, repeated input,
fresh starts, independent players, time-only progression, and invalid outputs.
Verify a custom injected registry restores each player's stage on reconnect
without special routes or rendering branches, and LED intent causes no serial
write. Run from `Test_Server`:

```sh
uv run pytest -q
uvx ruff check board.py gamemodes.py race.py leaderboard.py app.py tests
```
