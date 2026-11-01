# P-tschMeIfYouCan
Ein Geschicklichkeitsspiel mit Spaß am Gerät

## Important: Name Entry Keyboard Behavior

This is a required interaction contract. Preserve it when changing the dashboard:

- **Enter in the first displayed input:** move to the second input if its name is empty or whitespace-only; otherwise submit both names and proceed to the leaderboard.
- **Enter in the second displayed input:** submit both names and proceed to the leaderboard, even when either name is blank. Empty or whitespace-only names skip that player's leaderboard entry.
- **Tab / Shift+Tab:** cycle between the name inputs, including wrapping; never submit names or advance to the leaderboard, even when both names are filled.

The inputs are displayed in result order (winner first), not fixed player-number order. These rules apply regardless of which player wins or whether the race is tied. Automatic name-entry timeout is separate from keyboard submission.

[Keyboard regression tests](Test_Server/tests/test_name_entry_keyboard.py) execute the dashboard's actual JavaScript handler. Run them when changing name entry or keyboard handling; they do not test live serial hardware.

## Leaderboard Policy

Each leaderboard shows one row per case-insensitive name, separately for each game mode and leaderboard identity (numbered or named event). Ranks compare each player's **best time**; equal times share a rank. The name uses the latest spelling, and **Set at** is the best attempt's timestamp (the latest matching attempt for equal best times). The **Attempts** column counts all saved scores for that name in the same mode and board, including historical scores. Blank names are still skipped, and retrying a score save does not add an attempt.

First place, including tied first places, is displayed as **Top of Rack**; other ranks remain numeric.

After a race, that player's row is highlighted even when the new time is worse. A worse time appears as **This race** beneath the best time without changing the rank. A first score or strictly improved best earns a compact orange **New PB!** badge; matching an existing best does not. Idle browsing does not show current-race details or badges.

All attempt history remains in SQLite; grouping does not delete or rewrite scores and requires no database migration. A paginated browse freezes the best times, latest attempts, and counts at its opening snapshot. [Leaderboard regression tests](Test_Server/tests/test_leaderboard.py) cover case-insensitive grouping, best-time ranking, attempt counts, and snapshot pagination; [transport tests](Test_Server/tests/test_app.py) cover current-race highlighting and personal-best detection. Neither covers physical hardware or browser layout.

### Explicit Requirement: Post-Race Score Position

When players finish and the post-race leaderboard opens after name entry,
automatically scroll to the faster player from this race and place that
player's score row at the top of the visible list. Choose by this race's time,
not historical personal-best rank. Players must immediately see their placing
without manually searching or scrolling, including beyond the first page.
Preserve the actual leaderboard order and ranks; this requirement changes the
initial scroll position, not the standings.

This is an explicit user requirement. The jump happens once per result view;
manual navigation takes precedence. Ties choose Player 1. If the faster player
skips their name, use the other saved participant; if both skip, stay at the top.
Case-insensitive matching uses the participant's grouped personal-best row.
[Positioning tests](Test_Server/tests/test_result_position.py) cover winner
selection, pagination, ties, skipped names and manual-scroll preservation;
they do not replace native-browser geometry checks.

### Explicit Decision: Current Race Time

The post-race leaderboard must still show the current race time. The main time
and rank remain the player's personal best. If this race was slower, show its
time separately as **This race** beneath the best; if it matched or improved
the best, the main time already shows this race's time. Do not replace or hide
the current result just because an older personal best is faster. Idle browsing
does not show this post-race detail.

### Failed Score Saves

If saving fails while results are open, **Retry saving** resends the captured race,
names, times, mode and captured leaderboard. Automatic return pauses until saving succeeds, then
the leaderboard refreshes and its normal 60-second countdown restarts. Repeated
retries do not add attempts, including when a previous write committed before
reporting an error. Name-entry timeouts and skipped blank names are unchanged.

Back to game, L, Escape and Space require confirmation before abandoning failed
results. Leaving, reset and expiry are blocked during every pending write, including
the initial save. Confirmation gives
up recovery; it does not remove any scores already written. Browser refresh or
reconnection restores recovery from server state, but **restarting the backend
loses unsaved results held in memory**. This is not a durable retry queue.

Browsing another board never dismisses these results. The captured score destination,
save error and Retry saving control remain visible there. A completed save refreshes
the current page snapshot without redirecting an explicitly browsed board.

[Recovery tests](Test_Server/tests/test_score_recovery.py) cover identity,
concurrent requests, idempotence and timeout boundaries;
[recovery UI tests](Test_Server/tests/test_score_recovery_ui.py) execute the actual
dashboard handlers for confirmation, refresh and page invalidation. Neither
replaces browser layout checks or tests live hardware.

## Leaderboard Slots

