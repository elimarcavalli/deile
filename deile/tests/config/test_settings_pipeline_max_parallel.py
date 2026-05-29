"""Tests for DEILE_PIPELINE_MAX_PARALLEL auto-mode (issue #408).

Covers:
- ``_to_pos_int_or_auto`` converter: numeric, "auto", invalid values.
- ``_apply_env_overrides`` propagates "auto" to ``pipeline_max_parallel``.
- ``apply_overrides`` JSON path accepts "auto" via ``pipeline.max_parallel``.
- ``build_default_pipeline_config`` resolves "auto" via kubectl at startup.
- Fallback to 2 when kubectl is unavailable or replicas cannot be read.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deile.config.settings import (
    Settings,
    _apply_env_overrides,
    _to_pos_int_or_auto,
    reset_settings,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_settings()
    yield
    reset_settings()


# ---------------------------------------------------------------------------
# _to_pos_int_or_auto unit tests
# ---------------------------------------------------------------------------

class TestToPosIntOrAuto:
    def test_numeric_string_returns_int(self):
        assert _to_pos_int_or_auto("5") == 5
        assert _to_pos_int_or_auto("1") == 1
        assert _to_pos_int_or_auto("100") == 100

    def test_int_value_returns_int(self):
        assert _to_pos_int_or_auto(3) == 3

    def test_auto_lowercase_returns_sentinel(self):
        assert _to_pos_int_or_auto("auto") == "auto"

    def test_auto_uppercase_returns_sentinel(self):
        assert _to_pos_int_or_auto("AUTO") == "auto"

    def test_auto_mixed_case_returns_sentinel(self):
        assert _to_pos_int_or_auto("Auto") == "auto"

    def test_auto_with_whitespace_returns_sentinel(self):
        assert _to_pos_int_or_auto("  auto  ") == "auto"

    def test_zero_raises(self):
        with pytest.raises(ValueError, match=">= 1"):
            _to_pos_int_or_auto("0")

    def test_negative_raises(self):
        with pytest.raises(ValueError, match=">= 1"):
            _to_pos_int_or_auto("-1")

    def test_bool_raises(self):
        with pytest.raises(TypeError, match="bool"):
            _to_pos_int_or_auto(True)

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            _to_pos_int_or_auto("not-a-number")


# ---------------------------------------------------------------------------
# Env-var layer
# ---------------------------------------------------------------------------

class TestEnvVarAutoMode:
    def test_env_numeric_applies(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_MAX_PARALLEL", "4")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_max_parallel == 4

    def test_env_auto_applies(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_MAX_PARALLEL", "auto")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_max_parallel == "auto"

    def test_env_invalid_keeps_default(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_MAX_PARALLEL", "0")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_max_parallel == 2  # default unchanged


# ---------------------------------------------------------------------------
# JSON / apply_overrides path
# ---------------------------------------------------------------------------

class TestJsonPathAutoMode:
    def test_json_numeric_applies(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"max_parallel": 6}})
        assert s.pipeline_max_parallel == 6

    def test_json_auto_applies(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"max_parallel": "auto"}})
        assert s.pipeline_max_parallel == "auto"

    def test_json_zero_keeps_default(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"max_parallel": 0}})
        assert s.pipeline_max_parallel == 2  # rejected, default stays


# ---------------------------------------------------------------------------
# build_default_pipeline_config with auto mode
# ---------------------------------------------------------------------------

def _patch_build_deps(monkeypatch, settings, tmp_path):
    monkeypatch.setattr("deile.config.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "deile.orchestration.pipeline.constants.resolve_pipeline_repo",
        lambda: "owner/repo",
    )
    monkeypatch.setattr(
        "deile.tools._pipeline_paths.resolve_base_path", lambda: tmp_path
    )


def _make_settings(**kwargs) -> Settings:
    s = Settings()
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


class TestBuildDefaultPipelineConfigAutoMode:
    def test_numeric_setting_used_directly(self, monkeypatch, tmp_path):
        s = _make_settings(pipeline_max_parallel=5)
        _patch_build_deps(monkeypatch, s, tmp_path)

        from deile.orchestration.pipeline.monitor import build_default_pipeline_config
        cfg = build_default_pipeline_config()

        assert cfg.max_parallel == 5

    def test_auto_reads_replicas_from_kubectl(self, monkeypatch, tmp_path):
        s = _make_settings(pipeline_max_parallel="auto")
        _patch_build_deps(monkeypatch, s, tmp_path)

        import subprocess
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "4"

        with patch("shutil.which", return_value="/usr/bin/kubectl"), \
             patch("subprocess.run", return_value=fake_proc):
            from deile.orchestration.pipeline.monitor import build_default_pipeline_config
            cfg = build_default_pipeline_config()

        assert cfg.max_parallel == 4

    def test_auto_falls_back_to_2_when_kubectl_missing(self, monkeypatch, tmp_path):
        s = _make_settings(pipeline_max_parallel="auto")
        _patch_build_deps(monkeypatch, s, tmp_path)

        with patch("shutil.which", return_value=None):
            from deile.orchestration.pipeline.monitor import build_default_pipeline_config
            cfg = build_default_pipeline_config()

        assert cfg.max_parallel == 2

    def test_auto_falls_back_to_2_when_kubectl_fails(self, monkeypatch, tmp_path):
        s = _make_settings(pipeline_max_parallel="auto")
        _patch_build_deps(monkeypatch, s, tmp_path)

        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = "deployment not found"

        with patch("shutil.which", return_value="/usr/bin/kubectl"), \
             patch("subprocess.run", return_value=fake_proc):
            from deile.orchestration.pipeline.monitor import build_default_pipeline_config
            cfg = build_default_pipeline_config()

        assert cfg.max_parallel == 2

    def test_auto_falls_back_to_2_when_replicas_not_digit(self, monkeypatch, tmp_path):
        s = _make_settings(pipeline_max_parallel="auto")
        _patch_build_deps(monkeypatch, s, tmp_path)

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = ""  # empty output

        with patch("shutil.which", return_value="/usr/bin/kubectl"), \
             patch("subprocess.run", return_value=fake_proc):
            from deile.orchestration.pipeline.monitor import build_default_pipeline_config
            cfg = build_default_pipeline_config()

        assert cfg.max_parallel == 2

    def test_auto_falls_back_to_2_on_timeout(self, monkeypatch, tmp_path):
        import subprocess
        s = _make_settings(pipeline_max_parallel="auto")
        _patch_build_deps(monkeypatch, s, tmp_path)

        with patch("shutil.which", return_value="/usr/bin/kubectl"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("kubectl", 10)):
            from deile.orchestration.pipeline.monitor import build_default_pipeline_config
            cfg = build_default_pipeline_config()

        assert cfg.max_parallel == 2
