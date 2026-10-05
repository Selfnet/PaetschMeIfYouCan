# Gamemodes Design

## Status and Goal

Historical design, implemented and deployed on `feat/gamemodes` through `194a6cb`. [The README](../../../README.md) documents the current name-entry keyboard contract and test prerequisites; this document records the original design, not current deployment instructions.

The gameplay and interaction decisions below reflect the conversation. Technical defaults introduced while writing this spec include 50-row pages, snapshot pagination, tick precision, and the detailed focus and error contracts. These are proposed implementation choices for review, not separately approved product requirements.

Introduce extensible, stateful gamemodes without changing the familiar switch overview and race flow. Ship Full Field, Half Field, and Quarter Field first. Keep mode logic independent of Flask, the browser, serial transport, and persistence so contributors can add modes without changing those integrations.

Work happens on `feat/gamemodes` in the canonical checkout's `.worktrees/gamemodes/` directory. Deployment is not part of this work.

## Existing Context

- `Test_Server/app.py` currently owns cached board feedback, expected cable identities, serial parsing, and application wiring. Serial readings become open/wrong/correct states before reaching the race.
- `Test_Server/race.py` owns the locked race lifecycle and hardcodes completion as all 48 ports being correct.
- `Test_Server/templates/index.html` renders each board as two rows of 24 ports. Port indices alternate top/bottom: column `i // 2`, row `i % 2`.
- `Test_Server/leaderboard.py` stores mode-independent scores and queries the top 10.
- `Test_Firmware/src/main.cpp` reads port identities and drives yellow/green LEDs locally. It does not receive host LED commands.

Related designs:

- [Post-race results](2026-08-28-post-race-leaderboard-design.md): background for race timing, result entry, persistence, and failure handling. This spec replaces its all-48 completion rule, top-10 restriction, and fixed leaderboard deadline; it is not an authoritative description of later name-entry changes.
- [Idle leaderboard](2026-08-30-idle-leaderboard-design.md): background for local browsing, dismissal, timestamps, and stale-response protection. This spec replaces its top-10 restriction and adds scrolling and navigation-driven inactivity handling; it does not redesign race controls.

Preserve current working behavior outside the changes explicitly described here, including simultaneous name-entry support, ties, per-player diagnostic stops, and result timing.

## Scope

Included:

- Board observations separated from mode-specific interpretation.
- Stateful, independently testable per-player mode sessions.
- Three built-in field modes and a documented authoring interface.
- A hidden keyboard-accessible main-screen mode selector.
- Generic mode objective, progress, stage, and port presentation.
- A hardware-independent boundary for future LED output.
- Per-mode leaderboards, existing-score migration, all-score browsing, and inactivity handling.

Excluded:

- Firmware modifications, a serial LED protocol, or sending LED commands.
- Plugin discovery, uploaded code, hot reloading, or a custom-mode editor.
- Additional shipped gameplay beyond the three field modes.
- Empty-board enforcement, anti-cheat measures, or fresh-insertion requirements for field modes.
- Leaderboard deletion or administration, durable recovery of unfinished races, and deployment.

## Built-in Rules

Each player has a separate 48-port board, arranged as two rows of 24 columns. Both players use the same selected mode.

| Mode ID | Label | Required region | Required ports |
| --- | --- | --- | --- |
| `full-field` | Full Field | All 24 columns | 48, indices 0-47 |
| `half-field` | Half Field | Leftmost 12 columns | 24, indices 0-23 |
| `quarter-field` | Quarter Field | Leftmost six columns | 12, indices 0-11 |

Half and Quarter Field are single-region objectives, not left-to-right sequences. Quarter Field therefore requires six top and six bottom ports. Ports outside the region are ignored, regardless of whether they are open, correct, or incorrectly patched.

All three modes reuse one implementation parameterized by required ports. In normal verification mode, each required port must contain its expected cable identity. Preserve the existing verification-bypass diagnostic behavior: occupied required ports count when verification is disabled. Verification is explicit evaluation context, not inferred from rendered feedback.

