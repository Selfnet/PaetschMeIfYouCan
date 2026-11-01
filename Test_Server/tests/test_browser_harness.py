import sqlite3

import pytest

from app import create_app
from leaderboard import LeaderboardStore
from tests.browser_harness import create_harness
from tests.test_app import CONFIG, finish_race, latest_event, run_blocked, unblock


def test_wall_time_controls_change_policy_without_advancing_race_duration(tmp_path):
    app, sio, runtime, _ = create_harness(tmp_path / "browser.db")
    http, client = app.test_client(), sio.test_client(app)
    response = http.post(
        "/__test__/wall-time", json={"now_utc": "2026-11-01T09:00:00Z"}
    )
    assert response.status_code == 200
    assert response.json == {"now_utc": "2026-11-01T09:00:00+00:00"}
    created = client.emit(
        "save_event",
        {
            "event": {
                "name": "Browser",
                "starts_at": "2026-11-01T10:00:00Z",
                "ends_at": "2026-11-01T11:00:00Z",
            },
            "revision": 0,
        },
        callback=True,
    )
    assert created["accepted"]
    assert (
        http.post("/__test__/wall-time", json={"advance_ms": 3_600_000}).status_code
        == 200
    )
    assert (
        latest_event(client, "game_state")["leaderboard_policy"]["active_event"]["id"]
        == created["event"]["id"]
    )
    http.get("/start-clock")
    http.post("/__test__/advance/1234")
    assert http.get("/__test__/wall-time").json == {
        "now_utc": "2026-11-01T10:00:00+00:00"
    }
    assert (
        http.post("/__test__/wall-time", json={"advance_ms": 3_600_000}).status_code
        == 200
    )
    state = latest_event(client, "game_state")
    assert state["race_leaderboard"]["id"] == created["event"]["id"]
    assert state["leaderboard_policy"]["next"]["slot"] == 1
    assert state["players"]["1"]["duration_ms"] == 1234
    assert runtime.race.snapshot()["phase"] == "racing"


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"now_utc": True},
        {"now_utc": "2026-11-01T10:00"},
        {"now_utc": "invalid"},
        {"advance_ms": True},
        {"advance_ms": 1.5},
        {"advance_ms": 2**63},
        {"unknown": 1},
        {"now_utc": "2026-11-01T10:00:00Z", "advance_ms": 1},
    ],
)
def test_malformed_wall_time_controls_leave_time_unchanged(tmp_path, data):
    app, _, _, _ = create_harness(tmp_path / "browser.db")
    http = app.test_client()
    assert (
        http.post(
            "/__test__/wall-time", json={"now_utc": "2026-11-01T10:00:00Z"}
        ).status_code
        == 200
    )
    assert http.post("/__test__/wall-time", json=data).status_code == 400
    assert http.get("/__test__/wall-time").json == {
        "now_utc": "2026-11-01T10:00:00+00:00"
    }


@pytest.mark.parametrize("fail", [False, True])
def test_insert_controls_hold_initial_save_then_release_or_fail_and_retry(
    tmp_path, fail
):
    app, sio, runtime, controls = create_harness(tmp_path / "browser.db")
    http, client = app.test_client(), sio.test_client(app)
    http.post("/__test__/wall-time", json={"now_utc": "2026-11-01T10:00:00Z"})
    event = client.emit(
        "save_event",
        {
            "event": {
                "name": "Browser",
                "start_now": True,
                "ends_at": "2026-11-01T11:00:00Z",
            },
            "revision": 0,
        },
        callback=True,
    )["event"]
    race_id = finish_race(runtime, runtime.race._clock_ns)
    response = http.post(
        "/__test__/inserts", json={"delay_next": True, "fail_next": fail}
    )
    assert response.status_code == 200
    thread, errors = run_blocked(
        lambda: runtime.apply(runtime.race.submit_name, race_id, 2, "Grace"),
        controls.insert_entered,
        controls.insert_release,
    )
    try:
        assert http.get("/__test__/inserts").json["waiting"] is True
        assert latest_event(client, "game_state")["persistence_status"] == "pending"
        http.post("/__test__/advance/300000")
        http.post("/__test__/wall-time", json={"advance_ms": 3_600_000})
        http.get("/reset-clock")
        assert runtime.race.snapshot()["phase"] == "leaderboard"
        assert runtime.store.snapshot_boundary("full-field", event["id"]) == 0
    finally:
        assert http.post("/__test__/inserts/release").status_code == 200
        unblock(thread, errors, controls.insert_release)
    state = latest_event(client, "game_state")
    assert state["persistence_status"] == ("error" if fail else "saved")
    if fail:
        assert client.emit(
            "retry_scores",
            {"race_id": race_id, "browse_epoch": state["browse_epoch"]},
            callback=True,
        )["accepted"]
    with sqlite3.connect(runtime.store.path) as db:
        assert db.execute(
            "SELECT player_number, name, duration_ms, leaderboard_id FROM leaderboard_entries WHERE race_id = ? ORDER BY player_number",
            (race_id,),
        ).fetchall() == [(1, "Ada", 1000, event["id"]), (2, "Grace", 1100, event["id"])]
    assert http.get("/__test__/inserts").json["waiting"] is False


@pytest.mark.parametrize(
    "data", [None, [], {"fail_next": 1}, {"delay_next": "true"}, {"unknown": True}]
)
def test_invalid_insert_flags_do_not_arm_failure(tmp_path, data):
    app, _, runtime, controls = create_harness(tmp_path / "browser.db")
    assert app.test_client().post("/__test__/inserts", json=data).status_code == 400
    race_id = finish_race(runtime, runtime.race._clock_ns)
    runtime.apply(runtime.race.submit_name, race_id, 2, "Grace")
    assert runtime.race.snapshot()["persistence_status"] == "saved"
    assert not controls.insert_entered.is_set()


def test_harness_controls_are_not_registered_in_production_app(tmp_path):
    app, _ = create_app(
        CONFIG, store=LeaderboardStore(tmp_path / "production-shaped.db")
    )
    for path in (
        "/__test__/wall-time",
        "/__test__/inserts",
        "/__test__/inserts/release",
    ):
        assert app.test_client().post(path, json={}).status_code == 404


def test_injected_insert_controls_still_work_after_storage_reload(tmp_path):
    app, sio, runtime, _ = create_harness(tmp_path / "browser.db")
    client = sio.test_client(app)
    assert client.emit("retry_leaderboard_storage", {}, callback=True)["accepted"]
    assert (
        app.test_client()
        .post("/__test__/inserts", json={"fail_next": True})
        .status_code
        == 200
    )
    race_id = finish_race(runtime, runtime.race._clock_ns)
    runtime.apply(runtime.race.submit_name, race_id, 2, "Grace")
    state = latest_event(client, "game_state")
    assert state["persistence_status"] == "error"
    assert client.emit(
        "retry_scores",
        {"race_id": race_id, "browse_epoch": state["browse_epoch"]},
        callback=True,
    )["accepted"]
    assert runtime.race.snapshot()["persistence_status"] == "saved"
