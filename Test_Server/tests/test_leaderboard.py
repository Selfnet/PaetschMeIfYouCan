import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from leaderboard import LeaderboardStore, NewLeaderboardEntry, default_database_path


class AdvancingUtcClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(microseconds=1)
        return value


def entry(
    race_id: str, player: int, name: str, duration_ms: int, mode_id: str = "full-field"
) -> NewLeaderboardEntry:
    return NewLeaderboardEntry(race_id, player, name, duration_ms, mode_id)


def test_default_path_uses_service_home_and_environment_override(tmp_path, monkeypatch):
    monkeypatch.delenv("LEADERBOARD_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert (
        default_database_path()
        == tmp_path / ".local/share/patchmeifyoucan/leaderboard.sqlite3"
    )

    override = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("LEADERBOARD_DB", str(override))
    assert default_database_path() == override


def test_store_initializes_and_reopens_existing_database(tmp_path):
    path = tmp_path / "scores" / "leaderboard.sqlite3"
    clock = AdvancingUtcClock()
    first = LeaderboardStore(path, now=clock)
    inserted = first.insert_entries([entry("race-1", 1, "Ada", 1234)])

    second = LeaderboardStore(path, now=clock)

    assert len(inserted) == 1
    assert [
        (row.name, row.duration_ms)
        for row in second.page_entries(
            "full-field", second.snapshot_boundary("full-field"), 0
        ).rows
    ] == [("Ada", 1234)]


def test_insert_is_atomic_and_duplicate_race_player_is_idempotent(tmp_path):
    store = LeaderboardStore(tmp_path / "scores.sqlite3", now=AdvancingUtcClock())
    submissions = [
        entry("race-1", 1, "Ada", 1000),
        entry("race-1", 2, "Grace", 1100),
    ]

    assert store.insert_entries([]) == set()
    first_ids = store.insert_entries(submissions)
    retry_ids = store.insert_entries(submissions)

    assert retry_ids == first_ids
    assert [
        row.name
        for row in store.page_entries(
            "full-field", store.snapshot_boundary("full-field"), 0
        ).rows
    ] == ["Ada", "Grace"]


@pytest.mark.parametrize(
    "submission",
    [
        ("", 1, "Ada", 1),
        (True, 1, "Ada", 1),
        ("race", 0, "Ada", 1),
        ("race", 3, "Ada", 1),
        ("race", True, "Ada", 1),
        ("race", 1, "", 1),
        ("race", 1, " " * 2, 1),
        ("race", 1, "A" * 13, 1),
        ("race", 1, "Ada\n", 1),
        ("race", 1, "Ada", -1),
        ("race", 1, "Ada", True),
        ("race", 1, "Ada", 1, ""),
        ("race", 1, "Ada", 1, None),
        ("race", 1, "Ada", 1, True),
    ],
)
def test_insert_rejects_invalid_rows_without_partial_writes(tmp_path, submission):
    store = LeaderboardStore(tmp_path / "scores.sqlite3", now=AdvancingUtcClock())

    with pytest.raises(ValueError):
        store.insert_entries([entry("valid", 1, "Valid", 10), entry(*submission)])

    assert (
        store.page_entries("full-field", store.snapshot_boundary("full-field"), 0).rows
        == ()
    )


def test_limited_page_uses_stable_order_and_competition_ranks(tmp_path):
    store = LeaderboardStore(tmp_path / "scores.sqlite3", now=AdvancingUtcClock())
    store.insert_entries(
        [
            entry(f"race-{index}", 1, f"P{index}", duration)
            for index, duration in enumerate(
                [100, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100]
            )
        ]
    )

    rows = store.page_entries(
        "full-field", store.snapshot_boundary("full-field"), 0, 10
    ).rows

    assert len(rows) == 10
    assert [(row.rank, row.name, row.duration_ms) for row in rows[:3]] == [
        (1, "P0", 100),
        (1, "P1", 100),
        (3, "P2", 200),
    ]
    assert rows[-1].duration_ms == 900


