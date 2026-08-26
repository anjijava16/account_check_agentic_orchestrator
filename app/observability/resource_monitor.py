"""Background sampler for CPU / memory / disk gauges."""
from __future__ import annotations

import asyncio
import os
import shutil

from app.core.logging import get_logger
from app.observability.metrics import DISK_USAGE, PROCESS_CPU, PROCESS_MEMORY

log = get_logger(__name__)


class ResourceMonitor:
    def __init__(self, interval: float = 15.0, mounts: tuple[str, ...] = ("/",)) -> None:
        self.interval = interval
        self.mounts = mounts
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="resource-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        try:
            import psutil  # type: ignore

            proc = psutil.Process(os.getpid())
        except ImportError:
            proc = None
            log.info("resource_monitor.psutil_absent", note="disk metrics only")

        while True:
            try:
                if proc is not None:
                    PROCESS_CPU.set(proc.cpu_percent(interval=None))
                    PROCESS_MEMORY.set(proc.memory_info().rss)
                for mount in self.mounts:
                    usage = shutil.disk_usage(mount)
                    DISK_USAGE.labels(mount=mount).set(usage.used / usage.total * 100)
            except Exception as exc:  # noqa: BLE001
                log.debug("resource_monitor.sample_failed", error=str(exc))
            await asyncio.sleep(self.interval)
