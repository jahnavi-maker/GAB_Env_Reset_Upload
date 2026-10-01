from __future__ import annotations

import heapq
import threading
import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from materialize.provision.store import Job


class FairQueue:
    """Round-robin across accounts so one large mailbox cannot starve others."""

    def __init__(self) -> None:
        self._order: deque[str] = deque()
        self._buckets: dict[str, deque[Job]] = {}
        self._cv = threading.Condition()
        self._closed = False

    def put(self, job: Job) -> None:
        key = job.account_id or "(shared)"
        with self._cv:
            if key not in self._buckets:
                self._buckets[key] = deque()
                self._order.append(key)
            self._buckets[key].append(job)
            self._cv.notify()

    def qsize(self) -> int:
        with self._cv:
            return sum(len(b) for b in self._buckets.values())

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def get(self, timeout: float = 0.25) -> Job | None:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                n = len(self._order)
                for _ in range(n):
                    acc = self._order.popleft()
                    bucket = self._buckets.get(acc)
                    if not bucket:
                        self._buckets.pop(acc, None)
                        continue
                    job = bucket.popleft()
                    if bucket:
                        self._order.append(acc)
                    else:
                        self._buckets.pop(acc, None)
                    return job
                if self._closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)


class RetryHeap:
    def __init__(self) -> None:
        self._heap: list[tuple[float, int, Job]] = []
        self._cv = threading.Condition()
        self._seq = 0
        self._closed = False

    def put(self, job: Job, ready_at: float) -> None:
        with self._cv:
            self._seq += 1
            heapq.heappush(self._heap, (ready_at, self._seq, job))
            self._cv.notify()

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def pop_ready(self, now: float | None = None) -> list[Job]:
        now = time.time() if now is None else now
        ready: list[Job] = []
        with self._cv:
            while self._heap and self._heap[0][0] <= now:
                _when, _seq, job = heapq.heappop(self._heap)
                ready.append(job)
        return ready

    def wait(self, timeout: float = 0.25) -> None:
        with self._cv:
            if self._closed:
                return
            if self._heap:
                delay = max(0.0, self._heap[0][0] - time.time())
                self._cv.wait(min(timeout, delay))
            else:
                self._cv.wait(timeout)
