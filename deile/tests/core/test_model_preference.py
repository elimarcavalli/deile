from types import SimpleNamespace

import pytest

from deile.core.agent import _select_configured_model_provider
from deile.core.exceptions import ModelError


def _provider(provider_id: str, model_name: str):
    return SimpleNamespace(provider_id=provider_id, model_name=model_name)


def _router(*providers):
    return SimpleNamespace(
        providers={
            f"{provider.provider_id}:{provider.model_name}": provider
            for provider in providers
        }
    )


def _session(**context_data):
    return SimpleNamespace(context_data=context_data)


@pytest.mark.unit
def test_forced_model_is_hard_override(monkeypatch):
    preferred = _provider("deepseek", "deepseek-v4-pro")
    default = _provider("gemini", "gemini-2.5-flash-lite")
    monkeypatch.setattr(
        "deile.core.agent._get_config_default_model",
        lambda: "gemini:gemini-2.5-flash-lite",
    )

    selected, forced, soft_handle, soft_source = _select_configured_model_provider(
        _router(preferred, default),
        _session(
            forced_model="deepseek:deepseek-v4-pro",
            preferred_model="gemini:gemini-2.5-flash-lite",
        ),
    )

    assert selected is preferred
    assert forced == "deepseek:deepseek-v4-pro"
    assert soft_handle is None
    assert soft_source is None


@pytest.mark.unit
def test_forced_model_missing_raises():
    with pytest.raises(ModelError) as exc:
        _select_configured_model_provider(
            _router(_provider("gemini", "gemini-2.5-flash-lite")),
            _session(forced_model="deepseek:deepseek-v4-pro"),
        )

    assert exc.value.error_code == "FORCED_MODEL_NOT_REGISTERED"


@pytest.mark.unit
def test_bot_preferred_model_wins_over_core_default(monkeypatch):
    preferred = _provider("deepseek", "deepseek-v4-pro")
    default = _provider("gemini", "gemini-2.5-flash-lite")
    monkeypatch.setattr(
        "deile.core.agent._get_config_default_model",
        lambda: "gemini:gemini-2.5-flash-lite",
    )

    selected, forced, soft_handle, soft_source = _select_configured_model_provider(
        _router(preferred, default),
        _session(preferred_model="deepseek:deepseek-v4-pro"),
    )

    assert selected is preferred
    assert forced is None
    assert soft_handle == "deepseek:deepseek-v4-pro"
    assert soft_source == "preferred_model"


@pytest.mark.unit
def test_unregistered_bot_preference_falls_back_to_core_default(monkeypatch):
    default = _provider("gemini", "gemini-2.5-flash-lite")
    monkeypatch.setattr(
        "deile.core.agent._get_config_default_model",
        lambda: "gemini:gemini-2.5-flash-lite",
    )

    selected, forced, soft_handle, soft_source = _select_configured_model_provider(
        _router(default),
        _session(preferred_model="deepseek:deepseek-v4-pro"),
    )

    assert selected is default
    assert forced is None
    assert soft_handle == "gemini:gemini-2.5-flash-lite"
    assert soft_source == "default_model"


@pytest.mark.unit
def test_malformed_soft_preference_is_ignored(monkeypatch):
    default = _provider("gemini", "gemini-2.5-flash-lite")
    monkeypatch.setattr(
        "deile.core.agent._get_config_default_model",
        lambda: "gemini:gemini-2.5-flash-lite",
    )

    selected, _, soft_handle, soft_source = _select_configured_model_provider(
        _router(default),
        _session(preferred_model="deepseek-v4-pro"),
    )

    assert selected is default
    assert soft_handle == "gemini:gemini-2.5-flash-lite"
    assert soft_source == "default_model"