There is no unplugging prerequisite. Pre-patched ports count. At race start, evaluate the latest valid observation at elapsed time zero; an already-complete board may finish immediately. If no valid reading has arrived, the board is unknown and cannot complete until a valid observation is received. Do not add anti-cheat logic.

## Architecture

### GameBoard

One `GameBoard` per player owns the latest validated raw port identities and whether any valid observation has arrived. It exposes immutable observation snapshots and the fixed physical layout, including expected identities and coordinate/index helpers.

Keep raw identities available to custom modes rather than reducing all input to open/wrong/correct. Board updates must contain exactly 48 integer byte values; reject booleans, out-of-range values, malformed text, and partial frames. A rejected update leaves the previous observation intact and is not delivered to a session.

The serial adapter remains responsible for decoding and reversing wire order into the canonical port order. That reversal happens once. Modes do not know serial devices, wire ordering, or LED shift-register mappings.

### GameMode and ModeSession

A mode definition provides a stable ID, label, description, and a factory for fresh sessions. Use an explicit in-process registry with unique IDs; no dynamic plugin loading. Invalid definitions fail startup clearly rather than silently replacing another mode.

Each player receives a separate session at race start. A session owns its private gameplay state and supports:

- Initialization against the current observation and evaluation context at elapsed time zero.
- Observation updates with an immutable board snapshot and monotonic elapsed time.
- Periodic ticks with the latest observation and elapsed time, allowing timed stages to progress without new serial input.
- An immutable presentation snapshot after evaluation.

Reset or abandonment discards sessions. Sessions must not retain state across races or share mutable gameplay state between players. They have no Flask, Socket.IO, database, serial, or wall-clock dependencies and must not block on I/O.

The session snapshot contains:

- Short objective text and an optional stage label.
- Progress as a non-negative current value and positive total, with a short unit label; omit progress for objectives without a meaningful counter.
- Per-port target membership and semantic feedback: open, wrong, correct, or unknown. Inactive appearance is derived from target membership, not a substitute for the observation.
- A completion flag.
- Optional desired per-port LED states, described below.

Field modes report correct required ports out of required ports, for example `8 / 12 correct`. Stateful modes can change targets, instructions, and stages through the same contract. The browser contains no mode-ID-specific gameplay branches.

A pure ready-state preview supplies targets and presentation from current observations without advancing a race session. This preserves live board feedback before a race while ensuring race start always creates fresh gameplay state.

### Race Controller and Runtime

The race controller owns selection, race lifecycle, per-player session ownership, timing, result ordering, name entry, and deadlines. The selected mode is fixed for the entire race and its results. The runtime handles transport, persistence, and broadcasting.

Serialize selection, start, reset, observation delivery, tick evaluation, and completion under the authoritative controller lock. Capture one monotonic timestamp per transition so both start evaluations use the same origin. Never run persistence, serial I/O, or socket emission while holding that lock; publish immutable snapshots and transition results afterward.

Publish each validated board observation and deliver it to the current session in the same locked transition. Start reads both current boards, creates both sessions, and evaluates both at the shared start timestamp in one locked transition. Serial decoding can happen outside the lock, but cached snapshots must not be captured there and subsequently delivered to a replacement session. Lock acquisition determines whether an observation precedes or follows start/reset.

First completion remains latched. Later observations, ticks, or unplugging cannot change the recorded finish time. Preserve millisecond floor timing and existing manual-stop behavior. Ordinary mode progression must be broadcast even when race phase and completion status do not change.

An individual diagnostic stop freezes that player's displayed clock, but while the shared phase remains racing the unfinished session continues receiving observations and ticks using elapsed time from the original race start. A later completion replaces the manual stop with its actual completion timestamp. Global stop, or stopping both players, ends evaluation for all sessions; physical observations may still update. Reset discards those sessions. Completed sessions no longer advance.