def test_names_collapse_using_best_time_and_unicode_casefold(tmp_path):
    store = LeaderboardStore(tmp_path / "scores.sqlite3", now=AdvancingUtcClock())
    store.insert_entries(
        [
            entry("old", 1, "Ada", 100),
            entry("old", 2, "Grace", 200),
            entry("new", 1, "ADA", 300),
            entry("new", 2, "ada", 400),
            entry("unicode-old", 1, "Straße", 500),
            entry("unicode-new", 2, "STRASSE", 200),
            entry("accent-old", 1, "Änne", 600),
            entry("accent-new", 1, "änne", 700),
        ]
    )
    store.insert_entries([entry("new", 2, "ada", 400)])
    reopened = LeaderboardStore(store.path)
    rows = reopened.page_entries(
        "full-field", reopened.snapshot_boundary("full-field"), 0
    ).rows
    assert [(r.rank, r.name, r.duration_ms, r.attempts) for r in rows] == [
        (1, "ada", 100, 3),
        (2, "Grace", 200, 1),
        (2, "STRASSE", 200, 2),
        (4, "änne", 600, 2),
    ]
    assert rows[0].race_id == "old"
    assert rows[0].player_number == 1
    assert (rows[0].latest_race_id, rows[0].latest_duration_ms) == ("new", 400)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM leaderboard_entries"
        ).fetchone() == (8,)


