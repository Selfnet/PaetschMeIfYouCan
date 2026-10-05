import json
import sqlite3
import threading

import pytest

from app import create_app
from board import EXPECTED_IDS
from leaderboard import LeaderboardStore, NewLeaderboardEntry

CONFIG = {
    "TESTING": True,
    "START_SERIAL_ON_CONNECT": False,
    "START_BACKGROUND_TASKS": False,
}


@pytest.fixture
def app_bundle(tmp_path, clock):
    app, socketio = create_app(
        CONFIG, clock_ns=clock, store=LeaderboardStore(tmp_path / "scores.db")
    )
    return app, socketio, app.extensions["game_runtime"]


def latest_event(client, name):
    events = [
        event["args"][0] for event in client.get_received() if event["name"] == name
    ]
    assert events
    return events[-1]


def page_request(**kwargs):
    return {
        "request_id": 1,
        "mode_id": "full-field",
        "race_id": None,
        "boundary": None,
        "offset": 0,
    } | kwargs


def finish_race(runtime, clock):
    runtime.handle_change(runtime.race.start())
    clock.advance_ms(1000)
    runtime.observe_board(1, EXPECTED_IDS)
    clock.advance_ms(100)
    runtime.observe_board(2, EXPECTED_IDS)
    race_id = runtime.race.snapshot()["race_id"]
    runtime.handle_change(runtime.race.submit_name(race_id, 1, "Ada"))
    return race_id


def test_mode_selection_broadcast_and_ack(app_bundle):
    app, sio, runtime = app_bundle
    clients = [sio.test_client(app), sio.test_client(app)]
    for client in clients:
        client.get_received()
    assert clients[0].emit(
        "select_mode", {"mode_id": "quarter-field"}, callback=True
    ) == {"accepted": True, "mode_id": "quarter-field", "error": None}
    for client in clients:
        state = latest_event(client, "game_state")
        assert state["mode"]["id"] == "quarter-field"
        assert state["revision"] > 0
    assert clients[0].emit("select_mode", {"mode_id": "quarter-field"}, callback=True)[
        "accepted"
    ]
    runtime.handle_change(runtime.race.start())
    assert not clients[0].emit("select_mode", {"mode_id": "half-field"}, callback=True)[
        "accepted"
    ]


@pytest.mark.parametrize(
    "data", [None, [], {}, {"mode_id": []}, {"mode_id": "unknown"}]
)
def test_malformed_selection_rejected(app_bundle, data):
    app, sio, runtime = app_bundle
    client = sio.test_client(app)
    before = runtime.race.snapshot()
    assert not client.emit("select_mode", data, callback=True)["accepted"]
    assert runtime.race.snapshot() == before


@pytest.mark.parametrize("action", ["confirm", "reset"])
def test_same_origin_invalidation_broadcasts_epoch_to_both_views(app_bundle, action):
    app, sio, runtime = app_bundle
    viewer, controller = sio.test_client(app), sio.test_client(app)
    before = latest_event(viewer, "game_state")
    controller.get_received()
    viewer.emit("request_leaderboard", page_request())
    first = latest_event(viewer, "leaderboard_snapshot")
    if action == "confirm":
        assert controller.emit("select_mode", {"mode_id": "full-field"}, callback=True)[
            "accepted"
        ]
    else:
        runtime.apply(runtime.race.reset)
    for client in (viewer, controller):
        state = latest_event(client, "game_state")
        assert state["browse_epoch"] > before["browse_epoch"]
        assert (state["phase"], state["mode"], state["race_id"]) == (
            before["phase"],
            before["mode"],
            before["race_id"],
        )
    viewer.emit(
        "request_leaderboard",
        page_request(boundary=first["boundary"], offset=50),
    )
    assert not viewer.get_received()
    viewer.emit("request_leaderboard", page_request(request_id=2))
    replacement = latest_event(viewer, "leaderboard_snapshot")
    assert replacement["request_id"] == 2
    assert replacement["offset"] == 0


def test_reconnect_has_cached_presentation(app_bundle):
    app, sio, runtime = app_bundle
    runtime.observe_board(1, EXPECTED_IDS)
    state = latest_event(sio.test_client(app), "game_state")
    assert state["players"]["1"]["presentation"]["complete"]
    assert runtime.serial_threads is None


