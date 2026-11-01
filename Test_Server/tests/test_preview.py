import sqlite3

import pytest


def test_preview_routes_are_not_registered_in_normal_game(tmp_path):
    from app import create_app
    from leaderboard import LeaderboardStore

    app, _ = create_app(
        {"START_SERIAL_ON_CONNECT": False, "START_BACKGROUND_TASKS": False},
        store=LeaderboardStore(tmp_path / "game.sqlite3"),
    )
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/preview").status_code == 404
    assert client.post("/preview/reset").status_code == 404
    assert (
        client.post("/preview/board", json={"player": 1, "count": 48}).status_code
        == 404
    )


def test_preview_uses_new_database_and_never_environment_database(
    tmp_path, monkeypatch
):
    from preview import create_preview

    live = tmp_path / "live.sqlite3"
    live.write_bytes(b"must not touch")
    monkeypatch.setenv("LEADERBOARD_DB", str(live))
    app, _ = create_preview(tmp_path / "preview", background=False)
    runtime = app.extensions["game_runtime"]
    assert runtime.store.path == tmp_path / "preview" / "leaderboard.sqlite3"
    assert live.read_bytes() == b"must not touch"
    assert app.config["START_SERIAL_ON_CONNECT"] is False
    assert app.config["START_BACKGROUND_TASKS"] is False
    with sqlite3.connect(runtime.store.path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM leaderboard_entries").fetchone()[0] == 180
        )
    assert app.test_client().get("/restart-serial").status_code == 403
    assert runtime.serial_threads is None


def test_preview_refuses_existing_data_directory(tmp_path):
    from preview import create_preview

    with pytest.raises(FileExistsError):
        create_preview(tmp_path, background=False)


def test_preview_runs_real_race_and_saves_only_local_scores(tmp_path):
    from preview import create_preview

    app, socketio = create_preview(tmp_path / "preview", background=False)
    client = app.test_client()
    socket = socketio.test_client(app)
    runtime = app.extensions["game_runtime"]
    assert client.get("/preview").status_code == 200
    assert socket.emit("select_mode", {"mode_id": "quarter-field"}, callback=True)[
        "accepted"
    ]
    client.post("/preview/reset")
    client.get("/start-clock")
    for player in (2, 1):
        response = client.post("/preview/board", json={"player": player, "count": 12})
        assert response.status_code == 200
    state = runtime.race.snapshot()
    assert state["phase"] == "name_entry"
    for player in (2, 1):
        socket.emit(
            "submit_name",
            {
                "race_id": state["race_id"],
                "player_number": player,
                "draft": "PreviewTest" if player == 2 else "",
            },
        )
    assert runtime.race.snapshot()["phase"] == "leaderboard"
    with sqlite3.connect(runtime.store.path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM leaderboard_entries").fetchone()[0] == 181
        )
        assert db.execute(
            "SELECT mode_id, leaderboard_id FROM leaderboard_entries WHERE name = ?",
            ("PreviewTest",),
        ).fetchone() == ("quarter-field", 1)
    client.post("/preview/reset")
    state = runtime.race.snapshot()
    assert state["phase"] == "ready"
    assert state["players"]["1"]["presentation"]["progress"]["current"] == 0
    assert runtime.serial_threads is None
    socket.disconnect()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"player": 3, "count": 1},
        {"player": 1, "count": 49},
        {"player": True, "count": 1},
        {"player": 1, "count": -1},
        {"player": 1, "count": 1, "wrong": "yes"},
    ],
)
def test_preview_rejects_invalid_board_changes(tmp_path, payload):
    from preview import create_preview

    app, _ = create_preview(tmp_path / "preview", background=False)
    response = app.test_client().post("/preview/board", json=payload)
    assert response.status_code == 400
    assert (
        app.extensions["game_runtime"].race.snapshot()["players"]["1"]["presentation"][
            "progress"
        ]["current"]
        == 0
    )


def test_preview_simulates_wrong_connection(tmp_path):
    from preview import create_preview

    app, _ = create_preview(tmp_path / "preview", background=False)
    response = app.test_client().post(
        "/preview/board", json={"player": 1, "count": 6, "wrong": True}
    )
    assert response.status_code == 200
    ports = app.extensions["game_runtime"].race.snapshot()["players"]["1"][
        "presentation"
    ]["ports"]
    assert [port["feedback"] for port in ports[:8]] == ["correct"] * 6 + [
        "wrong",
        "open",
    ]


def test_preview_supports_injected_event_clock(tmp_path):
    from datetime import UTC, datetime

    from preview import create_preview
    from tests.test_app import latest_event

    now = datetime(2026, 11, 1, 10, tzinfo=UTC)
    app, sio = create_preview(tmp_path / "timed", background=False, now_utc=lambda: now)
    client = sio.test_client(app)
    state = latest_event(client, "game_state")
    result = client.emit(
        "save_event",
        {
            "event": {
                "name": "Preview",
                "start_now": True,
                "ends_at": "2026-11-01T11:00:00Z",
            },
            "revision": state["leaderboard_policy"]["revision"],
        },
        callback=True,
    )
    assert result["accepted"]
    assert result["event"]["starts_at"] == "2026-11-01T10:00:00+00:00"
    app.test_client().get("/start-clock")
    assert (
        latest_event(client, "game_state")["race_leaderboard"]["id"]
        == result["event"]["id"]
    )
