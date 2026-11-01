import sqlite3
import threading

import pytest

from leaderboard import NewLeaderboardEntry
from tests.test_app import (
    app_bundle as app_bundle,  # noqa: PLC0414 - expose pytest fixture
)
from tests.test_app import (
    finish_race,
    latest_event,
    page_request,
    run_blocked,
    unblock,
)


def fail_save(entries):
    raise sqlite3.OperationalError("fixture write failure")


def failed_race(runtime, clock, monkeypatch):
    runtime.apply(runtime.race.select_mode, "quarter-field")
    assert runtime.select_leaderboard_slot(4)["accepted"]
    race_id = finish_race(runtime, clock)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.store, "insert_entries", fail_save)
        runtime.apply(runtime.race.submit_name, race_id, 2, "Grace")
    return race_id


def retry_request(runtime):
    state = runtime.race.snapshot()
    return {"race_id": state["race_id"], "browse_epoch": state["browse_epoch"]}


def test_failed_save_pauses_timeout_and_activity_until_recovery(
    app_bundle, clock, monkeypatch
):
    app, sio, runtime = app_bundle
    race_id = failed_race(runtime, clock, monkeypatch)
    client = sio.test_client(app)
    state = latest_event(client, "game_state")
    assert state["persistence_status"] == "error"
    assert state["remaining_seconds"] is None
    clock.advance_ms(300_000)
    runtime.tick()
    client.emit("leaderboard_activity", {"race_id": race_id})
    assert runtime.race.snapshot()["phase"] == "leaderboard"
    assert runtime.race.snapshot()["remaining_seconds"] is None
    assert client.emit("retry_scores", retry_request(runtime), callback=True)[
        "accepted"
    ]
    assert runtime.race.snapshot()["remaining_seconds"] == 60
    clock.advance_ms(59_000)
    runtime.tick()
    assert runtime.race.snapshot()["remaining_seconds"] == 1
    clock.advance_ms(1000)
    runtime.tick()
    assert runtime.race.snapshot()["phase"] == "ready"


@pytest.mark.parametrize("previous_time", [500, 2000])
def test_retry_saves_original_identity_and_refreshes_frozen_browse(
    app_bundle, clock, monkeypatch, previous_time
):
    app, sio, runtime = app_bundle
    runtime.store.insert_entries(
        [NewLeaderboardEntry("old", 1, "ADA", previous_time, "quarter-field", 4)]
    )
    race_id = failed_race(runtime, clock, monkeypatch)
    client = sio.test_client(app)
    before = latest_event(client, "game_state")
    request = page_request(mode_id="quarter-field", leaderboard_id=4, race_id=race_id)
    client.emit("request_leaderboard", request)
    old = latest_event(client, "leaderboard_snapshot")
    assert old["target_score_id"] is None
    assert old["rows"][0]["attempts"] == 1
    assert old["rows"][0]["current_race"] is False
    # Neither later client edits nor namespace controls can alter the saved attempt.
    client.emit(
        "submit_name", {"race_id": race_id, "player_number": 1, "draft": "Other"}
    )
    client.emit("select_mode", {"mode_id": "full-field"})
    client.emit("select_leaderboard_slot", {"leaderboard_slot": 8})
    clock.advance_ms(10_000)
    token = retry_request(runtime)
    assert client.emit(
        "retry_scores",
        token
        | {
            "name": "Changed",
            "duration_ms": 1,
            "mode_id": "full-field",
            "leaderboard_slot": 8,
        },
        callback=True,
    )["accepted"]
    after = latest_event(client, "game_state")
    assert after["persistence_status"] == "saved"
    assert after["persistence_error"] is None
    assert after["browse_epoch"] > before["browse_epoch"]
    client.emit(
        "request_leaderboard", request | {"boundary": old["boundary"], "offset": 50}
    )
    assert client.get_received() == []
    client.emit("request_leaderboard", request | {"request_id": 2})
    refreshed = latest_event(client, "leaderboard_snapshot")
    rows = refreshed["rows"]
    assert refreshed["target_score_id"] == rows[0]["id"]
    assert [(r["name"], r["race_duration_ms"], r["attempts"]) for r in rows] == [
        ("Ada", 1000, 2),
        ("Grace", 1100, 1),
    ]
    assert all(
        r["current_race"]
        and r["mode_id"] == "quarter-field"
        and r["leaderboard_id"] == 4
        for r in rows
    )
    assert rows[0]["personal_best"] is (previous_time == 2000)
    assert rows[1]["personal_best"] is True
    with sqlite3.connect(runtime.store.path) as connection:
        assert connection.execute(
            "SELECT race_id, player_number, name, duration_ms, mode_id, leaderboard_id "
            "FROM leaderboard_entries WHERE race_id = ? ORDER BY player_number",
            (race_id,),
        ).fetchall() == [
            (race_id, 1, "Ada", 1000, "quarter-field", 4),
            (race_id, 2, "Grace", 1100, "quarter-field", 4),
        ]
    assert not client.emit("retry_scores", token, callback=True)["accepted"]
    assert not client.emit("retry_scores", retry_request(runtime), callback=True)[
        "accepted"
    ]
    assert runtime.store.snapshot_boundary("full-field", 8) == 0


