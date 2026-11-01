import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import pytest

from app import create_app
from leaderboard import LeaderboardStore

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index.html"


class DashboardHTML(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.parents = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        element = dict(attrs) | {"tag": tag, "parents": self.parents.copy()}
        self.elements.append(element)
        if tag not in {"meta", "link", "img", "input", "br", "hr"}:
            self.parents.append((tag, element.get("id")))

    def handle_endtag(self, tag):
        for index in range(len(self.parents) - 1, -1, -1):
            if self.parents[index][0] == tag:
                del self.parents[index:]
                break

    def by_id(self, element_id):
        return next(item for item in self.elements if item.get("id") == element_id)


@pytest.fixture
def dashboard(tmp_path):
    app, _ = create_app(
        {
            "TESTING": True,
            "START_SERIAL_ON_CONNECT": False,
            "START_BACKGROUND_TASKS": False,
        },
        store=LeaderboardStore(tmp_path / "scores.sqlite3"),
    )
    client = app.test_client()
    return client, DashboardHTML(client.get("/").get_data(as_text=True))


def run_dashboard(functions, script, data=None):
    source = TEMPLATE.read_text()
    helpers = []
    for name in functions:
        start = source.index(f"    function {name}(")
        end = source.index("\n    }", start) + len("\n    }")
        helpers.append(source[start:end])
    program = """
const data = JSON.parse(process.argv[1]);
const elements = {};
function element(id) {
    return elements[id] ||= {textContent: "", hidden: false, disabled: false, style: {},
        previousElementSibling: {textContent: ""},
        classList: {toggle() {}}, setAttribute(name, value) { this[name] = value; }};
}
const document = {getElementById: element};
"""
    result = subprocess.run(
        ["node", "-e", program + "\n".join(helpers) + script, json.dumps(data)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_header_reading_order_puts_selfnet_before_game_title(dashboard):
    _, html = dashboard
    branding = [
        item
        for item in html.elements
        if (item.get("alt") == "Selfnet" or item.get("id") == "game-title")
        and any(tag == "header" for tag, _ in item["parents"])
    ]
    assert [item["tag"] for item in branding] == ["img", "h1"]


def test_current_event_title_belongs_to_heading_and_starts_hidden(dashboard):
    _, html = dashboard
    title = html.by_id("currentEventTitle")
    assert ("h1", "game-title") in title["parents"]
    assert "hidden" in title


def test_dashboard_brand_and_css_assets_are_served_locally(dashboard):
    client, html = dashboard
    logos = [item for item in html.elements if item["tag"] == "img"]
    assert any(item.get("alt") == "Selfnet" for item in logos)
    styles = [item for item in html.elements if item.get("rel") == "stylesheet"]
    assert styles, "Dashboard must load its stylesheet"
    urls = [item["src"] for item in logos]
    for style in styles:
        assert style["href"].startswith("/static/")
        response = client.get(style["href"])
        assert response.status_code == 200
        assert response.mimetype == "text/css"
        urls.extend(
            urljoin(style["href"], asset)
            for asset in re.findall(r'url\(["\']?([^"\')]+)', response.text)
        )
    mimetypes = set()
    for url in urls:
        assert url.startswith("/static/")
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.data
        mimetypes.add(response.mimetype)
    assert {"image/svg+xml", "image/jpeg", "font/ttf"} <= mimetypes


@pytest.mark.parametrize(
    "control",
    [
        "modeButton",
        "leaderboardButton",
        "idleLeaderboardSlot",
        "viewLeaderboardSlot",
        "gameInstructions",
        "clockButton",
    ],
)
def test_game_controls_belong_to_shared_footer(dashboard, control):
    _, html = dashboard
    parents = html.by_id(control)["parents"]
    assert any(tag == "footer" for tag, _ in parents)
    assert not any(
        element_id in {"raceView", "nameEntryView", "leaderboardView"}
        for _, element_id in parents
    )


@pytest.mark.parametrize(
    "phase,browsing,mode_disabled,leaderboard_disabled,status",
    [
        ("ready", False, False, False, "Ready"),
        ("racing", False, True, True, "Racing"),
        ("stopped", False, True, False, "Stopped"),
        ("name_entry", False, True, True, "Finished"),
        ("leaderboard", True, True, True, "Finished"),
        ("ready", True, True, False, "Ready"),
    ],
)
def test_footer_controls_track_live_phase(
    phase, browsing, mode_disabled, leaderboard_disabled, status
):
    result = run_dashboard(
        ["showPhase"],
        """
const browsing = data.browsing ? {raceId: null} : null;
const gameState = {phase: data.phase, race_id: "race-1"};
const lastStateRevision = 1;
const closedLeaderboardRaceId = null;
showPhase(data.phase);
console.log(JSON.stringify(elements));
""",
        {"phase": phase, "browsing": browsing},
    )
    assert not result["modeButton"]["hidden"]
    assert result["modeButton"]["disabled"] is mode_disabled
    assert result["leaderboardButton"]["hidden"] is browsing
    assert result["leaderboardButton"]["disabled"] is leaderboard_disabled
    assert result["gameStatus"]["textContent"] == status
    assert result["closeLeaderboard"]["hidden"] is not browsing
    assert result["nameEntryCountdown"]["hidden"] is (phase != "name_entry")
    assert result["leaderboardCountdown"]["hidden"] is not browsing
    assert result["clockButton"]["hidden"] is (
        browsing or phase not in {"ready", "racing", "stopped"}
    )
    if not result["clockButton"]["hidden"]:
        assert (
            result["clockAction"]["textContent"]
            == {"ready": "Start", "racing": "Stop", "stopped": "Reset"}[phase]
        )
    assert result["modeShortcut"]["hidden"] is (browsing or phase != "name_entry")


@pytest.mark.parametrize(
    "phase", ["ready", "racing", "stopped", "name_entry", "leaderboard"]
)
@pytest.mark.parametrize("browsing_open", [False, True])
def test_clock_button_uses_space_action_only_in_playable_views(phase, browsing_open):
    binding = next(
        (
            line
            for line in TEMPLATE.read_text().splitlines()
            if 'getElementById("clockButton").addEventListener' in line
        ),
        "",
    )
    result = run_dashboard(
        [],
        """
const gameState = {phase: data.phase};
const browsing = data.browsing ? {} : null;
const lastStateRevision = 1;
let calls = 0, click = () => {};
async function advanceClockWithSpacebar() { calls++; }
element("clockButton").addEventListener = (_, callback) => { click = callback; };
"""
        + binding
        + "\nclick();\nconsole.log(JSON.stringify(calls));",
        {"phase": phase, "browsing": browsing_open},
    )
    assert result == int(not browsing_open and phase in {"ready", "racing", "stopped"})


def test_port_layout_exposes_all_physical_ports_as_read_only_list_items():
    result = run_dashboard(
        ["createBoard", "updateBoard"],
        """
const NUM_CELLS = 48;
const PLAYER_IDS = [1, 2];
const players = {1: {states: Array(48).fill(0)}};
const board = element("statusBoard1");
board.children = [];
board.appendChild = (cell) => { board.children.push(cell); elements[cell.id] = cell; };
document.createElement = () => ({
    style: {setProperty(name, value) { this[name] = value; }},
    classList: {toggle(name, on) { this[name] = on; }},
    setAttribute(name, value) { this[name] = value; }, addEventListener() {},
});
function updateStats() {}
createBoard(1);
updateBoard(1, Array.from({length: 48}, (_, index) => ({
    required: index < 4,
    feedback: ["open", "correct", "wrong", "unknown"][index % 4],
})));
console.log(JSON.stringify(board.children));
""",
    )
    assert len(result) == 48
    assert [cell["textContent"] for cell in result] == list(range(48))
    assert [cell["style"]["--switch-column"] for cell in result[:4]] == [1, 1, 2, 2]
    assert [cell["style"]["--switch-row"] for cell in result[:4]] == [1, 2, 1, 2]
    assert result[-1]["style"] == {"--switch-column": 24, "--switch-row": 2}
    assert all(cell["role"] == "listitem" for cell in result)
    assert result[1]["classList"]["is-patched"]
    assert result[2]["classList"]["is-wrong"]
    assert result[3]["classList"]["is-unknown"]
    assert result[4]["classList"]["is-inactive"]
    assert result[2]["aria-label"] == "Port 2, required, wrong"
    assert result[5]["aria-label"] == "Port 5, not required, correct"


def test_name_entry_has_an_explicit_submit_control(dashboard):
    _, html = dashboard
    buttons = [item for item in html.elements if item["tag"] == "button"]
    assert any(
        item.get("id") == "submitNamesButton"
        and ("section", "nameEntryView") in item["parents"]
        for item in buttons
    )


@pytest.mark.parametrize("order", [[1, 2], [2, 1]])
@pytest.mark.parametrize("names", [["", ""], ["Ada", ""]])
def test_submit_button_saves_both_names_even_when_click_keeps_first_input_focused(
    order, names
):
    source = TEMPLATE.read_text()
    helpers = source[source.index("    function submitNames() {") :].split(
        "    async function advanceClockWithSpacebar()", 1
    )[0]
    binding = next(
        line
        for line in source.splitlines()
        if 'getElementById("submitNamesButton").addEventListener' in line
    )
    result = run_dashboard(
        [],
        helpers
        + """
const emitted = [];
const gameState = {phase: "name_entry", race_id: "original-race"};
const socket = {emit(event, payload) { emitted.push({event, ...payload}); }};
const inputs = data.order.map((player, index) => ({
    dataset: {playerNumber: String(player)}, value: data.names[index],
    focus() { document.activeElement = this; },
}));
document.querySelectorAll = () => inputs;
document.activeElement = inputs[0];
element("submitNamesButton").addEventListener = (event, callback) => callback();
"""
        + binding
        + "\nconsole.log(JSON.stringify(emitted));",
        {"order": order, "names": names},
    )
    assert result == [
        {
            "event": "submit_name",
            "race_id": "original-race",
            "player_number": player,
            "draft": name,
        }
        for player, name in zip(order, names, strict=True)
    ]


@pytest.mark.parametrize(
    "presentation,objective,stage,count,remaining",
    [
        (
            {
                "objective": "Patch all ports",
                "stage": None,
                "progress": {"current": 12, "total": 48, "unit": "correct"},
            },
            "Patch all ports",
            "",
            "12 / 48",
            "36",
        ),
        (
            {
                "objective": "Patch port 0, then wait two seconds.",
                "stage": "Wait",
                "progress": {"current": 500000000, "total": 2000000000, "unit": "ns"},
            },
            "Patch port 0, then wait two seconds.",
            "Wait",
            "500000000 / 2000000000",
            "1500000000",
        ),
        (
            {"objective": "Unplug everything", "stage": "Clear", "progress": None},
            "Unplug everything",
            "Clear",
            "-",
            "-",
        ),
    ],
)
def test_player_lanes_render_mode_objective_stage_and_progress(
    presentation, objective, stage, count, remaining
):
    result = run_dashboard(
        [
            "formatDuration",
            "currentDurationMs",
            "progressFor",
            "patchRate",
            "renderStat",
            "updateStats",
        ],
        """
const PLAYER_IDS = [1, 2];
const players = {1: {durationMs: 12500}, 2: {durationMs: 9000}};
const gameState = {players: {"1": {presentation: data}, "2": {presentation: data}}};
const clockState = {activePlayers: {1: false, 2: false}, syncedAt: performance.now()};
updateStats();
console.log(JSON.stringify(elements));
""",
        presentation,
    )
    for player in (1, 2):
        assert (
            result.get(f"playerObjective{player}", {}).get("textContent") == objective
        )
        assert result[f"playerStage{player}"]["textContent"] == stage
        assert result[f"playerStage{player}"]["hidden"] is (not stage)
        assert result[f"patchCount{player}"]["textContent"] == count
        assert str(result[f"remaining{player}"]["textContent"]) == remaining
    assert result["clockDisplay1"]["textContent"] == "00:12.500"
    assert result["clockDisplay2"]["textContent"] == "00:09.000"
