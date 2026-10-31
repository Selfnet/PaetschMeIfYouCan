"""Local-only game preview with simulated boards and a new, disposable database."""

import argparse
from pathlib import Path

from flask import jsonify, render_template, request

from app import create_app
from board import EXPECTED_IDS, NUM_PORTS, OPEN_ID
from gamemodes import BUILTIN_MODES
from leaderboard import LeaderboardStore, NewLeaderboardEntry


def create_preview(data_dir: Path, *, background=True):
    data_dir = Path(data_dir).expanduser()
    # Never open an existing directory/database, including LEADERBOARD_DB.
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    store = LeaderboardStore(data_dir / "leaderboard.sqlite3")
    store.insert_entries(
        [
            NewLeaderboardEntry(
                race_id=f"sample-{mode.id}-{index}",
                mode_id=mode.id,
                player_number=1,
                name=f"Sample{index:02}",
                duration_ms=20_000 + index * 1713,
            )
            for mode in BUILTIN_MODES.metadata()
            for index in range(60)
        ]
    )
    app, socketio = create_app(
        {
            "START_SERIAL_ON_CONNECT": False,
            "START_BACKGROUND_TASKS": background,
            "TEMPLATES_AUTO_RELOAD": True,
        },
        store=store,
    )
    runtime = app.extensions["game_runtime"]

    def reset_boards():
        runtime.apply(runtime.race.reset)
        for player in (1, 2):
            runtime.observe_board(player, [OPEN_ID] * NUM_PORTS)

    reset_boards()

    @app.before_request
    def block_hardware():
        if request.endpoint == "restart_serial":
            return "Serial hardware is disabled in the local preview", 403

    @app.get("/preview")
    def preview():
        return render_template("preview.html")

    @app.post("/preview/reset")
    def reset():
        reset_boards()
        return jsonify(ok=True)

    @app.post("/preview/board")
    def board():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Expected a board update"), 400
        player, count, wrong = (
            data.get("player"),
            data.get("count"),
            data.get("wrong", False),
        )
        if (
            type(player) is not int
            or player not in (1, 2)
            or type(count) is not int
            or not 0 <= count <= NUM_PORTS
            or type(wrong) is not bool
        ):
            return jsonify(error="Invalid player, count or wrong flag"), 400
        values = list(EXPECTED_IDS[:count]) + [OPEN_ID] * (NUM_PORTS - count)
        if wrong and count < NUM_PORTS:
            values[count] = EXPECTED_IDS[count] ^ 1
        runtime.observe_board(player, values)
        return jsonify(ok=True)

    return app, socketio


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        required=True,
        type=Path,
        help="New directory for disposable sample scores; must not exist",
    )
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()
    try:
        app, socketio = create_preview(args.data_dir)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Could not create preview: {error}\n")
    print(f"Local preview: http://127.0.0.1:{args.port}/preview", flush=True)
    socketio.run(
        app,
        host="127.0.0.1",
        port=args.port,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )


if __name__ == "__main__":
    main()
