import sqlite3
import threading
from dataclasses import replace

import pytest

from app import create_app
from board import EXPECTED_IDS
from leaderboard import LeaderboardStore, NewLeaderboardEntry
from race import RaceStateMachine
from tests.test_app import CONFIG, latest_event, page_request, run_blocked, unblock


@pytest.fixture
def bundle(tmp_path, clock):
    store = LeaderboardStore(tmp_path / "slots.sqlite3")
    app, sio = create_app(CONFIG, clock_ns=clock, store=store)
    return app, sio, app.extensions["game_runtime"]


def score(race_id, slot, mode="full-field", duration=100):
    return NewLeaderboardEntry(race_id, 1, "Ada", duration, mode, slot)


def rows(store, slot, mode="full-field"):
    boundary = store.snapshot_boundary(mode, slot)
    return store.page_entries(mode, boundary, 0, leaderboard_slot=slot).rows


def test_all_slots_isolate_modes_boundaries_and_ranks(tmp_path):
    store = LeaderboardStore(tmp_path / "slots.sqlite3")
    for slot in range(1, 10):
        store.insert_entries(
            [
                score(f"race-{slot}", slot, duration=1000 + slot),
                score(f"other-{slot}", slot, "quarter-field", duration=1),
            ]
        )
    for slot in range(1, 10):
        page = rows(store, slot)
        assert [(row.race_id, row.leaderboard_slot, row.rank) for row in page] == [
            (f"race-{slot}", slot, 1)
        ]
        assert [row.race_id for row in rows(store, slot, "quarter-field")] == [
            f"other-{slot}"
        ]
    boundary = store.snapshot_boundary("full-field", 9)
    store.insert_entries([score("later", 9, duration=0)])
    assert [
        row.race_id
        for row in store.page_entries(
            "full-field", boundary, 0, leaderboard_slot=9
        ).rows
    ] == ["race-9"]


@pytest.mark.parametrize("has_mode", [False, True])
def test_migration_preserves_historical_ids_times_and_retry_identity(
    tmp_path, has_mode
):
    path = tmp_path / "legacy.sqlite3"
    mode_column = ", mode_id TEXT NOT NULL" if has_mode else ""
    mode_value = ", 'quarter-field'" if has_mode else ""
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE leaderboard_entries (id INTEGER PRIMARY KEY, "
            "race_id TEXT NOT NULL, player_number INTEGER NOT NULL, name TEXT NOT NULL, "
            "duration_ms INTEGER NOT NULL, created_at TEXT NOT NULL"
            + mode_column
            + ", UNIQUE (race_id, player_number))"
        )
        connection.execute(
            "INSERT INTO leaderboard_entries VALUES "
            "(42, 'old', 1, 'Grace', 1234, '2026-01-01T00:00:00+00:00'"
            + mode_value
            + ")"
        )
    mode = "quarter-field" if has_mode else "full-field"
    for _ in range(3):
        store = LeaderboardStore(path)
        assert store.selected_slot() == 1
        historical = rows(store, 1, mode)[0]
        assert (
            historical.id,
            historical.name,
            historical.duration_ms,
            historical.created_at,
            historical.leaderboard_slot,
        ) == (42, "Grace", 1234, "2026-01-01T00:00:00+00:00", 1)
        assert store.insert_entries([score("old", 9, mode)]) == {42}
        assert rows(store, 9, mode) == ()


@pytest.mark.parametrize("slot", [None, True, False, 0, 10, -1, 1.0, "1", [], {}])
def test_storage_rejects_invalid_slots_atomically(tmp_path, slot):
    store = LeaderboardStore(tmp_path / "slots.sqlite3")
    for operation in (
        lambda: store.set_selected_slot(slot),
        lambda: store.snapshot_boundary("full-field", slot),
        lambda: store.page_entries("full-field", 0, 0, leaderboard_slot=slot),
        lambda: store.insert_entries([score("valid", 1), score("invalid", slot)]),
    ):
        with pytest.raises(ValueError):
            operation()
    assert store.selected_slot() == 1
    assert rows(store, 1) == ()


