"""Free-play run budget: at most N graph runs admitted in any sliding 60 s window.

The budget lives in one process (per worker): several workers each allow N a minute. Cache
hits never reach it, and a run shed for lack of a slot is refunded: only graph runs count.
"""

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

WINDOW_S = 60.0


@dataclass(frozen=True, eq=False)
class Admission:
    """One admitted run, by identity: the token `refund()` takes back."""

    at: float  # admission time on the budget's clock


class RunBudget:
    """Admits a graph run while fewer than `runs_per_minute` were admitted in the last 60 s."""

    def __init__(
        self, *, runs_per_minute: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if runs_per_minute < 1:
            raise ValueError("A run budget needs at least one run a minute")
        self.runs_per_minute = runs_per_minute
        self._clock = clock
        self._admitted: deque[Admission] = deque()  # oldest first

    def admit(self) -> Admission | None:
        """Count and admit a run if the window has room; a refused run (None) is not counted."""
        now = self._clock()
        self._forget_before(now - WINDOW_S)
        if len(self._admitted) >= self.runs_per_minute:
            return None
        admission = Admission(now)
        self._admitted.append(admission)
        return admission

    def refund(self, admission: Admission) -> None:
        """Uncount this admission: the admitted request then ran no graph."""
        if admission in self._admitted:  # it may already have aged out of the window
            self._admitted.remove(admission)

    def retry_after_s(self) -> int:
        """Whole seconds (at least 1) until the oldest counted run leaves the window."""
        now = self._clock()
        self._forget_before(now - WINDOW_S)
        if len(self._admitted) < self.runs_per_minute:
            return 1
        return max(1, math.ceil(self._admitted[0].at + WINDOW_S - now))

    def _forget_before(self, cutoff: float) -> None:
        """Drop admissions at or before `cutoff`: they have aged out of the window."""
        while self._admitted and self._admitted[0].at <= cutoff:
            self._admitted.popleft()
