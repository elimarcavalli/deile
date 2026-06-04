"""Tests for cron constants — hot-reload behaviour after reset_settings()."""

from __future__ import annotations

import json

from deile.config.settings import get_settings, reset_settings
from deile.cron.constants import (CRON_STOP_TIMEOUT_SECONDS,
                                  cron_poll_interval_seconds)


class TestCronPollIntervalSeconds:
    """cron_poll_interval_seconds() must reflect settings changes after reset_settings()."""

    def test_returns_current_setting_value(self):
        """Sanity: returns the default (or whatever is currently set)."""
        reset_settings()
        result = cron_poll_interval_seconds()
        assert isinstance(result, int)
        assert result > 0

    def test_reflects_settings_change_after_reset(self, tmp_path, monkeypatch):
        """After settings.json change + reset_settings(), next call returns new value."""
        settings_file = tmp_path / "settings.json"
        monkeypatch.setenv("DEILE_SETTINGS_FILE", str(settings_file))

        settings_file.write_text(json.dumps({
            "cron": {"poll_interval": 77}
        }))
        reset_settings()
        assert cron_poll_interval_seconds() == 77

        settings_file.write_text(json.dumps({
            "cron": {"poll_interval": 577}
        }))
        reset_settings()
        assert cron_poll_interval_seconds() == 577

    def test_each_call_re_reads_settings(self):
        """Two calls in a row with different settings return different values."""
        reset_settings()
        first = cron_poll_interval_seconds()

        settings = get_settings()
        settings.cron_poll_interval = first + 200
        second = cron_poll_interval_seconds()

        assert second == first + 200
        assert second != first


class TestCronStopTimeoutConstant:
    """CRON_STOP_TIMEOUT_SECONDS must remain a pure Python constant (regression)."""

    def test_is_constant_not_callable(self):
        """Regression: CRON_STOP_TIMEOUT_SECONDS is an int, not a function."""
        assert isinstance(CRON_STOP_TIMEOUT_SECONDS, int)
        assert not callable(CRON_STOP_TIMEOUT_SECONDS)

    def test_value_is_five(self):
        assert CRON_STOP_TIMEOUT_SECONDS == 5
