from dataclasses import FrozenInstanceError, replace

import pytest
from gamemodes import (
    BUILTIN_MODES,
    EvaluationContext,
    GameMode,
    LedState,
    ModeRegistry,
    ModeSnapshot,
    PortPresentation,
    Progress,
    validate_snapshot,
)

from board import EXPECTED_IDS, GameBoard

FIELDS = [("full-field", 48), ("half-field", 24), ("quarter-field", 12)]


@pytest.fixture
def valid_snapshot():
    return BUILTIN_MODES.get("full-field").preview(
        GameBoard().snapshot(), EvaluationContext(True)
    )


@pytest.mark.parametrize("mode_id,count", FIELDS)
def test_field_targets_and_prepatched_completion(mode_id, count):
    mode = BUILTIN_MODES.get(mode_id)
    observation = GameBoard().update(EXPECTED_IDS[:count] + (255,) * (48 - count))
    result = mode.new_session().initialize(observation, EvaluationContext(True), 0)
    assert validate_snapshot(result) is result
    assert result.complete
    assert result.progress == Progress(count, count, "correct")
    assert tuple(i for i, port in enumerate(result.ports) if port.required) == tuple(
        range(count)
    )
    assert result.objective == mode.description
    assert result.stage is None


@pytest.mark.parametrize("mode_id,count", FIELDS)
@pytest.mark.parametrize("value,feedback", [(255, "open"), (0, "wrong")])
def test_required_open_or_wrong_prevents_completion(mode_id, count, value, feedback):
    values = list(EXPECTED_IDS)
    values[count - 1] = value
    result = BUILTIN_MODES.get(mode_id).preview(
        GameBoard().update(values), EvaluationContext(True)
    )
    assert not result.complete
    assert result.progress.current == count - 1
    assert result.ports[count - 1] == PortPresentation(True, feedback)


@pytest.mark.parametrize("mode_id,count", FIELDS[1:])
@pytest.mark.parametrize(
    "value,feedback", [(255, "open"), (0, "wrong"), (None, "correct")]
)
def test_inactive_ports_preserve_feedback_but_never_block_completion(
    mode_id, count, value, feedback
):
    values = (
        EXPECTED_IDS
        if value is None
        else EXPECTED_IDS[:count] + (value,) * (48 - count)
    )
    result = BUILTIN_MODES.get(mode_id).preview(
        GameBoard().update(values), EvaluationContext(True)
    )
    assert result.complete
    assert result.progress == Progress(count, count, "correct")
    assert result.ports[count:] == (PortPresentation(False, feedback),) * (48 - count)


@pytest.mark.parametrize("mode_id,count", FIELDS)
def test_verification_bypass_counts_occupied_but_not_open_ports(mode_id, count):
    mode = BUILTIN_MODES.get(mode_id)
    board = GameBoard()
    observation = board.update([0] * 48)
    result = mode.preview(observation, EvaluationContext(False))
    assert result.complete
    assert result.progress.current == count
    assert all(port.feedback == "correct" for port in result.ports)
    observation = board.update([255] + [0] * 47)
    result = mode.preview(observation, EvaluationContext(False))
    assert not result.complete
    assert result.progress.current == count - 1
    assert result.ports[0].feedback == "open"
    assert observation.identities == (255,) + (0,) * 47


@pytest.mark.parametrize("mode_id,count", FIELDS)
def test_unknown_board_and_repeated_preview_are_pure(mode_id, count):
    mode = BUILTIN_MODES.get(mode_id)
    observation = GameBoard().snapshot()
    for verify in (True, False):
        context = EvaluationContext(verify)
        session = mode.new_session()
        initial = session.initialize(observation, context, 0)
        assert not initial.complete
        assert initial.progress == Progress(0, count, "correct")
        assert all(port.feedback == "unknown" for port in initial.ports)
        assert mode.preview(observation, context) == initial
        assert mode.preview(observation, context) == initial
        assert session.observe(observation, context, 5) == initial
        assert session.tick(observation, context, 10) == initial
    assert observation.identities is None
    assert mode.new_session() is not mode.new_session()


