from datetime import UTC, datetime

import pytest

from leaderboard_policy import (
    Leaderboard,
    LeaderboardPolicy,
    active_event,
    parse_event_time,
    resolve_destination,
)


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def policy(*events, selected="2026-11-01T09:00:00", slot=4):
    return LeaderboardPolicy(
        tuple(Leaderboard(i, i, None, None, None) for i in range(1, 10)) + events,
        slot,
        utc(selected),
        0,
    )


@pytest.mark.parametrize(
    "now,want,active",
    [
        ("2026-11-01T09:59:59.999999", 4, None),
        ("2026-11-01T10:00:00", 10, 10),
        ("2026-11-01T11:59:59.999999", 10, 10),
        ("2026-11-01T12:00:00", 1, None),
        ("2026-11-02T00:00:00", 1, None),
    ],
)
def test_half_open_event_and_offline_expiry(now, want, active):
    event = Leaderboard(
        10, None, "Party", utc("2026-11-01T10:00:00"), utc("2026-11-01T12:00:00")
    )
    snapshot = policy(event)
    assert resolve_destination(snapshot, utc(now)).id == want
    result = active_event(snapshot, utc(now))
    assert (result.id if result else None) == active


def test_adjacent_event_wins_over_expiry_fallback():
    snapshot = policy(
        Leaderboard(
            10, None, "First", utc("2026-11-01T10:00:00"), utc("2026-11-01T12:00:00")
        ),
        Leaderboard(
            11, None, "Next", utc("2026-11-01T12:00:00"), utc("2026-11-01T13:00:00")
        ),
    )
    assert resolve_destination(snapshot, utc("2026-11-01T12:00:00")).id == 11


@pytest.mark.parametrize("selected", ["2026-11-01T12:00:00", "2026-11-01T12:00:01"])
def test_manual_choice_at_or_after_expiry_survives_restart(selected):
    event = Leaderboard(
        10, None, "Party", utc("2026-11-01T10:00:00"), utc("2026-11-01T12:00:00")
    )
    assert (
        resolve_destination(
            policy(event, selected=selected), utc("2026-11-02T00:00:00")
        ).slot
        == 4
    )


@pytest.mark.parametrize(
    "value,want",
    [
        ("2026-11-01T12:00", "2026-11-01T11:00:00+00:00"),
        ("2026-07-01T12:00", "2026-07-01T10:00:00+00:00"),
        ("2026-10-25T02:30:00+02:00", "2026-10-25T00:30:00+00:00"),
        ("2026-10-25T02:30:00+01:00", "2026-10-25T01:30:00+00:00"),
        ("2026-11-01T12:00:00Z", "2026-11-01T12:00:00+00:00"),
    ],
)
def test_berlin_and_explicit_offset_conversion(value, want):
    assert parse_event_time(value).isoformat() == want


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        123,
        [],
        "",
        "2026-11-01",
        "2026-10-25T02:30",
        "2026-03-29T02:30",
        "not a time",
        "0001-01-01T00:00:00+23:00",
        "9999-12-31T23:00:00-23:00",
        "0001-01-01T00:00",
    ],
)
def test_invalid_or_nonunique_local_times_are_rejected(value):
    with pytest.raises(ValueError):
        parse_event_time(value)