def test_selection_broadcasts_and_restores_on_app_restart(bundle, clock):
    app, sio, runtime = bundle
    clients = [sio.test_client(app), sio.test_client(app)]
    before = runtime.race.snapshot()
    for client in clients:
        client.get_received()
    assert clients[0].emit(
        "select_leaderboard_slot", {"leaderboard_slot": 9}, callback=True
    ) == {
        "accepted": True,
        "leaderboard_slot": 9,
        "error": None,
    }
    for client in clients:
        state = latest_event(client, "game_state")
        assert state["leaderboard_slot"] == 9
        assert state["browse_epoch"] > before["browse_epoch"]
    assert runtime.store.selected_slot() == 9
    restarted, restart_sio = create_app(
        CONFIG, clock_ns=clock, store=LeaderboardStore(runtime.store.path)
    )
    assert (
        latest_event(restart_sio.test_client(restarted), "game_state")[
            "leaderboard_slot"
        ]
        == 9
    )


def test_duplicate_selection_from_two_clients_preserves_open_browse(bundle):
    app, sio, runtime = bundle
    first, second = sio.test_client(app), sio.test_client(app)
    assert first.emit(
        "select_leaderboard_slot", {"leaderboard_slot": 2}, callback=True
    )["accepted"]
    origin = runtime.race.capture_browse("full-field", None, 2)
    before = runtime.race.snapshot()
    for client in (first, second):
        client.get_received()
    assert second.emit(
        "select_leaderboard_slot", {"leaderboard_slot": 2}, callback=True
    ) == {"accepted": True, "leaderboard_slot": 2, "error": None}
    assert runtime.race.snapshot() == before
    assert runtime.race.browse_is_current(origin)[0]
    for client in (first, second):
        assert client.get_received() == []
    assert runtime.store.selected_slot() == 2
    second.emit("request_leaderboard", page_request(leaderboard_slot=2))
    assert latest_event(second, "leaderboard_snapshot")["leaderboard_slot"] == 2


@pytest.mark.parametrize(
    "data",
    [None, [], {}]
    + [
        {"leaderboard_slot": value}
        for value in [True, False, None, 0, 10, "1", 1.0, [], {}]
    ],
)
def test_malformed_selection_does_not_write_or_mutate(bundle, monkeypatch, data):
    app, sio, runtime = bundle
    client = sio.test_client(app)
    client.get_received()
    before = runtime.race.snapshot()
    monkeypatch.setattr(
        runtime.store, "set_selected_slot", lambda *_: pytest.fail("invalid write")
    )
    response = client.emit("select_leaderboard_slot", data, callback=True)
    assert response["accepted"] is False
    assert response["leaderboard_slot"] == 1
    assert response["error"]
    assert runtime.race.snapshot() == before
    assert client.get_received() == []


@pytest.mark.parametrize("phase", ["racing", "stopped", "name_entry", "leaderboard"])
def test_selection_is_ready_only(bundle, clock, monkeypatch, phase):
    app, sio, runtime = bundle
    runtime.apply(runtime.race.start)
    if phase == "stopped":
        runtime.apply(runtime.race.space)
    elif phase in ("name_entry", "leaderboard"):
        clock.advance_ms(100)
        for player in (1, 2):
            runtime.observe_board(player, EXPECTED_IDS)
        if phase == "leaderboard":
            race_id = runtime.race.snapshot()["race_id"]
            for player in (1, 2):
                runtime.apply(runtime.race.submit_name, race_id, player, "")
    assert runtime.race.snapshot()["phase"] == phase
    monkeypatch.setattr(
        runtime.store, "set_selected_slot", lambda *_: pytest.fail("busy write")
    )
    before = runtime.race.snapshot()
    client = sio.test_client(app)
    assert not client.emit(
        "select_leaderboard_slot", {"leaderboard_slot": 2}, callback=True
    )["accepted"]
    assert runtime.race.snapshot() == before