def test_led_absence_and_all_off_are_distinct(valid_snapshot):
    assert validate_snapshot(valid_snapshot).led_intent is None
    explicit = replace(valid_snapshot, led_intent=(LedState(False, False),) * 48)
    assert validate_snapshot(explicit).led_intent == (LedState(False, False),) * 48
    mixed = replace(
        valid_snapshot, led_intent=(LedState(True, False), LedState(False, True)) * 24
    )
    assert validate_snapshot(mixed) is mixed


@pytest.mark.parametrize(
    "changes",
    [
        {"objective": ""},
        {"objective": "  "},
        {"objective": 1},
        {"stage": 1},
        {"complete": 1},
        {"complete": None},
        {"progress": {}},
        {"progress": Progress(True, 48, "correct")},
        {"progress": Progress(1.0, 48, "correct")},
        {"progress": Progress(-1, 48, "correct")},
        {"progress": Progress(0, False, "correct")},
        {"progress": Progress(0, 1.0, "correct")},
        {"progress": Progress(0, 0, "correct")},
        {"progress": Progress(0, -1, "correct")},
        {"progress": Progress(0, 48, "")},
        {"progress": Progress(0, 48, " ")},
        {"progress": Progress(0, 48, 1)},
        {"ports": []},
        {"ports": ()},
        {"ports": (PortPresentation(True, "open"),) * 47},
        {"ports": (PortPresentation(True, "open"),) * 49},
        {"ports": (PortPresentation(1, "open"),) * 48},
        {"ports": (PortPresentation(True, "inactive"),) * 48},
        {"ports": (PortPresentation(True, []),) * 48},
        {"ports": ({"required": True, "feedback": "open"},) * 48},
        {"led_intent": []},
        {"led_intent": ()},
        {"led_intent": (LedState(False, False),) * 47},
        {"led_intent": (LedState(False, False),) * 49},
        {"led_intent": (LedState(0, False),) * 48},
        {"led_intent": (LedState(False, 1),) * 48},
        {"led_intent": ((False, False),) * 48},
    ],
)
def test_malformed_snapshot_rejected(valid_snapshot, changes):
    with pytest.raises(ValueError):
        validate_snapshot(replace(valid_snapshot, **changes))


def test_mutable_lists_and_dataclass_subclasses_are_rejected(valid_snapshot):
    class DerivedSnapshot(ModeSnapshot):
        pass

    class DerivedPort(PortPresentation):
        pass

    class DerivedProgress(Progress):
        pass

    class DerivedLed(LedState):
        pass

    invalid = [
        None,
        {},
        DerivedSnapshot("Patch", None, None, valid_snapshot.ports, False),
        replace(valid_snapshot, ports=list(valid_snapshot.ports)),
        replace(valid_snapshot, led_intent=[LedState(False, False)] * 48),
        replace(valid_snapshot, ports=(DerivedPort(True, "open"),) * 48),
        replace(valid_snapshot, progress=DerivedProgress(0, 1, "correct")),
        replace(valid_snapshot, led_intent=(DerivedLed(False, False),) * 48),
    ]
    for snapshot in invalid:
        with pytest.raises(ValueError):
            validate_snapshot(snapshot)


def test_optional_progress_and_progress_above_total_are_valid(valid_snapshot):
    for progress in (None, Progress(49, 48, "correct")):
        snapshot = replace(valid_snapshot, progress=progress, stage="")
        assert validate_snapshot(snapshot) is snapshot


def test_presentation_values_are_frozen(valid_snapshot):
    for value, attribute in [
        (valid_snapshot, "complete"),
        (valid_snapshot.progress, "current"),
        (valid_snapshot.ports[0], "required"),
        (EvaluationContext(True), "verify"),
        (LedState(False, False), "yellow"),
        (BUILTIN_MODES.metadata()[0], "id"),
        (BUILTIN_MODES.get("full-field"), "label"),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, attribute, None)


