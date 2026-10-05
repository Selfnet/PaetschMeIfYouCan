# P-tschMeIfYouCan
Ein Geschicklichkeitsspiel mit Spaß am Gerät

## Important: Name Entry Keyboard Behavior

This is a required interaction contract. Preserve it when changing the dashboard:

- **Enter with a name missing:** focus the unnamed player's input without submitting either name, skipping a player, or opening the leaderboard. Whitespace-only input counts as missing.
- **Enter with both names filled:** submit both names and proceed to the leaderboard.
- **Tab / Shift+Tab:** cycle between the name inputs, including wrapping; never submit names or advance to the leaderboard, even when both names are filled.

These rules apply regardless of which player wins or whether the race is tied. Automatic name-entry timeout is separate from keyboard submission.

[Keyboard regression tests](Test_Server/tests/test_name_entry_keyboard.py) execute the dashboard's actual JavaScript handler. Run them when changing name entry or keyboard handling; they do not test live serial hardware.

## Development

[Adding gamemodes](docs/gamemode-authoring.md) explains the board/session contract, registry, validation, and scoring identities. Start there when adding a mode; firmware protocols and deployment are outside its scope.

Run `uv run pytest -q` from `Test_Server/`. Node.js must be on `PATH` for the name-entry keyboard tests, which execute the dashboard's JavaScript handler.
