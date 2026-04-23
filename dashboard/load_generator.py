import asyncio
import math
import time

import httpx

from metrics import MetricsStore


class LoadGenerator:
    """Async HTTP load generator with real-time adjustable RPS."""

    def __init__(self, target_url: str, metrics_store: MetricsStore):
        self.target_url = target_url
        self.target_rps: float = 10
        self.is_running: bool = False
        self._metrics = metrics_store
        self._ticker_task: asyncio.Task | None = None
        self._in_flight: set[asyncio.Task] = set()
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(200)  # max concurrent requests

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._client = httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=200))
        self._ticker_task = asyncio.create_task(self._ticker())

    async def pause(self) -> None:
        self.is_running = False
        if self._ticker_task:
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except asyncio.CancelledError:
                pass
            self._ticker_task = None
        # Wait for in-flight requests to finish
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)
            self._in_flight.clear()
        if self._client:
            await self._client.aclose()
            self._client = None

    def set_rps(self, value: float) -> None:
        self.target_rps = max(1, min(500, value))

    async def _ticker(self) -> None:
        """Fires requests at the target RPS without waiting for responses."""
        try:
            while self.is_running:
                interval = 1.0 / max(0.1, self.target_rps)
                start = time.monotonic()

                # Fire a request without blocking on response
                await self._semaphore.acquire()
                task = asyncio.create_task(self._send_request())
                self._in_flight.add(task)
                task.add_done_callback(self._in_flight.discard)

                # Sleep to maintain target rate
                elapsed = time.monotonic() - start
                sleep_time = max(0, interval - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            pass

    async def _send_request(self) -> None:
        """Send a single request and record metrics."""
        start = time.monotonic()
        try:
            response = await self._client.post(self.target_url)
            elapsed_ms = (time.monotonic() - start) * 1000
            self._metrics.record_response(elapsed_ms, response.status_code)
        except (httpx.RequestError, httpx.HTTPStatusError):
            elapsed_ms = (time.monotonic() - start) * 1000
            self._metrics.record_response(elapsed_ms, 500)
        finally:
            self._semaphore.release()
