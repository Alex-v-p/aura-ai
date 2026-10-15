"""Thin ingest-worker composition entrypoint.

The Collector delivers normalized batches to the API in this first slice.  A
worker process is still provided as a stable deployment seam for durable queue
consumption in the next work item; it performs no Core imports or table reads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from aura_observatory.composition import build_ingest_service, build_query_service
from aura_observatory.ingest import TelemetryIngestService
from aura_observatory.settings import Settings


class IngestWorker:
    """Consume normalized batches from a process-local handoff queue.

    The Collector transport is composed in the platform work item; this
    queue keeps the worker entrypoint real and testable without inventing a
    second persistence or Core dependency.
    """

    def __init__(self, service: TelemetryIngestService) -> None:
        self.service = service
        self._queue: asyncio.Queue[tuple[Any, str | None]] = asyncio.Queue()

    async def submit(self, payload: Any, credential: str | None) -> None:
        await self._queue.put((payload, credential))

    async def run(self, stop: Awaitable[None] | None = None) -> None:
        if stop is None:
            while True:
                payload, credential = await self._queue.get()
                await self.service.ingest(payload, credential=credential)
        else:
            stop_task = asyncio.create_task(_await_stop(stop))
            try:
                while True:
                    item_task = asyncio.create_task(self._queue.get())
                    done, _ = await asyncio.wait(
                        (item_task, stop_task), return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop_task in done:
                        item_task.cancel()
                        return
                    payload, credential = item_task.result()
                    await self.service.ingest(payload, credential=credential)
            finally:
                stop_task.cancel()


async def _await_stop(stop: Awaitable[None]) -> None:
    await stop


async def run(settings: Settings | None = None) -> None:
    """Run the production worker with configured Observatory adapters."""

    resolved = settings or Settings()
    query = build_query_service(resolved)
    worker = IngestWorker(build_ingest_service(resolved))
    retention_stop = asyncio.Event()
    retention_task = asyncio.create_task(query.run_retention_worker(retention_stop))
    try:
        await worker.run()
    finally:
        retention_stop.set()
        await retention_task


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()


__all__ = ["main", "run"]
