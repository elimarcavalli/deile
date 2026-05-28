"""Tests for pipeline constants — hot-reload behaviour after reset_settings()."""

from __future__ import annotations

import json

from deile.config.settings import get_settings, reset_settings
from deile.orchestration.pipeline.constants import (
    PIPELINE_STOP_TIMEOUT_SECONDS,
    claude_timeout_seconds,
    pipeline_poll_interval_seconds,
)


class TestClaudeTimeoutSeconds:
    """claude_timeout_seconds() must reflect settings changes after reset_settings()."""

    def test_returns_current_setting_value(self):
        """Sanity: returns the default (or whatever is currently set)."""
        reset_settings()
        result = claude_timeout_seconds()
        assert isinstance(result, int)
        assert result > 0

    def test_reflects_settings_change_after_reset(self, tmp_path, monkeypatch):
        """After settings.json change + reset_settings(), next call returns new value."""
        settings_file = tmp_path / "settings.json"
        monkeypatch.setenv("DEILE_SETTINGS_FILE", str(settings_file))

        # Write initial value
        settings_file.write_text(json.dumps({
            "pipeline": {"claude_timeout": 999}
        }))
        reset_settings()
        assert claude_timeout_seconds() == 999

        # Write new value — simulates SettingsManager.set_setting() + reset_settings()
        settings_file.write_text(json.dumps({
            "pipeline": {"claude_timeout": 1998}
        }))
        reset_settings()
        assert claude_timeout_seconds() == 1998

    def test_each_call_re_reads_settings(self):
        """Two calls in a row with different settings return different values."""
        reset_settings()
        first = claude_timeout_seconds()

        settings = get_settings()
        settings.pipeline_claude_timeout = first + 500
        # No reset_settings() here — just mutate the singleton
        second = claude_timeout_seconds()

        assert second == first + 500
        assert second != first


class TestPipelinePollIntervalSeconds:
    """pipeline_poll_interval_seconds() must reflect settings changes after reset_settings()."""

    def test_returns_current_setting_value(self):
        reset_settings()
        result = pipeline_poll_interval_seconds()
        assert isinstance(result, int)
        assert result > 0

    def test_reflects_settings_change_after_reset(self, tmp_path, monkeypatch):
        """After settings.json change + reset_settings(), next call returns new value."""
        settings_file = tmp_path / "settings.json"
        monkeypatch.setenv("DEILE_SETTINGS_FILE", str(settings_file))

        settings_file.write_text(json.dumps({
            "pipeline": {"poll_interval": 45}
        }))
        reset_settings()
        assert pipeline_poll_interval_seconds() == 45

        settings_file.write_text(json.dumps({
            "pipeline": {"poll_interval": 345}
        }))
        reset_settings()
        assert pipeline_poll_interval_seconds() == 345

    def test_each_call_re_reads_settings(self):
        reset_settings()
        first = pipeline_poll_interval_seconds()

        settings = get_settings()
        settings.pipeline_poll_interval = first + 100
        second = pipeline_poll_interval_seconds()

        assert second == first + 100
        assert second != first


class TestPipelineStopTimeoutConstant:
    """PIPELINE_STOP_TIMEOUT_SECONDS must remain a pure Python constant (regression)."""

    def test_is_constant_not_callable(self):
        """Regression: PIPELINE_STOP_TIMEOUT_SECONDS is an int, not a function."""
        assert isinstance(PIPELINE_STOP_TIMEOUT_SECONDS, int)
        assert not callable(PIPELINE_STOP_TIMEOUT_SECONDS)

    def test_value_is_five(self):
        assert PIPELINE_STOP_TIMEOUT_SECONDS == 5


class TestResetSettingsClearsAndRebuilds:
    """reset_settings() must clear the singleton so get_settings() rebuilds."""

    def test_reset_settings_creates_fresh_instance(self, tmp_path, monkeypatch):
        settings_file = tmp_path / "settings.json"
        monkeypatch.setenv("DEILE_SETTINGS_FILE", str(settings_file))

        settings_file.write_text(json.dumps({
            "pipeline": {"claude_timeout": 777}
        }))
        reset_settings()
        s1 = get_settings()
        assert s1.pipeline_claude_timeout == 777

        # Mutate in-memory (not persisted)
        s1.pipeline_claude_timeout = 1

        # reset_settings() clears the singleton; next get_settings() reads from file
        reset_settings()
        s2 = get_settings()
        # File still says 777 — the in-memory mutation was discarded
        assert s2.pipeline_claude_timeout == 777