@pytest.mark.parametrize(
    "data",
    [
        None,
        False,
        [],
        {},
        {"race_id": False},
        {"race_id": []},
        {"race_id": "old"},
        {"race_id": "old", "browse_epoch": 0},
        {"browse_epoch": False},
        {"browse_epoch": 1.0},
        {"browse_epoch": -1},
        {"browse_epoch": []},
    ],
)
def test_malformed_and_stale_retries_leave_failure_recoverable(
    app_bundle, clock, monkeypatch, data
):
    app, sio, runtime = app_bundle
    failed_race(runtime, clock, monkeypatch)
    client = sio.test_client(app)
    before = runtime.race.snapshot()
    payload = retry_request(runtime) | data if isinstance(data, dict) and data else data
    assert client.emit("retry_scores", payload, callback=True) == {"accepted": False}
    assert runtime.race.snapshot() == before
    assert runtime.store.snapshot_boundary("quarter-field", 4) == 0


def test_page_started_before_retry_cannot_publish_after_success(
    app_bundle, clock, monkeypatch
):
    app, sio, runtime = app_bundle
    race_id = failed_race(runtime, clock, monkeypatch)
    client = sio.test_client(app)
    client.get_received()
    entered, release = threading.Event(), threading.Event()
    original = runtime.store.page_entries

    def delayed(*args):
        page = original(*args)
        entered.set()
        assert release.wait(3)
        return page

    request = page_request(mode_id="quarter-field", leaderboard_id=4, race_id=race_id)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.store, "page_entries", delayed)
        thread, errors = run_blocked(
            lambda: client.emit("request_leaderboard", request), entered, release
        )
        try:
            assert client.emit("retry_scores", retry_request(runtime), callback=True)[
                "accepted"
            ]
        finally:
            unblock(thread, errors, release)
    assert not [
        event
        for event in client.get_received()
        if event["name"] == "leaderboard_snapshot"
    ]
    client.emit("request_leaderboard", request | {"request_id": 2})
    rows = latest_event(client, "leaderboard_snapshot")["rows"]
    assert [
        (row["name"], row["current_race"], row["personal_best"]) for row in rows
    ] == [("Ada", True, True), ("Grace", True, True)]


def test_name_timeout_still_saves_only_confirmed_names_when_save_needs_retry(
    app_bundle, clock, monkeypatch
):
    app, sio, runtime = app_bundle
    race_id = finish_race(runtime, clock)
    runtime.apply(runtime.race.set_name_draft, race_id, 2, "Unconfirmed")
    clock.advance_ms(119_999)
    runtime.tick()
    assert runtime.race.snapshot()["phase"] == "name_entry"
    with monkeypatch.context() as patch:
        patch.setattr(runtime.store, "insert_entries", fail_save)
        clock.advance_ms(1)
        runtime.tick()
    client = sio.test_client(app)
    assert client.emit("retry_scores", retry_request(runtime), callback=True)[
        "accepted"
    ]
    client.emit("request_leaderboard", page_request(race_id=race_id))
    rows = latest_event(client, "leaderboard_snapshot")["rows"]
    assert [
        (row["name"], row["player_number"], row["race_duration_ms"]) for row in rows
    ] == [("Ada", 1, 1000)]


def test_repeated_failure_rejects_old_retry_then_can_recover_after_refresh(
    app_bundle, clock, monkeypatch
):
    app, sio, runtime = app_bundle
    failed_race(runtime, clock, monkeypatch)
    client = sio.test_client(app)
    old_token = retry_request(runtime)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.store, "insert_entries", fail_save)
        assert client.emit("retry_scores", old_token, callback=True)["accepted"]
    assert runtime.race.snapshot()["persistence_status"] == "error"
    assert runtime.race.snapshot()["remaining_seconds"] is None
    assert not client.emit("retry_scores", old_token, callback=True)["accepted"]
    client.disconnect()
    refreshed = sio.test_client(app)
    state = latest_event(refreshed, "game_state")
    assert state["persistence_status"] == "error"
    assert refreshed.emit(
        "retry_scores",
        {"race_id": state["race_id"], "browse_epoch": state["browse_epoch"]},
        callback=True,
    )["accepted"]
    assert runtime.race.snapshot()["persistence_status"] == "saved"
    assert runtime.store.snapshot_boundary("quarter-field", 4) == 2


