import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from leaderboard import LeaderboardStore, NewLeaderboardEntry
from leaderboard_policy import resolve_destination

NOW = datetime(2026, 11, 1, 9, tzinfo=UTC)


def event_data(**changes):
    return {
        "name": " Party ",
        "starts_at": "2026-11-01T10:00:00Z",
        "ends_at": "2026-11-01T12:00:00Z",
    } | changes


@pytest.fixture
def store(tmp_path):
    return LeaderboardStore(tmp_path / "events.sqlite3", now=lambda: NOW)


def create(store, **changes):
    return store.save_event(event_data(**changes), store.load_policy().revision, NOW)


def test_legacy_migration_preserves_every_row_and_setting(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    original = [
        (
            41 + slot,
            f"old-{slot}",
            1,
            "Ada",
            100 + slot,
            "2026-01-01T12:34:56+00:00",
            "quarter-field",
            slot,
        )
        for slot in range(1, 10)
    ]
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE leaderboard_entries (id INTEGER PRIMARY KEY, race_id TEXT NOT NULL,
                player_number INTEGER NOT NULL, name TEXT NOT NULL, duration_ms INTEGER NOT NULL,
                created_at TEXT NOT NULL, mode_id TEXT NOT NULL,
                leaderboard_slot INTEGER NOT NULL CHECK (leaderboard_slot BETWEEN 1 AND 9),
                UNIQUE (race_id, player_number));
            CREATE TABLE leaderboard_settings (id INTEGER PRIMARY KEY, leaderboard_slot INTEGER NOT NULL,
                maintenance_requested INTEGER NOT NULL);
            INSERT INTO leaderboard_settings VALUES (1, 4, 1);
        """)
        db.executemany(
            "INSERT INTO leaderboard_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?)", original
        )
    for _ in range(2):
        migrated = LeaderboardStore(path, now=lambda: NOW)
        snapshot = migrated.load_policy()
        assert [
            (b.id, b.slot, b.name, b.starts_at, b.ends_at) for b in snapshot.boards
        ] == [(i, i, None, None, None) for i in range(1, 10)]
        assert snapshot.manual_slot == 4
        assert snapshot.manual_selected_at == NOW
        assert migrated.maintenance_requested() is True
        with sqlite3.connect(path) as db:
            assert (
                db.execute(
                    "SELECT id, race_id, player_number, name, duration_ms, created_at, mode_id, leaderboard_id FROM leaderboard_entries ORDER BY id"
                ).fetchall()
                == original
            )
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrated.insert_entries(
            [NewLeaderboardEntry("old-1", 1, "Other", 1, "full-field", 9)]
        ) == {42}
        assert migrated.snapshot_boundary("full-field", 9) == 0


def test_same_name_events_are_distinct_empty_boards_with_independent_pbs(store):
    first = create(store)
    second = create(
        store, starts_at="2026-11-01T12:00:00Z", ends_at="2026-11-01T13:00:00Z"
    )
    assert first.id != second.id
    assert first.name == second.name == "Party"
    assert first.slot is second.slot is None
    for board in (first, second):
        assert store.snapshot_boundary("full-field", board.id) == 0
    entry = NewLeaderboardEntry("first", 1, "Ada", 1000, "full-field", first.id)
    store.insert_entries(
        [
            entry,
            replace(entry, race_id="second", duration_ms=2000),
            replace(entry, race_id="other", leaderboard_id=second.id, duration_ms=50),
            replace(entry, race_id="mode", mode_id="quarter-field", duration_ms=20),
        ]
    )
    page = store.page_entries(
        "full-field", 100, 0, leaderboard_id=first.id, race_id="second"
    )
    assert [
        (r.name, r.duration_ms, r.attempts, r.race_duration_ms, r.leaderboard_id)
        for r in page.rows
    ] == [("Ada", 1000, 2, 2000, first.id)]
    assert store.median_duration_ms("full-field", first.id) == 1000
    assert store.median_duration_ms("full-field", second.id) == 50


@pytest.mark.parametrize(
    "changes",
    [
        {"name": None},
        {"name": True},
        {"name": "   "},
        {"name": "x" * 61},
        {"name": "a\nb"},
        {"starts_at": False},
        {"ends_at": []},
        {"starts_at": "2026-11-01T12:00:00Z"},
        {"ends_at": "2026-11-01T09:00:00Z"},
        {"starts_at": "2026-11-01T08:00:00Z"},
        {"start_now": 1},
        {"start_now": "true"},
        {"id": True},
        {"id": 1},
        {"id": 999},
    ],
)
def test_invalid_event_is_atomic(store, changes):
    before = store.load_policy()
    with pytest.raises(ValueError):
        store.save_event(event_data(**changes), before.revision, NOW)
    assert store.load_policy() == before


@pytest.mark.parametrize("revision", [True, None, -1, 1.0, "0", [], 999])
def test_invalid_or_stale_revision_is_atomic(store, revision):
    before = store.load_policy()
    with pytest.raises(ValueError):
        store.save_event(event_data(), revision, NOW)
    assert store.load_policy() == before


def test_overlaps_and_stale_edits_leave_committed_policy_intact(store):
    event = create(store)
    before = store.load_policy()
    with pytest.raises(ValueError, match="overlap"):
        create(store, starts_at="2026-11-01T11:00:00Z", ends_at="2026-11-01T13:00:00Z")
    with pytest.raises(ValueError, match="revision|changed"):
        store.save_event(
            event_data(id=event.id, name="Other"), before.revision - 1, NOW
        )
    assert store.load_policy() == before


def test_event_edit_end_and_cancel_eligibility(store):
    event = create(store)
    event = store.save_event(
        event_data(id=event.id, name="Renamed", ends_at="2026-11-01T13:00:00Z"),
        store.load_policy().revision,
        NOW,
    )
    active = datetime(2026, 11, 1, 11, tzinfo=UTC)
    with pytest.raises(ValueError):
        store.cancel_event(event.id, store.load_policy().revision, active)
    with pytest.raises(ValueError):
        store.save_event(
            event_data(id=event.id, starts_at="2026-11-01T10:01:00Z"),
            store.load_policy().revision,
            active,
        )
    event = store.save_event(
        event_data(id=event.id, ends_at="2026-11-01T14:00:00Z"),
        store.load_policy().revision,
        active,
    )
    ended = store.end_event(event.id, store.load_policy().revision, active)
    assert ended.ends_at == active
    renamed = store.save_event(
        event_data(id=event.id, name="Archive", ends_at=active.isoformat()),
        store.load_policy().revision,
        active,
    )
    assert renamed.id == event.id and renamed.name == "Archive"
    with pytest.raises(ValueError):
        store.save_event(event_data(id=event.id), store.load_policy().revision, active)
    with pytest.raises(ValueError):
        store.end_event(event.id, store.load_policy().revision, active)
    future = create(
        store, starts_at="2026-11-02T10:00:00Z", ends_at="2026-11-02T12:00:00Z"
    )
    store.cancel_event(future.id, store.load_policy().revision, NOW)
    assert future.id not in [b.id for b in store.load_policy().boards]
    fresh = create(
        store, starts_at="2026-11-03T10:00:00Z", ends_at="2026-11-03T12:00:00Z"
    )
    assert fresh.id > future.id
    store.insert_entries(
        [NewLeaderboardEntry("future", 1, "Ada", 1, "full-field", fresh.id)]
    )
    with pytest.raises(ValueError):
        store.cancel_event(fresh.id, store.load_policy().revision, NOW)


def test_start_now_uses_server_time_and_same_slot_selection_renews_timestamp(store):
    store.set_selected_slot(4, selected_at=NOW)
    event = create(store, start_now=True, starts_at=None)
    assert event.starts_at == NOW
    later = datetime(2026, 11, 1, 12, tzinfo=UTC)
    restarted = LeaderboardStore(store.path, now=lambda: later)
    assert resolve_destination(restarted.load_policy(), later).slot == 1
    restarted.set_selected_slot(4, selected_at=later)
    assert (
        resolve_destination(LeaderboardStore(store.path).load_policy(), later).slot == 4
    )


def test_missing_board_rejects_whole_score_transaction(store):
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        store.insert_entries(
            [
                NewLeaderboardEntry("valid", 1, "Ada", 1, "full-field", 1),
                NewLeaderboardEntry("missing", 1, "Ada", 1, "full-field", 999),
            ]
        )
    assert store.snapshot_boundary("full-field", 1) == 0


def test_corrupt_policy_is_not_silently_used(store):
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE leaderboard_settings SET manual_selected_at = 'garbage'")
    with pytest.raises(ValueError):
        store.load_policy()


def test_concurrent_operators_commit_only_one_revision(store):
    ready = threading.Barrier(2)
    accepted, rejected = [], []

    def create_event(hour):
        ready.wait(timeout=3)
        try:
            accepted.append(
                store.save_event(
                    event_data(
                        starts_at=f"2026-11-01T{hour}:00:00Z",
                        ends_at=f"2026-11-01T{hour}:30:00Z",
                    ),
                    0,
                    NOW,
                )
            )
        except ValueError as error:
            rejected.append(str(error))

    workers = [threading.Thread(target=create_event, args=(hour,)) for hour in (10, 11)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    assert len(accepted) == len(rejected) == 1
    assert store.load_policy().revision == 1
    assert (
        len([board for board in store.load_policy().boards if board.slot is None]) == 1
    )


def test_failed_revision_write_rolls_back_event_and_manual_choice(store):
    before = store.load_policy()
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER reject_revision BEFORE UPDATE ON leaderboard_settings BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        create(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.set_selected_slot(4)
    assert store.load_policy() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"starts_at": "2026-11-01T10:30:00Z", "ends_at": "2026-11-01T11:00:00Z"},
        {"starts_at": "2026-11-01T09:00:00Z", "ends_at": "2026-11-01T13:00:00Z"},
    ],
)
def test_contained_and_containing_event_windows_are_overlaps(store, changes):
    create(store)
    with pytest.raises(ValueError, match="overlap"):
        create(store, **changes)


def test_end_now_at_exact_start_rejects_atomically_then_positive_instant_succeeds(
    store,
):
    event = create(store, start_now=True)
    store.insert_entries(
        [NewLeaderboardEntry("captured", 1, "Ada", 1000, "full-field", event.id)]
    )
    before = store.load_policy()
    with sqlite3.connect(store.path) as db:
        scores_before = db.execute("SELECT * FROM leaderboard_entries").fetchall()
    with pytest.raises(ValueError, match="nonempty window"):
        store.end_event(event.id, before.revision, event.starts_at)
    assert store.load_policy() == before
    with sqlite3.connect(store.path) as db:
        assert (
            db.execute("SELECT * FROM leaderboard_entries").fetchall() == scores_before
        )
    later = event.starts_at + timedelta(microseconds=1)
    ended = store.end_event(event.id, before.revision, later)
    assert ended == replace(event, ends_at=later)
    after = store.load_policy()
    assert after.revision == before.revision + 1
    assert after.boards == tuple(
        ended if b.id == event.id else b for b in before.boards
    )
    assert after.manual_slot == before.manual_slot
    assert after.manual_selected_at == before.manual_selected_at
    with sqlite3.connect(store.path) as db:
        assert (
            db.execute("SELECT * FROM leaderboard_entries").fetchall() == scores_before
        )
