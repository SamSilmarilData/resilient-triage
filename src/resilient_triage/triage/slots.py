"""Run slot pool: bounds the graph runs in flight, and how long a request queues for one.

A `TriageGateway` takes its slots from a pool. Free play's gateway builds its own; every
scenario run's gateway is handed the shared scenario slot pool, or, for the shed scenario, a
private pool whose slots are already held (ADR-009).
"""

import asyncio


class RunSlotPool:
    """A fixed number of run slots; a request waits up to the queue timeout for one."""

    def __init__(self, *, capacity: int, queue_timeout_s: float) -> None:
        if capacity < 1:
            raise ValueError("A run slot pool needs at least one slot")
        if queue_timeout_s <= 0:
            raise ValueError("A run slot pool needs a positive queue timeout")
        self.capacity = capacity
        self.queue_timeout_s = queue_timeout_s
        self._slots = asyncio.Semaphore(capacity)
        self._held = 0

    @property
    def free(self) -> int:
        """Slots no one holds right now."""
        return self.capacity - self._held

    async def acquire(self) -> bool:
        """Wait up to the queue timeout for a slot; True if one was taken."""
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self.queue_timeout_s)
        except TimeoutError:
            return False
        self._held += 1
        return True

    def release(self) -> None:
        """Give back a slot taken by acquire()."""
        self._held -= 1
        self._slots.release()
