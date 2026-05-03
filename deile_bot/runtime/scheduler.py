"""Generic cron-job scheduler.

YAML-driven jobs; handler resolved as `module.path:fn_name`. Used by
Discord daily_digest and any other periodic task.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Dict, List

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class CronJob(BaseModel):
    name: str
    cron: str
    handler: str
    args: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class Scheduler:
    def __init__(self, jobs: List[CronJob], context: Dict[str, Any] = None):
        self.jobs = [j for j in jobs if j.enabled]
        self.context = context or {}
        self._scheduler: Any = None
        self._started = False

    def _load_handler(self, dotted: str) -> Callable:
        if ":" in dotted:
            mod_path, fn_name = dotted.split(":", 1)
        else:
            mod_path, fn_name = dotted.rsplit(".", 1)
        module = importlib.import_module(mod_path)
        return getattr(module, fn_name)

    async def start(self) -> None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            logger.warning("apscheduler not installed; scheduler disabled")
            return
        self._scheduler = AsyncIOScheduler()
        for job in self.jobs:
            try:
                fn = self._load_handler(job.handler)
            except Exception:
                logger.exception(f"failed to load handler for job {job.name}")
                continue
            trigger = CronTrigger.from_crontab(job.cron)

            async def runner(_fn=fn, _job=job):
                kwargs = dict(_job.args or {})
                kwargs.update(self.context)
                try:
                    if hasattr(_fn, "__call__"):
                        result = _fn(**kwargs)
                        if hasattr(result, "__await__"):
                            await result
                except Exception:
                    logger.exception(f"job {_job.name} raised")

            self._scheduler.add_job(runner, trigger=trigger, name=job.name)
        self._scheduler.start()
        self._started = True

    async def stop(self) -> None:
        if self._scheduler is not None and self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False

    def list_jobs(self) -> List[Dict[str, Any]]:
        return [
            {"name": j.name, "cron": j.cron, "handler": j.handler, "enabled": j.enabled}
            for j in self.jobs
        ]