def test_pending_retry_rejects_concurrent_retry_and_all_destructive_controls(
    app_bundle, clock, monkeypatch
):
    app, sio, runtime = app_bundle
    race_id = failed_race(runtime, clock, monkeypatch)
    clients = [sio.test_client(app), sio.test_client(app)]
    entered, release = threading.Event(), threading.Event()
    original = runtime.store.insert_entries
    writes = []

    def insert(entries):
        writes.append(entries)
        entered.set()
        assert release.wait(3)
        return original(entries)

    monkeypatch.setattr(runtime.store, "insert_entries", insert)
    token = retry_request(runtime)
    # The missing handler must fail here, not as a worker timeout.
    assert "retry_scores" in sio.server.handlers["/"]
    thread, errors = run_blocked(
        lambda: clients[0].emit("retry_scores", token), entered, release
    )
    try:
        state = latest_event(clients[1], "game_state")
        assert state["persistence_status"] == "pending"
        assert state["persistence_error"]
        assert not clients[1].emit(
            "retry_scores", retry_request(runtime), callback=True
        )["accepted"]
        clients[1].emit(
            "dismiss_leaderboard",
            {
                "race_id": race_id,
                "discard_unsaved": True,
                "browse_epoch": state["browse_epoch"],
            },
        )
        for route in ("/reset-clock", "/space-clock", "/start-clock"):
            app.test_client().get(route)
        clock.advance_ms(300_000)
        runtime.tick()
        clients[1].emit("leaderboard_activity", {"race_id": race_id})
        assert runtime.race.snapshot()["phase"] == "leaderboard"
        assert runtime.race.snapshot()["remaining_seconds"] is None
    finally:
        unblock(thread, errors, release)
    assert len(writes) == 1
    assert runtime.race.snapshot()["persistence_status"] == "saved"
    assert runtime.race.snapshot()["remaining_seconds"] == 60
    assert runtime.store.snapshot_boundary("quarter-field", 4) == 2


def test_retry_after_committed_write_exception_keeps_attempt_counts(
    app_bundle, clock, monkeypatch
):
    app, sio, runtime = app_bundle
    race_id = finish_race(runtime, clock)
    original = runtime.store.insert_entries

    def commit_then_fail(entries):
        original(entries)
        raise OSError("fixture lost completion")

    with monkeypatch.context() as patch:
        patch.setattr(runtime.store, "insert_entries", commit_then_fail)
        runtime.apply(runtime.race.submit_name, race_id, 2, "Grace")
    client = sio.test_client(app)
    assert client.emit("retry_scores", retry_request(runtime), callback=True)[
        "accepted"
    ]
    client.emit("request_leaderboard", page_request(race_id=race_id))
    rows = latest_event(client, "leaderboard_snapshot")["rows"]
    assert [
        (r["name"], r["attempts"], r["current_race"], r["personal_best"]) for r in rows
    ] == [("Ada", 1, True, True), ("Grace", 1, True, True)]
    assert runtime.store.snapshot_boundary("full-field") == 2


@pytest.mark.parametrize("discard", [None, False, 1, "true", []])
def test_failed_results_require_explicit_current_confirmation_to_leave(
    app_bundle, clock, monkeypatch, discard
):
    app, sio, runtime = app_bundle
    race_id = failed_race(runtime, clock, monkeypatch)
    client = sio.test_client(app)
    before = runtime.race.snapshot()
    client.emit(
        "dismiss_leaderboard", retry_request(runtime) | {"discard_unsaved": discard}
    )
    app.test_client().get("/reset-clock")
    assert runtime.race.snapshot() == before
    client.emit(
        "dismiss_leaderboard",
        {
            "race_id": race_id,
            "discard_unsaved": True,
            "browse_epoch": before["browse_epoch"] - 1,
        },
    )
    assert runtime.race.snapshot() == before
    token = retry_request(runtime)
    client.emit("dismiss_leaderboard", token | {"discard_unsaved": True})
    assert runtime.race.snapshot()["phase"] == "ready"
    assert not client.emit("retry_scores", token, callback=True)["accepted"]
    assert runtime.store.snapshot_boundary("quarter-field", 4) == 0
