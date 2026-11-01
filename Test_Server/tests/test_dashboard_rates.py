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
            "patchRate",
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


def rate_samples(*, reference=288000, count=1, changed_at=6000, active=True):
    return run_dashboard(
        ["progressFor", "patchRate"],
        """
const gameState = {pace_reference_ms: data.reference, mode: {required_port_count: 48},
    players: {1: {progress_changed_ms: data.changed_at,
        presentation: {progress: {current: data.count, total: 48, unit: "correct"}}}}};
const clockState = {activePlayers: {1: data.active}};
console.log(JSON.stringify([6000, 9000, 12000, 18000, 60000].map(t => patchRate(1, t))));
""",
        {
            "reference": reference,
            "count": count,
            "changed_at": changed_at,
            "active": active,
        },
    )


def test_rate_holds_for_one_estimated_patch_interval_then_decays():
    assert rate_samples() == pytest.approx([10, 10, 10, 8, 10 / 3])


def test_empty_leaderboard_uses_the_players_own_pace():
    assert rate_samples(reference=None) == pytest.approx([10, 10, 10, 5, 10 / 9])


def test_rate_is_zero_until_progress_has_a_measured_time():
    assert rate_samples(count=0) == [0] * 5
    assert rate_samples(changed_at=0) == [0] * 5


def test_frozen_clocks_use_actual_average_without_a_prior_or_grace_period():
    assert rate_samples(active=False) == pytest.approx([10, 20 / 3, 5, 10 / 3, 1])


def test_median_influence_fades_as_real_patches_accumulate():
    result = run_dashboard(
        ["progressFor", "patchRate"],
        """
const gameState = {pace_reference_ms: 288000, mode: {required_port_count: 48}, players: {}};
const clockState = {activePlayers: {1: true}};
const rates = [1, 12, 47].map(n => {
    gameState.players[1] = {progress_changed_ms: n * 3000,
        presentation: {progress: {current: n, total: 48, unit: "correct"}}};
    return patchRate(1, n * 3000);
});
gameState.players[1].presentation.progress.current = 48;
clockState.activePlayers[1] = false;
rates.push(patchRate(1, 144000));
console.log(JSON.stringify(rates));
""",
    )
    assert result == pytest.approx([80 / 7, 50 / 3, 1000 / 53, 20])


@pytest.mark.parametrize(
    "counts,phase,units,stages,labels,values,highlighted",
    [
        (
            [8, 5],
            "racing",
            ["correct"] * 2,
            [None] * 2,
            ["Leading by", "Behind by"],
            ["3 patches"] * 2,
            [True, False],
        ),
        (
            [5, 6],
            "racing",
            ["correct"] * 2,
            [None] * 2,
            ["Behind by", "Leading by"],
            ["1 patch"] * 2,
            [False, True],
        ),
        (
            [5, 5],
            "racing",
            ["correct"] * 2,
            [None] * 2,
            ["Lead"] * 2,
            ["Even"] * 2,
            [False] * 2,
        ),
        (
            [8, 5],
            "ready",
            ["correct"] * 2,
            [None] * 2,
            ["Lead"] * 2,
            ["-"] * 2,
            [False] * 2,
        ),
        (
            [8, 5],
            "racing",
            ["correct", "ns"],
            [None] * 2,
            ["Lead"] * 2,
            ["-"] * 2,
            [False] * 2,
        ),
        (
            [8, 5],
            "racing",
            ["correct"] * 2,
            ["A", "B"],
            ["Lead"] * 2,
            ["-"] * 2,
            [False] * 2,
        ),
        (
            [8, 5],
            "stopped",
            ["steps"] * 2,
            ["A"] * 2,
            ["Leading by", "Behind by"],
            ["3 steps"] * 2,
            [True, False],
        ),
    ],
)
def test_lead_indicators_match_progress_and_only_highlight_the_leader(
    counts, phase, units, stages, labels, values, highlighted
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
const players = {1: {durationMs: 10000}, 2: {durationMs: 10000}};
const clockState = {activePlayers: {1: false, 2: false}, syncedAt: performance.now()};
const gameState = {phase: data.phase, players: {}};
PLAYER_IDS.forEach((id, i) => {
    gameState.players[id] = {presentation: {stage: data.stages[i],
        progress: {current: data.counts[i], total: 48, unit: data.units[i]}}};
    element(`leadIndicator${id}`).classList = {toggle(name, on) { this[name] = on; }};
});
updateStats();
console.log(JSON.stringify(elements));
""",
        {"counts": counts, "phase": phase, "units": units, "stages": stages},
    )
    for index, player in enumerate((1, 2)):
        assert result[f"playerLead{player}"]["textContent"] == values[index]
        assert (
            result[f"playerLead{player}"]["previousElementSibling"]["textContent"]
            == labels[index]
        )
        assert (
            result[f"leadIndicator{player}"]["classList"]["is-leading"]
            is highlighted[index]
        )
