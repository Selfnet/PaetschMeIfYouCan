import sqlite3

import pytest

from app import create_app
from leaderboard import LeaderboardStore
from race import Phase

VERIFIED = [2] * 48


@pytest.fixture
def app_bundle(tmp_path, clock):
    store = LeaderboardStore(tmp_path / "leaderboard.sqlite3")
    app, socketio = create_app(
        {
            "TESTING": True,
            "START_SERIAL_ON_CONNECT": False,
            "START_BACKGROUND_TASKS": False,
        },
        clock_ns=clock,
        store=store,
        race_id_factory=lambda: "race-1",
    )
    return app, socketio, app.extensions["game_runtime"]


def latest_event(client, name):
    events = [event for event in client.get_received() if event["name"] == name]
    assert events
    return events[-1]["args"][0]


def finish_race(runtime, clock):
    runtime.handle_change(runtime.race.start())
    clock.advance_ms(1000)
    runtime.observe_cells(1, VERIFIED)
    clock.advance_ms(100)
    runtime.observe_cells(2, VERIFIED)


def test_connect_receives_ready_state_without_starting_serial(app_bundle):
    app, socketio, runtime = app_bundle

    client = socketio.test_client(app)

    assert latest_event(client, "game_state")["phase"] == Phase.READY
    assert runtime.serial_threads is None


def test_unavailable_default_database_does_not_prevent_ready_screen(
    tmp_path, monkeypatch, clock
):
    blocked_directory = tmp_path / "not-a-directory"
    blocked_directory.write_text("blocked")
    monkeypatch.setenv("LEADERBOARD_DB", str(blocked_directory / "scores.sqlite3"))

    app, socketio = create_app(
        {
            "TESTING": True,
            "START_SERIAL_ON_CONNECT": False,
            "START_BACKGROUND_TASKS": False,
        },
        clock_ns=clock,
        race_id_factory=lambda: "race-1",
    )

    assert latest_event(socketio.test_client(app), "game_state")["phase"] == Phase.READY


def test_state_changes_are_synchronized_across_clients(app_bundle):
    app, socketio, _ = app_bundle
    first = socketio.test_client(app)
    second = socketio.test_client(app)
    first.get_received()
    second.get_received()

    app.test_client().get("/start-clock")

    assert latest_event(first, "game_state")["phase"] == Phase.RACING
    assert latest_event(second, "game_state")["phase"] == Phase.RACING


@pytest.mark.parametrize(
    "target_phase",
    [Phase.READY, Phase.RACING, Phase.NAME_ENTRY, Phase.LEADERBOARD],
)
def test_reconnect_receives_complete_current_phase(app_bundle, clock, target_phase):
    app, socketio, runtime = app_bundle
    if target_phase != Phase.READY:
        runtime.handle_change(runtime.race.start())
    if target_phase in (Phase.NAME_ENTRY, Phase.LEADERBOARD):
        clock.advance_ms(1000)
        runtime.observe_cells(1, VERIFIED)
        runtime.observe_cells(2, VERIFIED)
    if target_phase == Phase.LEADERBOARD:
        race_id = runtime.race.snapshot()["race_id"]
        runtime.handle_change(runtime.race.submit_name(race_id, 1, ""))
        runtime.handle_change(runtime.race.submit_name(race_id, 2, ""))

    client = socketio.test_client(app)

    assert latest_event(client, "game_state")["phase"] == target_phase


def test_stale_name_event_is_rejected_without_broadcast(app_bundle, clock):
    app, socketio, runtime = app_bundle
    finish_race(runtime, clock)
    client = socketio.test_client(app)
    client.get_received()

    client.emit("name_draft", {"race_id": "old", "player_number": 1, "draft": "Ada"})

    assert client.get_received() == []
    assert runtime.race.snapshot()["name_entry"]["drafts"]["1"] == ""


def test_restart_serial_joins_readers_before_replacing_them(app_bundle, monkeypatch):
    _, _, runtime = app_bundle
    calls = []

    class Reader:
        def __init__(self, serial_id):
            self.serial_id = serial_id

        def join(self):
            calls.append(f"join-{self.serial_id}")

    runtime.serial_threads = [Reader(0), Reader(1)]
    monkeypatch.setattr(
        runtime, "_start_serial_unlocked", lambda: calls.append("start")
    )

    runtime.restart_serial()

    assert calls == ["join-0", "join-1", "start"]


def test_index_contains_all_phase_views_and_exact_copy(app_bundle):
    app, _, _ = app_bundle

    html = app.test_client().get("/").get_data(as_text=True)

    assert 'id="raceView"' in html
    assert 'id="nameEntryView"' in html
    assert 'id="leaderboardView"' in html
    assert "you were able to success" in html
    assert html.count('data-max-codepoints="12"') == 2
    assert 'id="nameEntryCountdown"' in html
    assert 'id="leaderboardCountdown"' in html
    assert 'id="persistenceError"' in html
    assert "grid-column: var(--switch-column)" in html
    assert "grid-row: var(--switch-row)" in html
    assert "Math.floor(port / 2) + 1" in html
    assert "(port % 2) + 1" in html
    assert 'id="spacebarIndicator"' in html
    assert 'aria-atomic="true" hidden' in html
    assert 'started: { action: "Go"' in html
    assert 'stopped: { action: "Pause"' in html
    assert 'reset: { action: "Reset"' in html
    assert "showSpacebarIndicator(action)" in html
    assert "aspect-ratio: 1" in html
    assert "Voice Controled: Loudly shout" in html
    assert "input.disabled = resolved" in html
    assert "document.activeElement" in html
    assert "submitNames()" in html


def test_submitting_names_persists_and_highlights_current_rows(app_bundle, clock):
    app, socketio, runtime = app_bundle
    finish_race(runtime, clock)
    client = socketio.test_client(app)
    race_id = runtime.race.snapshot()["race_id"]
    client.get_received()

    client.emit("submit_name", {"race_id": race_id, "player_number": 1, "draft": "Ada"})
    client.emit(
        "submit_name", {"race_id": race_id, "player_number": 2, "draft": "Grace"}
    )

    state = latest_event(client, "game_state")
    assert state["phase"] == Phase.LEADERBOARD
    assert [row["name"] for row in state["leaderboard"]] == ["Ada", "Grace"]
    assert all(row["current_race"] for row in state["leaderboard"])


class FailingStore:
    def insert_entries(self, entries):
        raise sqlite3.OperationalError("write failed")

    def top_entries(self):
        return []


def test_database_failure_still_enters_dismissible_leaderboard(clock):
    app, socketio = create_app(
        {
            "TESTING": True,
            "START_SERIAL_ON_CONNECT": False,
            "START_BACKGROUND_TASKS": False,
        },
        clock_ns=clock,
        store=FailingStore(),
        race_id_factory=lambda: "race-1",
    )
    runtime = app.extensions["game_runtime"]
    finish_race(runtime, clock)
    client = socketio.test_client(app)
    race_id = runtime.race.snapshot()["race_id"]
    client.get_received()

    client.emit("submit_name", {"race_id": race_id, "player_number": 1, "draft": "Ada"})
    client.emit("submit_name", {"race_id": race_id, "player_number": 2, "draft": ""})

    state = latest_event(client, "game_state")
    assert state["persistence_error"] == "Scores could not be saved"
    client.emit("dismiss_leaderboard", {"race_id": race_id})
    assert latest_event(client, "game_state")["phase"] == Phase.READY
