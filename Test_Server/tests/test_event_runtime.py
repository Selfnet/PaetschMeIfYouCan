import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from app import create_app
from board import EXPECTED_IDS
from leaderboard import LeaderboardStore, NewLeaderboardEntry
from tests.test_app import CONFIG, latest_event, page_request, run_blocked, unblock
from tests.test_event_storage import event_data


class WallClock:
    def __init__(self):
        self.value = datetime(2026, 11, 1, 9, tzinfo=UTC)

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = datetime.fromisoformat(value)


@pytest.fixture
def bundle(tmp_path, clock):
    wall = WallClock()
    store = LeaderboardStore(tmp_path / "events.sqlite3", now=wall)
    app, sio = create_app(CONFIG, clock_ns=clock, now_utc=wall, store=store)
    return app, sio, app.extensions["game_runtime"], wall


def save(client, **changes):
    catalog = client.emit("list_events", {"past_only": False}, callback=True)
    result = client.emit(
        "save_event",
        {"event": event_data(**changes), "revision": catalog["revision"]},
        callback=True,
    )
    assert result["accepted"], result
    return result


def browse(client, board_id, **changes):
    client.emit("request_leaderboard", page_request(leaderboard_id=board_id, **changes))
    return latest_event(client, "leaderboard_snapshot")


@pytest.mark.parametrize("route", ["/start-clock", "/space-clock"])
@pytest.mark.parametrize(
    "time,want",
    [
        ("09:59:59.999999", 4),
        ("10:00:00", 10),
        ("11:59:59.999999", 10),
        ("12:00:00", 1),
        ("12:00:00.000001", 1),
    ],
)
def test_every_start_resolves_at_boundary_without_waiting_for_tick(
    bundle, route, time, want
):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    assert runtime.select_leaderboard_slot(4)["accepted"]
    save(client)
    runtime.tick()
    wall.set(f"2026-11-01T{time}+00:00")
    app.test_client().get(route)
    state = latest_event(client, "game_state")
    assert state["phase"] == "racing"
    assert state["race_leaderboard"]["id"] == want
    assert state["leaderboard_policy"]["next"]["id"] == want
    assert "leaderboard_slot" not in state


def test_boundary_tick_and_connection_publish_policy_without_invalidating_view(bundle):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    save(client)
    page = browse(client, 2)
    epoch = runtime.race.snapshot()["browse_epoch"]
    wall.set("2026-11-01T10:00:00+00:00")
    runtime.tick()
    state = latest_event(client, "game_state")
    assert state["leaderboard_policy"]["active_event"]["id"] == 10
    assert state["browse_epoch"] == epoch
    assert (
        browse(client, 2, boundary=page["boundary"], offset=50)["leaderboard_id"] == 2
    )
    wall.set("2026-11-01T12:00:00+00:00")
    reconnected = latest_event(sio.test_client(app), "game_state")
    assert reconnected["leaderboard_policy"]["next"]["slot"] == 1
    assert latest_event(client, "game_state")["leaderboard_policy"]["next"]["slot"] == 1


def test_locked_old_client_selection_cannot_change_routing_and_browsing_is_local(
    bundle,
):
    app, sio, runtime, wall = bundle
    first, second = sio.test_client(app), sio.test_client(app)
    event = save(first)["event"]
    wall.set("2026-11-01T10:01:00+00:00")
    before = runtime.store.load_policy()
    rejected = second.emit(
        "select_leaderboard_slot", {"leaderboard_slot": 9}, callback=True
    )
    assert rejected["accepted"] is False and rejected["error"]
    assert runtime.store.load_policy() == before
    first.get_received()
    second.get_received()
    assert browse(first, 9)["leaderboard"]["slot"] == 9
    assert second.get_received() == []
    assert browse(second, event["id"])["leaderboard"]["name"] == "Party"
    assert first.get_received() == []
    app.test_client().get("/space-clock")
    assert latest_event(first, "game_state")["race_leaderboard"]["id"] == event["id"]


