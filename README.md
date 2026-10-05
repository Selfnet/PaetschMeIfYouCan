# P-tschMeIfYouCan
Ein Geschicklichkeitsspiel mit Spaß am Gerät

## Important: Name Entry Keyboard Behavior

This is a required interaction contract. Preserve it when changing the dashboard:

- **Enter:** submit both players' names and proceed to the leaderboard. Names are optional: empty or whitespace-only names skip that player's leaderboard entry.
- **Tab / Shift+Tab:** cycle between the name inputs, including wrapping; never submit names or advance to the leaderboard, even when both names are filled.

These rules apply regardless of which player wins or whether the race is tied. Automatic name-entry timeout is separate from keyboard submission.

[Keyboard regression tests](Test_Server/tests/test_name_entry_keyboard.py) execute the dashboard's actual JavaScript handler. Run them when changing name entry or keyboard handling; they do not test live serial hardware.

## Development

[Adding gamemodes](docs/gamemode-authoring.md) explains the board/session contract, registry, validation, and scoring identities. Start there when adding a mode; firmware protocols and deployment are outside its scope.

Run `uv run pytest -q` from `Test_Server/`. Node.js must be on `PATH` for the name-entry keyboard tests, which execute the dashboard's JavaScript handler.