def test_overview_exposes_mode_objective_and_recoverable_error(app_bundle):
    app, _, _ = app_bundle
    html = app.test_client().get("/").get_data(as_text=True)
    for element_id in (
        "modeButton",
        "currentMode",
        "objective1",
        "objective2",
        "modeError",
    ):
        assert f'id="{element_id}"' in html
    assert 'id="modeError" class="persistence-error" role="alert"' in html


def test_mode_selector_has_native_modal_and_visible_close(app_bundle):
    app, _, _ = app_bundle
    html = app.test_client().get("/").get_data(as_text=True)
    assert '<dialog id="modeDialog" aria-labelledby="modeDialogTitle">' in html
    assert 'id="modeOptions"' in html
    assert 'id="closeModeDialog"' in html
    assert 'id="modeSelectionError" role="status"' in html


def test_leaderboard_has_labeled_scroll_and_page_controls(app_bundle):
    app, _, _ = app_bundle
    html = app.test_client().get("/").get_data(as_text=True)
    assert 'tabindex="-1">Leaderboard</h2>' in html
    assert 'id="closeLeaderboard"' in html
    assert (
        'class="leaderboard-scroll" role="region" aria-label="Leaderboard scores"'
        in html
    )
    assert 'id="loadLeaderboardPage"' in html
    assert 'id="leaderboardPageError"' in html


def test_snapshots_can_arrive_newer_then_older_without_losing_revision(app_bundle):
    app, sio, runtime = app_bundle
    client = sio.test_client(app)
    older = runtime.race.snapshot()
    runtime.apply(runtime.race.select_mode, "quarter-field")
    newer = runtime.race.snapshot()
    client.get_received()
    sio.emit("game_state", newer)
    sio.emit("game_state", older)
    states = [
        event["args"][0]
        for event in client.get_received()
        if event["name"] == "game_state"
    ]
    assert [state["mode"] for state in states] == [newer["mode"], older["mode"]]
    assert states[0]["revision"] > states[1]["revision"]


@pytest.mark.parametrize("stopped", [False, True])
def test_open_and_subsequent_pages_are_targeted_and_boundary_fixed(app_bundle, stopped):
    app, sio, runtime = app_bundle
    runtime.store.insert_entries(
        [
            NewLeaderboardEntry(f"old-{i}", 1, f"Name{i}", i, "full-field")
            for i in range(121)
        ]
    )
    if stopped:
        runtime.handle_change(runtime.race.start())
        runtime.handle_change(runtime.race.space())
    requester, observer = sio.test_client(app), sio.test_client(app)
    requester.get_received()
    observer.get_received()
    requester.emit("request_leaderboard", page_request())
    opening = latest_event(requester, "leaderboard_snapshot")
    assert len(opening["rows"]) == 50
    assert opening["next_offset"] == 50
    requester.emit(
        "request_leaderboard", page_request(boundary=opening["boundary"], offset=50)
    )
    second = latest_event(requester, "leaderboard_snapshot")
    assert second["rows"][0]["rank"] == 51
    assert second["next_offset"] == 100
    requester.emit(
        "request_leaderboard",
        page_request(boundary=opening["boundary"] + 1, offset=100),
    )
    assert requester.get_received() == []
    assert observer.get_received() == []


def test_empty_boundary_zero_and_new_id_required_to_reopen(app_bundle):
    app, sio, _ = app_bundle
    client = sio.test_client(app)
    client.get_received()
    client.emit("request_leaderboard", page_request())
    assert latest_event(client, "leaderboard_snapshot")["boundary"] == 0
    client.emit("request_leaderboard", page_request())
    assert client.get_received() == []
    client.emit("request_leaderboard", page_request(request_id=2))
    assert latest_event(client, "leaderboard_snapshot")["rows"] == []
    client.emit("request_leaderboard", page_request())
    assert client.get_received() == []


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": True},
        {"request_id": 0},
        {"request_id": 2**53},
        {"offset": True},
        {"offset": -1},
        {"offset": 2**53},
        {"boundary": True},
        {"boundary": -1},
        {"boundary": 2**53},
        {"boundary": 1.0},
        {"offset": 1},
        {"mode_id": []},
        {"mode_id": "unknown"},
        {"race_id": 3},
    ],
)
def test_invalid_pages_do_not_read(app_bundle, monkeypatch, change):
    app, sio, runtime = app_bundle
    monkeypatch.setattr(
        runtime.store,
        "snapshot_boundary",
        lambda mode: pytest.fail("invalid request read"),
    )
    client = sio.test_client(app)
    client.get_received()
    client.emit("request_leaderboard", page_request(**change))
    assert client.get_received() == []


