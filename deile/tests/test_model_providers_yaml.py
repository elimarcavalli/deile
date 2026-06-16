"""Tests: model_providers.yaml structure and content validation."""

from pathlib import Path

import pytest
import yaml

_YAML_PATH = Path(__file__).parent.parent / "config" / "model_providers.yaml"


@pytest.fixture(scope="module")
def cfg():
    with open(_YAML_PATH) as f:
        return yaml.safe_load(f)


def test_yaml_loads(cfg):
    assert cfg is not None


def test_version(cfg):
    assert cfg["version"] == 1


def test_default_strategy(cfg):
    assert cfg["default_strategy"] in ("task_optimized", "cost_optimized")


def test_four_providers_defined(cfg):
    providers = cfg["providers"]
    for pid in ("anthropic", "openai", "deepseek", "gemini"):
        assert pid in providers, f"Provider '{pid}' missing"


def test_model_catalog_size_sane(cfg):
    # Catálogo cresce conforme providers liberam modelos; o teste apenas
    # garante que o YAML não está vazio ou patológico (>30 = provavelmente
    # duplicação acidental). Edits específicos no catálogo ficam cobertos
    # pelos testes downstream (router, cost, fallback).
    models = cfg["models"]
    assert 4 <= len(models) <= 30, f"Catálogo fora da faixa sã: {len(models)} modelos"


def test_all_four_tiers_present(cfg):
    tiers = {m["tier"] for m in cfg["models"]}
    for t in ("tier_1", "tier_2", "tier_3"):
        assert t in tiers, f"Tier '{t}' missing from models"
    # tier_4 appears only in policy cascades, not as a standalone model tier
    assert "tier_4" in cfg["policies"]["task_optimized"]


def test_two_policies_defined(cfg):
    policies = cfg["policies"]
    assert "task_optimized" in policies
    assert "cost_optimized" in policies


def test_each_policy_has_four_tiers(cfg):
    for strategy, tiers in cfg["policies"].items():
        for t in ("tier_1", "tier_2", "tier_3", "tier_4"):
            assert t in tiers, f"Policy '{strategy}' missing tier '{t}'"


def test_each_model_has_required_fields(cfg):
    required = {
        "provider_id",
        "model_id",
        "tier",
        "label",
        "display_name",
        "pricing",
        "context_window",
        "capabilities",
    }
    for m in cfg["models"]:
        missing = required - set(m.keys())
        assert not missing, f"Model {m.get('model_id')} missing fields: {missing}"


def test_pricing_fields(cfg):
    for m in cfg["models"]:
        p = m["pricing"]
        assert "input_per_1m_usd" in p
        assert "output_per_1m_usd" in p


def test_circuit_breaker_config(cfg):
    cb = cfg["circuit_breaker"]
    assert cb["consecutive_failures_threshold"] >= 1
    assert cb["cooldown_seconds"] >= 1


def test_budget_config(cfg):
    budget = cfg["budget"]
    assert budget["enabled"] is True
    assert budget["per_session_usd"] > 0


def test_feature_flags(cfg):
    assert "feature_flags" in cfg
    assert cfg["feature_flags"]["use_legacy_gemini_only"] is False


def test_provider_api_key_env_names(cfg):
    expected = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "gemini": "GOOGLE_API_KEY",
    }
    for pid, env_name in expected.items():
        assert cfg["providers"][pid]["api_key_env"] == env_name