Use **1-9**, including the numeric keypad, or the slot selector while the game is ready. Outside an active event this selects the next race's scoring slot and switches an open view. During an event, or while stopped or viewing post-race results, it only browses the numbered board. Event-locked digits from the game view open that board for visible feedback. Racing, name entry, maintenance and open dialogs guard the underlying shortcuts. Digits typed into inputs remain input.

Each slot has independent scores and ranks for every game mode. Switching never deletes scores. The manual selection and its selection time are saved in SQLite, independently of browser storage. An event that ends after that selection makes the effective default slot 1, including when the backend was offline for the whole event. Selecting a slot again, even the previously stored slot, records a fresh choice. The destination is captured at race start, so delayed writes cannot move a race to another board.

The database remains at `LEADERBOARD_DB` or `~/.local/share/patchmeifyoucan/leaderboard.sqlite3`. The event migration creates permanent numbered board records and replaces score slot references with stable `leaderboard_id` references. Existing scores keep their IDs, names, durations, timestamps, modes, race/player identities and numbered associations. The manual slot and maintenance setting are retained; migration creates no events. Take fresh verified database and source backups before deployment. Source-only rollback to slot-only code is unsupported, and a database rollback discards scores recorded since its backup.

[Slot persistence and transport tests](Test_Server/tests/test_leaderboard_slots.py) cover migration, isolation, restart restoration, failed settings writes, race identity, and stale page rejection; they do not exercise physical hardware. [Slot keyboard tests](Test_Server/tests/test_leaderboard_slot_keyboard.py) execute the actual dashboard shortcut and selection helpers, including modifier, focus, and phase guards; they do not replace browser layout checks.

## Event Leaderboards

Named events are independent boards, not names attached to slots. Each creation
starts empty, including when its name matches another event. One schedule applies
to all modes, with separate rankings, personal bests, attempt counts and pace
references for each board and mode. Names are trimmed printable text of 1-60
Unicode code points and are displayed as text.

The server assigns both players to the event active at race start, on the half-open
window `start <= now < end`. An active event locks scoring even when a display is
browsing another board. Name entry, delayed saves and retries keep that captured
identity after expiry or End now. Expiry changes future defaults to slot 1 (or an
adjacent active event), never an existing race or explicitly opened view. Race
duration still uses the monotonic clock; event eligibility uses the server's UTC
wall clock. Keep the server clock correct.

**L** toggles the leaderboard as before. Opening from the game defaults to the
active event, otherwise the effective numbered selection. Automatic post-race
results always open the captured board. **0**, including the keypad, returns to
the active event and does nothing without one. The Current event button is its
pointer equivalent. Named titles read `Leaderboard - <event name>`; numbered titles
read `Leaderboard`, with the slot shown in the selector. The compact status shows
the next race's destination, or the captured destination while a race is unresolved.

**E** or Past events opens the archive picker only from an open leaderboard.
Events appear newest-ended first, including empty boards, with their Berlin time
ranges. Arrows move focus, Enter selects, Tab stays in the dialog, and Escape/E
closes it and restores focus to Past events. Reopening fetches a fresh list; late
responses cannot reopen a closed picker. The picker blocks underlying shortcuts,
but normal inactivity expiry still closes it. Archive browsing is local and never
redirects scoring or another display. Global manual numbered changes update
ordinary numbered views without evicting archives. Board changes start a fresh
pagination snapshot; renames update titles without losing rows or focus. Current-race
highlighting and winner positioning apply only on the captured result board.

At **/control**, Event leaderboards offers creation, editing, End now and cancellation.
All entered dates/times mean **Europe/Berlin**, even on a browser in another zone.
Start now and event statuses use server time, independent of the browser clock.
The form shows the range before submission; the server
rejects invalid names, overlapping windows, ambiguous/nonexistent DST times and
stale revisions. Rejected or timed-out changes retain inputs and show an error.
Reload events explicitly reloads the selected edit after checking a stale or
unconfirmed change. No change is accepted before storage commits it.

Upcoming events can change their full window or be cancelled after confirmation.
Active events can change their future end or use confirmed End now; their start is
fixed. End now at exactly the start is rejected without changes to preserve a
positive window; retry once server time has advanced. Past events can only be
renamed. There is no past-event or score deletion,
recurrence, historical score move, or added authentication. The control page remains
on the existing trusted-network boundary.

Unknown routing metadata blocks new races visibly instead of choosing a fallback
board. **Retry storage** reloads/reinitializes it without resetting an in-memory
race, recovery or maintenance. After a successful load, the last committed schedule
continues to route while storage is failing. Failed metadata writes cannot change
that schedule. Out-of-band database edits are not supported.