def test_persistence_is_separate_from_pages_and_highlights_origin(app_bundle, clock):
    app, sio, runtime = app_bundle
    race_id = finish_race(runtime, clock)
    client = sio.test_client(app)
    client.get_received()
    runtime.handle_change(runtime.race.submit_name(race_id, 2, "Grace"))
    state = latest_event(client, "game_state")
    assert state["persistence_status"] == "saved"
    client.emit("request_leaderboard", page_request(race_id=race_id))
    page = latest_event(client, "leaderboard_snapshot")
    assert [row["name"] for row in page["rows"]] == ["Ada", "Grace"]
    assert all(row["current_race"] for row in page["rows"])


def run_blocked(operation, entered, release):
    errors = []

    def run():
        try:
            operation()
        except BaseException as error:  # noqa: BLE001 - propagate worker assertions to test
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(3)
    return thread, errors


def unblock(thread, errors, release):
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert not errors


def test_pending_broadcast_precedes_blocking_write_and_reset_is_responsive(
    app_bundle, clock, monkeypatch
):
    app, sio, runtime = app_bundle
    runtime.handle_change(runtime.race.select_mode("quarter-field"))
    race_id = finish_race(runtime, clock)
    clients = [sio.test_client(app), sio.test_client(app)]
    for client in clients:
        client.get_received()
    entered, release = threading.Event(), threading.Event()
    original = runtime.store.insert_entries
    captured = []

    def insert(entries):
        captured.extend(entries)
        entered.set()
        assert release.wait(3)
        return original(entries)

    monkeypatch.setattr(runtime.store, "insert_entries", insert)
    thread, errors = run_blocked(
        lambda: runtime.handle_change(runtime.race.submit_name(race_id, 2, "Grace")),
        entered,
        release,
    )
    try:
        for client in clients:
            assert latest_event(client, "game_state")["persistence_status"] == "pending"
        clients[0].emit(
            "request_leaderboard",
            page_request(mode_id="quarter-field", race_id=race_id),
        )
        assert clients[0].get_received() == []
        runtime.apply(runtime.race.reset)
        runtime.apply(runtime.race.select_mode, "half-field")
        runtime.apply(runtime.race.start)
    finally:
        unblock(thread, errors, release)
    assert {entry.mode_id for entry in captured} == {"quarter-field"}
    assert {entry.race_id for entry in captured} == {race_id}
    assert runtime.race.snapshot()["persistence_status"] == "idle"
    assert runtime.store.snapshot_boundary("quarter-field") == 2


@pytest.mark.parametrize(
    "replacement", ["reset", "start-reset", "mode", "expire", "request", "disconnect"]
)
@pytest.mark.parametrize("stage", ["boundary", "page"])
def test_delayed_opening_discards_stale_results(
    app_bundle, clock, monkeypatch, replacement, stage
):
    app, sio, runtime = app_bundle
    race_id = None
    if replacement == "expire":
        race_id = finish_race(runtime, clock)
        runtime.handle_change(runtime.race.submit_name(race_id, 2, ""))
    client = sio.test_client(app)
    observer = sio.test_client(app)
    client.get_received()
    observer.get_received()
    entered, release = threading.Event(), threading.Event()
    method = "snapshot_boundary" if stage == "boundary" else "page_entries"
    original = getattr(runtime.store, method)
    calls = 0

    def delayed(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.store, method, delayed)
    thread, errors = run_blocked(
        lambda: client.emit("request_leaderboard", page_request(race_id=race_id)),
        entered,
        release,
    )
    try:
        if replacement == "request":
            client.emit(
                "request_leaderboard", page_request(request_id=2, race_id=race_id)
            )
            assert latest_event(client, "leaderboard_snapshot")["request_id"] == 2
        elif replacement == "disconnect":
            client.disconnect()
            assert len(runtime._browsing) == 1
        elif replacement == "expire":
            clock.advance_ms(60_000)
        elif replacement == "mode":
            runtime.handle_change(runtime.race.select_mode("quarter-field"))
        else:
            if replacement == "start-reset":
                runtime.handle_change(runtime.race.start())
            runtime.handle_change(runtime.race.reset())
    finally:
        unblock(thread, errors, release)
    if replacement != "disconnect":
        assert not [
            event
            for event in client.get_received()
            if event["name"] == "leaderboard_snapshot"
        ]
    if replacement == "expire":
        assert latest_event(observer, "game_state")["phase"] == "ready"