@pytest.mark.parametrize("start,want", [("09:59:59", 4), ("10:01:00", 10)])
@pytest.mark.parametrize("mode", ["full-field", "half-field", "quarter-field"])
def test_names_after_expiry_save_both_original_identities(
    bundle, clock, start, want, mode
):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    runtime.select_leaderboard_slot(4)
    runtime.apply(runtime.race.select_mode, mode)
    save(client)
    wall.set(f"2026-11-01T{start}+00:00")
    app.test_client().get("/start-clock")
    for player in (1, 2):
        clock.advance_ms(1000)
        runtime.observe_board(player, EXPECTED_IDS)
    race_id = runtime.race.snapshot()["race_id"]
    wall.set("2026-11-01T12:01:00+00:00")
    for player, name in ((1, "Ada"), (2, "Grace")):
        client.emit(
            "submit_name",
            {
                "race_id": race_id,
                "player_number": player,
                "draft": name,
                "leaderboard_id": 9,
            },
        )
    state = latest_event(client, "game_state")
    assert state["race_leaderboard"]["id"] == want
    assert state["leaderboard_policy"]["next"]["slot"] == 1
    with sqlite3.connect(runtime.store.path) as db:
        assert db.execute(
            "SELECT player_number, name, duration_ms, mode_id, leaderboard_id FROM leaderboard_entries WHERE race_id = ? ORDER BY player_number",
            (race_id,),
        ).fetchall() == [(1, "Ada", 1000, mode, want), (2, "Grace", 2000, mode, want)]
    page = browse(client, want, race_id=race_id, mode_id=mode)
    assert all(row["current_race"] for row in page["rows"])
    assert page["target_score_id"] is not None
    archive = browse(client, 1, race_id=race_id, mode_id=mode, request_id=2)
    assert archive["target_score_id"] is None
    assert runtime.race.snapshot()["phase"] == "leaderboard"


@pytest.mark.parametrize("end_action", ["expiry", "end_now"])
def test_slow_initial_failure_retains_recovery_despite_navigation_and_reset(
    bundle, clock, monkeypatch, end_action
):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    saved = save(client)
    wall.set("2026-11-01T10:01:00+00:00")
    app.test_client().get("/start-clock")
    for player in (1, 2):
        clock.advance_ms(1000)
        runtime.observe_board(player, EXPECTED_IDS)
    race_id = runtime.race.snapshot()["race_id"]
    runtime.apply(runtime.race.submit_name, race_id, 1, "Ada")
    entered, release = threading.Event(), threading.Event()

    def delayed_failure(entries):
        entered.set()
        assert release.wait(3)
        raise OSError("fixture initial failure")

    with monkeypatch.context() as patch:
        patch.setattr(runtime.store, "insert_entries", delayed_failure)
        thread, errors = run_blocked(
            lambda: runtime.apply(runtime.race.submit_name, race_id, 2, "Grace"),
            entered,
            release,
        )
        try:
            state = latest_event(client, "game_state")
            assert state["persistence_status"] == "pending"
            for route in ("/reset-clock", "/space-clock", "/start-clock"):
                app.test_client().get(route)
            assert not client.emit(
                "dismiss_leaderboard",
                {
                    "race_id": race_id,
                    "discard_unsaved": True,
                    "browse_epoch": state["browse_epoch"],
                },
                callback=True,
            )["accepted"]
            assert browse(client, 9, race_id=race_id)["leaderboard_id"] == 9
            if end_action == "end_now":
                result = client.emit(
                    "end_event",
                    {"id": saved["event"]["id"], "revision": saved["revision"]},
                    callback=True,
                )
                assert result["accepted"]
            else:
                wall.set("2026-11-01T12:00:00+00:00")
            clock.advance_ms(300_000)
            runtime.tick()
            assert runtime.race.snapshot()["phase"] == "leaderboard"
        finally:
            unblock(thread, errors, release)
    state = latest_event(client, "game_state")
    assert state["persistence_status"] == "error" and state["remaining_seconds"] is None
    assert client.emit(
        "retry_scores",
        {"race_id": race_id, "browse_epoch": state["browse_epoch"]},
        callback=True,
    )["accepted"]
    with sqlite3.connect(runtime.store.path) as db:
        assert db.execute(
            "SELECT player_number, name, duration_ms, leaderboard_id FROM leaderboard_entries ORDER BY player_number"
        ).fetchall() == [(1, "Ada", 1000, 10), (2, "Grace", 2000, 10)]
    assert runtime.race.snapshot()["remaining_seconds"] == 60


