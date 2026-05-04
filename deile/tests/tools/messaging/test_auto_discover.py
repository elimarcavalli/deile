"""Auto-discovery rules for messaging tools.

Three scenarios:
  1. deilebot missing       → 0 tools registered, no warning
  2. settings unconfigured          → 0 tools registered
  3. both available + configured    → 7 tools registered
"""

from __future__ import annotations

import importlib

import pytest

from deile.integrations.bot.config import reset_bot_settings_cache
from deile.tools.messaging.auto_discover import register_messaging_tools
from deile.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reset_bot_settings_cache()
    yield
    reset_bot_settings_cache()


def test_missing_client_registers_zero(monkeypatch):
    """Simulate `deilebot` not being installed."""
    monkeypatch.setattr("deile.tools.messaging.auto_discover", importlib.import_module(
        "deile.tools.messaging.auto_discover"
    ))
    monkeypatch.setattr(
        "deile.integrations.bot.BOT_CLIENT_AVAILABLE", False, raising=True
    )
    registry = ToolRegistry()
    n = register_messaging_tools(registry)
    assert n == 0
    assert len(registry) == 0


def test_unconfigured_registers_zero(monkeypatch):
    """The integration is not configured → 0 tools register.

    This test patches BOT_CLIENT_AVAILABLE=True and forces an
    unconfigured BotIntegrationSettings instance, regardless of what
    the surrounding shell env or `.env` says. We can't rely on
    `monkeypatch.delenv` alone because pydantic-settings also reads
    `.env` from the project root.
    """
    monkeypatch.setattr(
        "deile.integrations.bot.BOT_CLIENT_AVAILABLE", True, raising=True
    )
    import deile.integrations.bot.config as cfg
    from deile.integrations.bot import BotIntegrationSettings
    forced = BotIntegrationSettings(endpoint="", auth_token="", disabled=True)
    monkeypatch.setattr(cfg, "get_bot_settings", lambda: forced, raising=True)
    # The auto_discover module re-imports get_bot_settings; patching the
    # source module is enough because it does `from .config import` lazily.
    monkeypatch.setattr(
        "deile.integrations.bot.get_bot_settings", lambda: forced, raising=True
    )
    registry = ToolRegistry()
    n = register_messaging_tools(registry)
    assert n == 0
    assert len(registry) == 0


def test_full_setup_registers_all_seven(monkeypatch):
    monkeypatch.setattr(
        "deile.integrations.bot.BOT_CLIENT_AVAILABLE", True, raising=True
    )
    monkeypatch.setenv("DEILE_BOT_ENDPOINT", "http://127.0.0.1:1234")
    monkeypatch.setenv("DEILE_BOT_AUTH_TOKEN", "tok")
    reset_bot_settings_cache()
    registry = ToolRegistry()
    n = register_messaging_tools(registry)
    assert n == 7
    expected = {
        "discord_send_message",
        "discord_send_dm",
        "discord_react",
        "discord_start_thread",
        "discord_pin_message",
        "discord_mention_role",
        "discord_get_user_profile",
    }
    assert {t.name for t in registry.list_all()} >= expected


def test_idempotent(monkeypatch):
    """Calling twice is safe — tools already registered are skipped."""
    monkeypatch.setattr(
        "deile.integrations.bot.BOT_CLIENT_AVAILABLE", True, raising=True
    )
    monkeypatch.setenv("DEILE_BOT_ENDPOINT", "http://127.0.0.1:1234")
    monkeypatch.setenv("DEILE_BOT_AUTH_TOKEN", "tok")
    reset_bot_settings_cache()
    registry = ToolRegistry()
    n1 = register_messaging_tools(registry)
    n2 = register_messaging_tools(registry)
    assert n1 == 7
    assert n2 == 0
    assert len(registry) == 7