def test_registry_metadata_order_and_fresh_sessions():
    metadata = BUILTIN_MODES.metadata()
    assert type(metadata) is tuple
    assert [
        (m.id, m.label, m.description, m.required_port_count) for m in metadata
    ] == [
        ("full-field", "Full Field", "Patch all 24 columns.", 48),
        ("half-field", "Half Field", "Patch the leftmost 12 columns.", 24),
        ("quarter-field", "Quarter Field", "Patch the leftmost six columns.", 12),
    ]
    for item in metadata:
        mode = BUILTIN_MODES.get(item.id)
        assert mode.new_session() is not mode.new_session()


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"id": " "},
        {"id": 1},
        {"label": ""},
        {"label": " "},
        {"label": None},
        {"description": ""},
        {"description": " "},
        {"description": 1},
        {"required_port_count": True},
        {"required_port_count": 0},
        {"required_port_count": -1},
        {"required_port_count": 1.0},
        {"session_factory": None},
        {"preview": None},
    ],
)
def test_invalid_registry_definitions_rejected(changes):
    with pytest.raises(ValueError):
        ModeRegistry([replace(BUILTIN_MODES.get("full-field"), **changes)])


def test_registry_rejects_duplicates_and_unknown_ids():
    mode = BUILTIN_MODES.get("full-field")
    with pytest.raises(ValueError):
        ModeRegistry([mode, mode])
    with pytest.raises(ValueError):
        ModeRegistry([None])
    for mode_id in ("missing", "", None, []):
        with pytest.raises(ValueError):
            BUILTIN_MODES.get(mode_id)


def test_custom_registry_accepts_optional_count_and_copies_definitions():
    base = BUILTIN_MODES.get("full-field")
    mode = GameMode(
        "custom", "Custom", "Custom objective", None, base.session_factory, base.preview
    )
    definitions = [mode]
    registry = ModeRegistry(definitions)
    definitions.clear()
    assert registry.get("custom") is mode
    assert registry.metadata()[0].required_port_count is None


def test_staged_sessions_advance_independently_with_ticks_and_pure_preview():
    from mode_fixtures import STAGED_MODE

    context = EvaluationContext(True)
    unknown = GameBoard().snapshot()
    first, second = STAGED_MODE.new_session(), STAGED_MODE.new_session()
    initial = first.initialize(unknown, context, 0)
    assert second.initialize(unknown, context, 0) == initial
    assert initial.stage == "Patch"
    assert tuple(i for i, port in enumerate(initial.ports) if port.required) == (0,)
    wrong = GameBoard().update([0] * 48)
    assert first.observe(wrong, EvaluationContext(False), 50).stage == "Patch"
    patched = GameBoard().update([EXPECTED_IDS[0]] + [255] * 47)
    waiting = first.observe(patched, context, 100)
    assert waiting.stage == "Wait"
    assert not waiting.complete
    assert tuple(i for i, port in enumerate(waiting.ports) if port.required) == (1,)
    assert STAGED_MODE.preview(unknown, context) == initial
    assert STAGED_MODE.preview(unknown, context) == initial
    assert second.tick(unknown, context, 3_000_000_000).stage == "Patch"
    assert not first.tick(patched, context, 2_000_000_099).complete
    assert first.tick(patched, context, 2_000_000_100).complete
    assert unknown.identities is None
    assert patched.identities == (EXPECTED_IDS[0],) + (255,) * 47
    assert second.observe(patched, context, 3_000_000_000).stage == "Wait"
    assert not second.tick(patched, context, 3_000_000_001).complete
    assert first.initialize(unknown, context, 0) == initial


def test_failure_fixtures_expose_factory_preview_and_session_failures():
    from mode_fixtures import FAILING_FACTORY_MODE, FAILING_PREVIEW_MODE, failing_mode

    observation, context = GameBoard().snapshot(), EvaluationContext(True)
    with pytest.raises(RuntimeError, match="factory"):
        FAILING_FACTORY_MODE.new_session()
    with pytest.raises(RuntimeError, match="preview"):
        FAILING_PREVIEW_MODE.preview(observation, context)
    for operation in ("initialize", "observe", "tick"):
        for malformed in (False, True):
            mode = failing_mode(operation, malformed=malformed)
            session = mode.new_session()
            if operation != "initialize":
                validate_snapshot(session.initialize(observation, context, 0))
            if malformed:
                with pytest.raises(ValueError):
                    validate_snapshot(
                        getattr(session, operation)(observation, context, 1)
                    )
            else:
                with pytest.raises(RuntimeError, match=operation):
                    getattr(session, operation)(observation, context, 1)
