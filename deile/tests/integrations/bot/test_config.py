"""Settings loading for the deile→bot integration."""

from __future__ import annotations

import pytest

from deile.integrations.bot import BotIntegrationSettings
from deile.integrations.bot.config import reset_bot_settings_cache


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_bot_settings_cache()
    yield
    reset_bot_settings_cache()


def test_defaults_are_unconfigured():
    s = BotIntegrationSettings(endpoint="", auth_token="")
    assert s.is_configured is False


def test_endpoint_alone_is_not_enough():
    s = BotIntegrationSettings(endpoint="http://x", auth_token="")
    assert s.is_configured is False


def test_token_alone_is_not_enough():
    s = BotIntegrationSettings(endpoint="", auth_token="t")
    assert s.is_configured is False


def test_both_makes_it_configured():
    s = BotIntegrationSettings(endpoint="http://x", auth_token="t")
    assert s.is_configured is True


def test_disabled_overrides():
    s = BotIntegrationSettings(endpoint="http://x", auth_token="t", disabled=True)
    assert s.is_configured is False


def test_env_loading(monkeypatch):
    monkeypatch.setenv("DEILE_BOT_ENDPOINT", "http://envhost:9999")
    monkeypatch.setenv("DEILE_BOT_AUTH_TOKEN", "envtoken")
    monkeypatch.setenv("DEILE_BOT_TIMEOUT_S", "7.5")
    s = BotIntegrationSettings()
    assert s.endpoint == "http://envhost:9999"
    assert s.auth_token == "envtoken"
    assert s.timeout_s == 7.5
    assert s.is_configured


def test_repr_masks_token():
    s = BotIntegrationSettings(endpoint="http://x", auth_token="topsecret")
    assert "topsecret" not in repr(s)
