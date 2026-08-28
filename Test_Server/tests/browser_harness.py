import itertools
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from leaderboard import LeaderboardStore


class BrowserClock:
    def __init__(self):
        self.nanoseconds = 0

    def __call__(self):
        return self.nanoseconds

    def advance_ms(self, milliseconds):
        self.nanoseconds += milliseconds * 1_000_000


clock = BrowserClock()
database = Path(tempfile.mkdtemp(prefix="patchme-browser-")) / "leaderboard.sqlite3"
race_ids = itertools.count(1)
app, socketio = create_app(
    {"START_SERIAL_ON_CONNECT": False, "START_BACKGROUND_TASKS": False},
    clock_ns=clock,
    store=LeaderboardStore(database),
    race_id_factory=lambda: f"browser-race-{next(race_ids)}",
)
runtime = app.extensions["game_runtime"]


@app.post("/__test__/complete/<int:player_number>/<int:duration_ms>")
def complete(player_number, duration_ms):
    current = runtime.race.clock_payload()["durations_ms"][str(player_number)]
    clock.advance_ms(max(0, duration_ms - current))
    runtime.observe_cells(player_number, [2] * 48)
    return "completed"


@app.post("/__test__/advance/<int:milliseconds>")
def advance(milliseconds):
    clock.advance_ms(milliseconds)
    runtime.tick()
    runtime.emit_state()
    return "advanced"


if __name__ == "__main__":
    socketio.run(app, host="127.0.0.1", port=5001, allow_unsafe_werkzeug=True)
