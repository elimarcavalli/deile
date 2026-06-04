"""Tests for issue #360: wire forge.bot_login → mention_handle.

Verifies that :func:`build_default_pipeline_config` picks up
``forge.bot_login`` / ``DEILE_FORGE_BOT_LOGIN`` instead of the hardcoded
``"@deile-one"`` default.
"""

from __future__ import annotations

from pathlib import Path

from deile.config.settings import Settings
from deile.orchestration.pipeline.monitor import (
    PipelineConfig, build_default_pipeline_config)


def _make_settings(**kwargs) -> Settings:
    s = Settings()
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _patch_build_defaults(monkeypatch, settings, tmp_path):
    """Patch the callables build_default_pipeline_config() needs."""
    monkeypatch.setattr("deile.config.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "deile.orchestration.pipeline.constants.resolve_pipeline_repo",
        lambda: "owner/repo",
    )
    monkeypatch.setattr(
        "deile.tools._pipeline_paths.resolve_base_path", lambda: tmp_path
    )


def test_mention_handle_default_when_nothing_set():
    """Default forge_bot_login is ``@deile-one``; PipelineConfig carries it."""
    s = Settings()
    assert s.forge_bot_login == "@deile-one"
    cfg = PipelineConfig(repo="o/r", base_repo_path=Path("/tmp"))
    assert cfg.mention_handle == "@deile-one"


def test_mention_handle_uses_forge_bot_login_setting(monkeypatch, tmp_path):
    """build_default_pipeline_config() propagates forge_bot_login to mention_handle."""
    s = _make_settings(forge_bot_login="@my-custom-bot")
    _patch_build_defaults(monkeypatch, s, tmp_path)

    cfg = build_default_pipeline_config()

    assert cfg.mention_handle == "@my-custom-bot"


def test_mention_handle_env_var_overrides_setting(monkeypatch, tmp_path):
    """DEILE_FORGE_BOT_LOGIN env var is picked up by the settings layer."""
    from deile.config.settings import _apply_env_overrides

    monkeypatch.setenv("DEILE_FORGE_BOT_LOGIN", "@env-bot")
    s = Settings()
    _apply_env_overrides(s)
    assert s.forge_bot_login == "@env-bot"

    _patch_build_defaults(monkeypatch, s, tmp_path)
    cfg = build_default_pipeline_config()
    assert cfg.mention_handle == "@env-bot"
