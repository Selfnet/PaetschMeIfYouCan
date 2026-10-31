import pytest

from tests.test_dashboard import run_dashboard


@pytest.mark.parametrize(
    "durations,counts,unit,rates",
    [
        ([12500, 9000], [12, 6], "correct", ["57.60", "40.00", "86.40"]),
        ([0, 0], [0, 0], "correct", ["0.00", "0.00", "0.00"]),
        ([0, 30000], [0, 12], "correct", ["0.00", "24.00", "24.00"]),
        ([1000, 1000], [1, 2], "ns", ["-", "-", "-"]),
    ],
)
def test_patch_rates_use_minutes_and_combined_elapsed_time(
    durations, counts, unit, rates
):
    result = run_dashboard(
        [
            "formatDuration",
            "currentDurationMs",
            "progressFor",
            "renderStat",
            "updateStats",
        ],
        """
const PLAYER_IDS = [1, 2];
const players = {}, gameState = {players: {}};
PLAYER_IDS.forEach((id, index) => {
    players[id] = {durationMs: data.durations[index]};
    gameState.players[id] = {presentation: {stage: null,
        progress: {current: data.counts[index], total: 48, unit: data.unit}}};
});
const clockState = {activePlayers: {1: false, 2: false}, syncedAt: performance.now()};
updateStats();
console.log(JSON.stringify(elements));
""",
        {"durations": durations, "counts": counts, "unit": unit},
    )
    for element_id, expected in zip(
        ("patchRate1", "patchRate2", "totalRate"), rates, strict=True
    ):
        assert result[element_id]["textContent"] == expected
        assert result[element_id]["previousElementSibling"]["textContent"] == (
            "Patches / min" if unit == "correct" else "Rate"
        )
