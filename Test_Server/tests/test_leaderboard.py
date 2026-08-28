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
    race_id: str, player: int, name: str, duration_ms: int
) -> NewLeaderboardEntry:
    return NewLeaderboardEntry(race_id, player, name, duration_ms)


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
    assert [(row.name, row.duration_ms) for row in second.top_entries()] == [
        ("Ada", 1234)
    ]


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
    assert [row.name for row in store.top_entries()] == ["Ada", "Grace"]


@pytest.mark.parametrize(
    "submission",
    [
        entry("", 1, "Ada", 1),
        entry(True, 1, "Ada", 1),
        entry("race", 0, "Ada", 1),
        entry("race", 3, "Ada", 1),
        entry("race", True, "Ada", 1),
        entry("race", 1, "", 1),
        entry("race", 1, " " * 2, 1),
        entry("race", 1, "A" * 13, 1),
        entry("race", 1, "Ada\n", 1),
        entry("race", 1, "Ada", -1),
        entry("race", 1, "Ada", True),
    ],
)
def test_insert_rejects_invalid_rows_without_partial_writes(tmp_path, submission):
    store = LeaderboardStore(tmp_path / "scores.sqlite3", now=AdvancingUtcClock())

    with pytest.raises(ValueError):
        store.insert_entries([entry("valid", 1, "Valid", 10), submission])

    assert store.top_entries() == []


def test_top_ten_uses_stable_order_and_competition_ranks(tmp_path):
    store = LeaderboardStore(tmp_path / "scores.sqlite3", now=AdvancingUtcClock())
    store.insert_entries(
        [
            entry(f"race-{index}", 1, f"P{index}", duration)
            for index, duration in enumerate(
                [100, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100]
            )
        ]
    )

    rows = store.top_entries()

    assert len(rows) == 10
    assert [(row.rank, row.name, row.duration_ms) for row in rows[:3]] == [
        (1, "P0", 100),
        (1, "P1", 100),
        (3, "P2", 200),
    ]
    assert rows[-1].duration_ms == 900
