import pytest


class MutableClock:
    def __init__(self) -> None:
        self.nanoseconds = 0

    def __call__(self) -> int:
        return self.nanoseconds

    def advance_ms(self, milliseconds: int) -> None:
        self.nanoseconds += milliseconds * 1_000_000

    def advance_ns(self, nanoseconds: int) -> None:
        self.nanoseconds += nanoseconds


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()
