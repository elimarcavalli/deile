"""Unit tests for the tiny cron evaluator."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from deile.orchestration.pipeline.cron import (CronExpressionError, matches,
                                               next_after, parse,
                                               previous_or_equal)


class TestParse:
    def test_star(self):
        m, h, dom, mon, dow = parse("* * * * *")
        assert m == list(range(60))
        assert h == list(range(24))
        assert dom == list(range(1, 32))
        assert mon == list(range(1, 13))
        assert dow == list(range(7))

    def test_step(self):
        m, *_ = parse("*/5 * * * *")
        assert m == [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]

    def test_range(self):
        _, h, *_ = parse("0 9-17 * * *")
        assert h == list(range(9, 18))

    def test_list(self):
        _, h, *_ = parse("0 8,12,18 * * *")
        assert h == [8, 12, 18]

    def test_predefined_daily(self):
        m, h, dom, mon, dow = parse("@daily")
        assert m == [0]
        assert h == [0]

    def test_invalid_field_count(self):
        with pytest.raises(CronExpressionError):
            parse("* * * *")

    def test_out_of_range(self):
        with pytest.raises(CronExpressionError):
            parse("99 * * * *")

    def test_invalid_step_zero(self):
        with pytest.raises(CronExpressionError):
            parse("*/0 * * * *")


class TestMatches:
    def _dt(self, s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

    def test_every_5_minutes_match(self):
        assert matches("*/5 * * * *", self._dt("2026-05-06T01:00:00"))
        assert matches("*/5 * * * *", self._dt("2026-05-06T01:05:00"))

    def test_every_5_minutes_no_match(self):
        assert not matches("*/5 * * * *", self._dt("2026-05-06T01:03:00"))

    def test_specific_hour(self):
        assert matches("0 14 * * *", self._dt("2026-05-06T14:00:00"))
        assert not matches("0 14 * * *", self._dt("2026-05-06T14:05:00"))

    def test_weekday_only(self):
        # 2026-05-06 is Wed (weekday 2 → cron dow 3)
        assert matches("0 9 * * 1-5", self._dt("2026-05-06T09:00:00"))
        # 2026-05-09 is Sat (weekday 5 → cron dow 6)
        assert not matches("0 9 * * 1-5", self._dt("2026-05-09T09:00:00"))


class TestNextAfter:
    def _dt(self, s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

    def test_strictly_after(self):
        nxt = next_after("*/5 * * * *", self._dt("2026-05-06T01:00:00"))
        # Strictly AFTER 01:00 → 01:05
        assert nxt == self._dt("2026-05-06T01:05:00")

    def test_handles_day_rollover(self):
        nxt = next_after("0 0 * * *", self._dt("2026-05-06T23:30:00"))
        assert nxt == self._dt("2026-05-07T00:00:00")

    def test_naive_datetime_assumed_utc(self):
        # Should not raise.
        nxt = next_after("*/10 * * * *", datetime(2026, 5, 6, 1, 0, 0))
        assert nxt.tzinfo is not None


class TestPreviousOrEqual:
    def _dt(self, s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

    def test_returns_self_when_match(self):
        assert previous_or_equal("*/5 * * * *", self._dt("2026-05-06T01:05:00")) == \
               self._dt("2026-05-06T01:05:00")

    def test_searches_backwards(self):
        # 01:03 is not a match for */5; previous match is 01:00
        assert previous_or_equal("*/5 * * * *", self._dt("2026-05-06T01:03:00")) == \
               self._dt("2026-05-06T01:00:00")
