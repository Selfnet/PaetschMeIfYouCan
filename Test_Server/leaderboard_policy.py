"""Immutable routing metadata and wall-clock policy, independent of storage."""

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


def utc_time(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("Expected a timezone-aware time")
    try:
        return value.astimezone(UTC)
    except OverflowError as error:
        raise ValueError("Time is outside the supported UTC range") from error


def parse_event_time(value: str) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Expected an ISO date and time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid date and time") from error
    if parsed.tzinfo is not None:
        return utc_time(parsed)
    candidates = set()
    for fold in (0, 1):
        candidate = utc_time(parsed.replace(tzinfo=BERLIN, fold=fold))
        if candidate.astimezone(BERLIN).replace(tzinfo=None) == parsed:
            candidates.add(candidate)
    if len(candidates) != 1:
        raise ValueError(
            "Choose an unambiguous Europe/Berlin time or an explicit UTC offset"
        )
    return candidates.pop()


@dataclass(frozen=True)
class Leaderboard:
    id: int
    slot: int | None
    name: str | None
    starts_at: datetime | None
    ends_at: datetime | None


@dataclass(frozen=True)
class LeaderboardPolicy:
    boards: tuple[Leaderboard, ...]
    manual_slot: int
    manual_selected_at: datetime
    revision: int


def active_event(policy: LeaderboardPolicy, now: datetime) -> Leaderboard | None:
    now = utc_time(now)
    return next(
        (
            board
            for board in policy.boards
            if board.slot is None and board.starts_at <= now < board.ends_at
        ),
        None,
    )


def resolve_destination(policy: LeaderboardPolicy, now: datetime) -> Leaderboard:
    now = utc_time(now)
    event = active_event(policy, now)
    if event is not None:
        return event
    slot = policy.manual_slot
    if any(
        board.slot is None and policy.manual_selected_at < board.ends_at <= now
        for board in policy.boards
    ):
        slot = 1
    return next(board for board in policy.boards if board.slot == slot)