def test_failed_settings_write_keeps_memory_database_and_browse_current(bundle):
    app, sio, runtime = bundle
    client = sio.test_client(app)
    client.get_received()
    origin = runtime.race.capture_browse("full-field", None, 1)
    before = runtime.race.snapshot()
    with sqlite3.connect(runtime.store.path) as connection:
        connection.execute("""CREATE TRIGGER reject_setting BEFORE UPDATE ON leaderboard_settings
            BEGIN SELECT RAISE(ABORT, 'forced failure'); END""")
    response = client.emit(
        "select_leaderboard_slot", {"leaderboard_slot": 2}, callback=True
    )
    assert response == {
        "accepted": False,
        "leaderboard_slot": 1,
        "error": "Leaderboard slot could not be saved",
    }
    assert runtime.store.selected_slot() == 1
    assert runtime.race.snapshot() == before
    assert runtime.race.browse_is_current(origin)[0]
    assert client.get_received() == []


def test_startup_read_failure_is_recoverable_and_selection_can_retry(
    tmp_path, clock, monkeypatch
):
    store = LeaderboardStore(tmp_path / "scores.sqlite3")
    store.set_selected_slot(9)

    def fail():
        raise sqlite3.OperationalError("fixture failure")

    monkeypatch.setattr(store, "selected_slot", fail)
    app, sio = create_app(CONFIG, clock_ns=clock, store=store)
    client = sio.test_client(app)
    assert latest_event(client, "game_state")["leaderboard_slot"] == 1
    assert client.emit(
        "select_leaderboard_slot", {"leaderboard_slot": 3}, callback=True
    )["accepted"]
    assert LeaderboardStore(store.path).selected_slot() == 3


def test_unavailable_store_selection_is_rejected_without_stopping_game(
    tmp_path, monkeypatch
):
    blocked = tmp_path / "blocked"
    blocked.write_text("blocked")
    monkeypatch.setenv("LEADERBOARD_DB", str(blocked / "scores.sqlite3"))
    app, sio = create_app(CONFIG)
    client = sio.test_client(app)
    assert latest_event(client, "game_state")["leaderboard_slot"] == 1
    assert not client.emit(
        "select_leaderboard_slot", {"leaderboard_slot": 2}, callback=True
    )["accepted"]
    app.test_client().get("/start-clock")
    assert latest_event(client, "game_state")["phase"] == "racing"


def test_race_capture_and_persistence_identity_include_slot(clock):
    race = RaceStateMachine(clock_ns=clock, leaderboard_slot=8)
    race.start()
    assert not race.select_leaderboard_slot(9).changed
    clock.advance_ms(100)
    for player in (1, 2):
        race.observe_board(player, EXPECTED_IDS)
    race_id = race.snapshot()["race_id"]
    race.submit_name(race_id, 1, "Ada")
    change = race.submit_name(race_id, 2, "Grace")
    assert {item.leaderboard_slot for item in change.submissions} == {8}
    assert change.origin.leaderboard_slot == 8
    assert not race.set_persistence(
        replace(change.origin, leaderboard_slot=9), None
    ).changed
    assert race.set_persistence(change.origin, None).changed
    assert race.capture_browse("full-field", race_id, 9) is None
    assert race.capture_browse("full-field", race_id, 8).leaderboard_slot == 8
    race.reset()
    race.select_leaderboard_slot(9)
    assert not race.set_persistence(change.origin, None).changed
    assert {item.leaderboard_slot for item in change.submissions} == {8}


def test_blocked_score_write_saves_captured_slot_after_reset_and_switch(
    bundle, clock, monkeypatch
):
    _, _, runtime = bundle
    assert runtime.select_leaderboard_slot(8)["accepted"]
    runtime.apply(runtime.race.start)
    clock.advance_ms(100)
    for player in (1, 2):
        runtime.observe_board(player, EXPECTED_IDS)
    race_id = runtime.race.snapshot()["race_id"]
    runtime.apply(runtime.race.submit_name, race_id, 1, "Ada")
    entered, release = threading.Event(), threading.Event()
    original = runtime.store.insert_entries

    def insert(entries):
        entered.set()
        assert release.wait(3)
        return original(entries)

    monkeypatch.setattr(runtime.store, "insert_entries", insert)
    thread, errors = run_blocked(
        lambda: runtime.apply(runtime.race.submit_name, race_id, 2, "Grace"),
        entered,
        release,
    )
    try:
        runtime.apply(runtime.race.reset)
        assert runtime.select_leaderboard_slot(9)["accepted"]
    finally:
        unblock(thread, errors, release)
    assert [row.race_id for row in rows(runtime.store, 8)] == [race_id, race_id]
    assert rows(runtime.store, 9) == ()
    assert runtime.race.snapshot()["persistence_status"] == "idle"