def test_grouped_pages_count_players_and_freeze_best_and_latest_attempts(tmp_path):
    store = LeaderboardStore(
        tmp_path / "scores.sqlite3", now=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    for prefix, name_prefix, duration_base in [("old", "P", 0), ("new", "p", 1000)]:
        store.insert_entries(
            [
                entry(f"{prefix}-{i}", 1, f"{name_prefix}{i}", duration_base + i)
                for i in range(60)
            ]
        )
    boundary = store.snapshot_boundary("full-field")
    first = store.page_entries("full-field", boundary, 0)
    store.insert_entries(
        [
            entry("later", 1, "P55", 0),
            entry("other-mode", 1, "P55", 0, "quarter-field"),
            NewLeaderboardEntry("other-slot", 1, "P55", 0, "full-field", 2),
        ]
    )
    second = store.page_entries("full-field", boundary, first.next_offset)
    assert first.next_offset == 50
    assert second.next_offset is None
    rows = first.rows + second.rows
    assert [(r.rank, r.name, r.duration_ms, r.attempts) for r in rows] == [
        (i + 1, f"p{i}", i, 2) for i in range(60)
    ]
    assert store.page_entries("full-field", boundary, 0) == first
    assert [r.latest_duration_ms for r in rows] == [1000 + i for i in range(60)]
    current = store.page_entries("full-field", store.snapshot_boundary("full-field"), 0)
    improved = next(row for row in current.rows if row.name == "P55")
    assert (improved.rank, improved.duration_ms, improved.attempts) == (1, 0, 3)
    for mode, slot in [("quarter-field", 1), ("full-field", 2)]:
        isolated = store.page_entries(
            mode, store.snapshot_boundary(mode, slot), 0, leaderboard_slot=slot
        )
        assert [(r.name, r.attempts) for r in isolated.rows] == [("P55", 1)]


def test_equal_best_uses_latest_matching_timestamp_without_improvement(tmp_path):
    store = LeaderboardStore(tmp_path / "scores.sqlite3", now=AdvancingUtcClock())
    store.insert_entries([entry("first", 1, "Ada", 100)])
    store.insert_entries([entry("equal", 2, "ADA", 100)])
    store.insert_entries([entry("worse", 1, "ada", 200)])
    row = store.page_entries(
        "full-field", store.snapshot_boundary("full-field"), 0
    ).rows[0]
    assert (row.race_id, row.duration_ms, row.attempts) == ("equal", 100, 3)
    assert (row.latest_race_id, row.latest_duration_ms) == ("worse", 200)
    assert not row.best_improved


def test_same_name_both_players_share_row_and_new_personal_best(tmp_path):
    store = LeaderboardStore(tmp_path / "scores.sqlite3")
    store.insert_entries([entry("old", 1, "Ada", 200)])
    store.insert_entries(
        [entry("current", 1, "ADA", 100), entry("current", 2, "ada", 100)]
    )
    row = store.page_entries(
        "full-field", store.snapshot_boundary("full-field"), 0, race_id="current"
    ).rows[0]
    assert (row.duration_ms, row.race_duration_ms, row.attempts) == (100, 100, 3)
    assert row.best_improved


@pytest.fixture
def legacy_database(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE leaderboard_entries (
                id INTEGER PRIMARY KEY,
                race_id TEXT NOT NULL,
                player_number INTEGER NOT NULL CHECK (player_number IN (1, 2)),
                name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 12),
                duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
                created_at TEXT NOT NULL,
                UNIQUE (race_id, player_number)
            );
            CREATE INDEX leaderboard_time_idx
                ON leaderboard_entries (duration_ms, created_at, id);
            INSERT INTO leaderboard_entries VALUES
                (7, 'old-race', 1, 'Ada', 100, '2026-01-01T00:00:00+00:00'),
                (42, 'old-race', 2, 'Grace', 200, '2026-01-02T00:00:00+00:00');
        """)
    return path


def test_legacy_migration_preserves_data_and_uniqueness_across_reopens(legacy_database):
    for _ in range(3):
        store = LeaderboardStore(legacy_database)
        rows = store.page_entries(
            "full-field", store.snapshot_boundary("full-field"), 0
        ).rows
        assert [
            (
                r.id,
                r.race_id,
                r.player_number,
                r.name,
                r.duration_ms,
                r.created_at,
                r.mode_id,
                r.rank,
            )
            for r in rows
        ] == [
            (
                7,
                "old-race",
                1,
                "Ada",
                100,
                "2026-01-01T00:00:00+00:00",
                "full-field",
                1,
            ),
            (
                42,
                "old-race",
                2,
                "Grace",
                200,
                "2026-01-02T00:00:00+00:00",
                "full-field",
                2,
            ),
        ]
        assert store.insert_entries(
            [entry("old-race", 1, "Changed", 1, "quarter-field")]
        ) == {7}
        assert store.snapshot_boundary("quarter-field") == 0
        with (
            sqlite3.connect(legacy_database) as connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
            connection.execute(
                "INSERT INTO leaderboard_entries (race_id, player_number, name, duration_ms, created_at, mode_id) VALUES ('old-race', 1, 'Ada', 1, 'now', 'quarter-field')"
            )


def test_migration_rolls_back_alter_table_when_index_creation_fails(legacy_database):
    class FailingMigrationStore(LeaderboardStore):
        def _connect(self):
            connection = super()._connect()
            connection.set_authorizer(
                lambda action, *_: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_CREATE_INDEX
                    else sqlite3.SQLITE_OK
                )
            )
            return connection

    with pytest.raises(sqlite3.DatabaseError):
        FailingMigrationStore(legacy_database)

    with sqlite3.connect(legacy_database) as connection:
        assert [
            row[1]
            for row in connection.execute("PRAGMA table_info(leaderboard_entries)")
        ] == ["id", "race_id", "player_number", "name", "duration_ms", "created_at"]
        assert connection.execute(
            "SELECT id, name FROM leaderboard_entries ORDER BY id"
        ).fetchall() == [(7, "Ada"), (42, "Grace")]
    assert LeaderboardStore(legacy_database).snapshot_boundary("full-field") == 42


def test_insert_sql_failure_rolls_back_entire_batch(legacy_database):
    store = LeaderboardStore(legacy_database)
    with sqlite3.connect(legacy_database) as connection:
        connection.execute("""CREATE TRIGGER reject_bad BEFORE INSERT ON leaderboard_entries
            WHEN NEW.race_id = 'bad' BEGIN SELECT RAISE(ABORT, 'forced failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        store.insert_entries([entry("good", 1, "Good", 1), entry("bad", 1, "Bad", 2)])
    assert store.snapshot_boundary("full-field") == 42
    assert [r.id for r in store.page_entries("full-field", 42, 0).rows] == [7, 42]


def test_pages_preserve_complete_snapshot_ranks_ties_and_mode_isolation(tmp_path):
    store = LeaderboardStore(
        tmp_path / "scores.sqlite3", now=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    durations = [1000 + i if i < 49 or i > 51 else 1049 for i in range(121)]
    store.insert_entries(
        [
            entry(f"race-{i}", 1, f"P{i}", duration)
            for i, duration in enumerate(durations)
        ]
    )
    boundary = store.snapshot_boundary("full-field")
    original = [
        store.page_entries("full-field", boundary, offset).rows
        for offset in (0, 50, 100)
    ]
    other = LeaderboardStore(store.path)
    other.insert_entries(
        [entry("other", 1, "Other", 0, "quarter-field"), entry("later", 1, "Later", 0)]
    )
    pages = [
        store.page_entries("full-field", boundary, offset) for offset in (0, 50, 100)
    ]
    assert [p.rows for p in pages] == original
    assert [len(p.rows) for p in pages] == [50, 50, 21]
    assert [p.next_offset for p in pages] == [50, 100, None]
    rows = [row for page in pages for row in page.rows]
    assert len({r.id for r in rows}) == 121
    assert [r.name for r in rows] == [f"P{i}" for i in range(121)]
    assert [r.rank for r in rows] == [
        1 + sum(d < duration for d in durations) for duration in durations
    ]
    assert [r.rank for r in rows[49:53]] == [50, 50, 50, 53]
    assert all(r.mode_id == "full-field" for r in rows)
    assert store.page_entries("full-field", boundary, 121).rows == ()
    assert store.page_entries("full-field", boundary, 999).next_offset is None
    assert [
        r.name
        for r in store.page_entries(
            "quarter-field", store.snapshot_boundary("quarter-field"), 0
        ).rows
    ] == ["Other"]


def test_empty_snapshot_stays_empty(tmp_path):
    store = LeaderboardStore(tmp_path / "scores.sqlite3")
    boundary = store.snapshot_boundary("quarter-field")
    assert boundary == 0
    store.insert_entries([entry("later", 1, "Ada", 1, "quarter-field")])
    page = store.page_entries("quarter-field", boundary, 0)
    assert page.rows == ()
    assert page.next_offset is None


@pytest.mark.parametrize("field", ["boundary", "offset", "limit"])
@pytest.mark.parametrize("value", [True, False, -1, 1.5, "1", None])
def test_page_rejects_invalid_bounds(tmp_path, field, value):
    store = LeaderboardStore(tmp_path / "scores.sqlite3")
    bounds = {"boundary": 0, "offset": 0, "limit": 50}
    bounds[field] = value
    with pytest.raises(ValueError):
        store.page_entries("full-field", **bounds)


@pytest.mark.parametrize("limit", [0, 51])
def test_page_rejects_out_of_range_limits(tmp_path, limit):
    store = LeaderboardStore(tmp_path / "scores.sqlite3")
    with pytest.raises(ValueError):
        store.page_entries("full-field", 0, 0, limit)


@pytest.mark.parametrize("mode_id", ["", None, True, 1])
def test_queries_reject_invalid_modes_before_connecting(tmp_path, mode_id, monkeypatch):
    store = LeaderboardStore(tmp_path / "scores.sqlite3")

    def unexpected_connection():
        pytest.fail("invalid mode reached SQL")

    monkeypatch.setattr(store, "_connect", unexpected_connection)
    with pytest.raises(ValueError):
        store.snapshot_boundary(mode_id)
    with pytest.raises(ValueError):
        store.page_entries(mode_id, 0, 0)


def test_modes_are_required_and_retired_identities_remain_readable(tmp_path):
    with pytest.raises(TypeError):
        NewLeaderboardEntry("race", 1, "Ada", 1)
    store = LeaderboardStore(tmp_path / "scores.sqlite3")
    ids = store.insert_entries([entry("old-mode", 1, "Ada", 0, "retired-mode-v1")])
    boundary = store.snapshot_boundary("retired-mode-v1")
    page = store.page_entries("retired-mode-v1", boundary, 0, 1)
    assert {r.id for r in page.rows} == ids
    assert page.next_offset is None
    with pytest.raises(TypeError):
        store.snapshot_boundary()
