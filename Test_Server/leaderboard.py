import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class NewLeaderboardEntry:
    race_id: str
    player_number: int
    name: str
    duration_ms: int


@dataclass(frozen=True)
class RankedLeaderboardEntry:
    id: int
    race_id: str
    player_number: int
    name: str
    duration_ms: int
    created_at: str
    rank: int


def default_database_path() -> Path:
    configured = os.environ.get("LEADERBOARD_DB")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local/share/patchmeifyoucan/leaderboard.sqlite3"


class LeaderboardStore:
    def __init__(self, path: Path, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self.now = now or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def insert_entries(self, entries: Sequence[NewLeaderboardEntry]) -> set[int]:
        validated = tuple(self._validate(entry) for entry in entries)
        if not validated:
            return set()

        with self._connect() as connection:
            for item in validated:
                connection.execute(
                    """INSERT INTO leaderboard_entries
                       (race_id, player_number, name, duration_ms, created_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(race_id, player_number) DO NOTHING""",
                    (
                        item.race_id,
                        item.player_number,
                        item.name,
                        item.duration_ms,
                        self.now().isoformat(),
                    ),
                )
            return {
                row[0]
                for item in validated
                for row in connection.execute(
                    "SELECT id FROM leaderboard_entries WHERE race_id = ? AND player_number = ?",
                    (item.race_id, item.player_number),
                )
            }

    def top_entries(self, limit: int = 10) -> list[RankedLeaderboardEntry]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, race_id, player_number, name, duration_ms, created_at, rank
                   FROM (
                       SELECT id, race_id, player_number, name, duration_ms, created_at,
                              RANK() OVER (ORDER BY duration_ms) AS rank
                       FROM leaderboard_entries
                   )
                   ORDER BY duration_ms, created_at, id
                   LIMIT ?""",
                (limit,),
            )
            return [RankedLeaderboardEntry(*row) for row in rows]

    @staticmethod
    def _validate(entry: NewLeaderboardEntry) -> NewLeaderboardEntry:
        if not isinstance(entry.race_id, str) or not entry.race_id:
            raise ValueError("race_id is required")
        if type(entry.player_number) is not int or entry.player_number not in (1, 2):
            raise ValueError("player_number must be 1 or 2")
        if not entry.name or entry.name != entry.name.strip() or len(entry.name) > 12:
            raise ValueError("name must contain 1 to 12 trimmed characters")
        if not all(character.isprintable() for character in entry.name):
            raise ValueError("name must be printable")
        if (
            not isinstance(entry.duration_ms, int)
            or isinstance(entry.duration_ms, bool)
            or entry.duration_ms < 0
        ):
            raise ValueError("duration_ms must be a non-negative integer")
        return entry


SCHEMA = """
CREATE TABLE IF NOT EXISTS leaderboard_entries (
    id INTEGER PRIMARY KEY,
    race_id TEXT NOT NULL,
    player_number INTEGER NOT NULL CHECK (player_number IN (1, 2)),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 12),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (race_id, player_number)
);
CREATE INDEX IF NOT EXISTS leaderboard_time_idx
    ON leaderboard_entries (duration_ms, created_at, id);
"""
