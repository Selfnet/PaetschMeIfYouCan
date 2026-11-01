import random
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

import serial
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

from board import EXPECTED_IDS, NUM_PORTS, OPEN_ID
from gamemodes import BUILTIN_MODES
from leaderboard import LeaderboardStore, NewLeaderboardEntry, default_database_path
from leaderboard_policy import active_event, resolve_destination, utc_time
from race import Phase, RaceStateMachine, StateChange

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


def board_metadata(board):
    if board is None:
        return None
    return asdict(board) | {
        "starts_at": board.starts_at.isoformat() if board.starts_at else None,
        "ends_at": board.ends_at.isoformat() if board.ends_at else None,
    }


class GameRuntime:
    def __init__(self, socketio, race, store, logger, now_utc, database_path):
        self.socketio = socketio
        self.race = race
        self.store = store
        self.logger = logger
        self.now_utc = now_utc
        self.database_path = database_path
        self.policy = None
        self._published_policy = None
        self._race_board = None
        try:
            self.policy = self._storage().load_policy()
        except (sqlite3.Error, OSError, ValueError):
            self.logger.exception(
                "Could not load leaderboard routing; new races are blocked"
            )
        self._browsing = {}
        self._browse_lock = threading.Lock()
        self._publication_lock = threading.RLock()
        self._pace_race_id = None
        self._pace_reference_ms = None
        self.serial_threads = None
        self.stop_serial = False
        self._serial_lock = threading.Lock()

    def _storage(self):
        if self.store is None:
            raise OSError("Leaderboard store is unavailable")
        return self.store

    def _board(self, identity):
        return (
            next((board for board in self.policy.boards if board.id == identity), None)
            if self.policy
            else None
        )

    def _policy_payload(self, now=None):
        known = self.policy is not None
        now = utc_time(now if now is not None else self.now_utc())
        return {
            "available": known,
            "error": None
            if known
            else "Leaderboard routing is unavailable; retry storage from control",
            "revision": self.policy.revision if known else None,
            "next": board_metadata(resolve_destination(self.policy, now))
            if known
            else None,
            "active_event": board_metadata(active_event(self.policy, now))
            if known
            else None,
            "numbered": [
                board_metadata(board)
                for board in sorted(
                    self.policy.boards, key=lambda board: board.slot or 10
                )
                if board.slot is not None
            ]
            if known
            else [],
        }

    def start(self):
        return self._race_control(self.race.start)

    def space(self):
        return self._race_control(self.race.space)

    def _race_control(self, operation):
        with self._publication_lock:
            if self.race.snapshot()["phase"] == Phase.READY:
                if self.policy is None:
                    self.emit_state()
                    return StateChange(False, action="routing_unavailable")
                destination = resolve_destination(self.policy, self.now_utc())
                self._race_board = destination
                change = operation(destination.id)
            else:
                change = operation()
            self._publish_change(change)
        self._after_change(change)
        return change

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

    def select_leaderboard_slot(self, leaderboard_slot):
        with self._publication_lock:
            now = utc_time(self.now_utc())
            error = None
            accepted = False
            if type(leaderboard_slot) is not int or not 1 <= leaderboard_slot <= 9:
                error = "Leaderboard slot must be an integer between 1 and 9"
            elif self.race.snapshot()["phase"] != Phase.READY:
                error = "Leaderboard slot selection is only available when ready"
            elif self.policy is None:
                error = "Leaderboard routing is unavailable"
            elif active_event(self.policy, now) is not None:
                error = "Scoring is locked to the active event; numbered boards can still be browsed"
            else:
                try:
                    self._storage().set_selected_slot(leaderboard_slot, selected_at=now)
                except (sqlite3.Error, OSError, ValueError):
                    self.logger.exception("Could not save selected leaderboard slot")
                    error = "Leaderboard slot could not be saved"
                else:
                    self.policy = replace(
                        self.policy,
                        manual_slot=leaderboard_slot,
                        manual_selected_at=now,
                        revision=self.policy.revision + 1,
                    )
                    self.emit_state()
                    accepted = True
            return {
                "accepted": accepted,
                "leaderboard_slot": resolve_destination(self.policy, now).slot
                if self.policy
                else None,
                "error": error,
            }

    def list_events(self, data):
        with self._publication_lock:
            if not isinstance(data, dict) or type(data.get("past_only")) is not bool:
                return {"accepted": False, "error": "Expected a boolean past_only flag"}
            if self.policy is None:
                return {
                    "accepted": False,
                    "error": "Leaderboard routing is unavailable",
                }
            now = utc_time(self.now_utc())
            events = [
                board
                for board in self.policy.boards
                if board.slot is None
                and (not data["past_only"] or board.ends_at <= now)
            ]
            events.sort(key=lambda board: (board.ends_at, board.id), reverse=True)
            return {
                "accepted": True,
                "error": None,
                "events": [board_metadata(board) for board in events],
                "revision": self.policy.revision,
            }

    def edit_event(self, action, data):
        with self._publication_lock:
            if not isinstance(data, dict) or self.policy is None:
                return {
                    "accepted": False,
                    "error": "Event data or leaderboard routing is unavailable",
                }
            revision = data.get("revision")
            if (
                action == "cancel_event"
                and self.race.race_leaderboard_id is not None
                and data.get("id") == self.race.race_leaderboard_id
            ):
                return {
                    "accepted": False,
                    "error": "The current race still belongs to this event",
                }
            try:
                now = utc_time(self.now_utc())
                if action == "save_event":
                    event = self._storage().save_event(data.get("event"), revision, now)
                    identity = event.id
                else:
                    identity = data.get("id")
                    event = getattr(self._storage(), action)(identity, revision, now)
            except ValueError as error:
                return {"accepted": False, "error": str(error)}
            except (sqlite3.Error, OSError):
                self.logger.exception("Could not save leaderboard event")
                return {"accepted": False, "error": "Event change could not be saved"}
            boards = tuple(
                board for board in self.policy.boards if board.id != identity
            )
            if event is not None:
                boards += (event,)
            self.policy = replace(self.policy, boards=boards, revision=revision + 1)
            self.emit_state()
            result = {"accepted": True, "error": None, "revision": self.policy.revision}
            if event is not None:
                result["event"] = board_metadata(event)
                self.socketio.emit(
                    "leaderboard_changed",
                    {"leaderboard": result["event"], "revision": self.policy.revision},
                )
            return result

    def retry_storage(self, data):
        if not isinstance(data, dict):
            return {"accepted": False, "error": "Expected a storage retry request"}
        with self._publication_lock:
            try:
                store = LeaderboardStore(self.database_path, now=self.now_utc)
                policy = store.load_policy()
            except (sqlite3.Error, OSError, ValueError):
                self.logger.exception("Could not reload leaderboard storage")
                return {
                    "accepted": False,
                    "error": "Leaderboard storage is still unavailable",
                }
            if self.store is None:
                self.store = store
            self.policy = policy
            self.emit_state()
            return {"accepted": True, "error": None}

    def set_maintenance(self, enabled):
        with self._publication_lock:
            if type(enabled) is not bool:
                return {
                    "accepted": False,
                    "error": "Expected a boolean maintenance setting",
                }
            try:
                self._storage().set_maintenance_requested(enabled)
            except (sqlite3.Error, OSError, ValueError):
                self.logger.exception("Could not save maintenance setting")
                return {
                    "accepted": False,
                    "error": "Maintenance setting could not be saved",
                }
            change = self.race.set_maintenance(enabled)
            self._publish_change(change)
            return {"accepted": True, "error": None}

    def _publish_change(self, change):
        if change.diagnostic:
            self.logger.error("Mode evaluation failed: %s", change.diagnostic)
        if change.changed:
            self.emit_state()

    def _after_change(self, change):
        if change.action in ("leaderboard", "retry_scores"):
            self._persist(change.origin, change.submissions)

    def _persist(self, origin, submissions):
        persistence_error = None
        try:
            self._storage().insert_entries(
                [
                    NewLeaderboardEntry(
                        race_id=item.race_id,
                        mode_id=item.mode_id,
                        player_number=item.player_number,
                        name=item.name,
                        duration_ms=item.duration_ms,
                        leaderboard_id=item.leaderboard_id,
                    )
                    for item in submissions
                ]
            )
        except (sqlite3.Error, OSError, ValueError):
            self.logger.exception("Could not save leaderboard scores")
            persistence_error = "Scores could not be saved"
        self.apply(self.race.set_persistence, origin, persistence_error)

    def emit_state(self, to=None):
        with self._publication_lock:
            policy = self._policy_payload()
            if policy != self._published_policy:
                self.race.policy_changed()
                self._published_policy = policy
                to = None
            snapshot = self.race.snapshot()
            identity = snapshot.pop("race_leaderboard_id")
            if identity is None:
                self._race_board = None
            else:
                self._race_board = self._board(identity) or self._race_board
            snapshot["leaderboard_policy"] = policy
            snapshot["race_leaderboard"] = board_metadata(self._race_board)
            if snapshot["race_id"] != self._pace_race_id:
                self._pace_race_id = snapshot["race_id"]
                self._pace_reference_ms = None
                if self._pace_race_id is not None:
                    try:
                        self._pace_reference_ms = self._storage().median_duration_ms(
                            snapshot["mode"]["id"], identity
                        )
                    except (sqlite3.Error, OSError, ValueError):
                        self.logger.exception("Could not load leaderboard pace")
            snapshot["pace_reference_ms"] = self._pace_reference_ms
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
        rejected = {
            "accepted": False,
            "error": "Leaderboard request is invalid or no longer current",
        }
        if not isinstance(data, dict):
            return rejected
        request_id, offset, boundary = (
            data.get(key) for key in ("request_id", "offset", "boundary")
        )
        mode_id, race_id = data.get("mode_id"), data.get("race_id")
        leaderboard_id = data.get("leaderboard_id")
        if (
            not safe_integer(request_id, 1)
            or not safe_integer(offset)
            or (boundary is not None and not safe_integer(boundary))
            or (boundary is None and offset != 0)
            or not isinstance(mode_id, str)
            or (race_id is not None and not isinstance(race_id, str))
            or not safe_integer(leaderboard_id, 1)
        ):
            return rejected
        with self._publication_lock:
            if self._board(leaderboard_id) is None:
                return rejected
            origin = self.race.capture_browse(mode_id, race_id, leaderboard_id)
            current, change = self.race.browse_is_current(origin)
            self._publish_change(change)
        self._after_change(change)
        if not current:
            return rejected
        with self._browse_lock:
            if sid not in self._browsing:
                return rejected
            token = self._browsing[sid]
            if token is None or request_id > token.request_id:
                if boundary is not None:
                    return rejected
                token = BrowseToken(request_id, origin, None)
                self._browsing[sid] = token
            elif (
                request_id != token.request_id
                or origin != token.origin
                or boundary != token.boundary
            ):
                return rejected
            if boundary is None:
                if token.opening:
                    return rejected
                token.opening = True
        rows, next_offset, error = [], None, None
        target_score_id = None
        captured_boundary = boundary
        result_race_id = (
            race_id if leaderboard_id == self.race.race_leaderboard_id else None
        )
        try:
            if boundary is None:
                captured_boundary = self._storage().snapshot_boundary(
                    mode_id, leaderboard_id
                )
                if not safe_integer(captured_boundary):
                    raise ValueError("invalid snapshot boundary")
            page = self._storage().page_entries(
                mode_id, captured_boundary, offset, 50, leaderboard_id, result_race_id
            )
            rows = [
                asdict(row)
                | {
                    "current_race": row.race_duration_ms is not None,
                    "personal_best": result_race_id is not None
                    and row.race_id == result_race_id
                    and bool(row.best_improved),
                }
                for row in page.rows
            ]
            next_offset = page.next_offset
            target_score_id = page.target_score_id
        except (sqlite3.Error, OSError, ValueError):
            self.logger.exception("Could not load leaderboard page")
            error = "Leaderboard could not be loaded"
        # Serialize final eligibility/publication against runtime view transitions.
        with self._publication_lock:
            current, change = self.race.browse_is_current(origin)
            self._publish_change(change)
            board = self._board(leaderboard_id)
            published = False
            with self._browse_lock:
                if self._browsing.get(sid) is token:
                    token.opening = False
                    if current and board is not None and token.boundary == boundary:
                        if error is None and boundary is None:
                            token.boundary = captured_boundary
                        self.socketio.emit(
                            "leaderboard_snapshot",
                            {
                                "request_id": request_id,
                                "mode_id": mode_id,
                                "leaderboard_id": leaderboard_id,
                                "leaderboard": board_metadata(board),
                                "race_id": race_id,
                                "boundary": token.boundary,
                                "offset": offset,
                                "next_offset": next_offset,
                                "target_score_id": target_score_id,
                                "rows": rows,
                                "error": error,
                            },
                            to=sid,
                        )
                        published = True
        self._after_change(change)
        return {"accepted": True, "error": error} if published else rejected

    def tick(self):
        self.apply(self.race.tick)
        with self._publication_lock:
            if self._policy_payload() != self._published_policy:
                self.emit_state()

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
        change = runtime.start()
        if change.action == "routing_unavailable":
            return "Leaderboard routing is unavailable", 503
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
        change = runtime.space()
        if change.action == "routing_unavailable":
            return "Leaderboard routing is unavailable", 503
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

    @socketio.on("set_maintenance")
    def set_maintenance(data=None):
        enabled = data.get("enabled") if isinstance(data, dict) else None
        return runtime.set_maintenance(enabled)

    @socketio.on("leaderboard_activity")
    def leaderboard_activity(data=None):
        if isinstance(data, dict) and isinstance(data.get("race_id"), str):
            runtime.apply(runtime.race.leaderboard_activity, data["race_id"])

    @socketio.on("select_leaderboard_slot")
    def select_leaderboard_slot(data=None):
        slot = data.get("leaderboard_slot") if isinstance(data, dict) else None
        return runtime.select_leaderboard_slot(slot)

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
    def dismiss(data=None):
        if isinstance(data, dict):
            change = runtime.apply(
                runtime.race.dismiss_leaderboard,
                data.get("race_id"),
                data.get("discard_unsaved"),
                data.get("browse_epoch"),
            )
            return {"accepted": change.action == "reset"}
        return {"accepted": False}

    @socketio.on("retry_scores")
    def retry_scores(data=None):
        if not isinstance(data, dict):
            return {"accepted": False}
        change = runtime.apply(
            runtime.race.retry_scores, data.get("race_id"), data.get("browse_epoch")
        )
        return {"accepted": change.action == "retry_scores"}

    @socketio.on("request_leaderboard")
    def request_leaderboard(data=None):
        return runtime.request_page(request.sid, data)

    @socketio.on("list_events")
    def list_events(data=None):
        return runtime.list_events(data)

    @socketio.on("save_event")
    def save_event(data=None):
        return runtime.edit_event("save_event", data)

    @socketio.on("cancel_event")
    def cancel_event(data=None):
        return runtime.edit_event("cancel_event", data)

    @socketio.on("end_event")
    def end_event(data=None):
        return runtime.edit_event("end_event", data)

    @socketio.on("retry_leaderboard_storage")
    def retry_leaderboard_storage(data=None):
        return runtime.retry_storage(data)


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
    now_utc=lambda: datetime.now(UTC),
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
    database_path = store.path if store is not None else default_database_path()
    if store is None:
        try:
            store = LeaderboardStore(database_path, now=now_utc)
        except (sqlite3.Error, OSError, ValueError):
            app.logger.exception("Could not initialize leaderboard scores")
    try:
        if store is not None:
            kwargs["maintenance_requested"] = store.maintenance_requested()
    except (sqlite3.Error, OSError, ValueError):
        app.logger.exception("Could not restore maintenance setting; allowing play")
    runtime = GameRuntime(
        socketio,
        RaceStateMachine(**kwargs),
        store,
        app.logger,
        now_utc,
        database_path,
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
