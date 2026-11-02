"""Thin process entrypoint for the isolated Core evaluation runner."""

from __future__ import annotations

import asyncio

from aura_core.bootstrap.evaluation_runner import build_evaluation_runner
from aura_core.platform.auth import Settings


async def run() -> None:
    runner, consumer, resources, telemetry = await build_evaluation_runner(Settings())
    stop = asyncio.Event()
    await telemetry.start()
    consumer_task = asyncio.create_task(consumer.consume(runner, stop))
    runner_task = asyncio.create_task(runner.serve(stop))
    try:
        done, _ = await asyncio.wait(
            {consumer_task, runner_task}, return_when=asyncio.FIRST_EXCEPTION
        )
        for task in done:
            error = task.exception()
            if error is not None:
                raise error
    finally:
        stop.set()
        for task in (consumer_task, runner_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(consumer_task, runner_task, return_exceptions=True)
        await telemetry.stop()
        connection, engine = resources
        await connection.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
