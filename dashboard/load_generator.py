import asyncio
import logging
import time

import httpx

from metrics import MetricsStore

logger = logging.getLogger("dashboard.load")


class LoadGenerator:
    """Async HTTP load generator. Fires at target_rps per enabled stack.

    In single-stack mode only Python receives traffic. In dual mode both
    Python and Rust get the full target_rps independently (same load,
    fair comparison of reactivity per stack).
    """

    def __init__(
        self,
        python_url: str,
        rust_url: str,
        metrics_store: MetricsStore,
    ):
        self.python_url = python_url
        self.rust_url = rust_url
        self.dual_stack_enabled: bool = False
        self.target_rps: float = 10
        self.is_running: bool = False
        self._metrics = metrics_store
        self._ticker_tasks: dict[str, asyncio.Task] = {}
        self._in_flight: set[asyncio.Task] = set()
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(400)  # max concurrent across both stacks
        # Per-stack error counter to avoid log spam under sustained failures.
        self._error_log_count: dict[str, int] = {"python": 0, "rust": 0}

    async def start(self) -> None:
        if self.is_running:
            logger.warning("start called but already running")
            return
        logger.info(
            "starting load gen — rps=%s dual=%s python_url=%s rust_url=%s",
            self.target_rps, self.dual_stack_enabled, self.python_url, self.rust_url,
        )
        self.is_running = True
        self._error_log_count = {"python": 0, "rust": 0}
        self._client = httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=400))
        self._ticker_tasks["python"] = asyncio.create_task(self._ticker("python", self.python_url))
        if self.dual_stack_enabled:
            self._ticker_tasks["rust"] = asyncio.create_task(self._ticker("rust", self.rust_url))

    async def pause(self) -> None:
        self.is_running = False
        for task in self._ticker_tasks.values():
            task.cancel()
        for task in list(self._ticker_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._ticker_tasks.clear()
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)
            self._in_flight.clear()
        if self._client:
            await self._client.aclose()
            self._client = None

    def set_rps(self, value: float) -> None:
        self.target_rps = max(1, min(500, value))

    def set_dual_stack(self, enabled: bool) -> None:
        """Must be called while the generator is paused."""
        self.dual_stack_enabled = enabled

    async def _ticker(self, stack: str, url: str) -> None:
        """Fires requests at target_rps without waiting for responses."""
        try:
            while self.is_running:
                interval = 1.0 / max(0.1, self.target_rps)
                start = time.monotonic()

                await self._semaphore.acquire()
                task = asyncio.create_task(self._send_request(stack, url))
                self._in_flight.add(task)
                task.add_done_callback(self._in_flight.discard)

                elapsed = time.monotonic() - start
                sleep_time = max(0, interval - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            pass

    async def _send_request(self, stack: str, url: str) -> None:
        """Send a single request and record metrics tagged with stack."""
        start = time.monotonic()
        try:
            response = await self._client.post(url)
            elapsed_ms = (time.monotonic() - start) * 1000
            self._metrics.record_response(elapsed_ms, response.status_code, stack)
            if response.status_code >= 400 and self._error_log_count.get(stack, 0) < 5:
                self._error_log_count[stack] = self._error_log_count.get(stack, 0) + 1
                logger.warning(
                    "[%s] HTTP %d from %s body=%s",
                    stack, response.status_code, url, response.text[:200],
                )
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            self._metrics.record_response(elapsed_ms, 500, stack)
            if self._error_log_count.get(stack, 0) < 5:
                self._error_log_count[stack] = self._error_log_count.get(stack, 0) + 1
                logger.warning("[%s] request to %s failed: %s", stack, url, e)
        finally:
            self._semaphore.release()
