import random
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass

import serial
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

from board import EXPECTED_IDS, NUM_PORTS, OPEN_ID
from gamemodes import BUILTIN_MODES
from leaderboard import LeaderboardStore, NewLeaderboardEntry, default_database_path
from race import Phase, RaceStateMachine

MAX_SAFE_INTEGER = 2**53 - 1


def decode_serial_frame(line: bytes) -> tuple[int, ...]:
    tokens = line.decode("ascii").split()
    if len(tokens) != NUM_PORTS or any(
        re.fullmatch(r"[0-9A-Fa-f]{1,2}", token) is None for token in tokens
    ):
        raise ValueError("expected 48 hexadecimal byte identities")
    return tuple(int(token, 16) for token in reversed(tokens))


def safe_integer(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_SAFE_INTEGER


@dataclass
class BrowseToken:
    request_id: int
    origin: object
    boundary: int | None
    opening: bool = False


class UnavailableLeaderboardStore:
    def insert_entries(self, entries):
        raise OSError("Leaderboard store is unavailable")

    def snapshot_boundary(self, mode_id):
        raise OSError("Leaderboard store is unavailable")

    def page_entries(self, mode_id, boundary, offset, limit=50):
        raise OSError("Leaderboard store is unavailable")


class GameRuntime:
    def __init__(self, socketio, race, store, logger):
        self.socketio = socketio
        self.race = race
        self.store = store
        self.logger = logger
        self._browsing = {}
        self._browse_lock = threading.Lock()
        self._publication_lock = threading.RLock()
        self.serial_threads = None
        self.stop_serial = False
        self._serial_lock = threading.Lock()

    def observe_board(self, player_number, values):
        self.apply(self.race.observe_board, player_number, values)

    def apply(self, operation, *args):
        with self._publication_lock:
            change = operation(*args)
            self._publish_change(change)
        self._after_change(change)
        return change

    def handle_change(self, change):
        with self._publication_lock:
            self._publish_change(change)
        self._after_change(change)

    def _publish_change(self, change):
        if change.diagnostic:
            self.logger.error("Mode evaluation failed: %s", change.diagnostic)
        if change.changed:
            self.emit_state()

    def _after_change(self, change):
        if change.action == "leaderboard":
            self._persist(change.origin, change.submissions)

    def _persist(self, origin, submissions):
        persistence_error = None
        try:
            self.store.insert_entries(
                [
                    NewLeaderboardEntry(
                        race_id=item.race_id,
                        mode_id=item.mode_id,
                        player_number=item.player_number,
                        name=item.name,
                        duration_ms=item.duration_ms,
                    )
                    for item in submissions
                ]
            )
        except (sqlite3.Error, OSError, ValueError):
            self.logger.exception("Could not save leaderboard scores")
            persistence_error = "Scores could not be saved"
        self.apply(self.race.set_persistence, origin, persistence_error)

    def emit_state(self, to=None):
        snapshot = self.race.snapshot()
        self.socketio.emit("game_state", snapshot, to=to)
        for number, player in snapshot["players"].items():
            presentation = player["presentation"]
            if presentation is not None:
                self.socketio.emit(
                    "table_update",
                    {
                        "switch_id": int(number),
                        "revision": snapshot["revision"],
                        "states": [
                            {"correct": 2, "wrong": 1}.get(port["feedback"], 0)
                            for port in presentation["ports"]
                        ],
                    },
                    to=to,
                )

    def request_page(self, sid, data):
        if not isinstance(data, dict):
            return
        request_id, offset, boundary = (
            data.get(key) for key in ("request_id", "offset", "boundary")
        )
        mode_id, race_id = data.get("mode_id"), data.get("race_id")
        if (
            not safe_integer(request_id, 1)
            or not safe_integer(offset)
            or (boundary is not None and not safe_integer(boundary))
            or (boundary is None and offset != 0)
            or not isinstance(mode_id, str)
            or (race_id is not None and not isinstance(race_id, str))
        ):
            return
        with self._publication_lock:
            origin = self.race.capture_browse(mode_id, race_id)
            current, change = self.race.browse_is_current(origin)
            self._publish_change(change)
        self._after_change(change)
        if not current:
            return
        with self._browse_lock:
            if sid not in self._browsing:
                return
            token = self._browsing[sid]
            if token is None or request_id > token.request_id:
                if boundary is not None:
                    return
                token = BrowseToken(request_id, origin, None)
                self._browsing[sid] = token
            elif (
                request_id != token.request_id
                or origin != token.origin
                or boundary != token.boundary
            ):
                return
            if boundary is None:
                if token.opening:
                    return
                token.opening = True
        rows, next_offset, error = [], None, None
        captured_boundary = boundary
        try:
            if boundary is None:
                captured_boundary = self.store.snapshot_boundary(mode_id)
                if not safe_integer(captured_boundary):
                    raise ValueError("invalid snapshot boundary")
            page = self.store.page_entries(mode_id, captured_boundary, offset, 50)
            rows = [
                asdict(row)
                | {"current_race": race_id is not None and row.race_id == race_id}
                for row in page.rows
            ]
            next_offset = page.next_offset
        except (sqlite3.Error, OSError, ValueError):
            self.logger.exception("Could not load leaderboard page")
            error = "Leaderboard could not be loaded"
        # Serialize final eligibility/publication against runtime view transitions.
        with self._publication_lock:
            current, change = self.race.browse_is_current(origin)
            self._publish_change(change)
            with self._browse_lock:
                if self._browsing.get(sid) is token:
                    token.opening = False
                    if current and token.boundary == boundary:
                        if error is None and boundary is None:
                            token.boundary = captured_boundary
                        self.socketio.emit(
                            "leaderboard_snapshot",
                            {
                                "request_id": request_id,
                                "mode_id": mode_id,
                                "race_id": race_id,
                                "boundary": token.boundary,
                                "offset": offset,
                                "next_offset": next_offset,
                                "rows": rows,
                                "error": error,
                            },
                            to=sid,
                        )
        self._after_change(change)

    def tick(self):
        self.apply(self.race.tick)

    def start_serial_once(self):
        with self._serial_lock:
            if self.serial_threads is not None:
                return
            self._start_serial_unlocked()

    def _start_serial_unlocked(self):
        self.stop_serial = False
        self.serial_threads = [
            threading.Thread(target=serial_loop, args=(self, serial_id), daemon=True)
            for serial_id in (0, 1)
        ]
        for thread in self.serial_threads:
            thread.start()

    def restart_serial(self):
        with self._serial_lock:
            threads = self.serial_threads or []
            self.stop_serial = True
            for thread in threads:
                thread.join()
            self.serial_threads = None
            self._start_serial_unlocked()


def serial_loop(runtime, serial_id):
    try:
        with serial.Serial(
            port=f"/dev/ttyACM{serial_id}",
            baudrate=9600,
            parity=serial.PARITY_ODD,
            stopbits=serial.STOPBITS_TWO,
            bytesize=serial.SEVENBITS,
            timeout=5,
        ) as connection:
            while not runtime.stop_serial:
                line = connection.readline()
                if not line:
                    continue
                try:
                    values = decode_serial_frame(line)
                except (UnicodeDecodeError, ValueError):
                    runtime.logger.warning(
                        "Ignored malformed serial update from reader %s", serial_id
                    )
                    continue
                runtime.observe_board(serial_id + 1, values)
    except serial.SerialException:
        runtime.logger.exception("Serial reader %s stopped", serial_id)


def register_routes(app, runtime):
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/control")
    def control():
        return render_template("control.html")

    @app.get("/randomize")
    def randomize():
        for player in (1, 2):
            runtime.observe_board(
                player,
                [
                    random.choice((OPEN_ID, expected, expected ^ 1))
                    for expected in EXPECTED_IDS
                ],
            )
        return jsonify(
            {
                number: player["presentation"]
                for number, player in runtime.race.snapshot()["players"].items()
            }
        )

    @app.get("/start-clock")
    def start_clock():
        runtime.apply(runtime.race.start)
        return "Clock started"

    @app.get("/stop-clock1")
    def stop_clock1():
        runtime.apply(runtime.race.manual_stop, 1)
        return "Clock stopped"

    @app.get("/stop-clock2")
    def stop_clock2():
        runtime.apply(runtime.race.manual_stop, 2)
        return "Clock stopped"

    @app.get("/reset-clock")
    def reset_clock():
        runtime.apply(runtime.race.reset)
        return "Clock reset"

    @app.get("/space-clock")
    def space_clock():
        change = runtime.apply(runtime.race.space)
        return f"Clock {change.action or 'ignored'}"

    @app.get("/toggle-verify")
    def toggle_verify():
        runtime.apply(runtime.race.toggle_verification)
        return "Done"

    @app.get("/restart-serial")
    def restart_serial():
        runtime.restart_serial()
        return "Done"


def register_socket_events(app, socketio, runtime):
    @socketio.on("connect")
    def handle_connect():
        with runtime._browse_lock:
            runtime._browsing[request.sid] = None
        runtime.emit_state(to=request.sid)
        if app.config["START_SERIAL_ON_CONNECT"]:
            runtime.start_serial_once()

    @socketio.on("disconnect")
    def handle_disconnect(reason=None):
        with runtime._browse_lock:
            runtime._browsing.pop(request.sid, None)

    @socketio.on("select_mode")
    def select_mode(data=None):
        mode_id = data.get("mode_id") if isinstance(data, dict) else None
        change = runtime.apply(runtime.race.select_mode, mode_id)
        return {
            "accepted": change.action == "mode_selected",
            "mode_id": runtime.race.snapshot()["mode"]["id"],
            "error": None
            if change.action == "mode_selected"
            else "Mode selection is not available",
        }

    @socketio.on("leaderboard_activity")
    def leaderboard_activity(data=None):
        if isinstance(data, dict) and isinstance(data.get("race_id"), str):
            runtime.apply(runtime.race.leaderboard_activity, data["race_id"])

    @socketio.on("name_draft")
    def name_draft(data):
        if isinstance(data, dict):
            runtime.apply(
                runtime.race.set_name_draft,
                data.get("race_id"),
                data.get("player_number"),
                data.get("draft"),
            )

    @socketio.on("submit_name")
    def submit_name(data):
        if isinstance(data, dict):
            runtime.apply(
                runtime.race.submit_name,
                data.get("race_id"),
                data.get("player_number"),
                data.get("draft"),
            )

    @socketio.on("dismiss_leaderboard")
    def dismiss(data):
        if isinstance(data, dict):
            runtime.apply(runtime.race.dismiss_leaderboard, data.get("race_id"))

    @socketio.on("request_leaderboard")
    def request_leaderboard(data=None):
        runtime.request_page(request.sid, data)


def background_loop(socketio, runtime):
    while True:
        runtime.tick()
        phase = runtime.race.snapshot()["phase"]
        if phase == Phase.RACING:
            socketio.emit("clock_update", runtime.race.clock_payload())
        elif phase in (Phase.NAME_ENTRY, Phase.LEADERBOARD):
            runtime.emit_state()
        socketio.sleep(1)


def create_app(
    config=None,
    *,
    clock_ns=time.monotonic_ns,
    store=None,
    race_id_factory=None,
    registry=BUILTIN_MODES,
):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY="secret", START_SERIAL_ON_CONNECT=True, START_BACKGROUND_TASKS=True
    )
    if config:
        app.config.update(config)
    socketio = SocketIO(app, async_mode="threading")
    kwargs = {"clock_ns": clock_ns, "registry": registry}
    if race_id_factory is not None:
        kwargs["race_id_factory"] = race_id_factory
    if store is None:
        try:
            store = LeaderboardStore(default_database_path())
        except (sqlite3.Error, OSError):
            app.logger.exception("Could not initialize leaderboard scores")
            store = UnavailableLeaderboardStore()
    runtime = GameRuntime(
        socketio,
        RaceStateMachine(**kwargs),
        store,
        app.logger,
    )
    app.extensions["game_runtime"] = runtime
    register_routes(app, runtime)
    register_socket_events(app, socketio, runtime)
    if app.config["START_BACKGROUND_TASKS"]:
        socketio.start_background_task(background_loop, socketio, runtime)
    return app, socketio


if __name__ == "__main__":
    app, socketio = create_app()
    socketio.run(app, host="0.0.0.0", debug=True, allow_unsafe_werkzeug=True)
