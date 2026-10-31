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

Each leaderboard shows one row per case-insensitive name, separately for each game mode and slot. Ranks compare each player's **best time**; equal times share a rank. The name uses the latest spelling, and **Set at** is the best attempt's timestamp (the latest matching attempt for equal best times). The **Attempts** column counts all saved scores for that name in the same mode and slot, including historical scores. Blank names are still skipped, and retrying a score save does not add an attempt.

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
names, times, mode and slot. Automatic return pauses until saving succeeds, then
the leaderboard refreshes and its normal 60-second countdown restarts. Repeated
retries do not add attempts, including when a previous write committed before
reporting an error. Name-entry timeouts and skipped blank names are unchanged.

Back to game, L, Escape and Space require confirmation before abandoning failed
results. Leaving and reset are blocked during a pending retry. Confirmation gives
up recovery; it does not remove any scores already written. Browser refresh or
reconnection restores recovery from server state, but **restarting the backend
loses unsaved results held in memory**. This is not a durable retry queue.

[Recovery tests](Test_Server/tests/test_score_recovery.py) cover identity,
concurrent requests, idempotence and timeout boundaries;
[recovery UI tests](Test_Server/tests/test_score_recovery_ui.py) execute the actual
dashboard handlers for confirmation, refresh and page invalidation. Neither
replaces browser layout checks or tests live hardware.

## Leaderboard Slots

Use **1-9**, including the numeric keypad, or the slot selector to choose a leaderboard while the game is ready. The shortcut also works while browsing the idle leaderboard, which refreshes to the selected slot. Slot switching is disabled during a race, name entry, post-race results, and the mode dialog. Digits typed into name inputs remain part of the name.

Each slot has independent scores and ranks for every game mode. Slot 1 retains all existing scores; slots 2-9 start empty. Switching never deletes scores. The selected slot is saved in SQLite and restored after backend or machine restarts, independently of browser storage. The slot is captured when a race starts, so delayed score writes cannot move a race into another slot.

The database remains at `LEADERBOARD_DB` or `~/.local/share/patchmeifyoucan/leaderboard.sqlite3`. Migration adds the slot column and a singleton selection setting without changing historical score IDs, timestamps, or mode IDs. Back up the database before deploying this migration. Source-only rollback to pre-slot code is unsafe: that code would combine all slots' scores. A database rollback also discards any scores recorded since its backup.

[Slot persistence and transport tests](Test_Server/tests/test_leaderboard_slots.py) cover migration, isolation, restart restoration, failed settings writes, race identity, and stale page rejection; they do not exercise physical hardware. [Slot keyboard tests](Test_Server/tests/test_leaderboard_slot_keyboard.py) execute the actual dashboard shortcut and selection helpers, including modifier, focus, and phase guards; they do not replace browser layout checks.

## Maintenance Mode

Use the **Maintenance mode** checkbox at `/control` to enable or disable it.
The control page reports **Off**, **Queued** or **Active**. Enabling while ready
activates immediately, including idle leaderboard browsing. During a race,
stopped game, name entry or post-race results, it waits for the normal return to
ready. It never stops a race or dismisses results itself. Uncheck it to cancel
a queued request. A stopped game still needs its normal reset.

While active, every connected dashboard is darkened behind large lowercase
**maintenance mode** text in Selfnet orange and Comic Sans. Dashboard controls
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
log the error and **allow play with maintenance off**, preserving operation
without the database. A readable saved setting is required for restart
persistence. Migration only adds a default-off settings column; it does not
change scores or the selected slot. Pre-maintenance source ignores this setting,
so a source rollback will no longer block games. Take a fresh database backup
before deployment; maintenance does not itself authorize deployment or restart.

Comic Sans must be installed on each dashboard device through a licensed font
distribution. It is referenced as a local font, not bundled in this repository.
Do not treat a CSS font-family declaration as proof it rendered: check the actual
font on the kiosk before deployment. Without it the browser uses its cursive
fallback, which does not satisfy the requested appearance.

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
