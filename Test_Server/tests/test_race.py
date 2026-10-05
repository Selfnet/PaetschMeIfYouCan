from dataclasses import asdict

import pytest

from board import EXPECTED_IDS, BoardObservation
from gamemodes import BUILTIN_MODES, EvaluationContext
from race import Phase, RaceStateMachine


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
        "presentation": asdict(
            BUILTIN_MODES.get("full-field").preview(
                BoardObservation(None), EvaluationContext(verify=True)
            )
        ),
    }
    assert machine.manual_stop(2).action == "stopped"
    assert machine.snapshot()["phase"] == Phase.STOPPED


@pytest.mark.parametrize(
    "states",
    [[2] * 47, [2] * 49, [2] * 47 + [-1], [2] * 47 + [256], [True] * 48, "2" * 48],
)
def test_invalid_raw_frames_are_rejected(clock, states):
    machine = started_race(clock)

    assert not machine.observe_board(1, states).changed
    assert not machine.snapshot()["players"]["1"]["completed"]


def test_first_valid_completion_is_idempotent_and_uses_floor_milliseconds(clock):
    machine = started_race(clock)
    clock.advance_ns(1_234_999_999)
    assert machine.observe_board(1, EXPECTED_IDS).changed
    clock.advance_ms(500)
    assert not machine.observe_board(1, EXPECTED_IDS).changed

    assert machine.snapshot()["players"]["1"]["duration_ms"] == 1234


def test_zero_timestamp_completion_and_stop_freeze_duration(clock):
    completed = started_race(clock)
    completed.observe_board(1, EXPECTED_IDS)
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
    machine.observe_board(1, EXPECTED_IDS)

    player = machine.snapshot()["players"]["1"]
    assert player["duration_ms"] == 1200
    assert player["completed"] is True
    assert player["manual_stopped"] is False


def test_second_completion_enters_name_entry_with_winner_order(clock):
    machine = started_race(clock)
    clock.advance_ms(900)
    machine.observe_board(2, EXPECTED_IDS)
    clock.advance_ms(100)
    transition = machine.observe_board(1, EXPECTED_IDS)

    state = machine.snapshot()
    assert transition.action == "name_entry"
    assert state["phase"] == Phase.NAME_ENTRY
    assert state["result_order"] == [2, 1]
    assert state["tie"] is False


def test_equal_floor_milliseconds_are_a_tie_and_prompt_player_one_first(clock):
    machine = started_race(clock)
    clock.advance_ns(1_000_100_000)
    machine.observe_board(2, EXPECTED_IDS)
    clock.advance_ns(700_000)
    machine.observe_board(1, EXPECTED_IDS)

    state = machine.snapshot()
    assert state["tie"] is True
    assert state["result_order"] == [1, 2]
    assert state["name_entry"]["active_player"] == 1


def completed_race(clock):
    machine = started_race(clock)
    clock.advance_ms(1000)
    machine.observe_board(1, EXPECTED_IDS)
    clock.advance_ms(250)
    machine.observe_board(2, EXPECTED_IDS)
    return machine


def test_name_entry_accepts_either_player_first_and_trims_confirmed_names(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]

    assert machine.set_name_draft(race_id, 1, "  Ada  ").changed
    assert machine.set_name_draft(race_id, 2, "Grace").changed
    assert machine.submit_name(race_id, 2, "Grace").action == "next_name"
    assert machine.snapshot()["name_entry"]["active_player"] == 1

    final = machine.submit_name(race_id, 1, "  Ada  ")
    assert final.action == "leaderboard"
    assert [(item.player_number, item.name) for item in final.submissions] == [
        (1, "Ada"),
        (2, "Grace"),
    ]
    assert [item.duration_ms for item in final.submissions] == [1000, 1250]


def test_empty_names_skip_rows_and_stale_events_are_ignored(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]

    assert not machine.set_name_draft("old-race", 1, "Ada").changed
    assert machine.submit_name(race_id, 1, "").action == "next_name"
    final = machine.submit_name(race_id, 2, "   ")

    assert final.submissions == ()
    assert machine.snapshot()["phase"] == Phase.LEADERBOARD


