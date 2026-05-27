"""Testes de não-regressão: env vars deprecated não emitem warnings (issue #309).

Garante que as env vars removidas na fase 3 da issue #309 sejam silenciosamente
ignoradas — zero warnings de deprecação no startup, mesmo que estejam no ambiente.
"""

from __future__ import annotations

import logging

import pytest

from deile.config.settings import (Settings, _apply_env_overrides,
                                   get_settings, reset_settings)

# Env vars removidas em issue #309 fase 3 → não devem mais emitir warnings
# nem alterar nenhum atributo de Settings.
_REMOVED_DEPRECATED_ENV_VARS = [
    "DEILE_DEBUG",
    "DEILE_PREFERRED_MODEL",
    "DEILE_VISION_MODEL",
    "DEILE_BOT_APPROVAL_AUTO",
    "DEILE_LOOP_GUARD_DISABLE",
    "DEILE_LOOP_GUARD_MAX_CALLS",
    "DEILE_LOOP_GUARD_REPEAT_THRESHOLD",
    "DEILE_LOOP_GUARD_WINDOW_SIZE",
    "DEILE_LOOP_GUARD_WINDOW_THRESHOLD",
    "DEILE_LOOP_GUARD_NO_PROGRESS",
    "DEILE_PIPELINE_BASE_PATH",
    "DEILE_PIPELINE_REPO",
    "DEILE_PIPELINE_NOTIFY_USER_ID",
    "DEILE_PIPELINE_POLL_INTERVAL",
    "DEILE_PIPELINE_CLAUDE_TIMEOUT",
    "DEILE_PIPELINE_DISPATCH_MODE",
    "DEILE_PIPELINE_RESUME_ENABLED",
    "DEILE_PIPELINE_RESUME_INTERVAL",
    "DEILE_PIPELINE_RESUME_MAX_ATTEMPTS",
    "DEILE_PIPELINE_RESUME_BUDGET",
    "DEILE_CRON_DB_PATH",
    "DEILE_CRON_POLL_INTERVAL",
]


@pytest.fixture(autouse=True)
def _reset():
    reset_settings()
    logging.disable(logging.NOTSET)
    yield
    reset_settings()


class TestNoDeprecationWarnings:
    """Nenhuma env var deprecated emite WARNING ao passar por _apply_env_overrides."""

    def test_all_deprecated_vars_produce_no_warning(self, monkeypatch, caplog):
        for var in _REMOVED_DEPRECATED_ENV_VARS:
            monkeypatch.setenv(var, "1")
        s = Settings()
        with caplog.at_level(logging.WARNING, logger="deile.config.settings"):
            _apply_env_overrides(s)
        deprecation_records = [
            r for r in caplog.records
            if "deprecated" in r.getMessage().lower()
            and r.name == "deile.config.settings"
        ]
        assert deprecation_records == [], (
            f"Unexpected deprecation warnings: "
            f"{[r.getMessage() for r in deprecation_records]}"
        )

    @pytest.mark.parametrize("var", _REMOVED_DEPRECATED_ENV_VARS)
    def test_single_deprecated_var_no_warning(self, monkeypatch, caplog, var):
        monkeypatch.setenv(var, "1")
        s = Settings()
        with caplog.at_level(logging.WARNING, logger="deile.config.settings"):
            _apply_env_overrides(s)
        assert "deprecated" not in caplog.text.lower()


class TestDeprecatedVarsSilentlyIgnored:
    """Env vars deprecated não alteram nenhum campo de Settings."""

    def test_deile_debug_ignored(self, monkeypatch):
        s = Settings()
        original = s.debug_enabled
        monkeypatch.setenv("DEILE_DEBUG", "1")
        _apply_env_overrides(s)
        assert s.debug_enabled == original

    def test_deile_preferred_model_ignored(self, monkeypatch):
        s = Settings()
        original = s.preferred_model
        monkeypatch.setenv("DEILE_PREFERRED_MODEL", "anthropic:sentinel-model")
        _apply_env_overrides(s)
        assert s.preferred_model == original

    def test_deile_bot_approval_auto_ignored(self, monkeypatch):
        s = Settings()
        original = s.bot_approval_auto
        monkeypatch.setenv("DEILE_BOT_APPROVAL_AUTO", "true")
        _apply_env_overrides(s)
        assert s.bot_approval_auto == original

    def test_deile_pipeline_repo_ignored(self, monkeypatch):
        s = Settings()
        original = s.pipeline_repo
        monkeypatch.setenv("DEILE_PIPELINE_REPO", "myorg/should-not-apply")
        _apply_env_overrides(s)
        assert s.pipeline_repo == original

    def test_deile_pipeline_dispatch_mode_ignored(self, monkeypatch):
        s = Settings()
        original = s.pipeline_dispatch_mode
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", "claude-worker")
        _apply_env_overrides(s)
        assert s.pipeline_dispatch_mode == original

    def test_deile_pipeline_resume_enabled_ignored(self, monkeypatch):
        s = Settings()
        original = s.pipeline_resume_enabled
        monkeypatch.setenv("DEILE_PIPELINE_RESUME_ENABLED", "false")
        _apply_env_overrides(s)
        assert s.pipeline_resume_enabled == original

    def test_deile_loop_guard_max_calls_ignored(self, monkeypatch):
        s = Settings()
        original = s.loop_guard_max_calls
        monkeypatch.setenv("DEILE_LOOP_GUARD_MAX_CALLS", "999")
        _apply_env_overrides(s)
        assert s.loop_guard_max_calls == original

    def test_deile_cron_poll_interval_ignored(self, monkeypatch):
        s = Settings()
        original = s.cron_poll_interval
        monkeypatch.setenv("DEILE_CRON_POLL_INTERVAL", "5")
        _apply_env_overrides(s)
        assert s.cron_poll_interval == original


class TestCurrentEnvVarsStillWork:
    """Env vars atuais (não-deprecated) continuam funcionando normalmente."""

    def test_deile_pipeline_autostart_applies(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_AUTOSTART", "true")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_autostart is True

    def test_deile_forge_repo_applies(self, monkeypatch):
        monkeypatch.setenv("DEILE_FORGE_REPO", "myorg/myrepo")
        s = Settings()
        _apply_env_overrides(s)
        assert s.forge_repo == "myorg/myrepo"

    def test_deile_forge_kind_applies(self, monkeypatch):
        monkeypatch.setenv("DEILE_FORGE_KIND", "GITLAB")
        s = Settings()
        _apply_env_overrides(s)
        assert s.forge_kind == "gitlab"  # lowercased

    def test_deile_max_tool_iterations_applies(self, monkeypatch):
        monkeypatch.setenv("DEILE_MAX_TOOL_ITERATIONS", "50")
        s = Settings()
        _apply_env_overrides(s)
        assert s.max_tool_iterations == 50

    def test_deile_subagent_max_parallel_applies(self, monkeypatch):
        monkeypatch.setenv("DEILE_SUBAGENT_MAX_PARALLEL", "4")
        s = Settings()
        _apply_env_overrides(s)
        assert s.subagent_max_parallel == 4