@pytest.mark.parametrize("failure", ["insert", "boundary", "page"])
def test_database_failures_are_recoverable(app_bundle, clock, monkeypatch, failure):
    app, sio, runtime = app_bundle
    method = {
        "insert": "insert_entries",
        "boundary": "snapshot_boundary",
        "page": "page_entries",
    }[failure]

    def fail(*args):
        raise sqlite3.OperationalError("fixture failure")

    monkeypatch.setattr(runtime.store, method, fail)
    race_id = finish_race(runtime, clock)
    runtime.handle_change(runtime.race.submit_name(race_id, 2, ""))
    client = sio.test_client(app)
    client.get_received()
    client.emit("request_leaderboard", page_request(race_id=race_id))
    page = latest_event(client, "leaderboard_snapshot")
    assert page["error"] if failure != "insert" else page["error"] is None
    assert runtime.race.snapshot()["persistence_status"] == (
        "error" if failure == "insert" else "saved"
    )
    runtime.handle_change(runtime.race.reset())
    runtime.observe_board(1, [255] * 48)
    runtime.observe_board(2, [255] * 48)
    runtime.handle_change(runtime.race.start())
    assert runtime.race.snapshot()["phase"] == "racing"


def test_unavailable_initialization_is_nonfatal(tmp_path, monkeypatch, clock):
    blocked = tmp_path / "blocked"
    blocked.write_text("blocked")
    monkeypatch.setenv("LEADERBOARD_DB", str(blocked / "scores.db"))
    app, sio = create_app(CONFIG, clock_ns=clock)
    client = sio.test_client(app)
    assert latest_event(client, "game_state")["phase"] == "ready"
    client.emit("request_leaderboard", page_request())
    assert latest_event(client, "leaderboard_snapshot")["error"]
    app.test_client().get("/start-clock")
    assert latest_event(client, "game_state")["phase"] == "racing"


def test_activity_expiry_and_page_reads_do_not_extend_deadline(app_bundle, clock):
    app, sio, runtime = app_bundle
    race_id = finish_race(runtime, clock)
    runtime.handle_change(runtime.race.submit_name(race_id, 2, ""))
    client = sio.test_client(app)
    clock.advance_ms(59_000)
    client.emit("request_leaderboard", page_request(race_id=race_id))
    assert runtime.race.snapshot()["remaining_seconds"] == 1
    client.emit("leaderboard_activity", {"race_id": race_id})
    assert runtime.race.snapshot()["remaining_seconds"] == 60
    clock.advance_ms(60_000)
    client.emit("leaderboard_activity", {"race_id": race_id})
    assert runtime.race.snapshot()["phase"] == "ready"


def test_serial_strict_reversed_raw_frame(app_bundle, monkeypatch):
    import app as module
    from app import decode_serial_frame, serial_loop

    assert decode_serial_frame(
        b" ".join(f"{i:x}".encode() for i in range(48))
    ) == tuple(reversed(range(48)))
    _, _, runtime = app_bundle
    observed = []

    class Connection:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def readline(self):
            runtime.stop_serial = True
            return b" ".join(f"{value:x}".encode() for value in reversed(EXPECTED_IDS))

        def write(self, *args):
            pytest.fail("host must not write LEDs")

    monkeypatch.setattr(module.serial, "Serial", Connection)
    monkeypatch.setattr(
        runtime,
        "observe_board",
        lambda player, values: observed.append((player, values)),
    )
    serial_loop(runtime, 0)
    assert observed == [(1, EXPECTED_IDS)]


@pytest.mark.parametrize(
    "line",
    [b"ff " * 47, b"ff " * 49, b"\xff " * 48]
    + [(token + b" ") * 48 for token in [b"-1", b"+1", b"0xff", b"100", b"gg", b"000"]],
)
def test_malformed_serial_frames_rejected(line):
    from app import decode_serial_frame

    with pytest.raises(ValueError):
        decode_serial_frame(line)


def test_toggle_verification_re_evaluates_raw_board(app_bundle):
    app, _, runtime = app_bundle
    runtime.observe_board(1, [0] * 48)
    before = runtime.race.snapshot()["revision"]
    app.test_client().get("/toggle-verify")
    state = runtime.race.snapshot()
    assert state["revision"] > before
    assert state["players"]["1"]["presentation"]["complete"]