def test_unknown_startup_storage_blocks_then_repairs_in_process(
    tmp_path, clock, monkeypatch
):
    blocked = tmp_path / "blocked"
    blocked.write_text("blocked")
    monkeypatch.setenv("LEADERBOARD_DB", str(blocked / "events.sqlite3"))
    wall = WallClock()
    app, sio = create_app(CONFIG, clock_ns=clock, now_utc=wall)
    runtime = app.extensions["game_runtime"]
    client = sio.test_client(app)
    state = latest_event(client, "game_state")
    assert state["leaderboard_policy"] == {
        "available": False,
        "error": "Leaderboard routing is unavailable; retry storage from control",
        "revision": None,
        "next": None,
        "active_event": None,
        "numbered": [],
    }
    for route in ("/start-clock", "/space-clock"):
        assert app.test_client().get(route).status_code == 503
        assert runtime.race.snapshot()["phase"] == "ready"
    assert not client.emit("retry_leaderboard_storage", {}, callback=True)["accepted"]
    blocked.unlink()
    blocked.mkdir()
    assert client.emit("retry_leaderboard_storage", {}, callback=True)["accepted"]
    assert latest_event(client, "game_state")["leaderboard_policy"]["available"]
    app.test_client().get("/start-clock")
    assert runtime.race.snapshot()["phase"] == "racing"


def test_reload_during_race_preserves_origin_maintenance_and_pace(bundle, clock):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    saved = save(client)
    runtime.store.insert_entries(
        [
            NewLeaderboardEntry(
                "prior", 1, "Ada", 1500, "full-field", saved["event"]["id"]
            )
        ]
    )
    wall.set("2026-11-01T10:01:00+00:00")
    app.test_client().get("/start-clock")
    client.emit("set_maintenance", {"enabled": True})
    before = latest_event(client, "game_state")
    clock.advance_ms(1000)
    wall.set("2026-11-01T12:01:00+00:00")
    assert client.emit("retry_leaderboard_storage", {}, callback=True)["accepted"]
    state = latest_event(client, "game_state")
    assert state["race_id"] == before["race_id"]
    assert state["phase"] == "racing" and state["maintenance_requested"] is True
    assert state["race_leaderboard"]["id"] == 10
    assert state["pace_reference_ms"] == 1500
    assert state["leaderboard_policy"]["next"]["slot"] == 1


def test_restart_after_entire_event_then_same_manual_slot_is_fresh(bundle, clock):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    runtime.select_leaderboard_slot(4)
    save(client)
    wall.set("2026-11-02T00:00:00+00:00")
    app, sio = create_app(
        CONFIG, clock_ns=clock, now_utc=wall, store=LeaderboardStore(runtime.store.path)
    )
    client = sio.test_client(app)
    assert latest_event(client, "game_state")["leaderboard_policy"]["next"]["slot"] == 1
    assert client.emit(
        "select_leaderboard_slot", {"leaderboard_slot": 4}, callback=True
    )["accepted"]
    app, sio = create_app(
        CONFIG, now_utc=wall, store=LeaderboardStore(runtime.store.path)
    )
    assert (
        latest_event(sio.test_client(app), "game_state")["leaderboard_policy"]["next"][
            "slot"
        ]
        == 4
    )


