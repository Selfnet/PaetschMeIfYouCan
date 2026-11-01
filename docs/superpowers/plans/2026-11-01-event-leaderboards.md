# Scheduled Event Leaderboards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add independent scheduled event leaderboards, safe race-start score
locking, archive browsing, and operator administration, then deploy without losing
scores or interrupting play.

**Architecture:** Keep SQLite and ranking in `leaderboard.py`, race lifecycle in
`race.py`, and side effects in `app.py`. Add one small pure routing-policy module.
Distinguish the next race's destination, the current race's captured identity, and
each browser's viewed leaderboard. Use the existing runtime loop and Socket.IO.

**Tech Stack:** Python 3.11+, Flask/Socket.IO, SQLite, vanilla JavaScript/CSS,
pytest, Node-based frontend tests, existing local browser harness.

**Spec:** [Approved design](../specs/2026-11-01-event-leaderboards-design.md).

## Global Constraints

- Named events are independent leaderboards, not names attached to slots 1-9.
- The server chooses the destination at race start. Both players, name entry,
  delayed writes, and retries retain that destination across event boundaries.
- Outside events, the existing manual selection of numbered slots remains.
- Event expiry resets the default and next-race destination to slot 1.
- Event times use Europe/Berlin. Windows do not overlap.
- An event is active on the half-open interval `start <= now < end`.
- `0` returns to the active event and does nothing when no event is active.
- In the leaderboard view, `E` opens a list of past events, newest first.
- Named boards show `Leaderboard - <event name>`; numbered boards show exactly
  `Leaderboard`.
- The event name is trimmed, printable text of 1-60 Unicode code points.
- New races are blocked when their score destination cannot be established.
- Preserve all existing score fields and associations; never use production data
  for interactive tests. No event or schedule is created in production by deployment.
- User follow-up: keep this elegant and simple, and remove the bright orange
  accent bar from the leader highlight. Retain the quieter leading-state treatment.
- No new framework, scheduler service, recurring-event engine, or generic plugin
  infrastructure. No unrelated refactor or deployment-helper overhaul.

## Review Focus

- A duplicated numbered selection after offline expiry must renew the manual
  choice rather than be swallowed by existing same-slot shortcuts (Task 1/2).
- Event edits and starts racing at a boundary must use one serialized authoritative
  decision, including direct legacy control routes (Task 1).
- Slow initial saves must retain recovery after expiry, reset attempts, and archive
  navigation, just like retries (Task 1/2).
- An old archive/page response must not replace a newly selected board, close a
  different user's archive, or hide failed-score recovery (Task 1/2).
- Unknown storage state must recover in process; retries must not overwrite a live
  race, its captured identity, or the maintenance setting (Task 1/3).

## Files And Ownership

- Create `Test_Server/leaderboard_policy.py`: frozen metadata values, UTC/Berlin
  parsing and pure active/default destination resolution. No I/O.
- Modify `Test_Server/leaderboard.py`: catalog/settings migration, generic score
  identity, transactional event CRUD, existing grouped score queries.
- Modify `Test_Server/race.py`: captured leaderboard ID, independent browse origin,
  pending-save protection. Keep phases and invariants together.
- Modify `Test_Server/app.py`: confirmed policy cache, wall-clock injection,
  event/control/browse handlers, storage retry, publication and start serialization.
- Modify `Test_Server/preview.py` and `tests/browser_harness.py` only for the new
  identity/time injection contracts; preserve disposable-database isolation.
- Modify `templates/index.html`, `templates/control.html`, `static/dashboard.css`:
  event views, picker, administration, and restrained leader highlight.
- Add `static/event-controls.js` for control-panel event administration if needed
  to keep new form logic out of the small existing maintenance script. Keep the
  dashboard's tightly coupled state/navigation logic in its existing script.
- Add focused policy/storage/runtime/UI tests and adapt existing contract tests.
- Update `README.md` and preview/gamemode documentation where contracts change.

## Shared Contracts

These names form the integration boundary between the backend and frontend tasks.
Implementation details inside each owner can remain small and idiomatic.

### Data And Policy

`Leaderboard(id: int, slot: int | None, name: str | None,
starts_at: datetime | None, ends_at: datetime | None)` is frozen metadata. Numbered
boards have slots 1-9 and no event fields; events have no slot and all event fields.
Numbered catalog IDs are seeded to 1-9 during migration, but callers use `slot`
metadata, not arithmetic on IDs, to identify numbered boards.

