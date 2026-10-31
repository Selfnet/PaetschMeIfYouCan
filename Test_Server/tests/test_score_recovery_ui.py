import pytest

from tests.test_dashboard import TEMPLATE, run_dashboard


@pytest.mark.parametrize("action", ["Back", "KeyL", "Escape", "Space"])
@pytest.mark.parametrize("browsing_open", [False, True])
@pytest.mark.parametrize("confirmed", [False, True])
def test_unsaved_exit_paths_confirm_and_wait_for_server(
    action, browsing_open, confirmed
):
    source = TEMPLATE.read_text()
    handler = source.split('    document.addEventListener("keydown", (event) => {', 1)[
        1
    ].split("\n    });", 1)[0]
    binding = next(
        line
        for line in source.splitlines()
        if 'getElementById("closeLeaderboard").addEventListener' in line
    )
    result = run_dashboard(
        ["closeLeaderboard"],
        """
let browsing = data.browsing ? {raceId: "race-1", pendingTimer: null} : null;
const gameState = {phase: "leaderboard", race_id: "race-1", browse_epoch: 12,
    persistence_status: "error", persistence_error: "Scores could not be saved"};
const lastStateRevision = 20;
let pendingScoreRetry = null;
let closedLeaderboardRaceId = null;
let activityTimer, lastActivityAt, scrollIntentUntil, suppressScrollUntil, scrollbarDragging;
const emitted = [], confirmations = [];
const socket = {emit(event, payload) { emitted.push({event, ...payload}); }};
const window = {confirm(message) { confirmations.push(message); return data.confirmed; }};
const focus = {closest() { return null; }};
document.activeElement = focus;
element("game-title").focus = () => {};
function showPhase() {}
let click;
element("closeLeaderboard").addEventListener = (_, callback) => { click = callback; };
"""
        + binding
        + "\nconst handler = (event) => {"
        + handler
        + "\n};\n"
        + """
if (data.action === "Back") click();
else handler({key: data.action, code: data.action, target: focus, preventDefault() {}});
console.log(JSON.stringify({emitted, confirmations, browsingOpen: Boolean(browsing)}));
""",
        {"action": action, "browsing": browsing_open, "confirmed": confirmed},
    )
    assert len(result["confirmations"]) == 1
    assert result["browsingOpen"] is browsing_open
    assert result["emitted"] == (
        [
            {
                "event": "dismiss_leaderboard",
                "race_id": "race-1",
                "browse_epoch": 12,
                "discard_unsaved": True,
            }
        ]
        if confirmed
        else []
    )


@pytest.mark.parametrize("pending", ["local", "server"])
def test_pending_retry_blocks_exit_without_prompting(pending):
    result = run_dashboard(
        ["closeLeaderboard"],
        """
let browsing = {raceId: "race-1", pendingTimer: null};
const gameState = {phase: "leaderboard", race_id: "race-1", browse_epoch: 12,
    persistence_status: data === "server" ? "pending" : "error",
    persistence_error: "Scores could not be saved"};
const lastStateRevision = 20;
let pendingScoreRetry = data === "local" ? {} : null;
let closedLeaderboardRaceId = null;
let activityTimer, lastActivityAt, scrollIntentUntil, suppressScrollUntil, scrollbarDragging;
const emitted = [], confirmations = [];
const socket = {emit(event, payload) { emitted.push({event, ...payload}); }};
const window = {confirm(message) { confirmations.push(message); return true; }};
element("game-title").focus = () => {};
function showPhase() {}
closeLeaderboard();
console.log(JSON.stringify({emitted, confirmations, browsingOpen: Boolean(browsing)}));
""",
        pending,
    )
    assert result == {"emitted": [], "confirmations": [], "browsingOpen": True}


def test_failure_countdown_explains_pause_instead_of_zero_seconds():
    result = run_dashboard(
        ["formatCountdown", "updateLeaderboardCountdown"],
        """
const browsing = {raceId: "race-1"};
const gameState = {remaining_seconds: null, persistence_status: "error"};
function checkLeaderboardExpiry() { return false; }
updateLeaderboardCountdown();
console.log(JSON.stringify(elements));
""",
    )
    assert "paused" in result["leaderboardCountdown"]["textContent"].lower()


def test_internal_browse_invalidation_does_not_mark_recovering_race_closed():
    result = run_dashboard(
        ["closeLeaderboard"],
        """
let browsing = {raceId: "race-1", pendingTimer: null};
const gameState = {phase: "leaderboard", race_id: "race-1", persistence_status: "pending"};
let closedLeaderboardRaceId = null;
let activityTimer, lastActivityAt, scrollIntentUntil, suppressScrollUntil, scrollbarDragging;
function showPhase() {}
closeLeaderboard({restoreFocus: false, dismiss: false});
console.log(JSON.stringify({browsing, closedLeaderboardRaceId}));
""",
    )
    assert result == {"browsing": None, "closedLeaderboardRaceId": None}


@pytest.mark.parametrize("status", ["error", "pending", "saved"])
def test_recovery_button_tracks_authoritative_save_status(status):
    result = run_dashboard(
        ["renderScoreRecovery"],
        """
const browsing = {raceId: "race-1"};
const gameState = {phase: "leaderboard", persistence_status: data,
    persistence_error: data === "saved" ? null : "Scores could not be saved"};
const lastStateRevision = 20;
const pendingScoreRetry = null;
renderScoreRecovery();
console.log(JSON.stringify(elements));
""",
        status,
    )
    assert result["retryScores"]["hidden"] is (status == "saved")
    assert result["retryScores"]["disabled"] is (status != "error")
    assert result["closeLeaderboard"]["disabled"] is (status == "pending")
    assert result["persistenceError"]["hidden"] is (status == "saved")