Out-of-lock operations carry immutable origin tokens. Score persistence carries the completed race ID and mode ID; page reads carry the browsing request ID, mode ID, snapshot boundary, and race ID for post-race views. Apply resulting controller updates only if the expected race, mode, and phase still match. A late write may finish saving already-confirmed submissions, but cannot modify a replacement race or revive an expired view. Browser responses are likewise checked against the active browsing token. Persistence and page responses never refresh inactivity deadlines.

Use the existing periodic background mechanism for ticks; this version promises no sub-second scheduling for time-driven modes. Pass actual elapsed monotonic time, not a count of ticks. Serial-driven completion retains its arrival-time resolution.

## Lifecycle and Errors

- Server startup selects Full Field.
- Selection is accepted only in `ready`, with a known registered mode ID. Both players change together.
- Selection survives explicit resets, leaderboard dismissal, and automatic return to ready. It does not persist across server restarts.
- Start creates new independent sessions and evaluates current valid observations immediately.
- Reset discards mode progress but retains physical observations and selected mode.
- A reconnect receives mode metadata, phase, objective, progress, port feedback, and current race identity without reconstructing gameplay in JavaScript.
- Invalid hardware input is rejected before mode evaluation.
- A session exception or invalid output stops the race with a visible, recoverable error, logs diagnostic details, and creates no leaderboard submissions. Reset returns to ready. A failing ready preview shows an error and prevents starting that mode, while still allowing another mode to be selected.

## Main-Screen UI

### Mode Selector

The switch overview remains the default. The mode selector is hidden initially and can be opened only while ready. Display a compact current-mode label, such as `Quarter Field · 12 ports`, and a subtle `M: mode` hint while ready. Other modes can omit the port count if inappropriate.

- `M` opens the selector; `M` again cancels it.
- Arrow keys move between options in registry order without wrapping at the ends.
- Tab and Shift+Tab move focus through selectable options and the close control in normal DOM order.
- Enter or clicking an option confirms it, updates the shared selected mode, and closes the selector.
- Escape cancels without changing the selection.
- Space does not start a race while the selector is open.
- Opening focuses the selected mode. Closing restores focus to the overview's mode control.

Each option shows its label and short description. Browsing focus does not change the shared selection or preview; only confirmation does. The selector's visibility and focus are browser-local. Accepted selection is broadcast to every browser. An authoritative phase change closes any open selector. Stale selection requests after a race starts are rejected.

Use a labeled modal dialog with ordinary option buttons and a close button. Background controls are inert while it is open. Tab/Shift+Tab visits every option and the close button, wrapping at the dialog boundary; arrows move only among mode options. If another browser changes the mode, update the selected marker without moving the local browsing focus. Enter confirms the locally focused option. On an external phase change, move focus to an appropriate element of the new view rather than a now-hidden mode control.

Mode shortcuts never intercept text entry; `M` remains a normal character in a name field. The selector and local leaderboard cannot be open simultaneously; their shortcuts do not open one behind the other.

### Board Presentation

Keep all 48 physical positions visible in the existing two-row arrangement. Required ports retain clear open/wrong/correct feedback. Unused ports are dimmed with readable numbers, and their state does not affect objective progress.

Show a concise objective and progress for each player, plus a stage label when present. Independent player sessions may show different stages and targets. Preserve timers and existing result/name-entry screens. Mode identity remains visible on results and leaderboards so the meaning of a time is clear.

Unknown observations are not presented as verified empty ports. Accessibility labels identify port number, whether it is required, and its feedback. Focus must remain visibly distinct from completion highlighting.

## Future LED Boundary

Sessions may describe optional LED intent as a complete 48-port frame of yellow/green boolean states. Absence means the mode supplies no host LED intent; it is distinct from an explicitly all-off frame. Frames describe desired state, not commands or electrical indices.

LED intent is separate from browser feedback because their capabilities differ. A future hardware adapter can map canonical port indices to firmware commands and handle capability negotiation, writes, and reconnects without changing game rules.