def test_operator_change_and_start_are_serialized_and_stale_edit_rejected(
    bundle, monkeypatch
):
    app, sio, runtime, _ = bundle
    client = sio.test_client(app)
    entered, release, attempted = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original = runtime.store.save_event

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(runtime.store, "save_event", delayed)
    replies = []
    payload = {"event": event_data(start_now=True), "revision": 0}
    thread, errors = run_blocked(
        lambda: replies.append(client.emit("save_event", payload, callback=True)),
        entered,
        release,
    )

    def start():
        attempted.set()
        app.test_client().get("/space-clock")

    starter = threading.Thread(target=start)
    starter.start()
    try:
        assert attempted.wait(3)
        assert runtime.race.snapshot()["phase"] == "ready"
    finally:
        unblock(thread, errors, release)
        starter.join(3)
    assert not starter.is_alive() and replies[0]["accepted"]
    assert (
        latest_event(client, "game_state")["race_leaderboard"]["id"]
        == replies[0]["event"]["id"]
    )
    stale = client.emit("save_event", payload, callback=True)
    assert not stale["accepted"] and stale["error"]
    assert (
        len(client.emit("list_events", {"past_only": False}, callback=True)["events"])
        == 1
    )


def test_event_list_order_rename_and_failed_change_leave_open_pages_valid(bundle):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    first = save(client)["event"]
    second = save(
        client, starts_at="2026-11-01T12:00:00Z", ends_at="2026-11-01T13:00:00Z"
    )["event"]
    save(client, starts_at="2026-11-03T10:00:00Z", ends_at="2026-11-03T12:00:00Z")
    wall.set("2026-11-02T00:00:00+00:00")
    listing = client.emit("list_events", {"past_only": True}, callback=True)
    assert [event["id"] for event in listing["events"]] == [second["id"], first["id"]]
    page = browse(client, first["id"])
    changed = save(client, id=first["id"], name="Renamed")
    assert latest_event(client, "leaderboard_changed") == {
        "leaderboard": changed["event"],
        "revision": changed["revision"],
    }
    assert (
        browse(client, first["id"], boundary=page["boundary"], offset=50)[
            "leaderboard"
        ]["name"]
        == "Renamed"
    )
    before = runtime.store.load_policy()
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "CREATE TRIGGER reject_event BEFORE UPDATE ON leaderboards BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
        )
    rejected = client.emit(
        "save_event",
        {
            "event": event_data(id=first["id"], name="Failed"),
            "revision": before.revision,
        },
        callback=True,
    )
    assert not rejected["accepted"]
    assert runtime.store.load_policy() == before
    client.get_received()
    runtime.emit_state()
    assert latest_event(client, "game_state")["leaderboard_policy"]["available"]


@pytest.mark.parametrize(
    "command,data",
    [
        ("list_events", {"past_only": 1}),
        ("list_events", []),
        ("list_events", {}),
        ("save_event", None),
        ("save_event", {"event": event_data(), "revision": True}),
        ("cancel_event", {"id": True, "revision": 0}),
        ("end_event", {"id": 1, "revision": 0}),
        ("retry_leaderboard_storage", []),
    ],
)
def test_malformed_controls_leave_policy_unchanged(bundle, command, data):
    app, sio, runtime, _ = bundle
    client = sio.test_client(app)
    before = runtime.store.load_policy()
    result = client.emit(command, data, callback=True)
    assert result["accepted"] is False and result["error"]
    assert runtime.store.load_policy() == before


def test_failed_storage_reload_retains_known_schedule_and_failed_results(
    bundle, clock, monkeypatch
):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    save(client)
    wall.set("2026-11-01T10:01:00+00:00")
    app.test_client().get("/start-clock")
    for player in (1, 2):
        clock.advance_ms(1000)
        runtime.observe_board(player, EXPECTED_IDS)
    race_id = runtime.race.snapshot()["race_id"]
    client.emit("set_maintenance", {"enabled": True})
    runtime.apply(runtime.race.submit_name, race_id, 1, "Ada")
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "CREATE TRIGGER reject_score BEFORE INSERT ON leaderboard_entries BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
        )
    runtime.apply(runtime.race.submit_name, race_id, 2, "Grace")
    before = runtime.race.snapshot()
    wall.set("2026-11-01T12:01:00+00:00")
    with monkeypatch.context() as patch:
        patch.setattr(
            runtime, "database_path", runtime.store.path / "unavailable.sqlite3"
        )
        assert not client.emit("retry_leaderboard_storage", {}, callback=True)[
            "accepted"
        ]
    runtime.emit_state()
    state = latest_event(client, "game_state")
    assert state["race_leaderboard"]["id"] == 10
    assert state["leaderboard_policy"]["available"] is True
    assert state["leaderboard_policy"]["next"]["slot"] == 1
    assert (
        state["persistence_status"] == "error"
        and state["maintenance_requested"] is True
    )
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("DROP TRIGGER reject_score")
    assert client.emit("retry_leaderboard_storage", {}, callback=True)["accepted"]
    assert runtime.race.snapshot()["browse_epoch"] == before["browse_epoch"]
    assert client.emit(
        "retry_scores",
        {"race_id": race_id, "browse_epoch": before["browse_epoch"]},
        callback=True,
    )["accepted"]
    assert runtime.store.snapshot_boundary("full-field", 10) == 2