`LeaderboardPolicy(boards: tuple[Leaderboard, ...], manual_slot: int,
manual_selected_at: datetime, revision: int)` holds a confirmed storage snapshot.
Use timezone-aware UTC datetimes internally and canonical UTC ISO strings in SQL
and transport. `active_event(policy, now) -> Leaderboard | None` and
`resolve_destination(policy, now) -> Leaderboard` implement the spec's half-open
window and last-manual-selection timestamp fallback. `parse_event_time(value:
str) -> datetime` accepts explicit offsets or unambiguous Europe/Berlin local
input, rejecting invalid/ambiguous/nonexistent local times.

`LeaderboardStore.load_policy() -> LeaderboardPolicy` returns a consistent read.
`set_selected_slot(slot: int, *, selected_at: datetime | None = None) -> None`
records a fresh manual choice, including a same-slot choice.
`save_event(data: dict, expected_revision: int, now: datetime) -> Leaderboard`
creates when `data.id` is absent or edits the referenced event otherwise.
`cancel_event(event_id: int, expected_revision: int, now: datetime) -> None`
removes only a future empty event. `end_event(event_id: int,
expected_revision: int, now: datetime) -> Leaderboard` implements End now.
All three serialize revision validation, overlap checks, and persistence in one
SQLite transaction. Editing rules come from the spec, not browser trust.

Score value objects, query arguments/results, race/submission origins, and the
score schema use `leaderboard_id`, not `leaderboard_slot`, for score identity.
Keep slot-only validation for manual numbered selection. Existing ranking,
personal-best, pagination and idempotence semantics remain unchanged.

### Runtime And Transport

`create_app(..., now_utc: Callable[[], datetime] = ...)` injects wall time separately
from `clock_ns`. Runtime starts resolve policy under the publication lock, then
pass the resolved ID into the state machine. Low-level state-machine unit tests
may supply an explicit ID; production routes cannot bypass policy resolution.

Every `game_state` adds a `leaderboard_policy` object:

```text
available: boolean
error: string | null
revision: integer | null
next: leaderboard metadata | null
active_event: leaderboard metadata | null
numbered: nine leaderboard metadata objects, or [] when unknown
```

Metadata is `{id, slot, name, starts_at, ends_at}` with UTC ISO event times.
`game_state.race_leaderboard` is captured board metadata or null. The existing
`leaderboard_slot` snapshot field is replaced, not repurposed. Race IDs, state
revisions, browse epochs and persistence fields retain their other meanings.
Policy changes update metadata without invalidating an explicit same-mode view.
Every `game_state` also includes `server_now`, a UTC ISO timestamp from the same
wall-time sample used for policy evaluation. It does not itself change the policy
revision. Operator status labels use this value rather than the browser clock.

Socket commands use acknowledgement objects `{accepted, error, ...}`:

- `select_leaderboard_slot {leaderboard_slot}`: existing numbered scoring command;
  ready-only, reject during event lock, acknowledge the effective numbered slot.
  Every successful selection also broadcasts `leaderboard_selected {leaderboard,
  revision}` after its committed state, including same-slot selections. This
  explicit signal updates other displays' ordinary numbered views; event edits
  and schedule boundaries never emit it, and explicit archives remain local.
- `request_leaderboard`: existing request IDs/boundaries/offset/mode/race context,
  with `leaderboard_id` replacing the requested slot. Allow any existing eligible
  board independently of routing. Result packets echo `leaderboard_id` and include
  `leaderboard` metadata for titles; only the captured board gets race highlighting.
- `list_events {past_only: boolean}`: acknowledgement
  `{accepted, error, events: metadata[], revision}`; newest ended first for the
  picker, all events for administration. Client generation tokens guard late replies.
- `save_event {event: {id?, name, starts_at, ends_at, start_now?}, revision}`:
  acknowledge `{accepted, error, event?, revision?}` after commit.
- `cancel_event {id, revision}` / `end_event {id, revision}`: same acknowledgement.
- `retry_leaderboard_storage {}`: reload/reinitialize storage and publish routing
  availability without resetting race/recovery/maintenance.

Backend policy resolves before every start, manual selection, connection snapshot
and background publication. A known last-committed schedule can continue routing
while score storage is failing; unknown policy cannot start a race. Name/time
edits publish a new policy revision and `leaderboard_changed {leaderboard,
revision}` for the changed board; open archive titles refresh without losing
pagination. No full archive is broadcast once per second.

