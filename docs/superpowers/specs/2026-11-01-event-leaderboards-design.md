# Scheduled Event Leaderboards

Status: approved and implemented after separate user authorization. Historical
baseline: `9db48eb`. Current behavior and regression references live in the
[README](../../../README.md#event-leaderboards); deployment approval is separate.

## Intent And Agreed Decisions

An operator can create a named leaderboard for an event with a defined time
window. During that window, every new race belongs to that event regardless of
which leaderboard a player is viewing. Players can still browse numbered boards
and previous events without redirecting event scores.

The user selected these behaviors:

- Named events are independent leaderboards, not names attached to slots 1-9.
  Each new event starts empty; previous events remain separately accessible.
- The server chooses the destination at race start. Both players, name entry,
  delayed writes, and retries retain that destination across event boundaries.
- Outside events, the existing manual selection of numbered slots remains.
- Event expiry resets the default and next-race destination to slot 1. It does
  not interrupt an existing race, name entry, results, or failed-save recovery.
- Event times use Europe/Berlin. Windows do not overlap. One event applies to all
  game modes, with separate rankings for each mode as today.
- Opening the leaderboard with `L` defaults to the active event. `0` returns to
  the active event and does nothing when no event is active.
- In the leaderboard view, `E` opens a list of past events, newest first.
- Named boards show `Leaderboard - <event name>`; numbered boards show exactly
  `Leaderboard`, with their slot identified in the existing selector.
- If the server cannot establish the score destination, new races are blocked
  rather than silently assigned to another leaderboard.

This spec adds no recurrence, authentication system, score deletion, historical
score reassignment, or unrelated architecture refactor.

## Leaderboard Model

There are two kinds of leaderboard with stable database identities:

| Kind | Identity | Name | New-race eligibility |
| --- | --- | --- | --- |
| Numbered | One permanent board for each slot 1-9 | None | Manual selection outside an active event |
| Event | A new identity for every created event | Required | Only during its scheduled window |

`0` is a keyboard shortcut, never a stored slot or leaderboard identity. An event
is not limited by or mapped onto the nine numbered slots. Renaming an event does
not create a new identity, move scores, or merge events with matching names.

The event name is trimmed, printable text of 1-60 Unicode code points. Duplicate
names are allowed; the picker also shows the date/time window to distinguish
them. Render names as text, never HTML. Long names must wrap without displacing
the controls or creating page-wide horizontal overflow.

The leaderboard identity and game mode jointly scope scores, personal bests,
attempt counts, pagination, winner-row positioning, and the median pace reference.
Changing the viewed board must not change a running race's pace reference.

## Scheduling And Race Assignment

The server's timezone-aware UTC wall clock determines schedule eligibility. The
existing monotonic clock continues to determine race duration. Neither browser
time nor the time at which names are submitted determines score assignment.

An event is active on the half-open interval `start <= now < end`. Adjacent events
are allowed; overlapping windows are rejected even if their names differ.
Boundaries are evaluated under the same runtime serialization as race start and
operator changes. A one-second background tick updates visible status, but race
start must evaluate the current time directly instead of relying on the last tick.

At race start, capture one immutable destination identity for both players:

1. If an event is active, capture its leaderboard identity.
2. Otherwise, capture the effective manual numbered selection.
3. If routing metadata is unavailable or invalid, reject start with a visible
   error and leave the game idle.

Every path that starts a race, including Space, `/start-clock`, and the control
panel, must use this rule. Selecting a numbered board through an old client or a
direct request must not bypass an active event lock. Reject attempts to change
the manual scoring selection while locked; browsing uses a separate request.

The captured identity is used for name submission, score insertion, retry,
post-race defaults, and pace lookup. Do not re-resolve it at completion or write
time, and do not accept a client-supplied destination in a score submission.

Examples for an event running from 10:00 to 12:00:

| Race starts | Names/save occur | Destination |
| --- | --- | --- |
| 09:59 | 10:02 | Numbered board captured at 09:59 |
| 10:00 | 10:02 | Event board |
| 11:59 | 12:03 | Event board |
| 12:00 | 12:02 | Slot 1, unless another event begins at 12:00 |

At expiry, later races and newly opened idle leaderboard views default to slot 1.
An adjacent active event takes precedence. Do not restore a numbered selection
made before the event. A fresh manual selection made after expiry is valid and
persists normally. Explicitly opened views are not forcibly replaced at a boundary.

Expiry must also work if the backend was offline for the entire event. Persist
the time of the last successful manual numbered selection alongside that slot.
When no event is active, an event that has ended since that selection makes the
effective selection slot 1. A manual selection at or after that end supersedes
the fallback. This makes the fallback restart-safe without requiring a timer to
have run or a recurring database write. The stored manual choice and the computed
effective destination are deliberately distinct.

A successful manual selection refreshes its timestamp even when the requested
slot equals the stored pre-event choice. For example, after expiry makes stored
slot 4 effectively slot 1, pressing `4` must select slot 4 anew and survive restart.
Do not suppress this action as a duplicate based only on the stored slot number.

Schedule evaluation follows the server wall clock, including clock corrections;
clock changes can affect future starts, never the identity or duration of an
already-started race. The machine must have a correct system clock.

## Player Interaction

### Opening And Titles

`L` retains its existing toggle behavior. From an eligible idle/stopped game view,
it opens the active event, or the effective numbered selection if none is active.
From an open leaderboard, it follows the existing return/dismissal rules.

The automatic post-race view opens the race's captured board, even if that event
has ended or a different event has started. This takes precedence over the idle
`L` default so players see their own result.

The title describes the board actually being viewed, not the scoring destination:
`Leaderboard - <event name>` for any named board, including a past event, and
`Leaderboard` for a numbered board. The same rule applies to automatic results.

Keep a separate concise status near the controls identifying the score destination
and whether it is event-locked. While results are unresolved, identify the current
race's captured destination instead of implying it will use the next-race default.
When viewing a different board, this distinction must remain visible.

### Numbered Boards And The Active Event

- While ready and outside an event, `1`-`9` and the numbered selector retain their
  current behavior: choose the next race's scoring slot and switch an open view.
- While ready during an event, the same controls browse the requested numbered
  board without changing the scoring destination. From the game view, they open
  that board so the action has visible feedback.
- In stopped or post-race leaderboard views, numbered selection is browsing only;
  it cannot change an existing race or the next-race manual selection.
- `0`, including the numeric keypad, opens/switches to the currently active event
  in the same contexts where leaderboard browsing is allowed. With no active
  event it has no effect and must not select slot 1 implicitly.
- Racing, name entry, maintenance, and an open mode dialog retain their existing
  shortcut guards. Digits typed into inputs must never navigate or change slots.
- Browsing a past event is always read-only and never selects it for new races.

The numbered selector remains available for pointer/touch use. Provide equivalent
`Current event` and `Past events` controls for `0` and `E`; hide or disable the
current-event action when no event is active.

### Past-Event Picker

Pressing `E` while the leaderboard view is open displays a `Past events` dialog.
Outside that view, `E` does nothing. Both `e` and `E` work; modifier combinations,
composition, and text entry retain the normal shortcut guards.

List only events whose end is at or before the server's current time, ordered by
end time descending and then stable identity descending. Future and active events
are not in this list. Include events with no scores, and do not filter events out
because the currently selected game mode has no attempts.

Each item shows the event name and its Europe/Berlin date/time window. Clicking an
item or pressing Enter opens that board for the current game mode and closes the
picker. An empty mode-specific board shows the normal empty-leaderboard state.

Arrow Up/Down moves through events; Tab/Shift+Tab remains within the dialog.
Escape or another `E` closes it without changing boards and restores focus to the
trigger. Keyboard focus starts at the currently viewed past event when present,
otherwise the newest event. Long lists scroll without truncating access to older
events. An empty list says `No past events yet`.

Load the list when the picker opens, rather than broadcasting all archived events
on every game-state update. A loading/error state must allow closing and retrying.
Late responses from a closed or superseded picker must be discarded. Freeze the
list for that opening; reopening obtains newly ended events and updated names.

While the picker is open, other game/navigation shortcuts cannot change the
underlying board or start/reset a race. Interaction counts as normal leaderboard
activity, but does not indefinitely disable its existing inactivity timeout. If
the underlying view closes, expires, or enters maintenance, close the picker too.

### Browsing And Results Are Separate

Browsing selections are local to each connected display. One viewer browsing an
archive must not redirect another viewer or alter global score routing. Outside
events, an accepted manual numbered selection still broadcasts globally as today;
it updates other displays' ordinary numbered views but does not evict their
explicitly opened archives. The initiating display switches to the board it
explicitly requested, including when leaving an archive or reselecting a slot.

Changing boards starts a new pagination snapshot. Preserve request IDs, captured
boundaries, stale-response rejection, and mode/leaderboard identity checks. Apply
current-race highlighting and winner-row positioning only on the captured result
board, never to a different board merely because it contains the same names.

Browsing away does not dismiss post-race results. The server retains the result
lifecycle, inactivity deadline, captured submissions, and recovery state. Save
errors and retry controls remain visible even when another board is being viewed.
Explicit abandonment retains the existing confirmation requirement.

All pending writes, including the first save, must prevent dismissal, reset, and
automatic result expiry until the outcome is known. This closes the reviewed
initial-write recovery gap while establishing the new routing contract. Schedule
boundaries and picker navigation cannot discard recovery or redirect a retry.

## Operator Controls

Add an `Event leaderboards` section to `/control` without changing existing clock,
serial, or maintenance controls. Show the current scoring destination, schedule
timezone, and the upcoming, active, and past events with explicit statuses.

Creating an event requires a name, start, and end. Offer `Start now` using server
time, or a future start date/time. End must be later than start and creation time.
Creation always allocates a fresh empty board, even if a previous event has the
same name. It never copies or moves scores from a numbered board or another event.

Dates entered without an offset are interpreted in Europe/Berlin, not the browser's
timezone. Display that timezone beside the inputs and show the resulting range
before submission. Reject nonexistent or ambiguous daylight-saving local times
with a clear request to choose an unambiguous time; never silently normalize them.
Store normalized UTC timestamps.

Editing rules keep identity stable:

- Names may be edited at any time and update displayed titles without resetting
  a race or pagination. This does not change eligibility or score ownership.
- Before an event starts, its start/end can be edited, or it can be cancelled.
  Cancellation removes only that future empty event, after confirmation.
- For an active event, its start cannot change. Its end can be extended or
  shortened to a future time, subject to overlap validation. An explicit confirmed
  `End now` action ends it at server time.
- Once ended, the window is immutable. Starting a later event means creating a
  new board, not reopening the old one. No control deletes past events or scores.
- Editing or ending an event does not change any already-captured race destination.

Validate names, windows, overlaps, and edit eligibility on the server. Persist a
change atomically before acknowledging or broadcasting it. Use a configuration
revision to reject a stale control-panel edit instead of silently overwriting a
newer schedule. Rejected/failed edits leave both persisted and live policy intact.
Operator actions can occur during a race because they only affect future starts;
they must not reset the race to make a change take effect.

The control panel remains on the existing trusted-network boundary. This feature
does not introduce authentication or treat event locks as operator authorization.

## Implementation Boundaries

This is a design, not a task-by-task implementation plan.

- `leaderboard.py` remains the owner of SQLite migrations, leaderboard metadata,
  event administration, manual-choice persistence, and score queries. Introduce
  a `leaderboards` table with a stable primary key, optional unique numbered slot,
  and event name/start/end fields. Enforce the numbered/event distinction and
  valid field combinations. Score rows reference `leaderboard_id` rather than
  overloading `leaderboard_slot` with event IDs.
- A small `leaderboard_policy.py` owns the typed routing data and pure selection
  logic: active window, effective manual fallback, and immutable race destination.
  It has no Flask, serial, DOM, scheduler, or database dependencies.
- `race.py` keeps the existing state machine. Replace slot-only race/submission/
  persistence identities with the resolved leaderboard identity. Preserve mode,
  timing, name-entry, and recovery ownership. It must not interpret browser time
  or query SQLite to choose a destination.
- `app.py` loads confirmed policy, supplies an injectable UTC wall clock, serializes
  policy changes with every race-start path, and publishes policy changes. Reuse
  the existing background loop; no cron job or scheduler process is needed.
- Dashboard code keeps viewed leaderboard identity separate from the next-race
  policy and captured race identity. Existing `leaderboard_slot` fields must not
  ambiguously mean an event ID or a viewed board.
- Control-panel commands, event-list requests, and browse requests use the existing
  Socket.IO acknowledgement/request conventions. Score submission still receives
  names, not a client-selected leaderboard destination.

The public state must distinguish: policy availability, effective next-race
destination, active event/lock, captured race destination, configuration revision,
and the existing result/browse lifecycle. Browsing requests specify the requested
leaderboard ID independently. A response echoes that ID and the request's mode,
race context, request ID, and pagination boundary.

Keep default idle routing distinct from explicit browsing. Policy changes can
update status without invalidating an otherwise valid explicit view. Phase/mode
changes and explicit view changes still invalidate incompatible page requests.

## Persistence, Failure, And Migration

Create the nine numbered leaderboard records during migration and map every
existing score to the matching record. Preserve every score ID, race/player
identity, name, duration, creation time, mode, and slot association. Preserve the
selected numbered slot and maintenance setting; initialize its manual-choice
timestamp without inventing historical event activity. Start with no events.

Retain uniqueness of `(race_id, player_number)` and transactional/idempotent saves.
Both participants use the same captured board. Retrying cannot duplicate attempts
or reassign an existing attempt. There is no automatic migration of the recent
slot-2 scores into a named event.

On startup, load and validate routing metadata before permitting a race. If that
fails, allow read-only/status UI where possible but block new starts with an
explicit error. Provide a control-panel retry that reinitializes/reloads storage
without a backend restart or loss of an existing in-memory race/recovery state.
This replaces the permanent unavailable-store fallback for this use case.

After a successful load, the last committed in-memory schedule remains
authoritative if storage later becomes unavailable. Known schedule boundaries can
still be evaluated. Failed metadata writes cannot alter that schedule; failed score
writes remain recoverable against their captured destination. Never substitute
slot 1 because a schedule query failed. No out-of-band database editing is supported.

Restarting still loses memory-only in-progress races and unsaved results; this
feature does not add a durable retry queue. Restart must restore schedules and
apply an expiry missed while offline. A metadata retry must not erase an existing
race's captured identity or maintenance state.

Migration is additive for leaderboard metadata but changes the score-reference
schema. Source-only rollback to slot-only code is unsupported. A future deployment
requires a fresh verified database/source backup, idle-only shutdown and migration,
row-preservation checks, and explicit approval. Do not deploy this spec itself or
modify the production database while designing/testing it.

## Acceptance And Verification

Use injected UTC and monotonic clocks plus disposable databases. No real waiting,
production scoring, or access to the deployed database is needed for these checks.

1. Migration preserves all legacy rows and settings and exposes numbered boards
   exactly as before. Creating two identically named events gives distinct empty
   boards and leaves all existing scores untouched.
2. Validate trimmed names, invalid types, missing/inverted windows, overlaps,
   adjacent windows, timezone conversion, daylight-saving ambiguity, and stale
   concurrent operator edits. Failed writes publish no speculative schedule.
3. Test starts immediately before, exactly at, and immediately after each boundary,
   including the gap between background ticks and competing start/edit requests.
4. During an active event, browse each numbered board and an archive from multiple
   clients; both players still save only to the captured event in every game mode.
   Direct/old-client numbered-selection requests cannot defeat the lock.
5. Finish, submit names, and retry after expiry or `End now`; the original event
   receives the results. A race started before activation keeps its numbered board.
   Renaming an event does not change any score or captured identity.
6. After expiry, idle defaults and new races use slot 1; a fresh manual numbered
   selection then works and survives restart. Test downtime spanning the entire
   event, adjacent events, cancelled future events, and later manual selections,
   including reselecting the same stored pre-event slot after fallback to slot 1.
7. Preserve initial and retry submissions during delayed/failed writes despite
   timeout, dismissal/reset attempts, schedule transitions, and browsing away.
   Recovery never needs a different leaderboard or a backend restart.
8. Test routing-unavailable startup, rejected starts, storage repair and in-process
   retry, failed metadata changes with a known schedule, and maintenance coexistence.
9. Verify `L`, `0`, `1`-`9`, `E`, keypad digits, modifier/input guards, and modal
   focus behavior. `E` lists past events newest first; active/future events are
   excluded. Empty lists and events without scores remain understandable.
10. Reject stale page/picker responses after navigation. Preserve pagination,
    grouped personal bests, current-race details, winner-row positioning, and pace
    scoping when viewing another board or switching back to the race's board.
11. Verify named/numbered titles, renamed events, long names, scrolling archives,
    pointer alternatives, and keyboard-only journeys in a native browser at kiosk
    and narrow viewports. Check reconnects while browsing and while recovery is open.
12. Run the existing full suite and local syntax/lint checks. Update tests whose
    previous contract forbade independent browsing or allowed loss of an initial
    pending save; retain the protections those tests exercised for score identity.

## Source References

- [README](../../../README.md) owns current keyboard, slot, ranking, maintenance,
  and pace contracts. Update it when implementation lands; it does not authorize
  deployment or describe the proposed event behavior yet.
- [Slot tests](../../../Test_Server/tests/test_leaderboard_slots.py) cover current
  migration, slot capture, concurrent selection, and browse isolation. Some
  selected-slot/browse coupling is intentionally superseded by this spec.
- [Recovery tests](../../../Test_Server/tests/test_score_recovery.py) cover retry
  identity and result protection; extend them for initial writes and event expiry.
- [Local preview](../../local-preview.md) owns disposable-database and browser-test
  boundaries, not live scheduling or deployment permission.
- [Gamemode authoring](../../gamemode-authoring.md) owns mode/session contracts.
  Events remain a scoring namespace and must not add mode-specific routing logic.
