"""Unit tests for ``_time_utils`` ISO-8601 UTC helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from deile.orchestration.pipeline._time_utils import (format_iso_utc, now_utc,
                                                      parse_iso_utc)


def test_now_utc_returns_utc_aware():
    dt = now_utc()
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_round_trip_now_utc_format_parse():
    dt = now_utc().replace(microsecond=0)  # canonical format drops micros
    s = format_iso_utc(dt)
    assert s is not None and s.endswith("Z")
    back = parse_iso_utc(s)
    assert back == dt


def test_parse_iso_utc_passthrough_on_none():
    assert parse_iso_utc(None) is None


def test_format_iso_utc_promotes_naive_to_utc():
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert naive.tzinfo is None
    s = format_iso_utc(naive)
    assert s == "2026-01-02T03:04:05Z"


def test_parse_iso_utc_raises_value_error_on_bad_type():
    with pytest.raises(ValueError):
        parse_iso_utc(12345)


def test_parse_iso_utc_handles_z_suffix():
    dt = parse_iso_utc("2026-05-19T12:00:00Z")
    assert dt == datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_utc_rejects_double_z_suffix():
    """Only one trailing Z is stripped — malformed inputs still raise."""
    with pytest.raises(ValueError):
        parse_iso_utc("2026-05-19T12:00:00ZZZ")