def test_late_archive_page_cannot_replace_new_board_or_highlight_same_name(
    bundle, clock, monkeypatch
):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    event = save(client)["event"]
    runtime.store.insert_entries(
        [NewLeaderboardEntry("archive", 1, "Ada", 1, "full-field", 2)]
    )
    wall.set("2026-11-01T10:01:00+00:00")
    app.test_client().get("/start-clock")
    for player in (1, 2):
        clock.advance_ms(1000)
        runtime.observe_board(player, EXPECTED_IDS)
    race_id = runtime.race.snapshot()["race_id"]
    for player, name in ((1, "Ada"), (2, "Grace")):
        runtime.apply(runtime.race.submit_name, race_id, player, name)
    entered, release = threading.Event(), threading.Event()
    original = runtime.store.page_entries

    def delayed(mode, boundary, offset, limit, board_id, result_race_id):
        page = original(mode, boundary, offset, limit, board_id, result_race_id)
        if board_id == event["id"]:
            entered.set()
            assert release.wait(3)
        return page

    with monkeypatch.context() as patch:
        patch.setattr(runtime.store, "page_entries", delayed)
        thread, errors = run_blocked(
            lambda: client.emit(
                "request_leaderboard",
                page_request(leaderboard_id=event["id"], race_id=race_id),
            ),
            entered,
            release,
        )
        try:
            page = browse(client, 2, race_id=race_id, request_id=2)
            assert page["rows"][0]["current_race"] is False
            assert page["rows"][0]["personal_best"] is False
            assert page["rows"][0]["race_duration_ms"] is None
            assert page["target_score_id"] is None
        finally:
            unblock(thread, errors, release)
    assert client.get_received() == []
    assert runtime.race.snapshot()["phase"] == "leaderboard"


def test_clock_correction_cannot_cancel_a_captured_event_before_scores_exist(
    bundle, clock
):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    saved = save(client)
    wall.set("2026-11-01T10:01:00+00:00")
    app.test_client().get("/start-clock")
    wall.set("2026-11-01T09:01:00+00:00")
    result = client.emit(
        "cancel_event",
        {"id": saved["event"]["id"], "revision": saved["revision"]},
        callback=True,
    )
    assert result["accepted"] is False and result["error"]
    for player in (1, 2):
        clock.advance_ms(1000)
        runtime.observe_board(player, EXPECTED_IDS)
    race_id = runtime.race.snapshot()["race_id"]
    for player, name in ((1, "Ada"), (2, "Grace")):
        runtime.apply(runtime.race.submit_name, race_id, player, name)
    assert runtime.race.snapshot()["persistence_status"] == "saved"
    assert runtime.store.snapshot_boundary("full-field", saved["event"]["id"]) == 2


