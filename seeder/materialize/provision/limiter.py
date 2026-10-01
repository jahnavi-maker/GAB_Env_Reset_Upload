from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable


class TokenBucket:
    """Process-local token bucket. Not a sleep-after-each-call limiter."""

    def __init__(self, rate: float, burst: float):
        self._rate = max(0.05, float(rate))
        self._burst = max(1.0, float(burst))
        self._tokens = self._burst
        self._updated = time.monotonic()
        self._cv = threading.Condition()

    @property
    def rate(self) -> float:
        with self._cv:
            return self._rate

    def set_rate(self, rate: float, burst: float | None = None) -> None:
        with self._cv:
            self._rate = max(0.05, float(rate))
            if burst is not None:
                self._burst = max(1.0, float(burst))
            self._tokens = min(self._tokens, self._burst)
            self._cv.notify_all()

    def _refill_unlocked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._cv:
            self._refill_unlocked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while True:
                self._refill_unlocked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    wait = min(remaining, max(0.001, (tokens - self._tokens) / self._rate))
                else:
                    wait = max(0.001, (tokens - self._tokens) / self._rate)
                self._cv.wait(wait)


class AdaptiveLimiter:
    """Token bucket that slowly raises rate on clean windows and cuts it on 429s."""

    def __init__(
        self,
        rate: float,
        burst: float,
        *,
        min_rate: float | None = None,
        max_rate: float | None = None,
        adaptive: bool = True,
    ):
        self.bucket = TokenBucket(rate, burst)
        self._min = max(0.05, min_rate if min_rate is not None else rate * 0.25)
        self._max = max(self._min, max_rate if max_rate is not None else rate * 4)
        self._adaptive = adaptive
        self._lock = threading.Lock()
        self._window: deque[tuple[float, bool, bool]] = deque(maxlen=200)
        self._last_adjust = time.monotonic()

    def acquire(self, timeout: float | None = None) -> bool:
        return self.bucket.acquire(timeout=timeout)

    def record(self, *, ok: bool, rate_limited: bool) -> None:
        now = time.monotonic()
        with self._lock:
            self._window.append((now, ok, rate_limited))
            if not self._adaptive or now - self._last_adjust < 5.0:
                if rate_limited:
                    self._cut_unlocked()
                    self._last_adjust = now
                return
            self._last_adjust = now
            recent = [row for row in self._window if now - row[0] <= 15.0]
            if not recent:
                return
            limited = sum(1 for _t, _ok, rl in recent if rl)
            succeeded = sum(1 for _t, ok, _rl in recent if ok)
            if limited / len(recent) >= 0.05:
                self._cut_unlocked()
            elif succeeded / len(recent) >= 0.95:
                self._raise_unlocked()

    def _cut_unlocked(self) -> None:
        self.bucket.set_rate(max(self._min, self.bucket.rate * 0.5))

    def _raise_unlocked(self) -> None:
        self.bucket.set_rate(min(self._max, self.bucket.rate * 1.1))


class PerAccountLimiter:
    def __init__(self, rate: float, burst: float = 1.0, factory: Callable[[], AdaptiveLimiter] | None = None):
        self._rate = rate
        self._burst = burst
        self._factory = factory
        self._guard = threading.Lock()
        self._buckets: dict[str, TokenBucket] = {}

    def acquire(self, account: str, timeout: float | None = None) -> bool:
        key = (account or "").strip().lower() or "(unknown)"
        with self._guard:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self._rate, self._burst)
                self._buckets[key] = bucket
        return bucket.acquire(timeout=timeout)


class ServiceLimiters:
    def __init__(
        self,
        *,
        drive: AdaptiveLimiter,
        calendar: AdaptiveLimiter,
        calendar_account: PerAccountLimiter,
        gmail: AdaptiveLimiter,
        generate: TokenBucket,
    ):
        self.drive = drive
        self.calendar = calendar
        self.calendar_account = calendar_account
        self.gmail = gmail
        self.generate = generate

    def acquire(self, service: str, account: str = "") -> None:
        if service == "drive":
            self.drive.acquire()
        elif service == "calendar":
            self.calendar.acquire()
            self.calendar_account.acquire(account)
        elif service == "gmail":
            self.gmail.acquire()
        elif service == "generate":
            self.generate.acquire()

    def record(self, service: str, *, ok: bool, rate_limited: bool) -> None:
        limiter = {
            "drive": self.drive,
            "calendar": self.calendar,
            "gmail": self.gmail,
        }.get(service)
        if limiter is not None:
            limiter.record(ok=ok, rate_limited=rate_limited)


def limiters_from_config(cfg) -> ServiceLimiters:
    return ServiceLimiters(
        drive=AdaptiveLimiter(cfg.drive_rate, cfg.drive_burst, adaptive=cfg.adaptive),
        calendar=AdaptiveLimiter(
            cfg.calendar_rate,
            cfg.calendar_burst,
            min_rate=0.1,
            max_rate=max(2.0, cfg.calendar_rate * 3),
            adaptive=cfg.adaptive,
        ),
        calendar_account=PerAccountLimiter(cfg.calendar_account_rate, burst=1.0),
        gmail=AdaptiveLimiter(cfg.gmail_rate, cfg.gmail_burst, adaptive=cfg.adaptive),
        generate=TokenBucket(cfg.generate_rate, max(2.0, cfg.generate_rate)),
    )
