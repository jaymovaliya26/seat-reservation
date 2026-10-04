"""A per-worker cap on request log lines.

Railway keeps at most 500 log lines per second per replica and silently drops the rest. During an
on-sale, that would mean losing request lines at random at exactly the moment they matter. Instead,
each worker writes at most `per_second` request lines; past that it counts the lines it skips and
writes one summary line per second. Server errors are always written. Nothing is lost from the
record: /metrics counts every request, and every booking is a row in Postgres.
"""

import asyncio
import time
from collections import Counter
from collections.abc import Callable

import structlog

from app.observability import metrics

log = structlog.get_logger(component="http")


class LogBudget:
    def __init__(self, per_second: int, clock: Callable[[], float] = time.monotonic) -> None:
        """`per_second` <= 0 means no cap."""
        self.per_second = per_second
        self._clock = clock
        self._window_start = clock()
        self._used = 0
        self._skipped: Counter[int] = Counter()

    def allow(self, status: int) -> bool:
        self._roll_window()
        if status >= 500 or self.per_second <= 0 or self._used < self.per_second:
            self._used += 1
            return True
        self._skipped[status] += 1
        metrics.LOG_LINES_SUPPRESSED.inc()
        return False

    def flush(self) -> None:
        """Write one summary line for everything skipped since the last flush."""
        if not self._skipped:
            return
        log.info(
            "request_lines_suppressed",
            count=sum(self._skipped.values()),
            by_status={str(status): n for status, n in sorted(self._skipped.items())},
            cap_per_second=self.per_second,
        )
        self._skipped.clear()

    async def flush_every_second(self) -> None:
        """So the last summary is written even if traffic stops."""
        while True:
            await asyncio.sleep(1.0)
            self._roll_window()

    def _roll_window(self) -> None:
        now = self._clock()
        if now - self._window_start >= 1.0:
            self.flush()
            self._window_start = now
            self._used = 0