---

### Task 1: Backend Identity, Scheduling, And Recovery

**Files:** `leaderboard_policy.py`, `leaderboard.py`, `race.py`, `app.py`,
`preview.py`; backend tests and fixture/harness adaptations only. Do not edit UI
templates, styles, or frontend behavior tests in this task.

**Consumes:** Existing SQLite score/settings schema, state machine, Socket.IO
handlers, README contracts, and the approved spec.

**Produces:** All Data And Policy and Runtime And Transport contracts above.
This backend milestone is not deployed until the UI task is complete.

- [ ] Write `tests/test_event_policy.py` with literal UTC expectations for starts
  before/at/end, adjacent events, Berlin conversion/DST rejection, offline expiry,
  and manual choice after expiry. For example, event `[10:00Z,12:00Z)` resolves to
  its ID at `10:00Z`, and to numbered slot 1 at `12:00Z` when the choice is older.
- [ ] Run `uv run pytest -q tests/test_event_policy.py`; record the expected RED
  evidence before implementing the policy module, then implement and rerun GREEN.
- [ ] Write `tests/test_event_storage.py` using actual disposable legacy SQLite
  schemas. Assert exact score/settings preservation, distinct empty same-name
  events, independent mode/PB scope, atomic invalid/stale/overlap rejection,
  rename/edit/end/cancel eligibility, and same-slot timestamp renewal.
- [ ] Run the storage tests RED, implement transactional migration/catalog CRUD
  and generic score queries, then run them GREEN. Do not leave a slot/event-ID
  compatibility wrapper that confuses the new identity model.
- [ ] Write `tests/test_event_runtime.py` covering both start routes and Space,
  last-tick/start-boundary races, serialized concurrent operator edits, locked
  old-client selection, cross-boundary names/retries, multi-client independent
  browsing, and restart across an entire event followed by fresh same-slot choice.
  Assert actual saved rows and transport payloads, not mock-call existence.
- [ ] Add recovery tests for a blocked initial write followed by failure while
  dismiss/reset/timeout are attempted. Assert results stay recoverable, then retry
  stores both original identities even after End now or expiry.
- [ ] Run focused tests RED; implement runtime routing, captured race IDs, browse
  independence, policy broadcasts and storage retry. Reuse existing locks and
  background loop; do not introduce a second game lifecycle.
- [ ] Add unknown-startup-storage -> repair -> in-process-retry tests, and a
  mid-race reload test preserving race origin and maintenance. Exercise malformed
  IDs/booleans/revisions/time values and stale pages without production resources.
- [ ] Adapt legacy backend tests to the explicitly changed contracts; retain tests
  for old slot associations and idempotence. Update preview fixtures to generic IDs.
- [ ] Run `uv run pytest -q` and `uvx ruff check .` / `uvx ruff format --check .`
  from `Test_Server`. Report exact results and TDD evidence. Parent reviews the
  diff before committing `feat: add scheduled leaderboard routing`.

### Task 2: Player And Operator Interfaces

**Files:** `templates/index.html`, `templates/control.html`,
`static/dashboard.css`, optional `static/event-controls.js`, UI tests,
`README.md`, `docs/local-preview.md`, `docs/gamemode-authoring.md` as needed.

**Consumes:** Task 1's exact public state, metadata and Socket.IO contracts. Inspect
the implemented handlers before wiring them; raise any contract mismatch to the
controller instead of inventing a second transport shape.

**Produces:** Complete named/numbered navigation, E archive picker, event controls,
quiet leader highlight, and updated behavior documentation.

- [ ] Write/extend `tests/test_event_ui.py` executing actual JavaScript with the
  existing Node test pattern. Pin title selection, L/0/E/digit guards, archive
  request generations, local view changes without scoring changes while locked,
  same stored-slot reselection after expiry, and persistence-error visibility on
  other boards. Observe RED before implementing helpers/listeners.
- [ ] Wire next-race policy and captured-race metadata separately from local
  browsing. Numbered selection outside events still calls the server scoring
  command; event-locked and stopped/result navigation only changes the view.
  Replace slot-based request/response matching with the generic leaderboard ID.
- [ ] Add the Past events dialog and pointer control. Use newest-ended-first
  server ordering, name plus Berlin range, arrows/Enter, Escape/E close, focus
  restoration, empty/loading/error states and generation-safe replies. Prevent
  underlying shortcuts while open, and preserve normal inactivity handling.
