"""Tests: intent_patterns.yaml tier annotations — Phase 9."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_YAML_PATH = Path(__file__).parents[2] / "deile" / "config" / "intent_patterns.yaml"

_VALID_TIERS = {"tier_1", "tier_2", "tier_3", "tier_4"}


@pytest.fixture(scope="module")
def patterns() -> dict:
    with open(_YAML_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("intent_patterns", {})


def test_yaml_loads(patterns):
    assert len(patterns) > 0


def test_all_patterns_have_tier_field(patterns):
    missing = [name for name, p in patterns.items() if "tier" not in p]
    assert missing == [], f"Patterns missing 'tier': {missing}"


def test_all_tier_values_are_valid(patterns):
    invalid = {
        name: p["tier"]
        for name, p in patterns.items()
        if p.get("tier") not in _VALID_TIERS
    }
    assert invalid == {}, f"Patterns with invalid tier values: {invalid}"


def test_tier_1_patterns_are_complex(patterns):
    """Patterns with tier_1 should have requires_workflow=True or be analysis/workflow."""
    tier_1 = {name: p for name, p in patterns.items() if p.get("tier") == "tier_1"}
    for name, p in tier_1.items():
        assert p.get("requires_workflow") is True or p.get("category") in (
            "analysis",
            "workflow",
            "troubleshooting",
        ), f"Pattern '{name}' is tier_1 but doesn't require workflow or complex category"


def test_tier_3_patterns_do_not_require_workflow(patterns):
    tier_3 = {name: p for name, p in patterns.items() if p.get("tier") == "tier_3"}
    for name, p in tier_3.items():
        assert (
            p.get("requires_workflow") is not True
        ), f"Pattern '{name}' is tier_3 but requires workflow — inconsistent"


def test_known_patterns_have_expected_tiers(patterns):
    expected = {
        "implementation_complex": "tier_1",
        "implementation_simple": "tier_2",
        "analysis_comprehensive": "tier_1",
        "analysis_simple": "tier_3",
        "modification_major": "tier_2",
        "modification_minor": "tier_3",
        "troubleshooting_complex": "tier_1",
        "information_query": "tier_3",
        "workflow_explicit": "tier_1",
        "self_awareness": "tier_3",
        "test_cases_specific": "tier_2",
    }
    for pattern_name, expected_tier in expected.items():
        assert pattern_name in patterns, f"Pattern '{pattern_name}' missing from YAML"
        assert patterns[pattern_name]["tier"] == expected_tier, (
            f"Pattern '{pattern_name}': expected tier={expected_tier!r}, "
            f"got tier={patterns[pattern_name].get('tier')!r}"
        )


def test_at_least_three_tier_1_patterns(patterns):
    tier_1_count = sum(1 for p in patterns.values() if p.get("tier") == "tier_1")
    assert tier_1_count >= 3


def test_at_least_two_tier_3_patterns(patterns):
    tier_3_count = sum(1 for p in patterns.values() if p.get("tier") == "tier_3")
    assert tier_3_count >= 2
