"""Per-caller limits for the gateway's expensive tools (combine's DuckDB threads, search_context's embedder + graph).

`Limiter.acquire(sub)` returns a release callable once a permit is held, or refuses at once with a stable code:

  rate_limited   this caller already holds `per_sub` permits (running or queued), or spent its rate budget
  gateway_busy   every permit is taken and `queue` callers are already waiting

The release callable is idempotent and may be called from a done-callback, so a permit can outlive the call that
took it (combine holds it until its worker thread really finishes, even when the awaiting call was cancelled). The
optional rate limit is a per-sub token bucket (`rate_per_min` calls a minute, bursts up to `burst`). Single event
loop only: every method runs on the loop thread (done-callbacks included).
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable

from prism.gateway.errors import GatewayError

Release = Callable[[], None]


class Limiter:
    def __init__(self, name: str, *, total: int, per_sub: int, queue: int, rate_per_min: float | None = None,
                 burst: int | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        if total < 1 or per_sub < 1 or queue < 0:
            raise ValueError("total and per_sub must be >= 1 and queue >= 0")
        self.name, self.total, self.per_sub, self.queue = name, total, per_sub, queue
        self.rate_per_min = rate_per_min
        self.burst = burst if burst is not None else (int(rate_per_min) if rate_per_min else 0)
        self._clock = clock
        self._sem = asyncio.Semaphore(total)
        self._by_sub: Counter[str] = Counter()     # permits held + queued, per caller
        self._buckets: dict[str, tuple[float, float]] = {}   # sub -> (tokens, last refill)
        self.running = 0
        self.waiting = 0

    def _take_token(self, sub: str) -> bool:
        if not self.rate_per_min:
            return True
        now = self._clock()
        tokens, last = self._buckets.get(sub, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - last) * self.rate_per_min / 60.0)
        if tokens < 1.0:
            self._buckets[sub] = (tokens, now)
            return False
        self._buckets[sub] = (tokens - 1.0, now)
        if len(self._buckets) > 10_000:   # forget full buckets of idle callers (bounded memory)
            for key in [k for k, (t, _) in self._buckets.items() if t >= self.burst - 1 and k != sub][:5_000]:
                del self._buckets[key]
        return True

    async def acquire(self, sub: str) -> Release:
        if self._by_sub[sub] >= self.per_sub:
            raise GatewayError("rate_limited", f"too many {self.name} calls in flight for this caller; "
                                               f"wait for one to finish")
        if not self._take_token(sub):
            raise GatewayError("rate_limited", f"too many {self.name} calls; slow down and retry shortly")
        if self._sem.locked() and self.waiting >= self.queue:
            raise GatewayError("gateway_busy", f"the gateway is busy ({self.name}); retry shortly")
        self._by_sub[sub] += 1
        self.waiting += 1
        try:
            await self._sem.acquire()
        except BaseException:
            self._drop(sub)
            raise
        finally:
            self.waiting -= 1
        self.running += 1
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self.running -= 1
            self._sem.release()
            self._drop(sub)

        return release

    def _drop(self, sub: str) -> None:
        self._by_sub[sub] -= 1
        if self._by_sub[sub] <= 0:
            del self._by_sub[sub]


async def run_in_thread_holding(release: Release, fn: Callable, *args):
    """Run `fn(*args)` in a worker thread; `release` runs when the THREAD finishes, not when the awaiting call ends.
    A cancelled caller stops waiting at once, but the permit stays held until the work is really over."""
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))

    def done(t: asyncio.Future) -> None:
        release()
        if not t.cancelled():
            t.exception()   # retrieved: an orphaned failure is not "never retrieved"

    task.add_done_callback(done)
    return await asyncio.shield(task)


__all__ = ["Limiter", "Release", "run_in_thread_holding"]
