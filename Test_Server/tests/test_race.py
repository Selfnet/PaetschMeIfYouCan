import pytest

from race import Phase, RaceStateMachine

VERIFIED = [2] * 48


def started_race(clock):
    machine = RaceStateMachine(clock_ns=clock, race_id_factory=lambda: "race-1")
    assert machine.start().action == "started"
    return machine


def test_start_and_global_space_follow_non_result_branch(clock):
    machine = RaceStateMachine(clock_ns=clock, race_id_factory=lambda: "race-1")

    assert machine.snapshot()["phase"] == Phase.READY
    assert machine.space().action == "started"
    clock.advance_ms(1250)
    assert machine.space().action == "stopped"
    assert machine.snapshot()["phase"] == Phase.STOPPED
    assert machine.snapshot()["result_order"] == []
    assert machine.space().action == "reset"
    assert machine.snapshot()["phase"] == Phase.READY


def test_manual_stops_freeze_display_without_completing_players(clock):
    machine = started_race(clock)
    clock.advance_ms(1000)
    assert machine.manual_stop(1).changed
    clock.advance_ms(500)

    state = machine.snapshot()
    assert state["phase"] == Phase.RACING
    assert state["players"]["1"] == {
        "duration_ms": 1000,
        "completed": False,
        "manual_stopped": True,
        "clock_active": False,
    }
    assert machine.manual_stop(2).action == "stopped"
    assert machine.snapshot()["phase"] == Phase.STOPPED


@pytest.mark.parametrize(
    "states",
    [[2] * 47, [2] * 49, [2] * 47 + [0], [2] * 47 + [1], [True] * 48, "2" * 48],
)
def test_only_exactly_48_integer_verified_cells_complete(clock, states):
    machine = started_race(clock)

    assert not machine.observe_cells(1, states).changed
    assert not machine.snapshot()["players"]["1"]["completed"]


def test_first_valid_completion_is_idempotent_and_uses_floor_milliseconds(clock):
    machine = started_race(clock)
    clock.advance_ns(1_234_999_999)
    assert machine.observe_cells(1, VERIFIED).changed
    clock.advance_ms(500)
    assert not machine.observe_cells(1, VERIFIED).changed

    assert machine.snapshot()["players"]["1"]["duration_ms"] == 1234


def test_zero_timestamp_completion_and_stop_freeze_duration(clock):
    completed = started_race(clock)
    completed.observe_cells(1, VERIFIED)
    clock.advance_ms(1000)

    stopped = started_race(clock)
    stopped.manual_stop(1)
    clock.advance_ms(1000)

    assert completed.snapshot()["players"]["1"]["duration_ms"] == 0
    assert stopped.snapshot()["players"]["1"]["duration_ms"] == 0


def test_manual_stop_can_be_replaced_by_later_verified_completion(clock):
    machine = started_race(clock)
    clock.advance_ms(500)
    machine.manual_stop(1)
    clock.advance_ms(700)
    machine.observe_cells(1, VERIFIED)

    player = machine.snapshot()["players"]["1"]
    assert player["duration_ms"] == 1200
    assert player["completed"] is True
    assert player["manual_stopped"] is False


def test_second_completion_enters_name_entry_with_winner_order(clock):
    machine = started_race(clock)
    clock.advance_ms(900)
    machine.observe_cells(2, VERIFIED)
    clock.advance_ms(100)
    transition = machine.observe_cells(1, VERIFIED)

    state = machine.snapshot()
    assert transition.action == "name_entry"
    assert state["phase"] == Phase.NAME_ENTRY
    assert state["result_order"] == [2, 1]
    assert state["tie"] is False


def test_equal_floor_milliseconds_are_a_tie_and_prompt_player_one_first(clock):
    machine = started_race(clock)
    clock.advance_ns(1_000_100_000)
    machine.observe_cells(2, VERIFIED)
    clock.advance_ns(700_000)
    machine.observe_cells(1, VERIFIED)

    state = machine.snapshot()
    assert state["tie"] is True
    assert state["result_order"] == [1, 2]
    assert state["name_entry"]["active_player"] == 1