@pytest.mark.parametrize("draft", ["A" * 13, "Ada\n", 123, None])
def test_invalid_drafts_do_not_change_state_or_extend_deadline(clock, draft):
    machine = completed_race(clock)
    before = machine.snapshot()
    clock.advance_ms(5000)

    assert not machine.set_name_draft(before["race_id"], 1, draft).changed
    after = machine.snapshot()
    assert after["name_entry"]["drafts"] == before["name_entry"]["drafts"]
    assert after["remaining_seconds"] == 115


def test_printable_unicode_draft_restarts_idle_deadline(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    clock.advance_ms(119_000)

    assert machine.set_name_draft(race_id, 1, "Zoë 🚀").changed
    assert machine.snapshot()["remaining_seconds"] == 120


def test_name_timeout_saves_confirmed_names_and_leaderboard_resets(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    machine.submit_name(race_id, 1, "Ada")
    machine.set_name_draft(race_id, 2, "Unconfirmed")
    clock.advance_ms(120_000)

    expired = machine.tick()
    assert [(item.player_number, item.name) for item in expired.submissions] == [
        (1, "Ada")
    ]
    assert machine.snapshot()["remaining_seconds"] == 60

    clock.advance_ms(60_000)
    assert machine.tick().action == "reset"
    assert machine.snapshot()["phase"] == Phase.READY


def test_name_event_at_deadline_enters_leaderboard_without_accepting_draft(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    clock.advance_ms(120_000)

    change = machine.set_name_draft(race_id, 1, "Too late")

    assert change.action == "leaderboard"
    assert change.submissions == ()
    assert machine.snapshot()["phase"] == Phase.LEADERBOARD


def test_persistence_status_and_dismiss(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    machine.submit_name(race_id, 1, "Ada")
    change = machine.submit_name(race_id, 2, "")
    machine.set_persistence(change.origin, "Scores could not be saved")

    state = machine.snapshot()
    assert state["persistence_status"] == "error"
    assert state["persistence_error"] == "Scores could not be saved"
    assert not machine.dismiss_leaderboard("old-race").changed
    assert machine.dismiss_leaderboard(race_id).action == "reset"


def test_prepatched_quarter_field_finishes_both_at_zero(clock):
    machine = RaceStateMachine(clock_ns=clock)
    machine.select_mode("quarter-field")
    values = EXPECTED_IDS[:12] + (255,) * 36
    machine.observe_board(1, values)
    machine.observe_board(2, values)
    machine.start()
    state = machine.snapshot()
    assert state["phase"] == "name_entry"
    assert state["tie"] is True
    assert [state["players"][str(p)]["duration_ms"] for p in (1, 2)] == [0, 0]
    machine.reset()
    assert machine.snapshot()["mode"]["id"] == "quarter-field"


def test_revision_is_monotonic_and_reads_are_pure(clock):
    machine = RaceStateMachine(clock_ns=clock)
    revision = machine.snapshot()["revision"]
    assert machine.snapshot()["revision"] == revision
    assert machine.clock_payload()["revision"] == revision
    for mutate in (
        lambda: machine.observe_board(1, (255,) * 48),
        lambda: machine.select_mode("half-field"),
        lambda: machine.set_verification(False),
        machine.start,
        machine.reset,
    ):
        assert mutate().changed
        assert machine.snapshot()["revision"] > revision
        revision = machine.snapshot()["revision"]


@pytest.mark.parametrize("action", ["confirm", "reset"])
def test_browser_epoch_exposes_same_origin_invalidation(clock, action):
    machine = RaceStateMachine(clock_ns=clock)
    before = machine.snapshot()
    origin = machine.capture_browse("full-field", None)
    change = (
        machine.select_mode("full-field") if action == "confirm" else machine.reset()
    )
    assert change.changed
    after = machine.snapshot()
    assert (after["phase"], after["mode"], after["race_id"]) == (
        before["phase"],
        before["mode"],
        before["race_id"],
    )
    assert after["browse_epoch"] > before["browse_epoch"]
    assert not machine.browse_is_current(origin)[0]
    assert machine.capture_browse("full-field", None).epoch == after["browse_epoch"]


def test_browser_epoch_survives_observation_tick_and_persistence(clock):
    machine = RaceStateMachine(clock_ns=clock)
    epoch = machine.snapshot()["browse_epoch"]
    machine.observe_board(1, (255,) * 48)
    machine.set_verification(False)
    clock.advance_ms(1000)
    machine.tick()
    assert machine.snapshot()["browse_epoch"] == epoch
    machine.start()
    epoch = machine.snapshot()["browse_epoch"]
    machine.observe_board(1, (255,) * 48)
    clock.advance_ms(1000)
    machine.tick()
    assert machine.snapshot()["browse_epoch"] == epoch
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    machine.submit_name(race_id, 1, "")
    change = machine.submit_name(race_id, 2, "")
    epoch = machine.snapshot()["browse_epoch"]
    machine.set_persistence(change.origin, None)
    machine.leaderboard_activity(race_id)
    clock.advance_ms(1000)
    machine.tick()
    machine.observe_board(1, (255,) * 48)
    assert machine.snapshot()["browse_epoch"] == epoch


def test_browse_origins_survive_progress_but_not_replacement_or_expiry(clock):
    machine = RaceStateMachine(clock_ns=clock)
    origin = machine.capture_browse("full-field", None)
    assert origin is not None
    machine.observe_board(1, (255,) * 48)
    assert machine.browse_is_current(origin)[0]
    machine.start()
    machine.reset()
    assert not machine.browse_is_current(origin)[0]
    assert machine.capture_browse("unknown", None) is None
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    machine.submit_name(race_id, 1, "")
    change = machine.submit_name(race_id, 2, "")
    assert change.origin.mode_id == "full-field"
    assert machine.capture_browse("full-field", race_id) is None
    machine.set_persistence(change.origin, None)
    origin = machine.capture_browse("full-field", race_id)
    assert machine.browse_is_current(origin)[0]
    assert machine.set_persistence(change.origin, None).changed
    clock.advance_ms(60_000)
    current, expired = machine.browse_is_current(origin)
    assert not current
    assert expired.action == "reset"
    assert machine.snapshot()["phase"] == "ready"
    assert not machine.set_persistence(change.origin, None).changed


def test_activity_refreshes_only_current_unexpired_leaderboard(clock):
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    machine.submit_name(race_id, 1, "")
    machine.submit_name(race_id, 2, "")
    clock.advance_ms(59_000)
    assert not machine.leaderboard_activity("old").changed
    assert machine.snapshot()["remaining_seconds"] == 1
    assert machine.leaderboard_activity(race_id).changed
    assert machine.snapshot()["remaining_seconds"] == 60
    clock.advance_ms(60_000)
    assert machine.leaderboard_activity(race_id).action == "reset"


def test_staged_progress_ticks_and_diagnostic_stop_use_real_elapsed(clock):
    from gamemodes import BUILTIN_MODES, ModeRegistry
    from tests.mode_fixtures import STAGED_MODE

    registry = ModeRegistry([BUILTIN_MODES.get("full-field"), STAGED_MODE])
    machine = RaceStateMachine(clock_ns=clock, registry=registry)
    machine.select_mode("staged")
    machine.start()
    clock.advance_ms(100)
    machine.observe_board(1, EXPECTED_IDS)
    assert machine.snapshot()["players"]["1"]["presentation"]["stage"] == "Wait"
    assert machine.snapshot()["players"]["2"]["presentation"]["stage"] == "Patch"
    clock.advance_ms(100)
    machine.manual_stop(1)
    clock.advance_ms(900)
    assert machine.tick().changed
    assert machine.snapshot()["players"]["1"]["duration_ms"] == 200
    clock.advance_ms(1000)
    machine.tick()
    assert machine.snapshot()["players"]["1"]["duration_ms"] == 2100
    assert machine.snapshot()["players"]["1"]["completed"]
    machine.reset()
    assert machine.snapshot()["players"]["1"]["presentation"]["stage"] == "Patch"
    machine.start()
    assert (
        machine.snapshot()["players"]["1"]["presentation"]["progress"]["current"] == 0
    )


@pytest.mark.parametrize("operation", ["initialize", "observe", "tick"])
@pytest.mark.parametrize("malformed", [False, True])
def test_active_mode_errors_stop_without_results(clock, operation, malformed):
    from gamemodes import BUILTIN_MODES, ModeRegistry
    from tests.mode_fixtures import failing_mode

    mode = failing_mode(operation, malformed=malformed)
    machine = RaceStateMachine(
        clock_ns=clock, registry=ModeRegistry([BUILTIN_MODES.get("full-field"), mode])
    )
    machine.select_mode(mode.id)
    change = machine.start()
    if operation == "observe":
        change = machine.observe_board(1, EXPECTED_IDS)
    elif operation == "tick":
        change = machine.tick()
    assert change.diagnostic
    assert change.submissions == ()
    state = machine.snapshot()
    assert state["phase"] == "stopped"
    assert state["mode_error"] and "Traceback" not in state["mode_error"]
    before = state["players"]
    clock.advance_ms(1000)
    assert machine.snapshot()["players"] == before
    machine.reset()
    assert machine.snapshot()["mode_error"] is None


def test_preview_failure_blocks_start_and_other_mode_recovers(clock):
    from gamemodes import BUILTIN_MODES, ModeRegistry
    from tests.mode_fixtures import FAILING_FACTORY_MODE, FAILING_PREVIEW_MODE

    machine = RaceStateMachine(
        clock_ns=clock,
        registry=ModeRegistry(
            [
                BUILTIN_MODES.get("full-field"),
                FAILING_PREVIEW_MODE,
                FAILING_FACTORY_MODE,
            ]
        ),
    )
    assert machine.select_mode(FAILING_PREVIEW_MODE.id).diagnostic
    assert not machine.start().changed
    assert machine.reset().diagnostic
    machine.select_mode(FAILING_FACTORY_MODE.id)
    assert machine.start().diagnostic
    assert machine.snapshot()["phase"] == "stopped"
    machine.reset()
    machine.select_mode("full-field")
    assert machine.start().action == "started"


def test_verification_and_completed_presentation_latch(clock):
    machine = started_race(clock)
    machine.observe_board(1, (0,) * 48)
    clock.advance_ms(123)
    assert machine.set_verification(False).changed
    before = machine.snapshot()["players"]["1"]
    assert before["completed"]
    assert before["duration_ms"] == 123
    machine.observe_board(1, (255,) * 48)
    machine.set_verification(True)
    assert machine.snapshot()["players"]["1"] == before


def test_selection_rejected_outside_ready_and_retained_on_all_resets(clock):
    machine = RaceStateMachine(clock_ns=clock)
    assert machine.snapshot()["mode"]["id"] == "full-field"
    assert not machine.select_mode("unknown").changed
    machine.select_mode("quarter-field")
    machine.start()
    assert not machine.select_mode("half-field").changed
    machine.space()
    machine.space()
    assert machine.snapshot()["mode"]["id"] == "quarter-field"
    for exit_method in ("dismiss", "expiry", "reset"):
        machine.start()
        machine.observe_board(1, EXPECTED_IDS)
        machine.observe_board(2, EXPECTED_IDS)
        race_id = machine.snapshot()["race_id"]
        machine.submit_name(race_id, 1, "Ada")
        change = machine.submit_name(race_id, 2, "")
        assert change.origin.mode_id == "quarter-field"
        assert change.submissions[0].mode_id == "quarter-field"
        if exit_method == "dismiss":
            machine.dismiss_leaderboard(race_id)
        elif exit_method == "expiry":
            clock.advance_ms(60_000)
            machine.tick()
        else:
            machine.reset()
        assert machine.snapshot()["mode"]["id"] == "quarter-field"


@pytest.mark.parametrize("stop", ["global", "both"])
def test_stops_freeze_presentations_but_reset_uses_latest_board(clock, stop):
    from gamemodes import BUILTIN_MODES, ModeRegistry
    from tests.mode_fixtures import STAGED_MODE

    machine = RaceStateMachine(
        clock_ns=clock,
        registry=ModeRegistry([BUILTIN_MODES.get("full-field"), STAGED_MODE]),
    )
    machine.select_mode("staged")
    machine.start()
    machine.observe_board(1, EXPECTED_IDS)
    if stop == "global":
        machine.space()
    else:
        machine.manual_stop(1)
        machine.manual_stop(2)
    before = machine.snapshot()["players"]
    clock.advance_ms(5000)
    machine.observe_board(1, (255,) * 48)
    assert not machine.tick().changed
    assert machine.snapshot()["players"] == before
    machine.reset()
    state = machine.snapshot()
    assert state["players"]["1"]["presentation"]["stage"] == "Patch"
    assert state["players"]["1"]["presentation"]["ports"][0]["feedback"] == "open"


def test_complete_player_and_stopped_player_still_finish_shared_race(clock):
    machine = started_race(clock)
    clock.advance_ms(100)
    machine.observe_board(1, EXPECTED_IDS)
    machine.manual_stop(2)
    assert machine.snapshot()["phase"] == "racing"
    clock.advance_ms(400)
    machine.observe_board(2, EXPECTED_IDS)
    assert machine.snapshot()["phase"] == "name_entry"
    assert machine.snapshot()["players"]["2"]["duration_ms"] == 500


def test_unknown_initial_boards_and_invalid_frame_do_not_reach_session(clock):
    machine = started_race(clock)
    before = machine.snapshot()
    assert not before["players"]["1"]["completed"]
    assert {
        port["feedback"] for port in before["players"]["1"]["presentation"]["ports"]
    } == {"unknown"}
    assert not machine.observe_board(1, [True] * 48).changed
    assert not machine.observe_board(True, EXPECTED_IDS).changed
    assert machine.snapshot() == before
    assert machine.boards[1].snapshot().identities is None


def test_start_evaluates_both_at_zero_and_snapshots_never_call_modes(
    clock, monkeypatch
):
    from dataclasses import replace

    from gamemodes import BUILTIN_MODES, ModeRegistry
    from tests.mode_fixtures import STAGED_MODE, StagedSession

    calls = []
    sessions = []

    class RecordingSession(StagedSession):
        def initialize(self, observation, context, elapsed_ns):
            calls.append(elapsed_ns)
            return super().initialize(observation, context, elapsed_ns)

    def factory():
        session = RecordingSession()
        sessions.append(session)
        return session

    mode = replace(STAGED_MODE, session_factory=factory)
    machine = RaceStateMachine(
        clock_ns=clock, registry=ModeRegistry([BUILTIN_MODES.get("full-field"), mode])
    )
    machine.select_mode(mode.id)
    machine.observe_board(1, EXPECTED_IDS)
    machine.start()
    assert calls == [0, 0]
    with monkeypatch.context() as patch:

        def unexpected(*args):
            pytest.fail("snapshot reads must not evaluate modes")

        patch.setattr(
            machine,
            "registry",
            ModeRegistry(
                [
                    replace(BUILTIN_MODES.get("full-field"), preview=unexpected),
                    replace(mode, preview=unexpected),
                ]
            ),
        )
        for session in sessions:
            for method in ("initialize", "observe", "tick"):
                patch.setattr(session, method, unexpected)
        for _ in range(3):
            machine.snapshot()
            machine.clock_payload()
    assert calls == [0, 0]
    machine.reset()
    machine.start()
    assert calls == [0, 0, 0, 0]
    assert len({id(session) for session in sessions}) == 4


def test_start_captures_advancing_clock_once():
    from dataclasses import replace

    from gamemodes import BUILTIN_MODES, ModeRegistry
    from tests.mode_fixtures import STAGED_MODE, StagedSession

    captures, elapsed = [], []

    def advancing_clock():
        captures.append(len(captures) * 1_000_000)
        return captures[-1]

    class RecordingSession(StagedSession):
        def initialize(self, observation, context, elapsed_ns):
            elapsed.append(elapsed_ns)
            return super().initialize(observation, context, elapsed_ns)

    mode = replace(STAGED_MODE, session_factory=RecordingSession)
    machine = RaceStateMachine(
        clock_ns=advancing_clock,
        registry=ModeRegistry([BUILTIN_MODES.get("full-field"), mode]),
    )
    machine.select_mode(mode.id)
    captures.clear()
    machine.start()
    assert captures == [0]
    assert elapsed == [0, 0]


def test_second_initialize_failure_does_not_publish_first_completion(clock):
    from dataclasses import replace

    from gamemodes import BUILTIN_MODES, ModeRegistry
    from tests.mode_fixtures import FailingSession

    field_mode = BUILTIN_MODES.get("full-field")
    sessions = iter([field_mode.new_session(), FailingSession("initialize")])
    mode = replace(
        field_mode, id="second-fails", session_factory=lambda: next(sessions)
    )
    machine = RaceStateMachine(
        clock_ns=clock, registry=ModeRegistry([field_mode, mode])
    )
    machine.select_mode(mode.id)
    machine.observe_board(1, EXPECTED_IDS)
    change = machine.start()
    assert change.diagnostic
    assert machine.snapshot()["phase"] == "stopped"
    assert not machine.snapshot()["players"]["1"]["completed"]
    assert machine.sessions == {}
    assert change.submissions == ()


def test_countdown_ticks_do_not_increase_revision_but_deadline_refresh_does(clock):
    machine = completed_race(clock)
    before = machine.snapshot()
    clock.advance_ms(1000)
    assert not machine.tick().changed
    assert machine.snapshot()["remaining_seconds"] == 119
    assert machine.snapshot()["revision"] == before["revision"]
    machine.set_name_draft(before["race_id"], 1, "")
    assert machine.snapshot()["revision"] > before["revision"]


def test_stale_persistence_mode_and_epoch_origins_are_rejected(clock):
    from dataclasses import replace

    from race import RaceOrigin

    machine = RaceStateMachine(clock_ns=clock)
    origin = machine.capture_browse("full-field", None)
    for field, value in (
        ("epoch", origin.epoch + 1),
        ("mode_id", "half-field"),
        ("phase", Phase.STOPPED),
        ("race_id", "stale"),
    ):
        assert not machine.browse_is_current(replace(origin, **{field: value}))[0]
    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    machine.submit_name(race_id, 1, "Ada")
    change = machine.submit_name(race_id, 2, "")
    assert not machine.set_persistence(RaceOrigin(race_id, "half-field"), None).changed
    before = machine.snapshot()["remaining_seconds"]
    clock.advance_ms(1000)
    machine.set_persistence(change.origin, None)
    assert machine.snapshot()["remaining_seconds"] == before - 1
    machine.reset()
    machine.start()
    assert not machine.set_persistence(change.origin, "stale error").changed
    assert machine.snapshot()["persistence_status"] == "idle"


@pytest.mark.parametrize("transition", ["start", "reset"])
@pytest.mark.parametrize("first", ["observation", "transition"])
def test_observations_follow_lock_order_during_start_and_reset(
    clock, monkeypatch, transition, first
):
    import threading
    from dataclasses import replace

    from gamemodes import BUILTIN_MODES, ModeRegistry
    from tests.mode_fixtures import STAGED_MODE, StagedSession, staged_preview

    entered = threading.Event()
    release = threading.Event()
    attempted = threading.Event()
    finished = threading.Event()
    armed = False
    records = []
    failures = []

    def barrier():
        entered.set()
        assert release.wait(5), "second operation never released first"

    class RecordingSession(StagedSession):
        def initialize(self, observation, context, elapsed_ns):
            if (
                armed
                and first == "transition"
                and transition == "start"
                and not entered.is_set()
            ):
                barrier()
            records.append(("initialize", observation.identities))
            return super().initialize(observation, context, elapsed_ns)

        def observe(self, observation, context, elapsed_ns):
            records.append(("observe", observation.identities))
            return super().observe(observation, context, elapsed_ns)

    def preview(observation, context):
        if (
            armed
            and first == "transition"
            and transition == "reset"
            and not entered.is_set()
        ):
            barrier()
        return staged_preview(observation, context)

    mode = replace(STAGED_MODE, session_factory=RecordingSession, preview=preview)
    machine = RaceStateMachine(
        clock_ns=clock, registry=ModeRegistry([BUILTIN_MODES.get("full-field"), mode])
    )
    machine.select_mode(mode.id)
    if transition == "reset":
        machine.start()
    records.clear()
    update = machine.boards[1].update

    def held_update(values):
        if first == "observation":
            barrier()
        return update(values)

    monkeypatch.setattr(machine.boards[1], "update", held_update)
    armed = True
    observe = lambda: machine.observe_board(1, EXPECTED_IDS)
    change = getattr(machine, transition)
    operations = (observe, change) if first == "observation" else (change, observe)

    def run(operation, second=False):
        try:
            if second:
                attempted.set()
            operation()
            if second:
                finished.set()
        except BaseException as error:  # noqa: BLE001 - propagate thread failures to test
            failures.append(error)

    first_thread = threading.Thread(target=run, args=(operations[0],))
    second_thread = threading.Thread(target=run, args=(operations[1], True))
    first_thread.start()
    try:
        assert entered.wait(5)
        second_thread.start()
        assert attempted.wait(5)
        assert not finished.is_set()
    finally:
        release.set()
        first_thread.join(5)
        if second_thread.ident is not None:
            second_thread.join(5)
    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert not failures
    assert machine.boards[1].snapshot().identities == EXPECTED_IDS
    if transition == "start":
        initial = [values for operation, values in records if operation == "initialize"]
        assert initial == (
            [EXPECTED_IDS, None] if first == "observation" else [None, None]
        )
        assert machine.snapshot()["players"]["1"]["presentation"]["stage"] == "Wait"
    else:
        observed = [values for operation, values in records if operation == "observe"]
        assert observed == ([EXPECTED_IDS] if first == "observation" else [])
        assert machine.snapshot()["players"]["1"]["presentation"]["stage"] == "Patch"
        assert (
            machine.snapshot()["players"]["1"]["presentation"]["ports"][0]["feedback"]
            == "correct"
        )


@pytest.mark.parametrize("operation", ["tick", "verification"])
@pytest.mark.parametrize("malformed", [False, True])
def test_second_evaluation_failure_does_not_publish_first_result(
    clock, operation, malformed
):
    from dataclasses import replace

    from gamemodes import ModeRegistry
    from tests.mode_fixtures import STAGED_MODE, FailingSession, StagedSession

    field_mode = BUILTIN_MODES.get("full-field")
    method = "tick" if operation == "tick" else "observe"
    first = StagedSession() if operation == "tick" else field_mode.new_session()
    sessions = iter([first, FailingSession(method, malformed)])
    mode = replace(STAGED_MODE, session_factory=lambda: next(sessions))
    machine = RaceStateMachine(
        clock_ns=clock, registry=ModeRegistry([field_mode, mode])
    )
    machine.select_mode(mode.id)
    machine.observe_board(1, EXPECTED_IDS if operation == "tick" else (0,) * 48)
    machine.start()
    before = machine.snapshot()["players"]["1"]["presentation"]
    clock.advance_ms(2000)
    if operation == "tick":
        change = machine.tick()
    else:
        change = machine.set_verification(False)
    state = machine.snapshot()
    assert change.diagnostic
    assert state["phase"] == "stopped"
    assert not state["players"]["1"]["completed"]
    assert state["players"]["1"]["presentation"] == before
    assert change.submissions == ()


def test_returned_snapshot_is_detached_from_controller(clock):
    machine = completed_race(clock)
    before = machine.snapshot()
    snapshot = machine.snapshot()
    snapshot["players"]["1"]["presentation"]["ports"][0]["feedback"] = "wrong"
    snapshot["players"]["1"]["presentation"]["progress"]["current"] = 0
    snapshot["mode"]["id"] = "altered"
    snapshot["available_modes"][0]["label"] = "altered"
    snapshot["name_entry"]["drafts"]["1"] = "altered"
    snapshot["result_order"].clear()
    assert machine.snapshot() == before


@pytest.mark.parametrize("replacement", ["reset", "selection", "race", "expiry"])
@pytest.mark.parametrize("saved", [False, True])
def test_delayed_reads_and_persistence_cannot_restore_old_view(
    clock, replacement, saved
):
    import threading

    machine = completed_race(clock)
    race_id = machine.snapshot()["race_id"]
    machine.submit_name(race_id, 1, "Ada")
    change = machine.submit_name(race_id, 2, "")
    if saved:
        machine.set_persistence(change.origin, None)
    browse = machine.capture_browse("full-field", race_id)
    if saved:
        assert browse is not None
        assert machine.browse_is_current(browse)[0]
    else:
        assert browse is None
    captured = threading.Event()
    release = threading.Event()
    results = []

    def delayed_response():
        captured.set()
        if not release.wait(5):
            return
        results.append(machine.set_persistence(change.origin, "old write failed"))
        if saved:
            results.append(machine.browse_is_current(browse))

    thread = threading.Thread(target=delayed_response)
    thread.start()
    try:
        assert captured.wait(5)
        if replacement == "expiry":
            clock.advance_ms(60_000)
        else:
            machine.reset()
            if replacement == "selection":
                machine.select_mode("quarter-field")
            elif replacement == "race":
                machine.observe_board(1, (255,) * 48)
                machine.observe_board(2, (255,) * 48)
                machine.start()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(results) == (2 if saved else 1)
    assert results[0].action == ("reset" if replacement == "expiry" else None)
    if saved:
        assert not results[1][0]
    assert machine.snapshot()["persistence_status"] == "idle"
    assert machine.snapshot()["persistence_error"] is None
