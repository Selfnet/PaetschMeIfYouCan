import pytest

from leaderboard import LeaderboardStore, NewLeaderboardEntry
from tests.test_dashboard import run_dashboard


def test_race_winner_target_uses_grouped_best_beyond_first_page(tmp_path):
    store = LeaderboardStore(tmp_path / "scores.db")
    store.insert_entries(
        [
            NewLeaderboardEntry(f"old-{i}", 1, f"P{i}", i, "full-field")
            for i in range(60)
        ]
    )
    winner_best = store.insert_entries(
        [NewLeaderboardEntry("old-winner", 1, "Winner", 100, "full-field")]
    ).pop()
    store.insert_entries(
        [
            NewLeaderboardEntry("race", 2, "WINNER", 200, "full-field"),
            NewLeaderboardEntry("race", 1, "P0", 300, "full-field"),
        ]
    )
    boundary = store.snapshot_boundary("full-field")
    store.insert_entries(
        [
            NewLeaderboardEntry("later", 2, "winner", 0, "full-field"),
            NewLeaderboardEntry("other-slot", 2, "winner", 0, "full-field", 2),
            NewLeaderboardEntry("other-mode", 2, "winner", 0, "quarter-field"),
        ]
    )
    first = store.page_entries("full-field", boundary, 0, race_id="race")
    second = store.page_entries("full-field", boundary, 50, race_id="race")
    assert first.target_score_id == second.target_score_id == winner_best
    assert winner_best not in [row.id for row in first.rows]
    assert second.rows[-1].id == winner_best
    assert (second.rows[-1].rank, second.rows[-1].race_duration_ms) == (61, 200)
    assert store.page_entries("full-field", boundary, 0).target_score_id is None
    assert (
        store.page_entries(
            "full-field", boundary, 0, leaderboard_id=2, race_id="race"
        ).target_score_id
        is None
    )


@pytest.mark.parametrize(
    "names,times,expected",
    [
        (("Ada", "Grace"), (200, 100), "Grace"),
        (("Ada", "Grace"), (100, 100), "Ada"),
        (("", "Grace"), (100, 200), "Grace"),
        (("", ""), (100, 200), None),
        (("ADA", "ada"), (100, 200), "ada"),
    ],
)
def test_target_handles_ties_skipped_names_and_grouping(
    tmp_path, names, times, expected
):
    store = LeaderboardStore(tmp_path / "scores.db")
    store.insert_entries(
        [
            NewLeaderboardEntry("race", player, name, duration, "full-field")
            for player, (name, duration) in enumerate(zip(names, times), 1)
            if name
        ]
    )
    page = store.page_entries(
        "full-field", store.snapshot_boundary("full-field"), 0, race_id="race"
    )
    target = next((row for row in page.rows if row.id == page.target_score_id), None)
    assert (target.name if target else None) == expected


@pytest.mark.parametrize("manual", [False, True])
def test_initial_position_loads_target_then_preserves_manual_scroll(manual):
    result = run_dashboard(
        ["positionRaceResult", "markScrollIntent"],
        """
let browsing = {positionPending: true, rows: new Map(), nextOffset: 50};
let requests = 0, focused = 0, suppressScrollUntil = 0, scrollIntentUntil = 0;
function requestNextPage() { requests++; }
const scroll = element("leaderboardScroll");
scroll.scrollTop = 0; scroll.clientHeight = 400; scroll.scrollHeight = 1000;
scroll.getBoundingClientRect = () => ({top: 100});
scroll.querySelector = () => ({getBoundingClientRect: () => ({height: 40})});
element("leaderboardTail").style = {};
positionRaceResult(61);
const winner = {getBoundingClientRect: () => ({top: 1000}),
    focus() { focused++; }};
browsing.rows.set(61, winner);
browsing.nextOffset = null;
if (data) markScrollIntent();
positionRaceResult(61);
const initial = scroll.scrollTop;
scroll.scrollTop = 25;
positionRaceResult(61);
console.log(JSON.stringify({requests, focused, initial, final: scroll.scrollTop,
    tail: element("leaderboardTail").style.height}));
""",
        manual,
    )
    assert result["requests"] == 1
    assert result["focused"] == (0 if manual else 1)
    assert result["initial"] == (0 if manual else 860)
    assert result["final"] == 25
    if not manual:
        assert result["tail"] == "260px"


def test_empty_or_idle_results_do_not_seek_pages():
    result = run_dashboard(
        ["newBrowsingState", "positionRaceResult"],
        """
const gameState = {leaderboard_slot: 1};
const IDLE_LEADERBOARD_TIMEOUT_MS = 60000;
let requests = 0;
function requestNextPage() { requests++; }
let browsing = newBrowsingState(1, "full-field", null);
positionRaceResult(61);
const idlePending = browsing.positionPending;
browsing = newBrowsingState(2, "full-field", "race");
positionRaceResult(null);
console.log(JSON.stringify({requests, idlePending, emptyPending: browsing.positionPending}));
""",
    )
    assert result == {"requests": 0, "idlePending": False, "emptyPending": False}