[Policy tests](Test_Server/tests/test_event_policy.py), [storage tests](Test_Server/tests/test_event_storage.py)
and [runtime tests](Test_Server/tests/test_event_runtime.py) cover schedule boundaries,
Berlin/DST validation, migration, immutable capture, revision checks and recovery.
[Player UI tests](Test_Server/tests/test_event_ui.py) and [operator UI tests](Test_Server/tests/test_event_controls.py)
execute the actual JavaScript with controlled DOM/Socket.IO boundaries; they do not
replace native-browser focus/layout checks or physical hardware tests.

## Live Race Statistics

During racing, patches/min uses the median personal-best time from the selected
mode and captured leaderboard as a starting weight of three patches. Each name
counts once, matching the leaderboard; zero-time bests are excluded. The median
is captured once per race and retained on reconnect. Empty or unavailable
leaderboards fall back to the player's own pace. Browsing another board never
changes a running race's reference.

After progress changes, the estimate holds for one estimated patching interval,
then declines if no further progress arrives. The historical weight becomes less
significant as patches accumulate. The rate stays zero before a timed patch; stopped
and completed players show their actual race average. The combined rate uses the
longer player's elapsed time, as before. These display estimates never affect
race times, winners or saved scores.

Each player's lead indicator sits between progress and the clock. Comparable
progress shows the patch gap, with the leading player's indicator highlighted;
ties are neutral and different mode stages are not compared.
[Rate and lead tests](Test_Server/tests/test_dashboard_rates.py) exercise the real
dashboard calculation and rendering; they do not verify kiosk layout or hardware.

## Maintenance Mode

Use the **Maintenance mode** checkbox at `/control` to enable or disable it.
The control page reports **Off**, **Queued** or **Active**. Enabling while ready
activates immediately, including idle leaderboard browsing. During a race,
stopped game, name entry or post-race results, it waits for the normal return to
ready. It never stops a race or dismisses results itself. Uncheck it to cancel
a queued request. A stopped game still needs its normal reset.

While active, every connected dashboard is darkened behind large lowercase
**maintenance mode** text in Selfnet orange and Comic Neue. Dashboard controls
and shortcuts cannot start a game or dismiss the overlay. Disable maintenance
from `/control` to return to ready. Serial observations continue for diagnostics.
Maintenance is an operator convenience on the existing trusted game network,
not authentication or an authorization boundary.

The request is saved in SQLite before it is accepted and is restored across
browser refreshes and backend restarts. A queued request becomes active on
backend restart because races are memory-only. **Do not restart to force queued
maintenance:** it would lose the current race or unsaved results. Failed score
saves still require retry or explicit abandonment; queued maintenance waits for
in-flight saves as well. Failed maintenance-setting writes leave the live setting
unchanged and show an error on `/control`.

Explicit operator policy: if the setting cannot be read at backend startup,
log the error and leave maintenance off. This does not override the separate event
routing safety rule: unknown routing metadata blocks new races until Retry storage
succeeds. A readable saved setting is required for restart
persistence. Migration only adds a default-off settings column; it does not
change scores or the selected slot. Pre-maintenance source ignores this setting,
so a source rollback will no longer block games. Take a fresh database backup
before deployment; maintenance does not itself authorize deployment or restart.

Comic Neue is the approved open-source substitute for Comic Sans. Its bold font
is bundled and served locally, with no OS font installation or external request.
[Dashboard assets](Test_Server/static/dashboard/README.md) records its source and
OFL license; verify the loaded font when checking the maintenance screen.

[Maintenance tests](Test_Server/tests/test_maintenance.py) cover queued
transitions, persistence, recovery and concurrent writes; [UI tests](Test_Server/tests/test_maintenance_ui.py)
exercise the actual overlay, operator and keyboard handlers. Neither verifies
physical hardware, installed fonts or native-browser layout.

## Development

The shared footer keeps instructions visible in every game view. Its leftmost
control, **Space**, can be clicked or activated with the Space key to start, stop
or reset the clock. It is hidden during name entry and leaderboard browsing.
[Dashboard tests](Test_Server/tests/test_dashboard.py) cover phase-dependent
controls; they do not replace native-browser keyboard or layout checks.

[Local preview](docs/local-preview.md) runs the real game with simulated boards
and an isolated, disposable leaderboard. Use it to try the dashboard without
hardware; it does not change or deploy to the game PC.

[Dashboard assets](Test_Server/static/dashboard/README.md) records the original
Selfnet/SIAM sources and font license, not game behavior or deployment.

[Adding gamemodes](docs/gamemode-authoring.md) explains the board/session contract, registry, validation, and scoring identities. Start there when adding a mode; firmware protocols and deployment are outside its scope.

Run `uv run pytest -q` from `Test_Server/`. Node.js must be on `PATH` for the name-entry keyboard tests, which execute the dashboard's JavaScript handler.
