"""Standalone worker: `python -m app.worker`. SIGTERM/SIGINT let the current job finish, then exit."""

import asyncio
import signal

from app.config import get_settings
from app.container import build_container
from app.jobs.runner import JobRunner
from app.logging import configure_logging


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.resolved_log_format)
    runner = JobRunner.from_container(build_container(settings))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, runner.stop)
    await runner.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