This version validates and tests the output contract but does not send it. Existing firmware behavior remains unchanged. It does not introduce blinking, brightness, animations, transport retries, or a new wire protocol.

## Per-Mode Leaderboards

### Persistence and Ranking

Store a stable mode ID with each score, propagated from the completed race through score submission. Never infer score mode from the current selection at insertion time.

Migrate all existing rows to `full-field`, preserving IDs, names, times, timestamps, and race/player uniqueness. Migration must be transactional and safe on repeated initialization. Add an index supporting per-mode score ordering.

Rank only within the selected mode. Preserve competition ranking for equal millisecond durations and deterministic ordering by duration, creation time, then database ID. Mode IDs are durable scoring identities: incompatible rule changes should receive a new ID rather than silently sharing old rankings.

The post-race leaderboard uses the completed race's mode. The local leaderboard uses the current selected mode. Keep rank, name, time, and locally formatted creation timestamp columns, and current-race highlighting wherever those rows occur.

### All-Score Browsing

Expose every score for the mode, not just the top 10. Fetch bounded pages on demand, initially 50 rows per page. A browsing snapshot captures the maximum included row ID on opening, and every page filters to that boundary. New inserts become visible when reopening, preventing duplicates or skipped rows as new scores arrive during browsing. Ranking is computed across the entire mode snapshot before pagination, not separately per page.

Use boundary `0` for an empty snapshot. Every page applies both the mode filter and `id <= boundary`, even for an empty snapshot. Page position is a validated non-negative offset into that snapshot's deterministic ordering. Deletion and editing of scores are outside this feature, so inserts alone cannot shift offsets within the boundary.

Requests and targeted responses identify the mode, browsing request, snapshot boundary, and page position. Validate bounds server-side and keep the page size capped. Each browser tracks its own rows, focus, and scroll position. Ignore responses belonging to a closed view, previous mode, old race, or replaced browsing request.

Do not put the growing row collection in periodic shared `game_state` broadcasts. Shared state supplies race identity, mode, timeout, and persistence status; targeted page responses supply rows. Periodic countdown updates must not recreate the table or reset browsing position.

If the selected mode changes in another browser, close the local leaderboard so it cannot continue displaying an incorrectly labeled mode. An active race transition also closes the local view, as today.

### Keyboard and Scrolling

Use a bounded scroll area inside the leaderboard view rather than scrolling the entire kiosk page. Open at the highest-ranked scores and retain heading focus on entry.

- Up/Down moves through score rows and scrolls the focused row into view. From the heading, Down enters the first row.
- Tab/Shift+Tab follows normal focus order through loaded rows and controls.
- Prefetch the next page near the loaded end. An accessible load-more control after loaded rows remains available if fetching has not completed; Enter activates it and moves focus to the first newly loaded row once available. Down at the last loaded row similarly requests and advances when more scores exist.
- Repeated navigation during a pending request must not issue duplicate page loads or lose focus. Loading failures retain existing rows and allow retry or dismissal.
- At the final score, Down stays on that row; Tab can continue to controls.
- Preserve `L` and Escape dismissal for both leaderboard views and Space dismissal for post-race. Space remains inert in the local idle leaderboard.

Preserve native table semantics and make each loaded score row focusable with `tabindex="0"`, deliberately allowing Tab through scores as requested. Give the scroll area an accessible label and keep a visible close control before the table so long lists do not require tabbing through every row to dismiss. Append rows without replacing existing nodes. Track focused rows by database ID. During navigation-triggered loading, retain focus on the initiating row or load-more control; move to the first appended row only if the same view and navigation intent remain current. If focus has moved or the view has closed, do not steal it when the response arrives. On failure, retain focus and expose a retry control with an announced error.

No automatic jump to current-race rows; initial browsing starts at the top. Highlight those rows when reached.

### Inactivity

The post-race leaderboard retains a server-owned 60-second deadline, now refreshed by valid user navigation or scrolling. Send an activity event tied to the current race ID. Validate phase and race ID and process expiry before accepting activity so late events cannot revive an expired view. Either connected viewer can extend the shared post-race deadline.

