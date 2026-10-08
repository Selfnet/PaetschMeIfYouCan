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
    mode_id: str
    leaderboard_slot: int = 1


@dataclass(frozen=True)
class RankedLeaderboardEntry:
    id: int
    race_id: str
    player_number: int
    name: str
    duration_ms: int
    created_at: str
    mode_id: str
    rank: int
    leaderboard_slot: int = 1


@dataclass(frozen=True)
class LeaderboardPage:
    rows: tuple[RankedLeaderboardEntry, ...]
    next_offset: int | None


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
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(SCHEMA)
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(leaderboard_entries)")
            }
            if "mode_id" not in columns:
                connection.execute(
                    "ALTER TABLE leaderboard_entries ADD COLUMN mode_id TEXT NOT NULL DEFAULT 'full-field'"
                )
            if "leaderboard_slot" not in columns:
                connection.execute(
                    "ALTER TABLE leaderboard_entries ADD COLUMN leaderboard_slot "
                    "INTEGER NOT NULL DEFAULT 1 CHECK (leaderboard_slot BETWEEN 1 AND 9)"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS leaderboard_slot_mode_time_idx "
                "ON leaderboard_entries (leaderboard_slot, mode_id, duration_ms, created_at, id)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS leaderboard_settings ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "leaderboard_slot INTEGER NOT NULL CHECK (leaderboard_slot BETWEEN 1 AND 9))"
            )
            connection.execute(
                "INSERT INTO leaderboard_settings (id, leaderboard_slot) VALUES (1, 1) "
                "ON CONFLICT(id) DO NOTHING"
            )

    def selected_slot(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT leaderboard_slot FROM leaderboard_settings WHERE id = 1"
            ).fetchone()
        if row is None:
            raise ValueError("leaderboard settings are missing")
        self.validate_slot(row[0])
        return row[0]

    def set_selected_slot(self, leaderboard_slot: int) -> None:
        self.validate_slot(leaderboard_slot)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO leaderboard_settings (id, leaderboard_slot) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET leaderboard_slot = excluded.leaderboard_slot",
                (leaderboard_slot,),
            )

    def insert_entries(self, entries: Sequence[NewLeaderboardEntry]) -> set[int]:
        validated = tuple(self._validate(entry) for entry in entries)
        if not validated:
            return set()

        with self._connect() as connection:
            for item in validated:
                connection.execute(
                    """INSERT INTO leaderboard_entries
                       (race_id, player_number, name, duration_ms, created_at, mode_id, leaderboard_slot)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(race_id, player_number) DO NOTHING""",
                    (
                        item.race_id,
                        item.player_number,
                        item.name,
                        item.duration_ms,
                        self.now().isoformat(),
                        item.mode_id,
                        item.leaderboard_slot,
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

    def snapshot_boundary(self, mode_id: str, leaderboard_slot: int = 1) -> int:
        self._validate_mode(mode_id)
        self.validate_slot(leaderboard_slot)
        with self._connect() as connection:
            return connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM leaderboard_entries "
                "WHERE mode_id = ? AND leaderboard_slot = ?",
                (mode_id, leaderboard_slot),
            ).fetchone()[0]

    def page_entries(
        self,
        mode_id: str,
        boundary: int,
        offset: int,
        limit: int = 50,
        leaderboard_slot: int = 1,
    ) -> LeaderboardPage:
        self._validate_mode(mode_id)
        self.validate_slot(leaderboard_slot)
        if type(boundary) is not int or boundary < 0:
            raise ValueError("boundary must be a non-negative integer")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer between 1 and 50")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, race_id, player_number, name, duration_ms, created_at, mode_id, rank, leaderboard_slot
                   FROM (
                       SELECT id, race_id, player_number, name, duration_ms, created_at, mode_id, leaderboard_slot,
                               RANK() OVER (ORDER BY duration_ms) AS rank
                       FROM leaderboard_entries
                       WHERE mode_id = ? AND leaderboard_slot = ? AND id <= ?
                   )
                   ORDER BY duration_ms, created_at, id
                   LIMIT ? OFFSET ?""",
                (mode_id, leaderboard_slot, boundary, limit + 1, offset),
            ).fetchall()
            return LeaderboardPage(
                tuple(RankedLeaderboardEntry(*row) for row in rows[:limit]),
                offset + limit if len(rows) > limit else None,
            )

    @staticmethod
    def validate_slot(leaderboard_slot: object) -> None:
        if type(leaderboard_slot) is not int or not 1 <= leaderboard_slot <= 9:
            raise ValueError("leaderboard_slot must be an integer between 1 and 9")

    @staticmethod
    def _validate_mode(mode_id: str) -> None:
        if not isinstance(mode_id, str) or not mode_id:
            raise ValueError("mode_id is required")

    @staticmethod
    def _validate(entry: NewLeaderboardEntry) -> NewLeaderboardEntry:
        LeaderboardStore._validate_mode(entry.mode_id)
        LeaderboardStore.validate_slot(entry.leaderboard_slot)
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
    mode_id TEXT NOT NULL,
    leaderboard_slot INTEGER NOT NULL DEFAULT 1 CHECK (leaderboard_slot BETWEEN 1 AND 9),
    UNIQUE (race_id, player_number)
);
"""
