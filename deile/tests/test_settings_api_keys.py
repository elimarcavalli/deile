"""Tests: Settings.get_api_key covers all four multi-provider keys."""

import os
import pytest

from deile.config.settings import Settings


def test_deepseek_key_read_from_env(monkeypatch):
    """get_api_key('deepseek') returns the value of DEEPSEEK_API_KEY."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    settings = Settings()
    assert settings.get_api_key("deepseek") == "ds-test-key"


def test_deepseek_key_absent_returns_none(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = Settings()
    assert settings.get_api_key("deepseek") is None


def test_anthropic_key_read_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    settings = Settings()
    assert settings.get_api_key("anthropic") == "sk-ant-test"


def test_openai_key_read_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
    settings = Settings()
    assert settings.get_api_key("openai") == "sk-oai-test"


def test_gemini_key_read_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "goog-test")
    settings = Settings()
    assert settings.get_api_key("gemini") == "goog-test"