def test_restart_serial_joins_before_start(app_bundle, monkeypatch):
    _, _, runtime = app_bundle
    calls = []

    class Reader:
        def join(self):
            calls.append("join")

    runtime.serial_threads = [Reader(), Reader()]
    monkeypatch.setattr(
        runtime, "_start_serial_unlocked", lambda: calls.append("start")
    )
    runtime.restart_serial()
    assert calls == ["join", "join", "start"]


def test_browser_harness_raw_reset_seeds_and_page_controls(tmp_path):
    from tests.browser_harness import create_harness

    app, sio, runtime, controls = create_harness(tmp_path / "browser.db")
    http = app.test_client()
    client = sio.test_client(app)
    client.get_received()
    assert http.post("/__test__/observe/1", json=list(EXPECTED_IDS)).status_code == 200
    assert runtime.race.snapshot()["players"]["1"]["presentation"]["complete"]
    assert http.post("/__test__/reset-unknown").status_code == 200
    assert runtime.race.boards[1].snapshot().identities is None
    assert runtime.race.boards[2].snapshot().identities is None
    for mode in runtime.race.registry.metadata():
        boundary = runtime.store.snapshot_boundary(mode.id)
        rows = [
            row
            for offset in (0, 50, 100)
            for row in runtime.store.page_entries(mode.id, boundary, offset).rows
        ]
        assert len(rows) == 121
        assert rows[49].rank == rows[50].rank
        assert rows[99].rank == rows[100].rank
    client.get_received()
    http.post("/__test__/pages", json={"fail_next": True})
    client.emit("request_leaderboard", page_request())
    failed = latest_event(client, "leaderboard_snapshot")
    assert failed["error"]
    assert failed["boundary"] is None
    client.emit("request_leaderboard", page_request())
    assert len(latest_event(client, "leaderboard_snapshot")["rows"]) == 50
    http.post("/__test__/pages", json={"delay_next": True})
    thread, errors = run_blocked(
        lambda: client.emit("request_leaderboard", page_request(request_id=2)),
        controls.entered,
        controls.release,
    )
    try:
        assert http.get("/__test__/pages").json["waiting"]
        assert not client.get_received()
    finally:
        http.post("/__test__/pages/release")
        unblock(thread, errors, controls.release)
    assert latest_event(client, "leaderboard_snapshot")["request_id"] == 2


def test_opening_failure_retry_and_later_failure_reuses_boundary(
    app_bundle, monkeypatch
):
    app, sio, runtime = app_bundle
    original = runtime.store.page_entries
    calls = []
    fail = True

    def page(*args):
        calls.append(args)
        if fail:
            raise OSError("page failed")
        return original(*args)

    monkeypatch.setattr(runtime.store, "page_entries", page)
    client = sio.test_client(app)
    client.get_received()
    client.emit("request_leaderboard", page_request())
    response = latest_event(client, "leaderboard_snapshot")
    assert response["boundary"] is None
    assert response["offset"] == 0
    fail = False
    client.emit("request_leaderboard", page_request())
    boundary = latest_event(client, "leaderboard_snapshot")["boundary"]
    monkeypatch.setattr(
        runtime.store,
        "snapshot_boundary",
        lambda mode: pytest.fail("later pages must not recapture"),
    )
    fail = True
    client.emit("request_leaderboard", page_request(boundary=boundary, offset=50))
    response = latest_event(client, "leaderboard_snapshot")
    assert response["error"]
    assert response["boundary"] == boundary
    assert response["offset"] == 50
    fail = False
    client.emit("request_leaderboard", page_request(boundary=boundary, offset=50))
    assert latest_event(client, "leaderboard_snapshot")["error"] is None


def test_final_page_publication_is_ordered_before_replacement(app_bundle, monkeypatch):
    app, sio, runtime = app_bundle
    client = sio.test_client(app)
    client.get_received()
    checked, release, attempted = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original = runtime.race.browse_is_current
    checks = 0

    def check(origin):
        nonlocal checks
        checks += 1
        result = original(origin)
        if checks == 2:
            checked.set()
            assert release.wait(3)
        return result

    monkeypatch.setattr(runtime.race, "browse_is_current", check)
    page_thread, errors = run_blocked(
        lambda: client.emit("request_leaderboard", page_request()), checked, release
    )

    def replace():
        attempted.set()
        runtime.apply(runtime.race.select_mode, "quarter-field")

    replacement = threading.Thread(target=replace)
    replacement.start()
    assert attempted.wait(3)
    unblock(page_thread, errors, release)
    replacement.join(3)
    assert not replacement.is_alive()
    events = client.get_received()
    names = [event["name"] for event in events]
    assert names.index("leaderboard_snapshot") < names.index("game_state")


