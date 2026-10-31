import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_dashboard import TEMPLATE, run_dashboard


def test_overlay_tracks_active_phase_without_reopening_on_board_updates():
    result = run_dashboard(
        ["renderMaintenance"],
        """
const overlay = element("maintenanceOverlay");
const actions = [];
overlay.open = false;
overlay.showModal = () => { overlay.open = true; actions.push("open"); };
overlay.close = () => { overlay.open = false; actions.push("close"); };
for (const phase of ["racing", "name_entry", "leaderboard", "maintenance", "maintenance", "ready"]) {
    renderMaintenance(phase);
}
console.log(JSON.stringify(actions));
""",
    )
    assert result == ["open", "close"]


@pytest.mark.parametrize(
    "key", ["Space", "KeyM", "KeyL", "KeyV", "KeyS", "Escape", "1"]
)
def test_maintenance_keyboard_cannot_issue_game_commands(key):
    handler = (
        TEMPLATE.read_text()
        .split('document.addEventListener("keydown", (event) => {', 1)[1]
        .split("\n    });", 1)[0]
    )
    result = run_dashboard(
        [],
        """
const gameState = {phase: "maintenance"};
const browsing = null;
const commands = [];
function openModeSelector() { commands.push("mode"); }
function toggleLeaderboard() { commands.push("leaderboard"); }
function fetch(url) { commands.push(url); }
let prevented = false;
const event = {key: data, code: data, target: {closest() { return null; }}, preventDefault() { prevented = true; }};
(function(event) {
"""
        + handler
        + "\n})(event); console.log(JSON.stringify({commands, prevented}));",
        key,
    )
    assert result == {"commands": [], "prevented": True}


def run_control(script):
    source = (
        Path(__file__).resolve().parents[1] / "templates/control.html"
    ).read_text()
    inline = re.findall(r"<script>(.*?)</script>", source, re.DOTALL)
    program = """
const elements = {};
const handlers = {}, managerHandlers = {}, requests = [];
const document = {getElementById(id) { return elements[id] ||= {
    checked: false, disabled: false, textContent: "", hidden: false,
    addEventListener(event, fn) { this[event] = fn; },
}; }};
const socketMock = {
    on(event, fn) { handlers[event] = fn; },
    io: {on(event, fn) { managerHandlers[event] = fn; }},
    timeout() { return this; },
    emit(event, data, callback) { requests.push({event, data, callback}); },
};
function io() { return socketMock; }
"""
    result = subprocess.run(
        ["node", "-e", program + "\n".join(inline) + script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_control_displays_queued_active_and_off_from_authoritative_state():
    result = run_control("""
const states = [];
for (const [revision, phase, maintenance_requested] of [[0, "ready", false], [1, "racing", true], [2, "maintenance", true], [3, "ready", false]]) {
    handlers.game_state({revision, phase, maintenance_requested});
    states.push({checked: elements.maintenanceToggle.checked, disabled: elements.maintenanceToggle.disabled, text: elements.maintenanceStatus.textContent});
}
console.log(JSON.stringify(states));
""")
    assert [item["checked"] for item in result] == [False, True, True, False]
    assert all(not item["disabled"] for item in result)
    assert [item["text"] for item in result] == [
        "Off",
        "Queued until the current game and results finish",
        "Active",
        "Off",
    ]


def test_control_save_failure_restores_checkbox_and_disconnect_disables_it():
    result = run_control("""
handlers.game_state({revision: 0, phase: "ready", maintenance_requested: false});
elements.maintenanceToggle.checked = true;
elements.maintenanceToggle.change();
const pending = elements.maintenanceToggle.disabled;
requests[0].callback(null, {accepted: false, error: "Save failed"});
const failed = {checked: elements.maintenanceToggle.checked, disabled: elements.maintenanceToggle.disabled, error: elements.maintenanceError.textContent};
handlers.disconnect();
console.log(JSON.stringify({pending, failed, disconnected: elements.maintenanceToggle.disabled, request: {event: requests[0].event, data: requests[0].data}}));
""")
    assert result == {
        "pending": True,
        "failed": {"checked": False, "disabled": False, "error": "Save failed"},
        "disconnected": True,
        "request": {"event": "set_maintenance", "data": {"enabled": True}},
    }
