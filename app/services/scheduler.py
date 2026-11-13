"""Background scheduler wrapper around APScheduler's AsyncIOScheduler.

Phase 1 only sets up the scheduler object and lifecycle. Phase 2 registers the
war-detection job; Phase 5 registers the roster / stats / status refresh jobs.
Keeping registration out of this module means the scheduler has no knowledge of
war logic — callers pass in coroutines.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.utils.logging import get_logger

logger = get_logger(__name__)

# A job is any zero-arg coroutine function. Its return value (if any) is ignored.
AsyncJob = Callable[[], Coroutine[Any, Any, Any]]


class Scheduler:
    def __init__(self) -> None:
        # coalesce: if the loop was blocked, run a missed job once, not N times.
        # max_instances=1: never run two copies of the same job concurrently.
        self._scheduler = AsyncIOScheduler(
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30}
        )

    def add_interval_job(
        self, func: AsyncJob, *, seconds: int, job_id: str, start_immediately: bool = True
    ) -> None:
        trigger = IntervalTrigger(seconds=seconds)
        self._scheduler.add_job(
            func,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            next_run_time=None if not start_immediately else _now(),
        )
        logger.info("scheduler.job_added", job_id=job_id, interval_seconds=seconds)

    def reschedule(self, job_id: str, *, seconds: int) -> None:
        """Change an existing job's interval (used to switch war-check cadence)."""
        self._scheduler.reschedule_job(job_id, trigger=IntervalTrigger(seconds=seconds))
        logger.info("scheduler.job_rescheduled", job_id=job_id, interval_seconds=seconds)

    def start(self) -> None:
        self._scheduler.start()
        logger.info("scheduler.started")

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler.stopped")


def _now() -> Any:
    from datetime import datetime

    return datetime.now(tz=UTC)
