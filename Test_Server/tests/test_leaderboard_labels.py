from tests.test_dashboard import run_dashboard


def test_leaderboard_labels_keep_tied_ranks_and_personal_best_flags():
    result = run_dashboard(
        ["formatDuration", "formatCreatedAt", "appendLeaderboardPage"],
        """
const browsing = {requestId: 1, modeId: "full-field", leaderboardSlot: 1,
    raceId: "race-1", boundary: null, pendingOffset: 0, rows: new Map()};
function checkLeaderboardExpiry() { return false; }
function updatePageControl() {}
document.createElement = () => ({children: [], dataset: {},
    classList: {toggle() {}}, setAttribute(name, value) { this[name] = value; },
    appendChild(child) { this.children.push(child); }});
const body = element("leaderboardRows");
body.children = [];
body.appendChild = (row) => body.children.push(row);
body.querySelector = () => null;
appendLeaderboardPage({request_id: 1, mode_id: "full-field", leaderboard_slot: 1,
    race_id: "race-1", boundary: 3, offset: 0, next_offset: null,
    rows: [
        {id: 1, rank: 1, name: "Ada", duration_ms: 1000, attempts: 2,
            created_at: null, current_race: true, personal_best: true, race_duration_ms: 1000},
        {id: 2, rank: 1, name: "Grace", duration_ms: 1000, attempts: 1,
            created_at: null, current_race: false, personal_best: false, race_duration_ms: null},
        {id: 3, rank: 3, name: "Linus", duration_ms: 2000, attempts: 2,
            created_at: null, current_race: true, personal_best: false, race_duration_ms: 3000}
    ]});
console.log(JSON.stringify(body.children));
""",
    )
    assert [row["children"][0]["textContent"] for row in result] == [
        "Top of Rack",
        "Top of Rack",
        "3",
    ]
    badge = result[0]["children"][1]["children"][0]
    assert badge["textContent"] == "New PB!"
    assert badge["aria-label"] == "New personal best!"
    assert not result[1]["children"][1]["children"]
    assert not result[2]["children"][1]["children"]
    assert (
        result[2]["children"][2]["children"][0]["textContent"] == "This race: 00:03.000"
    )
