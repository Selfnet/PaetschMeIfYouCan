import sqlite3
import threading

import pytest

from app import create_app
from board import EXPECTED_IDS
from leaderboard import LeaderboardStore
from tests.test_app import (
    CONFIG,
    finish_race,
    latest_event,
    page_request,
    run_blocked,
    unblock,
)
from tests.test_app import (
    app_bundle as app_bundle,  # noqa: PLC0414 - expose pytest fixture
)
from tests.test_score_recovery import failed_race, retry_request


def request_maintenance(client, enabled=True):
    return client.emit("set_maintenance", {"enabled": enabled}, callback=True)


def test_idle_activation_blocks_game_controls_and_survives_reset(app_bundle):
    app, sio, runtime = app_bundle
    operator, viewer = sio.test_client(app), sio.test_client(app)
    assert request_maintenance(operator)["accepted"]
    for client in (operator, viewer):
        state = latest_event(client, "game_state")
        assert state["phase"] == "maintenance"
        assert state["maintenance_requested"] is True
    http = app.test_client()
    for route in ("/start-clock", "/space-clock", "/reset-clock"):
        http.get(route)
        assert runtime.race.snapshot()["phase"] == "maintenance"
        assert runtime.race.snapshot()["race_id"] is None
    assert not operator.emit("select_mode", {"mode_id": "half-field"}, callback=True)[
        "accepted"
    ]
    assert not operator.emit(
        "select_leaderboard_slot", {"leaderboard_slot": 3}, callback=True
    )["accepted"]
    assert request_maintenance(operator, False)["accepted"]
    assert latest_event(viewer, "game_state")["phase"] == "ready"
    http.get("/start-clock")
    assert runtime.race.snapshot()["phase"] == "racing"


@pytest.mark.parametrize("exit_method", ["dismiss", "timeout"])
def test_queue_preserves_race_names_scores_and_results(app_bundle, clock, exit_method):
    app, sio, runtime = app_bundle
    client = sio.test_client(app)
    runtime.start()
    before = runtime.race.snapshot()
    assert request_maintenance(client)["accepted"]
    queued = runtime.race.snapshot()
    assert queued["phase"] == "racing"
    assert queued["race_id"] == before["race_id"]
    assert queued["browse_epoch"] == before["browse_epoch"]
    for player in (1, 2):
        clock.advance_ms(1000)
        runtime.observe_board(player, EXPECTED_IDS)
    state = runtime.race.snapshot()
    assert state["phase"] == "name_entry"
    assert state["maintenance_requested"] is True
    for player, name in ((1, "Ada"), (2, "Grace")):
        runtime.apply(runtime.race.submit_name, state["race_id"], player, name)
    state = runtime.race.snapshot()
    assert state["phase"] == "leaderboard"
    assert state["persistence_status"] == "saved"
    client.emit("request_leaderboard", page_request(race_id=state["race_id"]))
    assert [
        (row["name"], row["race_duration_ms"])
        for row in latest_event(client, "leaderboard_snapshot")["rows"]
    ] == [("Ada", 1000), ("Grace", 2000)]
    if exit_method == "dismiss":
        client.emit("dismiss_leaderboard", {"race_id": state["race_id"]})
    else:
        clock.advance_ms(60_000)
        runtime.tick()
    assert latest_event(client, "game_state")["phase"] == "maintenance"


def test_stopped_game_waits_for_reset_and_queue_can_be_cancelled(app_bundle, clock):
    app, sio, runtime = app_bundle
    client = sio.test_client(app)
    runtime.start()
    runtime.space()
    assert request_maintenance(client)["accepted"]
    clock.advance_ms(300_000)
    runtime.tick()
    assert runtime.race.snapshot()["phase"] == "stopped"
    assert request_maintenance(client, False)["accepted"]
    runtime.space()
    assert runtime.race.snapshot()["phase"] == "ready"
    runtime.start()
    assert request_maintenance(client)["accepted"]
    runtime.space()
    runtime.space()
    assert runtime.race.snapshot()["phase"] == "maintenance"


@pytest.mark.parametrize("phase", ["ready", "racing"])
def test_requested_setting_restores_on_reconnect_and_backend_restart(app_bundle, phase):
    app, sio, runtime = app_bundle
    if phase == "racing":
        runtime.start()
    client = sio.test_client(app)
    assert request_maintenance(client)["accepted"]
    client.disconnect()
    assert (
        latest_event(sio.test_client(app), "game_state")["maintenance_requested"]
        is True
    )
    for enabled in (True, False):
        if not enabled:
            assert request_maintenance(sio.test_client(app), False)["accepted"]
        restarted, restart_sio = create_app(
            CONFIG, store=LeaderboardStore(runtime.store.path)
        )
        state = latest_event(restart_sio.test_client(restarted), "game_state")
        assert state["maintenance_requested"] is enabled
        assert state["phase"] == ("maintenance" if enabled else "ready")


