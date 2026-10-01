from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any


class ServiceMetrics:
    def __init__(self) -> None:
        self.requests = 0
        self.success = 0
        self.retries = 0
        self.permanent = 0
        self.r429 = 0
        self.r403_limit = 0
        self.r5xx = 0
        self.latencies: deque[float] = deque(maxlen=2000)
        self.jobs_done = 0
        self._started = time.monotonic()

    def snapshot(self, queue_depth: int = 0) -> dict[str, Any]:
        elapsed = max(0.001, time.monotonic() - self._started)
        samples = sorted(self.latencies)
        def pct(p: float) -> float:
            if not samples:
                return 0.0
            idx = min(len(samples) - 1, int(round((p / 100.0) * (len(samples) - 1))))
            return samples[idx]
        return {
            "requests": self.requests,
            "success": self.success,
            "retries": self.retries,
            "permanent": self.permanent,
            "r429": self.r429,
            "r403_limit": self.r403_limit,
            "r5xx": self.r5xx,
            "queue_depth": queue_depth,
            "jobs_per_sec": self.jobs_done / elapsed,
            "requests_per_sec": self.requests / elapsed,
            "avg_latency_ms": (sum(samples) / len(samples) * 1000.0) if samples else 0.0,
            "p50_latency_ms": pct(50) * 1000.0,
            "p95_latency_ms": pct(95) * 1000.0,
        }


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.services = defaultdict(ServiceMetrics)
        self.accounts_total = 0
        self.jobs_total = 0
        self._started = time.monotonic()

    def mark_request(self, service: str, latency_s: float, *, ok: bool, status: int | None, rate_limited: bool, server: bool) -> None:
        with self._lock:
            row = self.services[service]
            row.requests += 1
            row.latencies.append(max(0.0, latency_s))
            if ok:
                row.success += 1
                row.jobs_done += 1
            if rate_limited:
                if status == 403:
                    row.r403_limit += 1
                else:
                    row.r429 += 1
            if server:
                row.r5xx += 1

    def mark_retry(self, service: str) -> None:
        with self._lock:
            self.services[service].retries += 1

    def mark_permanent(self, service: str) -> None:
        with self._lock:
            self.services[service].permanent += 1

    def snapshot(self, depths: dict[str, int] | None = None, counts: dict[str, int] | None = None) -> dict[str, Any]:
        depths = depths or {}
        counts = counts or {}
        with self._lock:
            elapsed = max(0.001, time.monotonic() - self._started)
            services = {
                name: row.snapshot(queue_depth=depths.get(name, 0))
                for name, row in self.services.items()
            }
            done = sum(row.jobs_done for row in self.services.values())
            return {
                "elapsed_s": elapsed,
                "accounts_total": self.accounts_total,
                "jobs_total": self.jobs_total,
                "jobs_per_sec": done / elapsed,
                "counts": counts,
                "drive": services.get("drive", ServiceMetrics().snapshot(depths.get("drive", 0))),
                "calendar": services.get("calendar", ServiceMetrics().snapshot(depths.get("calendar", 0))),
                "gmail": services.get("gmail", ServiceMetrics().snapshot(depths.get("gmail", 0))),
                "generate": services.get("generate", ServiceMetrics().snapshot(depths.get("generate", 0))),
            }

    def format_line(self, depths: dict[str, int] | None = None, counts: dict[str, int] | None = None) -> str:
        snap = self.snapshot(depths, counts)
        counts = snap.get("counts") or {}
        parts = [
            f"jobs {counts.get('SUCCESS', 0)}/{snap['jobs_total']} ({snap['jobs_per_sec']:.1f}/s)",
            f"pending={counts.get('PENDING', 0)} processing={counts.get('PROCESSING', 0)} "
            f"retry={counts.get('RETRY', 0)} fail={counts.get('PERMANENT_FAILURE', 0)}",
        ]
        for name in ("drive", "calendar", "gmail"):
            row = snap[name]
            parts.append(
                f"{name} q={row['queue_depth']} {row['jobs_per_sec']:.1f}/s "
                f"p95={row['p95_latency_ms']:.0f}ms 429={row['r429']}"
            )
        return " | ".join(parts)
