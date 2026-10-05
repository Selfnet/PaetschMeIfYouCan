import json
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index.html"


def run_keyboard(scenario):
    source = TEMPLATE.read_text()
    helpers = source.split("    function submitNames() {", 1)[1].split(
        "    async function advanceClockWithSpacebar()", 1
    )[0]
    handler = source.split('    document.addEventListener("keydown", (event) => {', 1)[
        1
    ].split("\n    });", 1)[0]
    script = r"""
const scenario = JSON.parse(process.argv[1]);
const emitted = [];
const inputs = scenario.order.map(player => ({
    value: scenario.names[player - 1],
    disabled: Boolean(scenario.resolved?.includes(player)),
    dataset: {playerNumber: String(player)},
    focus() { document.activeElement = this; },
    closest() { return this; },
}));
const document = {
    activeElement: scenario.editable === false
        ? {id: "leaderboardTitle", closest() { return null; }}
        : inputs.find(input => Number(input.dataset.playerNumber) === scenario.focus),
    querySelectorAll() { return inputs.filter(input => !input.disabled); },
    getElementById() { return {open: false}; },
};
const gameState = {phase: scenario.phase || "name_entry", race_id: "original-race"};
const browsing = scenario.loading ? {waitingForPersistence: true} : null;
const socket = {emit(event, data) {
    emitted.push({event, ...data});
    if (scenario.replaceRace) gameState.race_id = "replacement-race";
}};
"""
    script += "\nfunction submitNames() {" + helpers
    script += "\nconst handler = (event) => {" + handler + "\n};\n"
    script += r"""
const prevented = [];
for (const key of scenario.keys) {
    let defaultPrevented = false;
    handler({key: key.key, code: key.key, repeat: Boolean(key.repeat),
        shiftKey: Boolean(key.shift), target: document.activeElement || inputs[0],
        preventDefault() { defaultPrevented = true; },
    });
    prevented.push(defaultPrevented);
}
console.log(JSON.stringify({emitted, prevented,
    focus: Number(document.activeElement?.dataset?.playerNumber),
    disabled: inputs.map(input => input.disabled),
}));
"""
    result = subprocess.run(
        ["node", "-e", script, json.dumps(scenario)],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("order", [[1, 2], [2, 1]])
@pytest.mark.parametrize(
    ("names", "unnamed"),
    [(["Ada", ""], 2), (["", "Grace"], 1), (["", ""], None), (["Ada", "  "], 2)],
)
def test_enter_focuses_unnamed_player_without_resolving(order, names, unnamed):
    result = run_keyboard(
        {"order": order, "names": names, "focus": order[0], "keys": [{"key": "Enter"}]}
    )
    assert result["emitted"] == []
    assert result["focus"] == (unnamed or order[0])
    assert result["disabled"] == [False, False]
    assert result["prevented"] == [True]


@pytest.mark.parametrize("order", [[1, 2], [2, 1]])
def test_enter_submits_both_named_players_with_original_race(order):
    result = run_keyboard(
        {
            "order": order,
            "names": ["Ada", "Grace"],
            "focus": order[0],
            "replaceRace": True,
            "keys": [{"key": "Enter"}],
        }
    )
    assert result["emitted"] == [
        {
            "event": "submit_name",
            "race_id": "original-race",
            "player_number": player,
            "draft": ["Ada", "Grace"][player - 1],
        }
        for player in order
    ]


@pytest.mark.parametrize("order", [[1, 2], [2, 1]])
@pytest.mark.parametrize("names", [["Ada", "Grace"], ["Ada", ""], ["", ""]])
@pytest.mark.parametrize("shift", [False, True])
def test_tab_cycles_inputs_without_submission(order, names, shift):
    result = run_keyboard(
        {
            "order": order,
            "names": names,
            "focus": order[0],
            "keys": [{"key": "Tab", "shift": shift}] * 3,
        }
    )
    assert result["focus"] == order[1]
    assert result["emitted"] == []
    assert result["disabled"] == [False, False]
    assert result["prevented"] == [True] * 3


@pytest.mark.parametrize("names", [["Ada", "Grace"], ["Ada", ""]])
def test_repeated_enter_does_not_submit_or_move_focus(names):
    result = run_keyboard(
        {
            "order": [1, 2],
            "names": names,
            "focus": 1,
            "keys": [{"key": "Enter", "repeat": True}],
        }
    )
    assert result["focus"] == 1
    assert result["emitted"] == []
    assert result["prevented"] == [True]


def test_enter_submits_only_remaining_named_player():
    result = run_keyboard(
        {
            "order": [1, 2],
            "names": ["Ada", "Grace"],
            "focus": 2,
            "resolved": [1],
            "keys": [{"key": "Enter"}],
        }
    )
    assert result["emitted"] == [
        {
            "event": "submit_name",
            "race_id": "original-race",
            "player_number": 2,
            "draft": "Grace",
        }
    ]


@pytest.mark.parametrize("phase", ["ready", "racing", "leaderboard"])
@pytest.mark.parametrize("editable", [False, True])
def test_enter_outside_name_entry_never_submits_names(phase, editable):
    result = run_keyboard(
        {
            "order": [1, 2],
            "names": ["Ada", "Grace"],
            "focus": 1,
            "phase": phase,
            "loading": True,
            "editable": editable,
            "keys": [{"key": "Enter"}],
        }
    )
    assert result["emitted"] == []