@pytest.mark.parametrize("stage", ["snapshot_boundary", "page_entries"])
def test_slot_switch_discards_inflight_page(bundle, monkeypatch, stage):
    app, sio, runtime = bundle
    client = sio.test_client(app)
    client.get_received()
    entered, release = threading.Event(), threading.Event()
    original = getattr(runtime.store, stage)

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(runtime.store, stage, delayed)
    thread, errors = run_blocked(
        lambda: client.emit("request_leaderboard", page_request(leaderboard_slot=1)),
        entered,
        release,
    )
    try:
        assert runtime.select_leaderboard_slot(2)["accepted"]
    finally:
        unblock(thread, errors, release)
    assert not [
        event
        for event in client.get_received()
        if event["name"] == "leaderboard_snapshot"
    ]


def test_pages_enforce_selected_slot_and_echo_identity(bundle):
    app, sio, runtime = bundle
    runtime.store.insert_entries([score("one", 1), score("two", 2)])
    client = sio.test_client(app)
    client.get_received()
    client.emit("request_leaderboard", page_request())
    opening = latest_event(client, "leaderboard_snapshot")
    assert opening["leaderboard_slot"] == 1
    assert [row["race_id"] for row in opening["rows"]] == ["one"]
    runtime.select_leaderboard_slot(2)
    client.get_received()
    for request in (
        page_request(request_id=2),
        page_request(request_id=2, leaderboard_slot=1),
        page_request(boundary=opening["boundary"], offset=50, leaderboard_slot=2),
    ):
        client.emit("request_leaderboard", request)
        assert client.get_received() == []
    client.emit("request_leaderboard", page_request(request_id=2, leaderboard_slot=2))
    page = latest_event(client, "leaderboard_snapshot")
    assert page["leaderboard_slot"] == 2
    assert [(row["race_id"], row["leaderboard_slot"]) for row in page["rows"]] == [
        ("two", 2)
    ]


@pytest.mark.parametrize("slot", [None, True, False, 0, 10, 1.0, "1", [], {}])
def test_malformed_page_slot_does_not_read(bundle, monkeypatch, slot):
    app, sio, runtime = bundle
    monkeypatch.setattr(
        runtime.store, "snapshot_boundary", lambda *_: pytest.fail("invalid read")
    )
    client = sio.test_client(app)
    client.get_received()
    client.emit("request_leaderboard", page_request(leaderboard_slot=slot))
    assert client.get_received() == []


def test_settings_write_and_state_publication_are_serialized_against_start(
    bundle, monkeypatch
):
    app, sio, runtime = bundle
    client = sio.test_client(app)
    client.get_received()
    entered, release, attempted = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original = runtime.store.set_selected_slot

    def delayed(slot):
        entered.set()
        assert release.wait(3)
        original(slot)

    monkeypatch.setattr(runtime.store, "set_selected_slot", delayed)
    thread, errors = run_blocked(
        lambda: runtime.select_leaderboard_slot(9), entered, release
    )

    def start():
        attempted.set()
        runtime.apply(runtime.race.start)

    starter = threading.Thread(target=start)
    starter.start()
    try:
        assert attempted.wait(3)
        assert runtime.race.snapshot()["phase"] == "ready"
        assert runtime.race.snapshot()["leaderboard_slot"] == 1
    finally:
        unblock(thread, errors, release)
        starter.join(3)
    assert not starter.is_alive()
    assert runtime.race.race_leaderboard_slot == 9
    states = [
        event["args"][0]
        for event in client.get_received()
        if event["name"] == "game_state"
    ]
    assert [(state["phase"], state["leaderboard_slot"]) for state in states] == [
        ("ready", 9),
        ("racing", 9),
    ]
