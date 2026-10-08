import json
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index.html"


def run_slot_keyboard(scenario):
    source = TEMPLATE.read_text()
    helpers = source.split("    function updateLeaderboardSlotControls() {", 1)[
        1
    ].split("    function submitNames() {", 1)[0]
    handler = source.split('    document.addEventListener("keydown", (event) => {', 1)[
        1
    ].split("\n    });", 1)[0]
    script = r"""
const scenario = JSON.parse(process.argv[1]);
const emitted = [];
const controls = [{}, {}];
const elements = {};
const document = {
    querySelectorAll() { return controls; },
    getElementById(id) {
        if (!elements[id]) elements[id] = {open: Boolean(scenario.modal)};
        return elements[id];
    },
};
const gameState = {phase: scenario.phase || "ready", leaderboard_slot: scenario.current || 1};
const lastStateRevision = scenario.disconnected ? null : 1;
let pendingLeaderboardSlot = scenario.pending ? {} : null;
const browsing = scenario.browsing ? {raceId: null} : null;
const socket = {emit(event, data, callback) {
    emitted.push({event, ...data});
    if (scenario.ack) {
        if (scenario.ack.accepted) gameState.leaderboard_slot = data.leaderboard_slot;
        callback(scenario.ack);
    }
}};
"""
    script += "\nfunction updateLeaderboardSlotControls() {" + helpers
    script += "\nconst handler = (event) => {" + handler + "\n};\n"
    script += r"""
const prevented = [];
for (const key of scenario.keys || [scenario.key]) {
    let defaultPrevented = false;
    handler({key, code: scenario.numpad ? `Numpad${key}` : `Digit${key}`,
        repeat: Boolean(scenario.repeat), shiftKey: Boolean(scenario.shift),
        ctrlKey: Boolean(scenario.ctrl), metaKey: Boolean(scenario.meta),
        altKey: Boolean(scenario.alt), isComposing: Boolean(scenario.composing),
        target: {closest() { return scenario.editable ? {} : null; }},
        preventDefault() { defaultPrevented = true; },
    });
    prevented.push(defaultPrevented);
}
console.log(JSON.stringify({emitted, prevented, controls,
    pending: pendingLeaderboardSlot !== null, elements,
    selected: gameState.leaderboard_slot,
}));
"""
    result = subprocess.run(
        ["node", "-e", script, json.dumps(scenario)],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("slot", range(1, 10))
@pytest.mark.parametrize("browsing", [False, True])
@pytest.mark.parametrize("numpad", [False, True])
def test_digits_select_slots_from_ready_game_or_idle_leaderboard(
    slot, browsing, numpad
):
    result = run_slot_keyboard(
        {
            "key": str(slot),
            "current": 9 if slot == 1 else 1,
            "browsing": browsing,
            "numpad": numpad,
            "ack": {"accepted": True},
        }
    )
    assert result["emitted"] == [
        {"event": "select_leaderboard_slot", "leaderboard_slot": slot}
    ]
    assert result["prevented"] == [True]
    assert result["selected"] == slot
    assert all(
        control["value"] == str(slot) and not control["disabled"]
        for control in result["controls"]
    )


@pytest.mark.parametrize("phase", ["racing", "stopped", "name_entry", "leaderboard"])
def test_digits_do_not_change_slot_during_game_or_post_race(phase):
    result = run_slot_keyboard({"key": "2", "phase": phase})
    assert result["emitted"] == []
    assert result["selected"] == 1
    assert result["prevented"] == [False]


@pytest.mark.parametrize(
    "guard",
    [
        "ctrl",
        "meta",
        "alt",
        "shift",
        "composing",
        "editable",
        "repeat",
        "modal",
        "disconnected",
        "pending",
    ],
)
def test_slot_shortcut_respects_keyboard_and_connection_guards(guard):
    result = run_slot_keyboard({"key": "2", guard: True})
    assert result["emitted"] == []
    assert result["selected"] == 1


@pytest.mark.parametrize("key", ["0", "!", "a"])
def test_other_keys_do_not_select_slots(key):
    assert run_slot_keyboard({"key": key})["emitted"] == []


def test_current_slot_shortcut_does_not_restart_browsing():
    result = run_slot_keyboard({"key": "1", "browsing": True})
    assert result["emitted"] == []
    assert result["prevented"] == [True]


def test_selection_waits_for_ack_and_keeps_authoritative_slot():
    result = run_slot_keyboard({"keys": ["2", "3"]})
    assert result["emitted"] == [
        {"event": "select_leaderboard_slot", "leaderboard_slot": 2}
    ]
    assert result["pending"]
    assert result["selected"] == 1
    assert all(
        control["value"] == "1" and control["disabled"]
        for control in result["controls"]
    )


def test_rejected_selection_restores_controls_and_shows_error_on_both_views():
    result = run_slot_keyboard(
        {"key": "2", "ack": {"accepted": False, "error": "Could not save slot"}}
    )
    assert not result["pending"]
    assert result["selected"] == 1
    assert all(
        control["value"] == "1" and not control["disabled"]
        for control in result["controls"]
    )
    for element in ("slotSelectionError", "leaderboardSlotError"):
        assert result["elements"][element]["textContent"] == "Could not save slot"
        assert not result["elements"][element]["hidden"]
