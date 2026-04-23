import math
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class ResponseRecord:
    timestamp: float
    elapsed_ms: float
    status_code: int
    stack: str  # "python" | "rust"


@dataclass
class StackSnapshot:
    avg_ms: float
    p90_ms: float
    p99_ms: float
    actual_rps: int
    total_requests: int
    error_count: int


@dataclass
class MetricsSnapshot:
    timestamp: float
    python: StackSnapshot
    rust: StackSnapshot


def _percentile(sorted_data: list[float], pct: float) -> float:
    """Compute percentile from pre-sorted data using linear interpolation."""
    if not sorted_data:
        return 0.0
    if len(sorted_data) == 1:
        return sorted_data[0]
    k = (pct / 100) * (len(sorted_data) - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    return sorted_data[f] * (c - k) + sorted_data[c] * (k - f)


def _empty_stack() -> StackSnapshot:
    return StackSnapshot(
        avg_ms=0, p90_ms=0, p99_ms=0,
        actual_rps=0, total_requests=0, error_count=0,
    )


def _compute_stack(records: list[ResponseRecord]) -> StackSnapshot:
    if not records:
        return _empty_stack()
    times = sorted(r.elapsed_ms for r in records)
    errors = sum(1 for r in records if r.status_code >= 400)
    return StackSnapshot(
        avg_ms=round(sum(times) / len(times), 2),
        p90_ms=round(_percentile(times, 90), 2),
        p99_ms=round(_percentile(times, 99), 2),
        actual_rps=len(records),
        total_requests=len(records),
        error_count=errors,
    )


class MetricsStore:
    """Collects response times per-stack and computes rolling aggregates."""

    def __init__(self, window_seconds: int = 300):
        self._window_seconds = window_seconds
        self._records: deque[ResponseRecord] = deque()
        self._snapshots: deque[MetricsSnapshot] = deque(maxlen=window_seconds)
        self._last_snapshot_time: float = 0

    def record_response(self, elapsed_ms: float, status_code: int, stack: str) -> None:
        self._records.append(ResponseRecord(
            timestamp=time.time(),
            elapsed_ms=elapsed_ms,
            status_code=status_code,
            stack=stack,
        ))

    def compute_snapshot(self) -> MetricsSnapshot:
        """Compute a snapshot from records in the last 1 second, split by stack."""
        now = time.time()
        cutoff = now - 1.0

        # Evict old records beyond the rolling window.
        while self._records and self._records[0].timestamp < now - self._window_seconds:
            self._records.popleft()

        recent_python = [r for r in self._records if r.timestamp >= cutoff and r.stack == "python"]
        recent_rust = [r for r in self._records if r.timestamp >= cutoff and r.stack == "rust"]

        snapshot = MetricsSnapshot(
            timestamp=now,
            python=_compute_stack(recent_python),
            rust=_compute_stack(recent_rust),
        )
        self._snapshots.append(snapshot)
        self._last_snapshot_time = now
        return snapshot

    def get_history(self, seconds: int = 300) -> list[MetricsSnapshot]:
        """Return recent snapshots for chart history."""
        cutoff = time.time() - seconds
        return [s for s in self._snapshots if s.timestamp >= cutoff]

    def reset(self) -> None:
        self._records.clear()
        self._snapshots.clear()
