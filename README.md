# P-tschMeIfYouCan
Ein Geschicklichkeitsspiel mit Spaß am Gerät

## Important: Name Entry Keyboard Behavior

This is a required interaction contract. Preserve it when changing the dashboard:

- **Enter in the first displayed input:** move to the second input if its name is empty or whitespace-only; otherwise submit both names and proceed to the leaderboard.
- **Enter in the second displayed input:** submit both names and proceed to the leaderboard, even when either name is blank. Empty or whitespace-only names skip that player's leaderboard entry.
- **Tab / Shift+Tab:** cycle between the name inputs, including wrapping; never submit names or advance to the leaderboard, even when both names are filled.

The inputs are displayed in result order (winner first), not fixed player-number order. These rules apply regardless of which player wins or whether the race is tied. Automatic name-entry timeout is separate from keyboard submission.

[Keyboard regression tests](Test_Server/tests/test_name_entry_keyboard.py) execute the dashboard's actual JavaScript handler. Run them when changing name entry or keyboard handling; they do not test live serial hardware.

## Leaderboard Slots

Use **1-9**, including the numeric keypad, or the slot selector to choose a leaderboard while the game is ready. The shortcut also works while browsing the idle leaderboard, which refreshes to the selected slot. Slot switching is disabled during a race, name entry, post-race results, and the mode dialog. Digits typed into name inputs remain part of the name.

Each slot has independent scores and ranks for every game mode. Slot 1 retains all existing scores; slots 2-9 start empty. Switching never deletes scores. The selected slot is saved in SQLite and restored after backend or machine restarts, independently of browser storage. The slot is captured when a race starts, so delayed score writes cannot move a race into another slot.

The database remains at `LEADERBOARD_DB` or `~/.local/share/patchmeifyoucan/leaderboard.sqlite3`. Migration adds the slot column and a singleton selection setting without changing historical score IDs, timestamps, or mode IDs. Back up the database before deploying this migration. Source-only rollback to pre-slot code is unsafe: that code would combine all slots' scores. A database rollback also discards any scores recorded since its backup.

[Slot persistence and transport tests](Test_Server/tests/test_leaderboard_slots.py) cover migration, isolation, restart restoration, failed settings writes, race identity, and stale page rejection; they do not exercise physical hardware. [Slot keyboard tests](Test_Server/tests/test_leaderboard_slot_keyboard.py) execute the actual dashboard shortcut and selection helpers, including modifier, focus, and phase guards; they do not replace browser layout checks.

## Development

[Adding gamemodes](docs/gamemode-authoring.md) explains the board/session contract, registry, validation, and scoring identities. Start there when adding a mode; firmware protocols and deployment are outside its scope.

Run `uv run pytest -q` from `Test_Server/`. Node.js must be on `PATH` for the name-entry keyboard tests, which execute the dashboard's JavaScript handler.
