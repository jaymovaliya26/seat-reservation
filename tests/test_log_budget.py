from app.observability.log_budget import LogBudget


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_caps_lines_per_second_and_counts_the_rest() -> None:
    clock = FakeClock()
    budget = LogBudget(per_second=3, clock=clock)

    allowed = [budget.allow(409) for _ in range(10)]

    assert allowed == [True] * 3 + [False] * 7
    assert budget._skipped == {409: 7}


def test_server_errors_are_always_logged() -> None:
    budget = LogBudget(per_second=1, clock=FakeClock())
    budget.allow(201)
    assert all(budget.allow(500) for _ in range(5))
    assert budget.allow(409) is False


def test_a_new_second_resets_the_cap_and_flushes_the_summary() -> None:
    clock = FakeClock()
    budget = LogBudget(per_second=2, clock=clock)
    for _ in range(5):
        budget.allow(409)

    clock.now = 1.0
    assert budget.allow(201) is True
    assert not budget._skipped  # summarised and cleared when the window rolled


def test_zero_means_no_cap() -> None:
    budget = LogBudget(per_second=0, clock=FakeClock())
    assert all(budget.allow(409) for _ in range(10_000))


def test_windows_are_wall_clock_seconds() -> None:
    """A window that started mid-second must not let a worker write twice its cap in one second."""
    clock = FakeClock()
    clock.now = 10.6
    budget = LogBudget(per_second=2, clock=clock)
    assert [budget.allow(409) for _ in range(3)] == [True, True, False]

    clock.now = 10.99  # same wall-clock second: still capped
    assert budget.allow(409) is False

    clock.now = 11.0  # next second: fresh allowance
    assert budget.allow(409) is True
