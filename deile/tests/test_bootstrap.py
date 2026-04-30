"""Tests: multi-provider bootstrap — Phase 16."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deile.core.models.bootstrap import bootstrap_providers

_YAML_PATH = Path(__file__).parents[2] / "deile" / "config" / "model_providers.yaml"


# ---------------------------------------------------------------------------
# No API keys set → no providers registered
# ---------------------------------------------------------------------------

class TestBootstrapNoKeys:
    def test_no_keys_returns_empty(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        registered = bootstrap_providers(yaml_path=_YAML_PATH)
        assert registered == []


# ---------------------------------------------------------------------------
# ANTHROPIC_API_KEY only → anthropic registered
# ---------------------------------------------------------------------------

class TestBootstrapAnthropicOnly:
    def test_anthropic_only_registers(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        from deile.core.models.tier_router import reset_tier_router
        reset_tier_router()

        with patch("deile.core.models.bootstrap._import_provider_class") as mock_cls_factory:
            mock_provider = MagicMock()
            mock_provider.provider_id = "anthropic"
            mock_cls = MagicMock(return_value=mock_provider)
            mock_cls_factory.return_value = mock_cls

            registered = bootstrap_providers(yaml_path=_YAML_PATH)

        assert "anthropic" in registered
        assert "openai" not in registered
        assert "deepseek" not in registered

    def test_anthropic_only_no_openai(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        from deile.core.models.tier_router import reset_tier_router
        reset_tier_router()

        with patch("deile.core.models.bootstrap._import_provider_class") as mock_cls_factory:
            mock_provider = MagicMock()
            mock_cls = MagicMock(return_value=mock_provider)
            mock_cls_factory.return_value = mock_cls

            registered = bootstrap_providers(yaml_path=_YAML_PATH)

        assert len(registered) == 1
        assert registered[0] == "anthropic"


# ---------------------------------------------------------------------------
# All three main providers → all registered
# ---------------------------------------------------------------------------

class TestBootstrapAllProviders:
    def test_all_three_registered(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        from deile.core.models.tier_router import reset_tier_router
        reset_tier_router()

        with patch("deile.core.models.bootstrap._import_provider_class") as mock_cls_factory:
            mock_provider_a = MagicMock()
            mock_provider_a.provider_id = "anthropic"
            mock_provider_b = MagicMock()
            mock_provider_b.provider_id = "openai"
            mock_provider_c = MagicMock()
            mock_provider_c.provider_id = "deepseek"

            # Each call to mock_cls_factory returns a class that creates a different mock
            call_count = [0]
            providers = [mock_provider_a, mock_provider_b, mock_provider_c]

            def _make_cls(*args, **kwargs):
                idx = call_count[0]
                call_count[0] += 1
                return providers[idx % 3]

            mock_cls = MagicMock(side_effect=_make_cls)
            mock_cls_factory.return_value = mock_cls

            registered = bootstrap_providers(yaml_path=_YAML_PATH)

        assert "anthropic" in registered
        assert "openai" in registered
        assert "deepseek" in registered

    def test_three_providers_count(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        from deile.core.models.tier_router import reset_tier_router
        reset_tier_router()

        with patch("deile.core.models.bootstrap._import_provider_class") as mock_cls_factory:
            mock_provider = MagicMock()
            mock_cls = MagicMock(return_value=mock_provider)
            mock_cls_factory.return_value = mock_cls

            registered = bootstrap_providers(yaml_path=_YAML_PATH)

        assert len(registered) == 3


# ---------------------------------------------------------------------------
# Registers provider in router when router is passed
# ---------------------------------------------------------------------------

class TestBootstrapRouterRegistration:
    def test_registers_in_passed_router(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        from deile.core.models.tier_router import reset_tier_router
        reset_tier_router()

        mock_router = MagicMock()

        with patch("deile.core.models.bootstrap._import_provider_class") as mock_cls_factory:
            mock_provider = MagicMock()
            mock_cls = MagicMock(return_value=mock_provider)
            mock_cls_factory.return_value = mock_cls

            bootstrap_providers(yaml_path=_YAML_PATH, router=mock_router)

        mock_router.register_provider.assert_called()

    def test_skipped_providers_not_registered(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        mock_router = MagicMock()
        registered = bootstrap_providers(yaml_path=_YAML_PATH, router=mock_router)

        assert registered == []
        mock_router.register_provider.assert_not_called()


# ---------------------------------------------------------------------------
# Provider instantiation failure is handled gracefully
# ---------------------------------------------------------------------------

class TestBootstrapInstantiationFailure:
    def test_instantiation_error_does_not_crash(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        with patch("deile.core.models.bootstrap._import_provider_class") as mock_cls_factory:
            mock_cls = MagicMock(side_effect=ValueError("SDK init failed"))
            mock_cls_factory.return_value = mock_cls

            registered = bootstrap_providers(yaml_path=_YAML_PATH)

        assert registered == []