def test_failed_score_recovery_must_finish_before_maintenance(
    app_bundle, clock, monkeypatch
):
    app, sio, runtime = app_bundle
    failed_race(runtime, clock, monkeypatch)
    client = sio.test_client(app)
    before = runtime.race.snapshot()
    assert request_maintenance(client)["accepted"]
    assert runtime.race.snapshot()["browse_epoch"] == before["browse_epoch"]
    app.test_client().get("/reset-clock")
    clock.advance_ms(300_000)
    runtime.tick()
    assert runtime.race.snapshot()["phase"] == "leaderboard"
    assert runtime.race.snapshot()["persistence_status"] == "error"
    assert client.emit("retry_scores", retry_request(runtime), callback=True)[
        "accepted"
    ]
    assert runtime.race.snapshot()["phase"] == "leaderboard"
    clock.advance_ms(60_000)
    runtime.tick()
    assert runtime.race.snapshot()["phase"] == "maintenance"
    assert runtime.store.snapshot_boundary("quarter-field", 4) == 2


def test_queue_does_not_abandon_inflight_score_save(app_bundle, clock, monkeypatch):
    app, sio, runtime = app_bundle
    race_id = finish_race(runtime, clock)
    client = sio.test_client(app)
    assert request_maintenance(client)["accepted"]
    entered, release = threading.Event(), threading.Event()
    original = runtime.store.insert_entries

    def delayed(entries):
        entered.set()
        assert release.wait(3)
        return original(entries)

    monkeypatch.setattr(runtime.store, "insert_entries", delayed)
    thread, errors = run_blocked(
        lambda: runtime.apply(runtime.race.submit_name, race_id, 2, "Grace"),
        entered,
        release,
    )
    try:
        app.test_client().get("/reset-clock")
        client.emit("dismiss_leaderboard", {"race_id": race_id})
        clock.advance_ms(300_000)
        runtime.tick()
        assert runtime.race.snapshot()["phase"] == "leaderboard"
        assert runtime.race.snapshot()["persistence_status"] == "pending"
    finally:
        unblock(thread, errors, release)
    assert runtime.race.snapshot()["phase"] == "leaderboard"
    clock.advance_ms(60_000)
    runtime.tick()
    assert runtime.race.snapshot()["phase"] == "maintenance"
    assert runtime.store.snapshot_boundary("full-field") == 2


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_malformed_request_cannot_change_setting(app_bundle, value):
    app, sio, runtime = app_bundle
    client = sio.test_client(app)
    before = runtime.race.snapshot()
    response = request_maintenance(client, value)
    assert response and response["accepted"] is False
    assert runtime.race.snapshot() == before


@pytest.mark.parametrize("enabled", [True, False])
def test_failed_settings_write_leaves_authoritative_state_unchanged(
    app_bundle, enabled
):
    app, sio, runtime = app_bundle
    client = sio.test_client(app)
    assert request_maintenance(client, not enabled)["accepted"]
    before = runtime.race.snapshot()
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "CREATE TRIGGER reject_setting BEFORE UPDATE ON leaderboard_settings BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
        )
    result = request_maintenance(client, enabled)
    assert result["accepted"] is False
    assert result["error"]
    assert runtime.race.snapshot() == before
    assert LeaderboardStore(runtime.store.path).maintenance_requested() is not enabled


def test_settings_migration_preserves_existing_slot_and_scores(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE leaderboard_settings (id INTEGER PRIMARY KEY, leaderboard_slot INTEGER NOT NULL)"
        )
        db.execute("INSERT INTO leaderboard_settings VALUES (1, 7)")
    for _ in range(2):
        store = LeaderboardStore(path)
        assert store.selected_slot() == 7
        assert store.maintenance_requested() is False


def test_unreadable_maintenance_setting_preserves_degraded_play(tmp_path, monkeypatch):
    store = LeaderboardStore(tmp_path / "scores.sqlite3")

    def fail():
        raise sqlite3.OperationalError("fixture failure")

    monkeypatch.setattr(store, "maintenance_requested", fail)
    app, sio = create_app(CONFIG, store=store)
    client = sio.test_client(app)
    assert latest_event(client, "game_state")["phase"] == "ready"
    app.test_client().get("/start-clock")
    assert app.extensions["game_runtime"].race.snapshot()["phase"] == "racing"
    assert request_maintenance(client)["accepted"]
    assert latest_event(client, "game_state")["maintenance_requested"] is True


def test_maintenance_write_is_serialized_against_start(app_bundle, monkeypatch):
    app, sio, runtime = app_bundle
    client = sio.test_client(app)
    entered, release, attempted = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original = runtime.store.set_maintenance_requested

    def delayed(enabled):
        entered.set()
        assert release.wait(3)
        original(enabled)

    monkeypatch.setattr(runtime.store, "set_maintenance_requested", delayed)
    thread, errors = run_blocked(lambda: request_maintenance(client), entered, release)

    def start():
        attempted.set()
        runtime.start()

    starter = threading.Thread(target=start)
    starter.start()
    try:
        assert attempted.wait(3)
    finally:
        unblock(thread, errors, release)
        starter.join(3)
    assert not starter.is_alive()
    assert runtime.race.snapshot()["phase"] == "maintenance"
    assert runtime.race.snapshot()["race_id"] is None