@pytest.mark.parametrize("empty", [False, True])
def test_write_completion_processes_expiry_and_keeps_empty_origin(
    app_bundle, clock, monkeypatch, empty
):
    app, sio, runtime = app_bundle
    runtime.apply(runtime.race.select_mode, "quarter-field")
    runtime.apply(runtime.race.start)
    clock.advance_ms(1000)
    runtime.observe_board(1, EXPECTED_IDS)
    runtime.observe_board(2, EXPECTED_IDS)
    race_id = runtime.race.snapshot()["race_id"]
    runtime.apply(runtime.race.submit_name, race_id, 1, "" if empty else "Ada")
    entered, release = threading.Event(), threading.Event()
    original_insert, original_set = (
        runtime.store.insert_entries,
        runtime.race.set_persistence,
    )
    origins = []

    def insert(entries):
        entered.set()
        assert release.wait(3)
        return original_insert(entries)

    def set_persistence(origin, error):
        origins.append(origin)
        return original_set(origin, error)

    monkeypatch.setattr(runtime.store, "insert_entries", insert)
    monkeypatch.setattr(runtime.race, "set_persistence", set_persistence)
    observer = sio.test_client(app)
    observer.get_received()
    thread, errors = run_blocked(
        lambda: runtime.apply(runtime.race.submit_name, race_id, 2, ""),
        entered,
        release,
    )
    try:
        assert latest_event(observer, "game_state")["persistence_status"] == "pending"
        clock.advance_ms(60_000)
    finally:
        unblock(thread, errors, release)
    state = latest_event(observer, "game_state")
    assert state["phase"] == "ready"
    assert state["persistence_status"] == "idle"
    assert [(origin.race_id, origin.mode_id) for origin in origins] == [
        (race_id, "quarter-field")
    ]
    assert runtime.store.snapshot_boundary("quarter-field") == (0 if empty else 1)


def test_randomize_generates_raw_identities_and_returns_presentation(
    app_bundle, monkeypatch
):
    app, _, runtime = app_bundle
    import app as module

    monkeypatch.setattr(module.random, "choice", lambda choices: choices[1])
    response = app.test_client().get("/randomize")
    assert runtime.race.boards[1].snapshot().identities == EXPECTED_IDS
    assert runtime.race.boards[2].snapshot().identities == EXPECTED_IDS
    assert response.json["1"]["complete"]
    assert response.json["2"]["complete"]


@pytest.mark.parametrize("disconnect", [False, True])
def test_old_page_expiring_replacement_name_entry_persists_new_origin(
    app_bundle, clock, monkeypatch, disconnect
):
    app, sio, runtime = app_bundle
    client = sio.test_client(app)
    observer = sio.test_client(app)
    client.get_received()
    observer.get_received()
    entered, release = threading.Event(), threading.Event()
    original = runtime.store.page_entries

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(runtime.store, "page_entries", delayed)
    thread, errors = run_blocked(
        lambda: client.emit("request_leaderboard", page_request()), entered, release
    )
    try:
        race_id = finish_race(runtime, clock)
        if disconnect:
            client.disconnect()
        clock.advance_ms(120_000)
    finally:
        unblock(thread, errors, release)
    state = latest_event(observer, "game_state")
    assert state["phase"] == "leaderboard"
    assert state["persistence_status"] == "saved"
    boundary = runtime.store.snapshot_boundary("full-field")
    rows = original("full-field", boundary, 0).rows
    assert [(row.race_id, row.name) for row in rows] == [(race_id, "Ada")]


@pytest.mark.parametrize("phase", ["ready", "racing", "name_entry", "leaderboard"])
def test_reconnect_restores_full_cached_phase_presentation(app_bundle, clock, phase):
    app, sio, runtime = app_bundle
    if phase in ("name_entry", "leaderboard"):
        race_id = finish_race(runtime, clock)
        if phase == "leaderboard":
            runtime.apply(runtime.race.submit_name, race_id, 2, "")
    elif phase == "racing":
        runtime.apply(runtime.race.start)
        runtime.observe_board(1, EXPECTED_IDS)
    expected = json.loads(json.dumps(runtime.race.snapshot()))
    client = sio.test_client(app)
    state = latest_event(client, "game_state")
    assert state == expected
    assert state["phase"] == phase
    assert len(state["players"]["1"]["presentation"]["ports"]) == 48
    assert len(state["players"]["2"]["presentation"]["ports"]) == 48