@pytest.mark.parametrize("selection", [1, 4])
@pytest.mark.parametrize("publish_expiry", [False, True])
def test_manual_selection_broadcasts_committed_state_then_choice_to_both_clients(
    bundle, selection, publish_expiry
):
    app, sio, runtime, wall = bundle
    first, second = sio.test_client(app), sio.test_client(app)
    save(first)
    wall.set("2026-11-01T10:01:00+00:00")
    runtime.tick()
    browse(first, 3)
    wall.set("2026-11-01T12:00:00+00:00")
    if publish_expiry:
        runtime.tick()
    for client in (first, second):
        client.get_received()
    before_revision = runtime.store.load_policy().revision
    ack = second.emit(
        "select_leaderboard_slot", {"leaderboard_slot": selection}, callback=True
    )
    assert ack["accepted"]
    assert runtime.store.load_policy().manual_slot == selection
    for client in (first, second):
        packets = [
            packet
            for packet in client.get_received()
            if packet["name"] in {"game_state", "leaderboard_selected"}
        ]
        assert [packet["name"] for packet in packets] == [
            "game_state",
            "leaderboard_selected",
        ]
        policy = packets[0]["args"][0]["leaderboard_policy"]
        assert policy["revision"] == before_revision + 1
        assert packets[1]["args"][0] == {
            "leaderboard": policy["next"],
            "revision": policy["revision"],
        }
        assert policy["next"]["slot"] == selection


def test_nonmanual_policy_changes_and_rejected_selections_never_broadcast_choice(
    bundle,
):
    app, sio, runtime, wall = bundle
    first, second = sio.test_client(app), sio.test_client(app)
    event = save(first)["event"]
    save(first, id=event["id"], name="Renamed")
    wall.set("2026-11-01T10:00:00+00:00")
    runtime.tick()
    assert not second.emit(
        "select_leaderboard_slot", {"leaderboard_slot": 3}, callback=True
    )["accepted"]
    wall.set("2026-11-01T12:00:00+00:00")
    runtime.tick()
    with sqlite3.connect(runtime.store.path) as db:
        db.execute(
            "CREATE TRIGGER reject_choice BEFORE UPDATE ON leaderboard_settings BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
        )
    before = runtime.store.load_policy()
    assert not second.emit(
        "select_leaderboard_slot", {"leaderboard_slot": 1}, callback=True
    )["accepted"]
    assert runtime.store.load_policy() == before
    for client in (first, second):
        packets = client.get_received()
        assert any(packet["name"] == "game_state" for packet in packets)
        assert any(packet["name"] == "leaderboard_changed" for packet in packets)
        assert not any(packet["name"] == "leaderboard_selected" for packet in packets)


def test_state_server_time_and_policy_use_one_wall_clock_sample(bundle, monkeypatch):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    save(client)
    client.get_received()
    calls = []

    def crossing_boundary():
        wall.set(
            "2026-11-01T09:59:59.999999+00:00"
            if not calls
            else "2026-11-01T10:00:00+00:00"
        )
        calls.append(wall())
        return wall()

    monkeypatch.setattr(runtime, "now_utc", crossing_boundary)
    runtime.emit_state()
    state = latest_event(client, "game_state")
    assert state["server_now"] == "2026-11-01T09:59:59.999999+00:00"
    assert state["leaderboard_policy"]["active_event"] is None
    assert len(calls) == 1


def test_server_time_advances_without_policy_or_state_revision_churn(bundle):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    initial = latest_event(client, "game_state")
    wall.set("2026-11-01T09:00:01+00:00")
    runtime.emit_state()
    later = latest_event(client, "game_state")
    assert later["server_now"] == "2026-11-01T09:00:01+00:00"
    assert later["leaderboard_policy"] == initial["leaderboard_policy"]
    assert later["revision"] == initial["revision"]
    assert later["browse_epoch"] == initial["browse_epoch"]


@pytest.mark.parametrize(
    "now,active",
    [("10:00:00", True), ("12:00:00", False), ("11:59:59", True)],
)
def test_server_time_tracks_boundaries_and_backward_corrections(bundle, now, active):
    app, sio, runtime, wall = bundle
    client = sio.test_client(app)
    save(client)
    wall.set("2026-11-01T12:01:00+00:00")
    runtime.tick()
    wall.set(f"2026-11-01T{now}+00:00")
    runtime.emit_state()
    state = latest_event(client, "game_state")
    assert state["server_now"] == f"2026-11-01T{now}+00:00"
    assert bool(state["leaderboard_policy"]["active_event"]) is active
