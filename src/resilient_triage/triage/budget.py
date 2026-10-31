"""Free-play run budget: at most N graph runs admitted in any sliding 60 s window.

The budget lives in one process (per worker): several workers each allow N a minute. Cache
hits never reach it; a run counts once admitted, even if it is then shed for lack of a slot.
"""

import math
import time
from collections import deque
from collections.abc import Callable

WINDOW_S = 60.0


class RunBudget:
    """Admits a graph run while fewer than `runs_per_minute` were admitted in the last 60 s."""

    def __init__(
        self, *, runs_per_minute: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if runs_per_minute < 1:
            raise ValueError("A run budget needs at least one run a minute")
        self.runs_per_minute = runs_per_minute
        self._clock = clock
        self._admitted: deque[float] = deque()  # admission times, oldest first

    def admit(self) -> bool:
        """Count and admit a run if the window has room; a refused run is not counted."""
        now = self._clock()
        self._forget_before(now - WINDOW_S)
        if len(self._admitted) >= self.runs_per_minute:
            return False
        self._admitted.append(now)
        return True

    def retry_after_s(self) -> int:
        """Whole seconds (at least 1) until the oldest counted run leaves the window."""
        now = self._clock()
        self._forget_before(now - WINDOW_S)
        if len(self._admitted) < self.runs_per_minute:
            return 1
        return max(1, math.ceil(self._admitted[0] + WINDOW_S - now))

    def _forget_before(self, cutoff: float) -> None:
        """Drop admissions at or before `cutoff`: they have aged out of the window."""
        while self._admitted and self._admitted[0] <= cutoff:
            self._admitted.popleft()
