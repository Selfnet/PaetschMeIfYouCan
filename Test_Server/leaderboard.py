import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from statistics import median

from leaderboard_policy import (
    Leaderboard,
    LeaderboardPolicy,
    parse_event_time,
    utc_time,
)


@dataclass(frozen=True)
class NewLeaderboardEntry:
    race_id: str
    player_number: int
    name: str
    duration_ms: int
    mode_id: str
    leaderboard_id: int = 1


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
    leaderboard_id: int = 1
    attempts: int = 1
    latest_race_id: str = ""
    latest_duration_ms: int = 0
    best_improved: bool = True
    race_duration_ms: int | None = None


@dataclass(frozen=True)
class LeaderboardPage:
    rows: tuple[RankedLeaderboardEntry, ...]
    next_offset: int | None
    target_score_id: int | None = None


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
        connection.execute("PRAGMA foreign_keys = ON")
        connection.create_function("casefold", 1, str.casefold, deterministic=True)
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(CATALOG_SCHEMA)
            connection.executemany(
                "INSERT INTO leaderboards (id, slot) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                [(slot, slot) for slot in range(1, 10)],
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(leaderboard_entries)")
            }
            if columns and "leaderboard_id" not in columns:
                connection.execute(
                    "ALTER TABLE leaderboard_entries RENAME TO legacy_entries"
                )
                connection.execute(SCHEMA)
                mode = "mode_id" if "mode_id" in columns else "'full-field'"
                slot = "leaderboard_slot" if "leaderboard_slot" in columns else "1"
                connection.execute(
                    "INSERT INTO leaderboard_entries "
                    "(id, race_id, player_number, name, duration_ms, created_at, mode_id, leaderboard_id) "
                    f"SELECT id, race_id, player_number, name, duration_ms, created_at, {mode}, {slot} FROM legacy_entries"
                )
                connection.execute("DROP TABLE legacy_entries")
            else:
                connection.execute(SCHEMA)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS leaderboard_id_mode_time_idx "
                "ON leaderboard_entries (leaderboard_id, mode_id, duration_ms, created_at, id)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS leaderboard_settings ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "leaderboard_slot INTEGER NOT NULL CHECK (leaderboard_slot BETWEEN 1 AND 9))"
            )
            settings_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(leaderboard_settings)")
            }
            if "maintenance_requested" not in settings_columns:
                connection.execute(
                    "ALTER TABLE leaderboard_settings ADD COLUMN maintenance_requested "
                    "INTEGER NOT NULL DEFAULT 0 CHECK (maintenance_requested IN (0, 1))"
                )
            if "manual_selected_at" not in settings_columns:
                connection.execute(
                    "ALTER TABLE leaderboard_settings ADD COLUMN manual_selected_at TEXT NOT NULL DEFAULT ''"
                )
                connection.execute(
                    "UPDATE leaderboard_settings SET manual_selected_at = ?",
                    (utc_time(self.now()).isoformat(),),
                )
            if "policy_revision" not in settings_columns:
                connection.execute(
                    "ALTER TABLE leaderboard_settings ADD COLUMN policy_revision INTEGER NOT NULL DEFAULT 0 CHECK (policy_revision >= 0)"
                )
            connection.execute(
                "INSERT INTO leaderboard_settings (id, leaderboard_slot, maintenance_requested, manual_selected_at) "
                "VALUES (1, 1, 0, ?) ON CONFLICT(id) DO NOTHING",
                (utc_time(self.now()).isoformat(),),
            )

    def load_policy(self) -> LeaderboardPolicy:
        with self._connect() as connection:
            connection.execute("BEGIN")
            return self._load_policy(connection)

    def _load_policy(self, connection) -> LeaderboardPolicy:
        settings = connection.execute(
            "SELECT leaderboard_slot, manual_selected_at, policy_revision FROM leaderboard_settings WHERE id = 1"
        ).fetchone()
        if settings is None:
            raise ValueError("Leaderboard settings are missing")
        slot, selected_at, revision = settings
        self.validate_slot(slot)
        if type(revision) is not int or revision < 0:
            raise ValueError("Invalid policy revision")
        boards = []
        for identity, numbered, name, start, end in connection.execute(
            "SELECT id, slot, name, starts_at, ends_at FROM leaderboards ORDER BY id"
        ):
            self.validate_id(identity)
            if numbered is not None:
                self.validate_slot(numbered)
                if (name, start, end) != (None, None, None):
                    raise ValueError("Invalid numbered leaderboard")
            else:
                if self._event_name(name) != name:
                    raise ValueError("Invalid event name")
                start, end = self._stored_time(start), self._stored_time(end)
                if end <= start:
                    raise ValueError("Invalid event window")
            boards.append(Leaderboard(identity, numbered, name, start, end))
        if sorted(board.slot for board in boards if board.slot is not None) != list(
            range(1, 10)
        ):
            raise ValueError("Numbered leaderboards are missing")
        events = sorted(
            (board for board in boards if board.slot is None),
            key=lambda board: board.starts_at,
        )
        if any(first.ends_at > second.starts_at for first, second in pairwise(events)):
            raise ValueError("Event windows overlap")
        return LeaderboardPolicy(
            tuple(boards), slot, self._stored_time(selected_at), revision
        )

    @staticmethod
    def _stored_time(value):
        if not isinstance(value, str):
            raise ValueError("Invalid stored time")  # noqa: TRY004 - invalid storage data
        return utc_time(datetime.fromisoformat(value))

    @staticmethod
    def _event_name(value):
        if (
            not isinstance(value, str)
            or not value.isprintable()
            or not 1 <= len(value.strip()) <= 60
        ):
            raise ValueError("Event name must contain 1 to 60 printable characters")
        return value.strip()

    def _edit_policy(self, connection, expected_revision):
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**53:
            raise ValueError("Invalid policy revision")
        connection.execute("BEGIN IMMEDIATE")
        policy = self._load_policy(connection)
        if expected_revision != policy.revision:
            raise ValueError("Schedule changed; reload before editing")
        return policy

    @staticmethod
    def _bump_revision(connection):
        connection.execute(
            "UPDATE leaderboard_settings SET policy_revision = policy_revision + 1 WHERE id = 1"
        )

    def _find_event(self, policy, event_id):
        self.validate_id(event_id)
        event = next(
            (
                board
                for board in policy.boards
                if board.id == event_id and board.slot is None
            ),
            None,
        )
        if event is None:
            raise ValueError("Event does not exist")
        return event

    def save_event(
        self, data: dict, expected_revision: int, now: datetime
    ) -> Leaderboard:
        now = utc_time(now)
        if not isinstance(data, dict) or type(data.get("start_now", False)) is not bool:
            raise ValueError("Invalid event data")
        name = self._event_name(data.get("name"))
        start = (
            now if data.get("start_now") else parse_event_time(data.get("starts_at"))
        )
        end = parse_event_time(data.get("ends_at"))
        if end <= start:
            raise ValueError("Event end must be later than its start")
        with self._connect() as connection:
            policy = self._edit_policy(connection, expected_revision)
            existing = self._find_event(policy, data["id"]) if "id" in data else None
            if existing is None or now < existing.starts_at:
                if start < now or end <= now:
                    raise ValueError(
                        "New event windows must start now or in the future"
                    )
            elif now < existing.ends_at:
                if start != existing.starts_at or end <= now or data.get("start_now"):
                    raise ValueError(
                        "Active events keep their start and must end in the future; use End now"
                    )
            elif (start, end) != (existing.starts_at, existing.ends_at) or data.get(
                "start_now"
            ):
                raise ValueError("Ended event windows cannot be changed")
            if any(
                board.slot is None
                and board != existing
                and start < board.ends_at
                and board.starts_at < end
                for board in policy.boards
            ):
                raise ValueError("Event windows overlap")
            values = (name, start.isoformat(), end.isoformat())
            if existing is None:
                identity = connection.execute(
                    "INSERT INTO leaderboards (name, starts_at, ends_at) VALUES (?, ?, ?)",
                    values,
                ).lastrowid
            else:
                identity = existing.id
                connection.execute(
                    "UPDATE leaderboards SET name = ?, starts_at = ?, ends_at = ? WHERE id = ?",
                    (*values, identity),
                )
            self._bump_revision(connection)
            return Leaderboard(identity, None, name, start, end)

    def cancel_event(
        self, event_id: int, expected_revision: int, now: datetime
    ) -> None:
        now = utc_time(now)
        with self._connect() as connection:
            policy = self._edit_policy(connection, expected_revision)
            event = self._find_event(policy, event_id)
            has_scores = connection.execute(
                "SELECT 1 FROM leaderboard_entries WHERE leaderboard_id = ? LIMIT 1",
                (event_id,),
            ).fetchone()
            if now >= event.starts_at or has_scores:
                raise ValueError("Only future empty events can be cancelled")
            connection.execute("DELETE FROM leaderboards WHERE id = ?", (event_id,))
            self._bump_revision(connection)

    def end_event(
        self, event_id: int, expected_revision: int, now: datetime
    ) -> Leaderboard:
        now = utc_time(now)
        with self._connect() as connection:
            policy = self._edit_policy(connection, expected_revision)
            event = self._find_event(policy, event_id)
            if not event.starts_at < now < event.ends_at:
                raise ValueError(
                    "Only active events with a nonempty window can end now"
                )
            connection.execute(
                "UPDATE leaderboards SET ends_at = ? WHERE id = ?",
                (now.isoformat(), event_id),
            )
            self._bump_revision(connection)
            return Leaderboard(event.id, None, event.name, event.starts_at, now)

    def selected_slot(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT leaderboard_slot FROM leaderboard_settings WHERE id = 1"
            ).fetchone()
        if row is None:
            raise ValueError("leaderboard settings are missing")
        self.validate_slot(row[0])
        return row[0]

    def set_selected_slot(
        self, slot: int, *, selected_at: datetime | None = None
    ) -> None:
        self.validate_slot(slot)
        selected_at = utc_time(selected_at if selected_at is not None else self.now())
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE leaderboard_settings SET leaderboard_slot = ?, manual_selected_at = ?, "
                "policy_revision = policy_revision + 1 WHERE id = 1",
                (slot, selected_at.isoformat()),
            )
            if cursor.rowcount != 1:
                raise ValueError("Leaderboard settings are missing")

    def maintenance_requested(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT maintenance_requested FROM leaderboard_settings WHERE id = 1"
            ).fetchone()
        if row is None or row[0] not in (0, 1):
            raise ValueError("maintenance setting is missing or invalid")
        return bool(row[0])

    def set_maintenance_requested(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("maintenance setting must be a boolean")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE leaderboard_settings SET maintenance_requested = ? WHERE id = 1",
                (enabled,),
            )
            if cursor.rowcount != 1:
                raise ValueError("maintenance setting is missing")

    def insert_entries(self, entries: Sequence[NewLeaderboardEntry]) -> set[int]:
        validated = tuple(self._validate(entry) for entry in entries)
        if not validated:
            return set()

        with self._connect() as connection:
            for item in validated:
                connection.execute(
                    """INSERT INTO leaderboard_entries
                       (race_id, player_number, name, duration_ms, created_at, mode_id, leaderboard_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(race_id, player_number) DO NOTHING""",
                    (
                        item.race_id,
                        item.player_number,
                        item.name,
                        item.duration_ms,
                        self.now().isoformat(),
                        item.mode_id,
                        item.leaderboard_id,
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

    def median_duration_ms(self, mode_id: str, leaderboard_id: int = 1) -> float | None:
        self._validate_mode(mode_id)
        self.validate_id(leaderboard_id)
        with self._connect() as connection:
            times = [
                row[0]
                for row in connection.execute(
                    "SELECT MIN(duration_ms) FROM leaderboard_entries "
                    "WHERE mode_id = ? AND leaderboard_id = ? "
                    "GROUP BY casefold(name) HAVING MIN(duration_ms) > 0",
                    (mode_id, leaderboard_id),
                )
            ]
        return median(times) if times else None

    def snapshot_boundary(self, mode_id: str, leaderboard_id: int = 1) -> int:
        self._validate_mode(mode_id)
        self.validate_id(leaderboard_id)
        with self._connect() as connection:
            return connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM leaderboard_entries "
                "WHERE mode_id = ? AND leaderboard_id = ?",
                (mode_id, leaderboard_id),
            ).fetchone()[0]

    def page_entries(
        self,
        mode_id: str,
        boundary: int,
        offset: int,
        limit: int = 50,
        leaderboard_id: int = 1,
        race_id: str | None = None,
    ) -> LeaderboardPage:
        self._validate_mode(mode_id)
        self.validate_id(leaderboard_id)
        if race_id is not None and (not isinstance(race_id, str) or not race_id):
            raise ValueError("race_id must be a non-empty string or None")
        if type(boundary) is not int or boundary < 0:
            raise ValueError("boundary must be a non-negative integer")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer between 1 and 50")
        with self._connect() as connection:
            target = None
            if race_id is not None:
                target = connection.execute(
                    """WITH scoped AS (
                           SELECT * FROM leaderboard_entries
                           WHERE mode_id = ? AND leaderboard_id = ? AND id <= ?
                       )
                       SELECT id FROM scoped
                       WHERE casefold(name) = (
                           SELECT casefold(name) FROM scoped WHERE race_id = ?
                           ORDER BY duration_ms, player_number LIMIT 1
                       )
                       ORDER BY duration_ms, id DESC LIMIT 1""",
                    (mode_id, leaderboard_id, boundary, race_id),
                ).fetchone()
            rows = connection.execute(
                """WITH attempts AS (
                       SELECT *,
                              COUNT(*) OVER (PARTITION BY casefold(name)) AS attempts,
                              FIRST_VALUE(name) OVER (
                                  PARTITION BY casefold(name) ORDER BY id DESC
                              ) AS latest_name,
                              FIRST_VALUE(race_id) OVER (
                                  PARTITION BY casefold(name) ORDER BY id DESC
                              ) AS latest_race_id,
                              FIRST_VALUE(duration_ms) OVER (
                                  PARTITION BY casefold(name) ORDER BY id DESC
                              ) AS latest_duration_ms,
                              ROW_NUMBER() OVER (
                                  PARTITION BY casefold(name) ORDER BY duration_ms, id DESC
                              ) AS best
                       FROM leaderboard_entries
                       WHERE mode_id = ? AND leaderboard_id = ? AND id <= ?
                   )
                   SELECT id, race_id, player_number, latest_name, duration_ms, created_at,
                          mode_id, rank, leaderboard_id, attempts, latest_race_id,
                          latest_duration_ms,
                          NOT EXISTS (
                              SELECT 1 FROM attempts prior
                              WHERE casefold(prior.name) = casefold(ranked.name)
                                AND prior.race_id != ranked.race_id
                                AND prior.duration_ms <= ranked.duration_ms
                          ),
                          (SELECT duration_ms FROM attempts current_attempt
                           WHERE current_attempt.race_id = ?
                             AND casefold(current_attempt.name) = casefold(ranked.name)
                           ORDER BY current_attempt.id DESC LIMIT 1)
                   FROM (
                       SELECT *,
                                RANK() OVER (ORDER BY duration_ms) AS rank
                       FROM attempts
                       WHERE best = 1
                   ) ranked
                   ORDER BY duration_ms, created_at, id
                   LIMIT ? OFFSET ?""",
                (mode_id, leaderboard_id, boundary, race_id, limit + 1, offset),
            ).fetchall()
            return LeaderboardPage(
                tuple(RankedLeaderboardEntry(*row) for row in rows[:limit]),
                offset + limit if len(rows) > limit else None,
                target[0] if target else None,
            )

    @staticmethod
    def validate_id(leaderboard_id: object) -> None:
        if type(leaderboard_id) is not int or not 1 <= leaderboard_id < 2**53:
            raise ValueError("leaderboard_id must be a positive safe integer")

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
        LeaderboardStore.validate_id(entry.leaderboard_id)
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


CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS leaderboards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot INTEGER UNIQUE,
    name TEXT,
    starts_at TEXT,
    ends_at TEXT,
    CHECK (
        (slot IS NOT NULL AND slot BETWEEN 1 AND 9 AND name IS NULL AND starts_at IS NULL AND ends_at IS NULL)
        OR (slot IS NULL AND name IS NOT NULL AND length(name) BETWEEN 1 AND 60
            AND starts_at IS NOT NULL AND ends_at IS NOT NULL AND starts_at < ends_at)
    )
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS leaderboard_entries (
    id INTEGER PRIMARY KEY,
    race_id TEXT NOT NULL,
    player_number INTEGER NOT NULL CHECK (player_number IN (1, 2)),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 12),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    created_at TEXT NOT NULL,
    mode_id TEXT NOT NULL,
    leaderboard_id INTEGER NOT NULL DEFAULT 1 REFERENCES leaderboards(id),
    UNIQUE (race_id, player_number)
);
"""
