import math
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class ResponseRecord:
    timestamp: float
    elapsed_ms: float
    status_code: int


@dataclass
class MetricsSnapshot:
    timestamp: float
    avg_ms: float
    p90_ms: float
    p99_ms: float
    actual_rps: int
    total_requests: int
    error_count: int


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


class MetricsStore:
    """Collects response times and computes rolling aggregates."""

    def __init__(self, window_seconds: int = 300):
        self._window_seconds = window_seconds
        self._records: deque[ResponseRecord] = deque()
        self._snapshots: deque[MetricsSnapshot] = deque(maxlen=window_seconds)
        self._last_snapshot_time: float = 0

    def record_response(self, elapsed_ms: float, status_code: int) -> None:
        self._records.append(ResponseRecord(
            timestamp=time.time(),
            elapsed_ms=elapsed_ms,
            status_code=status_code,
        ))

    def compute_snapshot(self) -> MetricsSnapshot:
        """Compute a snapshot from records in the last 1 second."""
        now = time.time()
        cutoff = now - 1.0

        # Evict old records beyond the rolling window
        while self._records and self._records[0].timestamp < now - self._window_seconds:
            self._records.popleft()

        # Get records from the last second
        recent = [r for r in self._records if r.timestamp >= cutoff]

        if not recent:
            snapshot = MetricsSnapshot(
                timestamp=now,
                avg_ms=0,
                p90_ms=0,
                p99_ms=0,
                actual_rps=0,
                total_requests=0,
                error_count=0,
            )
        else:
            times = sorted(r.elapsed_ms for r in recent)
            errors = sum(1 for r in recent if r.status_code >= 400)
            snapshot = MetricsSnapshot(
                timestamp=now,
                avg_ms=round(sum(times) / len(times), 2),
                p90_ms=round(_percentile(times, 90), 2),
                p99_ms=round(_percentile(times, 99), 2),
                actual_rps=len(recent),
                total_requests=len(recent),
                error_count=errors,
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
