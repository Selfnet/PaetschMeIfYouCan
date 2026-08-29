import random
import sqlite3
import threading
import time
from dataclasses import asdict

import serial
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

from leaderboard import LeaderboardStore, NewLeaderboardEntry, default_database_path
from race import NUM_CELLS, Phase, RaceStateMachine

PANEL_IDS = [
    int(value, 2)
    for value in ("1110", "0110", "1010", "0010", "1100", "0100", "1000", "0000")
]
EXPECTED_IDS = [
    panel | int(side, 2)
    for outside, underside in (
        ("00000001", "01000000"),
        ("10000000", "01000001"),
        ("10000001", "11000000"),
    )
    for panel in PANEL_IDS
    for side in (outside, underside)
]


def parse_cell_state(value, expected, verify):
    if verify:
        return 2 if value == expected else 1 if value != 255 else 0
    return 2 if value != 255 else 0


class UnavailableLeaderboardStore:
    def insert_entries(self, entries):
        raise OSError("Leaderboard store is unavailable")

    def top_entries(self):
        raise OSError("Leaderboard store is unavailable")


class GameRuntime:
    def __init__(self, socketio, race, store, logger):
        self.socketio = socketio
        self.race = race
        self.store = store
        self.logger = logger
        self.cell_states = {1: [0] * NUM_CELLS, 2: [0] * NUM_CELLS}
        self.verify = True
        self.serial_threads = None
        self.stop_serial = False
        self._serial_lock = threading.Lock()

    def observe_cells(self, player_number, states):
        if (
            type(player_number) is int
            and player_number in self.cell_states
            and isinstance(states, (list, tuple))
        ):
            self.cell_states[player_number] = list(states)
            self.socketio.emit(
                "table_update", {"switch_id": player_number, "states": list(states)}
            )
        self.handle_change(self.race.observe_cells(player_number, states))

    def handle_change(self, change):
        if change.action == "leaderboard":
            self._persist_and_load(change.submissions)
        if change.changed:
            self.emit_state()

    def _persist_and_load(self, submissions):
        highlighted_ids = set()
        persistence_error = None
        try:
            highlighted_ids = self.store.insert_entries(
                [
                    NewLeaderboardEntry(
                        item.race_id, item.player_number, item.name, item.duration_ms
                    )
                    for item in submissions
                ]
            )
        except (sqlite3.Error, OSError, ValueError):
            self.logger.exception("Could not save leaderboard scores")
            persistence_error = "Scores could not be saved"
        rows, load_error = self.load_leaderboard()
        if load_error and persistence_error is None:
            persistence_error = load_error
        self.race.set_leaderboard(
            self.race.snapshot()["race_id"], rows, highlighted_ids, persistence_error
        )

    def load_leaderboard(self):
        try:
            return [asdict(row) for row in self.store.top_entries()], None
        except (sqlite3.Error, OSError):
            self.logger.exception("Could not load leaderboard scores")
            return [], "Leaderboard could not be loaded"

    def emit_state(self):
        self.socketio.emit("game_state", self.race.snapshot())

    def tick(self):
        self.handle_change(self.race.tick())

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
                    values = [
                        int(token, 16)
                        for token in reversed(line.decode("ascii").strip().split())
                    ]
                except (UnicodeDecodeError, ValueError):
                    runtime.logger.warning(
                        "Ignored malformed serial update from reader %s", serial_id
                    )
                    continue
                if len(values) != NUM_CELLS:
                    runtime.logger.warning(
                        "Ignored serial update with %s cells from reader %s",
                        len(values),
                        serial_id,
                    )
                    continue
                runtime.observe_cells(
                    serial_id + 1,
                    [
                        parse_cell_state(value, expected, runtime.verify)
                        for value, expected in zip(values, EXPECTED_IDS, strict=True)
                    ],
                )
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
            runtime.observe_cells(
                player, [random.choice((0, 1)) for _ in range(NUM_CELLS)]
            )
        return jsonify(runtime.cell_states)

    @app.get("/start-clock")
    def start_clock():
        runtime.handle_change(runtime.race.start())
        return "Clock started"

    @app.get("/stop-clock1")
    def stop_clock1():
        runtime.handle_change(runtime.race.manual_stop(1))
        return "Clock stopped"

    @app.get("/stop-clock2")
    def stop_clock2():
        runtime.handle_change(runtime.race.manual_stop(2))
        return "Clock stopped"

    @app.get("/reset-clock")
    def reset_clock():
        runtime.handle_change(runtime.race.reset())
        return "Clock reset"

    @app.get("/space-clock")
    def space_clock():
        change = runtime.race.space()
        runtime.handle_change(change)
        return f"Clock {change.action or 'ignored'}"

    @app.get("/toggle-verify")
    def toggle_verify():
        runtime.verify = not runtime.verify
        return "Done"

    @app.get("/restart-serial")
    def restart_serial():
        runtime.restart_serial()
        return "Done"


def register_socket_events(app, socketio, runtime):
    @socketio.on("connect")
    def handle_connect():
        socketio.emit("game_state", runtime.race.snapshot(), to=request.sid)
        for player in (1, 2):
            socketio.emit(
                "table_update",
                {"switch_id": player, "states": runtime.cell_states[player]},
                to=request.sid,
            )
        if app.config["START_SERIAL_ON_CONNECT"]:
            runtime.start_serial_once()

    @socketio.on("name_draft")
    def name_draft(data):
        if isinstance(data, dict):
            runtime.handle_change(
                runtime.race.set_name_draft(
                    data.get("race_id"), data.get("player_number"), data.get("draft")
                )
            )

    @socketio.on("submit_name")
    def submit_name(data):
        if isinstance(data, dict):
            runtime.handle_change(
                runtime.race.submit_name(
                    data.get("race_id"), data.get("player_number"), data.get("draft")
                )
            )

    @socketio.on("dismiss_leaderboard")
    def dismiss(data):
        if isinstance(data, dict):
            runtime.handle_change(runtime.race.dismiss_leaderboard(data.get("race_id")))

    @socketio.on("request_leaderboard")
    def request_idle_leaderboard(data=None):
        if runtime.race.snapshot()["phase"] not in (Phase.READY, Phase.STOPPED):
            return
        request_id = data.get("request_id") if isinstance(data, dict) else None
        rows, error = runtime.load_leaderboard()
        socketio.emit(
            "leaderboard_snapshot",
            {"request_id": request_id, "rows": rows, "error": error},
            to=request.sid,
        )


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
    config=None, *, clock_ns=time.monotonic_ns, store=None, race_id_factory=None
):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY="secret", START_SERIAL_ON_CONNECT=True, START_BACKGROUND_TASKS=True
    )
    if config:
        app.config.update(config)
    socketio = SocketIO(app, async_mode="threading")
    kwargs = {"clock_ns": clock_ns}
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
