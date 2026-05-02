"""Tests for runtime/scheduler.py."""

from __future__ import annotations

from deile_bot.runtime.scheduler import CronJob, Scheduler


class TestSchedulerLoading:
    def test_cron_job_dataclass(self):
        j = CronJob(name="test", cron="0 9 * * *", handler="some.module:fn")
        assert j.enabled is True
        assert j.args == {}

    def test_disabled_filtered(self):
        s = Scheduler([
            CronJob(name="a", cron="* * * * *", handler="x:y", enabled=False),
            CronJob(name="b", cron="* * * * *", handler="x:y", enabled=True),
        ])
        assert len(s.jobs) == 1
        assert s.jobs[0].name == "b"

    def test_list_jobs(self):
        s = Scheduler([CronJob(name="a", cron="0 9 * * *", handler="x:y")])
        out = s.list_jobs()
        assert out[0]["name"] == "a"
        assert out[0]["cron"] == "0 9 * * *"

    def test_handler_resolver(self):
        s = Scheduler([])
        # Resolve a real callable
        fn = s._load_handler("json:dumps")
        assert callable(fn)