- [ ] Add Current event/0 navigation, correct named/numbered titles and compact
  score-destination status. Post-race default is always the captured board; show
  retry/abandonment controls independently of the board being browsed. Rename
  updates must not discard score pagination or steal focus.
- [ ] Write control-form tests RED for create/edit/end/cancel, time/name errors,
  stale revisions, failed/timed-out acknowledgements, and storage retry. Implement
  a straightforward form and event list using existing button/fieldset patterns.
  Use browser-local form values as Berlin input, never automatic local-zone conversion.
- [ ] Remove the bright `border-left` accent and leading-state border color from
  `.player-lead`; retain the existing subtle background/text leading indication.
  Verify ties clear the leading state and no layout shifts are introduced.
- [ ] Adapt existing UI tests/fixtures for the new protocol while preserving name
  entry, maintenance, live-rate, winner positioning and generic-mode behavior.
- [ ] Update README with event and keyboard contracts plus test links; correct
  preview migration wording and mode-authoring score-identity guidance without
  changing deployment records before an actual deployment.
- [ ] Run `uv run pytest -q`, frontend syntax checks, Python lint and diff checks.
  Parent reviews the diff before committing `feat(ui): add event leaderboard controls`.

### Task 3: Integrated Verification And Release Review

**Files:** Disposable test data/screenshots/logs under
`~/workspace/scratchpad/patchme-events-2026-11-01/`; focused regression fixes only
if evidence requires them. Review the entire branch against `437395f`.

**Consumes:** The combined backend and frontend, completed task reports and reviews.
**Produces:** Verified release revision and migration evidence, without live writes.

- [ ] Run the full suite and lint/syntax checks independently. Rehearse migration
  against a disposable legacy fixture and verify every mapped row plus settings,
  integrity, and idempotent second startup.
- [ ] Start the existing disposable local preview on loopback; verify its actual
  listener. Exercise scheduled/Start now events, slot-lock browsing, name entry,
  late saves, expiry fallback, duplicate names, historical E navigation, picker
  focus, two-client independence, failed-save recovery, and maintenance.
- [ ] Verify native-browser layout at 1920x1080, 1366x768 and 390x844, including a
  long event name and long archive. Check console errors and both leading players
  without the orange accent bar. Use no production database for synthetic races.
- [ ] Dispatch a broad fresh-context code review against the spec and user follow-up.
  Address concrete findings with focused failing regressions and scoped re-review.
- [ ] Update plan progress, commit reviewed corrections, and fast-forward canonical
  main only if its worktree is clean and unchanged. Push the reviewed release to
  the verified origin under the user's standing publication preferences.

### Task 4: Idle-Only Deployment And Preservation Checks

**Files:** Task-local deployment helpers and backups in the scratch directory;
parent `AGENTS.md` after success. No broad `justfile` refactor.

**Consumes:** Reviewed release, SSH access and existing environment-based sudo
credential handling. Reuse op-cache handle if needed; never print secrets.
**Produces:** Running deployed release, verified scores/settings, rollback evidence.

- [ ] Read fresh live phase over Socket.IO. Wait for `ready`; never dismiss results
  or reset a race to create an idle window. Verify target/source and exclusions.
- [ ] Independently review the deployment guard and migration comparison helpers.
  Recheck idle immediately before stopping the backend. Take a fresh SQLite online
  backup and source archive while stopped, verify integrity, copy off-machine, and
  verify checksums before changing deployed files.
- [ ] Sync the exact reviewed checkout while stopped, excluding `.git`, worktrees,
  planning directories, credentials, databases and caches. Start the service to
  migrate; if it fails, inspect safely without discarding scores or improvising a
  source-only rollback.
- [ ] Compare every legacy score field and mapped numbered association with the
  backup, verify selected numbered slot/maintenance, database integrity, and no
  invented production events. Use itemized checksum rsync and served asset checks.
- [ ] Fresh idle check before refreshing/relaunching the kiosk with its explicit
  loopback game URL. Verify service stability, serial handles, browser connection,
  actual kiosk screenshot and operator page, without synthetic scoring or events.
- [ ] Record deployed revision and backup ownership/location in parent `AGENTS.md`;
  document source-only rollback incompatibility. Stop disposable previews. Report
  release, verification, any deviations, and remaining limitations concisely.
