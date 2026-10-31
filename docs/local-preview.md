# Local Preview

The preview runs the real Flask/Socket.IO game, timers, mode selection, name
entry and SQLite scoring with simulated boards. It does not connect to the
deployed PC or open serial devices. The simulation console is separate from
the game display and is not registered by the normal `app.py` entry point.

From `Test_Server/`, run:

```sh
uv run preview.py --data-dir "$HOME/workspace/scratchpad/patchme-preview-$(date +%Y%m%dT%H%M%S)"
```

Open <http://127.0.0.1:5001/preview>. Use `--port` to choose another local port
when necessary. The server binds explicitly to `127.0.0.1`; do not expose this
development tool through a network interface or proxy.

The console provides Start race, Reset and unplug, and each player's Patch 6,
Wrong cable and Finish actions. Click inside the game to use its normal keyboard
controls. Finish both players to enter optional names and save scores. The
game's own mode chooser supports Full Field, Half Field and Quarter Field.
The link above the console opens the game alone at full viewport size; keep the
console tab open to simulate board changes.

## Score Isolation

`--data-dir` is required and must not already exist. Each launch creates a new
`leaderboard.sqlite3` containing fictional `Sample` names in slot 1 for each
mode; other slots start empty. The preview deliberately ignores `LEADERBOARD_DB`
and never opens the normal game database. User-entered preview scores are real
SQLite writes, but only to this disposable file. Restart with a new directory
for a fresh preview. Stop the server with Ctrl+C; its data remains in that
directory until deliberately removed.

Never use a production database or backup for interactive testing. For a
deployment, make a fresh SQLite online backup and verify its integrity before
changing the deployed source. A preview-time backup does not include scores
recorded later. This UI redesign requires no schema migration or score rewrite;
restoring a database would discard scores recorded since that backup.

## Verification Boundaries

- [`tests/test_preview.py`](../Test_Server/tests/test_preview.py) checks database
  isolation, serial blocking and a complete simulated race through score saving;
  it does not exercise browser layout or physical readers.
- [`tests/browser_harness.py`](../Test_Server/tests/browser_harness.py) provides
  controlled time, board observations, pagination delays and page errors for
  deterministic browser checks. It uses a separate database, not preview data.
- The [game README](../README.md) owns required name-entry keyboard behavior,
  leaderboard grouping and slot semantics. The preview must retain those
  contracts; simulation is not proof of hardware or deployed kiosk behavior.

Preview approval is not deployment approval. Keep the deployed service and its
database untouched until deployment is explicitly authorized.