The idle leaderboard remains browser-local with its existing 60-second deadline, refreshed locally by the same interactions. It never changes authoritative race state. Throttle continuous scroll notifications; page responses, countdown rendering, reconnects, and programmatic scrolling alone do not count as activity. Navigation that causes programmatic scrolling counts once as the initiating user action.

Keep a subtle remaining-time indicator. Dismissal and automatic expiry restore the overview and appropriate focus.

### Failure Handling

Database migration, insertion, or read failures remain non-fatal to the kiosk, using the existing unavailable-store fallback and visible persistence error behavior. Scores must never fall back into another mode's leaderboard. Page failures preserve loaded rows, allow retry, and leave dismissal usable.

## Contributor Documentation

Add a concise mode-authoring guide during implementation, linked from the project README. Document the registry entry, observation layout, evaluation context, session lifecycle, output validation, tick precision, LED intent, and stable mode IDs.

Use built-in field modes as the static example and a small test fixture to demonstrate independent stateful stages and timed advancement. No extra production gamemode is required. A new mode should require only its definition/session, registry entry, and tests, not Flask routes, browser conditionals, or database schema edits.

## Verification and Acceptance

### Domain Tests

- Canonical top/bottom coordinates and exact full/half/quarter target sets.
- Valid raw observations, unknown initial state, malformed-frame rejection, and raw identity preservation.
- Required open/wrong ports prevent completion; inactive ports never do.
- Existing verification bypass remains explicit and functional.
- Pre-patched ports count, including immediate completion at start and zero-duration ties.
- Independent sessions, fresh state on reset/start, staged target changes, and time-driven transitions without new readings.
- First-completion latching, millisecond timing, manual-stop semantics, and stop/reset behavior.
- Selection allowed only while ready and retained across every reset path.
- Invalid session output or exceptions stop play recoverably without scores.
- Optional LED output is validated and never written to hardware.

### Persistence and Integration Tests

- Migration of a pre-mode database preserves scores and assigns Full Field; reopening is idempotent.
- Rankings, ties, ordering, and all pages remain isolated by mode.
- More than one page can be browsed without skipped/duplicated entries, including concurrent inserts and ties at page boundaries.
- Completed-race mode travels through submissions independently of later selection.
- Reconnect restores current objectives and progress, including different stages per player.
- Invalid or stale selection, page, and activity events cannot alter a race or reopen a view.
- Countdown broadcasts do not replace browser-local rows or focus.
- Inactivity extends on valid user activity, expires normally, and rejects late/stale activity.
- Database failures do not prevent starting the next race.
- Delayed persistence and page responses cannot update a reset, replaced, or expired race/view.
- Observation/start/reset ordering and timed-session behavior after individual and global stops follow the controller contract.
- Empty browsing snapshots remain empty despite concurrent inserts until reopened.

### Browser Acceptance

- Existing switch overview remains the default and fits the deployed 1920x1080 display.
- M, arrows, Tab/Shift+Tab, Enter, Escape, and mouse selection follow the specified behavior.
- Cancelling leaves selection unchanged; selection updates both players and closes the selector.
- Name entry accepts M normally; Space cannot start behind a selector or local leaderboard.
- Target dimming, progress, objective, and stage labels are legible without moving physical port positions.
- All mode scores are reachable by keyboard, focus stays visible, and loading/retry controls work.
- Post-race highlights and tied ranks remain correct beyond the first page.
- Neither countdown updates nor page appends reset focus or scroll position.
- Shared post-race and local idle inactivity handling preserve their separate ownership.
- Selector focus cannot escape into background controls; external selections update the marker without stealing focus.
- A delayed page response cannot steal focus after the user navigates away from its initiating row or control.

## Next Step

Implementation and deployment are complete. Read [the authoring guide](../../gamemode-authoring.md) for the implemented mode API; it does not cover firmware or deployment. The original design excluded deployment, which the user later authorized separately.
