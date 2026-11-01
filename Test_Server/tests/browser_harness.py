import itertools
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flask import jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from board import EXPECTED_IDS, GameBoard
from gamemodes import BUILTIN_MODES
from leaderboard import LeaderboardStore, NewLeaderboardEntry
from leaderboard_policy import utc_time


class BrowserClock:
    def __init__(self):
        self.nanoseconds = 0

    def __call__(self):
        return self.nanoseconds

    def advance_ms(self, milliseconds):
        self.nanoseconds += milliseconds * 1_000_000


class ControlledStore(LeaderboardStore):
    def __init__(self, path):
        super().__init__(path, now=lambda: datetime(2026, 8, 30, 14, 30, tzinfo=UTC))
        self.lock = threading.Lock()
        self.fail_next = False
        self.delay_next = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.insert_fail_next = False
        self.insert_delay_next = False
        self.insert_entered = threading.Event()
        self.insert_release = threading.Event()

    def insert_entries(self, entries):
        with self.lock:
            fail, delay = self.insert_fail_next, self.insert_delay_next
            self.insert_fail_next = self.insert_delay_next = False
        if delay:
            self.insert_entered.set()
            if not self.insert_release.wait(30):
                raise OSError("fixture insert release timed out")
        if fail:
            raise OSError("fixture insert failure")
        return super().insert_entries(entries)

    def page_entries(self, *args, **kwargs):
        with self.lock:
            fail, delay = self.fail_next, self.delay_next
            self.fail_next = self.delay_next = False
        if delay:
            self.entered.set()
            if not self.release.wait(30):
                raise OSError("fixture page release timed out")
        if fail:
            raise OSError("fixture page failure")
        return super().page_entries(*args, **kwargs)


def create_harness(database, registry=BUILTIN_MODES, *, now_utc=None):
    clock = BrowserClock()
    wall_source = now_utc or (lambda: datetime.now(UTC))
    wall_time = None

    def wall_now():
        return wall_time if wall_time is not None else utc_time(wall_source())

    race_ids = itertools.count(1)
    store = ControlledStore(database)
    store.insert_entries(
        [
            NewLeaderboardEntry(
                race_id=f"seed-{mode.id}-{index}",
                mode_id=mode.id,
                player_number=1,
                name=f"Score{index:03}",
                duration_ms=100
                + (49 if 49 <= index <= 51 else 99 if 99 <= index <= 101 else index),
            )
            for mode in registry.metadata()
            for index in range(121)
        ]
    )
    store.now = wall_now
    app, socketio = create_app(
        {
            "TESTING": True,
            "TEMPLATES_AUTO_RELOAD": True,
            "START_SERIAL_ON_CONNECT": False,
            "START_BACKGROUND_TASKS": False,
        },
        clock_ns=clock,
        now_utc=wall_now,
        store=store,
        race_id_factory=lambda: f"browser-race-{next(race_ids)}",
        registry=registry,
    )
    runtime = app.extensions["game_runtime"]

    @app.post("/__test__/complete/<int:player_number>/<int:duration_ms>")
    def complete(player_number, duration_ms):
        if player_number not in (1, 2):
            return "invalid player", 400
        current = runtime.race.clock_payload()["durations_ms"][str(player_number)]
        clock.advance_ms(max(0, duration_ms - current))
        runtime.observe_board(player_number, EXPECTED_IDS)
        return "completed"

    @app.post("/__test__/observe/<int:player_number>")
    def observe(player_number):
        runtime.observe_board(player_number, request.get_json())
        return jsonify(runtime.race.snapshot())

    @app.post("/__test__/reset-unknown")
    def reset_unknown():
        def reset_boards():
            with runtime.race._lock:
                runtime.race.boards = {1: GameBoard(), 2: GameBoard()}
                return runtime.race.reset()

        runtime.apply(reset_boards)
        return jsonify(runtime.race.snapshot())

    @app.post("/__test__/advance/<int:milliseconds>")
    def advance(milliseconds):
        clock.advance_ms(milliseconds)
        runtime.tick()
        runtime.emit_state()
        return "advanced"

    @app.route("/__test__/wall-time", methods=["GET", "POST"])
    def wall_clock():
        nonlocal wall_time
        if request.method == "POST":
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return "expected a wall time or millisecond advance", 400
            try:
                if set(data) == {"now_utc"} and isinstance(data["now_utc"], str):
                    value = utc_time(datetime.fromisoformat(data["now_utc"]))
                elif set(data) == {"advance_ms"} and type(data["advance_ms"]) is int:
                    value = wall_now() + timedelta(milliseconds=data["advance_ms"])
                else:
                    return "expected now_utc with an offset or integer advance_ms", 400
            except (ValueError, OverflowError):
                return "invalid or out-of-range wall time", 400
            wall_time = value
            runtime.tick()
            runtime.emit_state()
        return jsonify(now_utc=wall_now().isoformat())

    @app.route("/__test__/inserts", methods=["GET", "POST"])
    def inserts():
        if request.method == "POST":
            data = request.get_json(silent=True)
            if (
                not isinstance(data, dict)
                or set(data) - {"fail_next", "delay_next"}
                or any(type(value) is not bool for value in data.values())
            ):
                return "expected boolean fail_next and delay_next flags", 400
            with store.lock:
                store.insert_fail_next = data.get("fail_next", False)
                store.insert_delay_next = data.get("delay_next", False)
                if store.insert_delay_next:
                    store.insert_entered.clear()
                    store.insert_release.clear()
        return jsonify(
            waiting=store.insert_entered.is_set() and not store.insert_release.is_set()
        )

    @app.post("/__test__/inserts/release")
    def release_insert():
        store.insert_release.set()
        return "released"

    @app.route("/__test__/pages", methods=["GET", "POST"])
    def pages():
        if request.method == "POST":
            data = request.get_json()
            if not isinstance(data, dict) or any(
                type(value) is not bool for value in data.values()
            ):
                return "expected boolean flags", 400
            with store.lock:
                store.fail_next = data.get("fail_next", False)
                store.delay_next = data.get("delay_next", False)
                if store.delay_next:
                    store.entered.clear()
                    store.release.clear()
        return jsonify(waiting=store.entered.is_set() and not store.release.is_set())

    @app.post("/__test__/pages/release")
    def release():
        store.release.set()
        return "released"

    return app, socketio, runtime, store


if __name__ == "__main__":
    database = Path(tempfile.mkdtemp(prefix="patchme-browser-")) / "leaderboard.sqlite3"
    app, socketio, runtime, controls = create_harness(database)
    socketio.run(app, host="127.0.0.1", port=5001, allow_unsafe_werkzeug=True)
