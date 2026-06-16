"""Testes de não-regressão: env vars não emitem warnings de deprecação (issue #309).

A issue #309 fase 3 removeu a maquinaria de aviso de deprecação (o 4º campo
``deprecated=True`` da tabela ``_ENV_OVERRIDES``). Algumas env vars legadas
foram mantidas ativas (usadas por testes de isolamento e por operadores —
ver comentário em settings.py). Só um subconjunto delas foi silenciado de fato.

Este módulo testa:
  1. Nenhuma env var emite WARNING com "deprecated" no texto.
  2. As vars verdadeiramente silenciadas não alteram Settings.
  3. As vars atuais continuam funcionando.
"""

from __future__ import annotations

import logging

import pytest

from deile.config.settings import Settings, _apply_env_overrides, reset_settings

# Env vars que passam por _apply_env_overrides sem emitir WARNING "deprecated".
# Inclui tanto vars ativas quanto vars silenciadas — o critério aqui é
# ausência de warning, não ausência de efeito.
_ALL_KNOWN_ENV_VARS = [
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

# Subconjunto que foi genuinamente silenciado (não altera Settings). Não inclui
# vars que ainda são ativas (DEILE_DEBUG, LOOP_GUARD_*, BOT_APPROVAL_AUTO,
# CRON_DB_PATH, CRON_POLL_INTERVAL, PIPELINE_BASE_PATH, PIPELINE_DISPATCH_MODE)
# — essas continuam funcionando para isolamento de testes e compatibilidade.
_TRULY_SILENCED_ENV_VARS = [
    "DEILE_PIPELINE_REPO",
    "DEILE_PIPELINE_NOTIFY_USER_ID",
    "DEILE_PIPELINE_POLL_INTERVAL",
    "DEILE_PIPELINE_CLAUDE_TIMEOUT",
    "DEILE_PIPELINE_RESUME_ENABLED",
    "DEILE_PIPELINE_RESUME_INTERVAL",
    "DEILE_PIPELINE_RESUME_MAX_ATTEMPTS",
    "DEILE_PIPELINE_RESUME_BUDGET",
]

# Mantido como alias para compat com testes que referenciam o nome antigo.
_REMOVED_DEPRECATED_ENV_VARS = _ALL_KNOWN_ENV_VARS


@pytest.fixture(autouse=True)
def _reset():
    reset_settings()
    logging.disable(logging.NOTSET)
    yield
    reset_settings()


class TestNoDeprecationWarnings:
    """Nenhuma env var emite WARNING com texto 'deprecated' ao passar por
    _apply_env_overrides — o mecanismo de warning foi removido em #309 fase 3.
    """

    def test_all_known_vars_produce_no_deprecation_warning(self, monkeypatch, caplog):
        for var in _ALL_KNOWN_ENV_VARS:
            monkeypatch.setenv(var, "1")
        s = Settings()
        with caplog.at_level(logging.WARNING, logger="deile.config.settings"):
            _apply_env_overrides(s)
        deprecation_records = [
            r
            for r in caplog.records
            if "deprecated" in r.getMessage().lower()
            and r.name == "deile.config.settings"
        ]
        assert deprecation_records == [], (
            f"Unexpected deprecation warnings: "
            f"{[r.getMessage() for r in deprecation_records]}"
        )

    @pytest.mark.parametrize("var", _ALL_KNOWN_ENV_VARS)
    def test_single_var_no_deprecation_warning(self, monkeypatch, caplog, var):
        monkeypatch.setenv(var, "1")
        s = Settings()
        with caplog.at_level(logging.WARNING, logger="deile.config.settings"):
            _apply_env_overrides(s)
        assert "deprecated" not in caplog.text.lower()


class TestDeprecatedVarsSilentlyIgnored:
    """Vars verdadeiramente silenciadas (sem mapping em _ENV_OVERRIDES) não
    alteram nenhum campo de Settings. Vars mantidas ativas (loop_guard, cron,
    debug, etc.) continuam funcionando — ver _TRULY_SILENCED_ENV_VARS.
    """

    def test_deile_pipeline_repo_ignored(self, monkeypatch):
        s = Settings()
        original = s.pipeline_repo
        monkeypatch.setenv("DEILE_PIPELINE_REPO", "myorg/should-not-apply")
        _apply_env_overrides(s)
        assert s.pipeline_repo == original

    def test_deile_pipeline_resume_enabled_ignored(self, monkeypatch):
        s = Settings()
        original = s.pipeline_resume_enabled
        monkeypatch.setenv("DEILE_PIPELINE_RESUME_ENABLED", "false")
        _apply_env_overrides(s)
        assert s.pipeline_resume_enabled == original

    def test_deile_pipeline_resume_budget_ignored(self, monkeypatch):
        s = Settings()
        original = s.pipeline_resume_budget
        monkeypatch.setenv("DEILE_PIPELINE_RESUME_BUDGET", "99.9")
        _apply_env_overrides(s)
        assert s.pipeline_resume_budget == original


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
