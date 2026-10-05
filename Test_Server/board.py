from dataclasses import dataclass

NUM_PORTS = 48
OPEN_ID = 255
_PANEL_IDS = tuple(
    int(value, 2)
    for value in ("1110", "0110", "1010", "0010", "1100", "0100", "1000", "0000")
)
EXPECTED_IDS = tuple(
    panel | int(side, 2)
    for outside, underside in (
        ("00000001", "01000000"),
        ("10000000", "01000001"),
        ("10000001", "11000000"),
    )
    for panel in _PANEL_IDS
    for side in (outside, underside)
)


def coordinates(index: int) -> tuple[int, int]:
    if type(index) is not int or not 0 <= index < NUM_PORTS:
        raise ValueError("expected port index in 0..47")
    return index // 2, index % 2


def port_index(column: int, row: int) -> int:
    if type(column) is not int or not 0 <= column < NUM_PORTS // 2:
        raise ValueError("expected column in 0..23")
    if type(row) is not int or not 0 <= row < 2:
        raise ValueError("expected row in 0..1")
    return column * 2 + row


@dataclass(frozen=True)
class BoardObservation:
    identities: tuple[int, ...] | None


class GameBoard:
    def __init__(self):
        self._observation = BoardObservation(None)

    def snapshot(self) -> BoardObservation:
        return self._observation

    def update(self, values: object) -> BoardObservation:
        if not isinstance(values, (list, tuple)) or len(values) != NUM_PORTS:
            raise ValueError("expected 48 byte identities")
        if any(type(value) is not int or not 0 <= value <= OPEN_ID for value in values):
            raise ValueError("expected 48 byte identities")
        self._observation = BoardObservation(tuple(values))
        return self._observation