def test_retry_click_sends_only_original_race_token_and_blocks_duplicate_clicks():
    source = TEMPLATE.read_text()
    binding = next(
        (
            line
            for line in source.splitlines()
            if 'getElementById("retryScores").addEventListener' in line
        ),
        "",
    )
    result = run_dashboard(
        ["retryScores", "renderScoreRecovery"],
        """
const browsing = {raceId: "race-1"};
const gameState = {phase: "leaderboard", race_id: "race-1", browse_epoch: 12,
    persistence_status: "error", persistence_error: "Scores could not be saved"};
const lastStateRevision = 20;
let pendingScoreRetry = null;
const emitted = [];
let ack, click;
const socket = {timeout() { return this; }, emit(event, payload, callback) { emitted.push({event, ...payload}); ack = callback; }};
element("retryScores").addEventListener = (_, callback) => { click = callback; };
"""
        + binding
        + """
click();
click();
const pendingDisabled = element("retryScores").disabled;
ack(new Error("timeout"));
console.log(JSON.stringify({emitted, pendingDisabled, retryDisabled: element("retryScores").disabled}));
""",
    )
    assert result == {
        "emitted": [{"event": "retry_scores", "race_id": "race-1", "browse_epoch": 12}],
        "pendingDisabled": True,
        "retryDisabled": False,
    }


def test_space_on_retry_button_keeps_native_button_activation():
    source = TEMPLATE.read_text()
    handler = source.split('    document.addEventListener("keydown", (event) => {', 1)[
        1
    ].split("\n    });", 1)[0]
    result = run_dashboard(
        [],
        """
const gameState = {phase: "leaderboard"};
const browsing = {raceId: "race-1"};
let closed = false, prevented = false;
function closeLeaderboard() { closed = true; }
const button = element("retryScores");
button.closest = () => null;
"""
        + "\nconst handler = (event) => {"
        + handler
        + "\n};\n"
        + """
handler({key: " ", code: "Space", target: button, preventDefault() { prevented = true; }});
console.log(JSON.stringify({closed, prevented}));
""",
    )
    assert result == {"closed": False, "prevented": False}


def test_refresh_and_retry_reopen_results_with_new_page_identity():
    result = run_dashboard(
        [
            "renderGameState",
            "openLeaderboard",
            "closeLeaderboard",
            "newBrowsingState",
            "showPhase",
            "renderScoreRecovery",
            "requestNextPage",
            "updatePageControl",
            "checkLeaderboardExpiry",
            "updateLeaderboardCountdown",
            "formatCountdown",
        ],
        """
let browsing = null, lastStateRevision = null, pendingScoreRetry = null;
let closedLeaderboardRaceId = null, leaderboardRequestCounter = 0;
let activityTimer, lastActivityAt, scrollIntentUntil, suppressScrollUntil, scrollbarDragging;
let gameState = {phase: "ready", race_id: null, name_entry: {active_player: null}};
const emitted = [], PLAYER_IDS = [];
const clockState = {};
const socket = {emit(event, payload) { emitted.push({event, ...payload}); }};
class HTMLInputElement {}
document.querySelectorAll = () => [];
function updateModeOptions() {}
function updateLeaderboardSlotControls() {}
function closeModeSelector() {}
function updateStats() {}
function ensureClockAnimation() {}
function leaderboardPlaceholder() {}
function setTimeout() { return 1; }
function clearTimeout() {}
element("leaderboardRows").replaceChildren = () => {};
for (const id of ["leaderboardTitle", "game-title"]) element(id).focus = () => {};
const failed = {revision: 10, phase: "leaderboard", race_id: "race-1", browse_epoch: 6,
    leaderboard_slot: 4, mode: {id: "quarter-field", label: "Quarter Field"}, available_modes: [],
    result_order: [], name_entry: {active_player: null}, players: {}, remaining_seconds: null,
    persistence_status: "error", persistence_error: "Scores could not be saved"};
renderGameState(failed);
const refreshShowsRetry = !element("retryScores").hidden;
browsing.boundary = 7;
browsing.pendingOffset = null;
browsing.rows.set(99, {});
renderGameState({...failed, revision: 11, browse_epoch: 7, persistence_status: "pending"});
const pending = {requestCount: emitted.length, waiting: browsing?.waitingForPersistence,
    rows: browsing?.rows.size, retryDisabled: element("retryScores").disabled};
renderGameState({...failed, revision: 12, browse_epoch: 7, persistence_status: "saved",
    persistence_error: null, remaining_seconds: 60});
console.log(JSON.stringify({emitted, refreshShowsRetry, pending,
    retryHidden: element("retryScores").hidden, errorHidden: element("persistenceError").hidden,
    countdown: element("leaderboardCountdown").textContent}));
""",
    )
    assert result["refreshShowsRetry"] is True
    assert result["pending"] == {
        "requestCount": 1,
        "waiting": True,
        "rows": 0,
        "retryDisabled": True,
    }
    assert result["emitted"] == [
        {
            "event": "request_leaderboard",
            "request_id": request_id,
            "mode_id": "quarter-field",
            "race_id": "race-1",
            "leaderboard_slot": 4,
            "boundary": None,
            "offset": 0,
        }
        for request_id in (1, 2)
    ]
    assert result["retryHidden"] and result["errorHidden"]
    assert "1:00" in result["countdown"]
