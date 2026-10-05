from dataclasses import FrozenInstanceError

import pytest
from board import EXPECTED_IDS, NUM_PORTS, OPEN_ID, GameBoard, coordinates, port_index


@pytest.mark.parametrize(
    "values",
    [
        [0] * 47,
        [0] * 49,
        *([v] * 48 for v in (True, False, 1.0, "1", -1, 256)),
        None,
        bytes(48),
        "0" * 48,
    ],
)
def test_invalid_frame_keeps_previous_raw_observation(values):
    board = GameBoard()
    assert board.snapshot().identities is None
    accepted = board.update(EXPECTED_IDS)
    with pytest.raises(ValueError):
        board.update(values)
    assert board.snapshot() is accepted
    assert accepted.identities == EXPECTED_IDS


def test_layout_alternates_top_and_bottom():
    assert NUM_PORTS == 48
    for index in range(NUM_PORTS):
        assert coordinates(index) == (index // 2, index % 2)
        assert port_index(*coordinates(index)) == index


@pytest.mark.parametrize("index", [-1, 48, True, False, 1.0, "1", None])
def test_coordinates_reject_invalid_index(index):
    with pytest.raises(ValueError):
        coordinates(index)


@pytest.mark.parametrize(
    "column,row",
    [
        (-1, 0),
        (24, 0),
        (0, -1),
        (0, 2),
        (True, 0),
        (0, False),
        (1.0, 0),
        (0, 1.0),
        ("0", 0),
        (0, "0"),
        (None, 0),
    ],
)
def test_port_index_rejects_invalid_coordinates(column, row):
    with pytest.raises(ValueError):
        port_index(column, row)


def test_observation_freezes_input_and_preserves_raw_bytes():
    board = GameBoard()
    values = [0, OPEN_ID] * 24
    accepted = board.update(values)
    values[0] = 10
    assert accepted.identities == (0, 255) * 24
    with pytest.raises(FrozenInstanceError):
        accepted.identities = None
    board.update(EXPECTED_IDS)
    assert accepted.identities == (0, 255) * 24


def test_expected_id_mapping_is_unchanged():
    assert EXPECTED_IDS == tuple(
        panel | side
        for sides in ((1, 64), (128, 65), (129, 192))
        for panel in (14, 6, 10, 2, 12, 4, 8, 0)
        for side in sides
    )
