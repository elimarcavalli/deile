"""Tests: IntentAnalyzer → ModelTier classification — Phase 8."""

from __future__ import annotations

import pytest

from deile.core.intent_analyzer import (IntentAnalysisResult, IntentCategory,
                                        IntentType)
from deile.core.intent_tier_mapper import classify_tier
from deile.core.models.tier import ModelTier


def _result(intent_type: IntentType, category: IntentCategory) -> IntentAnalysisResult:
    return IntentAnalysisResult(
        intent_type=intent_type,
        primary_category=category,
        confidence=0.9,
        complexity_score=0.5,
    )


# ---------------------------------------------------------------------------
# Intent type → tier mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("intent_type,expected_tier", [
    (IntentType.WORKFLOW_REQUIRED, ModelTier.TIER_1),
    (IntentType.COMPLEX_ANALYSIS, ModelTier.TIER_1),
    (IntentType.MULTI_STEP, ModelTier.TIER_2),
    (IntentType.SIMPLE_TASK, ModelTier.TIER_3),
    (IntentType.INFORMATION_QUERY, ModelTier.TIER_3),
    (IntentType.UNKNOWN, ModelTier.TIER_2),
])
def test_intent_type_to_tier(intent_type, expected_tier):
    result = _result(intent_type, IntentCategory.INFORMATION)
    assert classify_tier(result) == expected_tier


# ---------------------------------------------------------------------------
# Category floor — implementation always ≥ TIER_2
# ---------------------------------------------------------------------------

def test_implementation_category_raises_simple_task_to_tier2():
    """SIMPLE_TASK + IMPLEMENTATION category → floor to TIER_2."""
    result = _result(IntentType.SIMPLE_TASK, IntentCategory.IMPLEMENTATION)
    assert classify_tier(result) == ModelTier.TIER_2


def test_implementation_category_does_not_lower_tier1():
    """WORKFLOW_REQUIRED + IMPLEMENTATION → stays TIER_1."""
    result = _result(IntentType.WORKFLOW_REQUIRED, IntentCategory.IMPLEMENTATION)
    assert classify_tier(result) == ModelTier.TIER_1


def test_workflow_category_raises_information_query_to_tier1():
    """INFORMATION_QUERY + WORKFLOW category → floor to TIER_1."""
    result = _result(IntentType.INFORMATION_QUERY, IntentCategory.WORKFLOW)
    assert classify_tier(result) == ModelTier.TIER_1


def test_neutral_category_does_not_change_tier():
    """MODIFICATION category has no floor — tier comes from intent_type only."""
    result = _result(IntentType.SIMPLE_TASK, IntentCategory.MODIFICATION)
    assert classify_tier(result) == ModelTier.TIER_3


# ---------------------------------------------------------------------------
# Sanity: unknown intent_type gets safe default
# ---------------------------------------------------------------------------

def test_unknown_type_defaults_to_tier2():
    result = _result(IntentType.UNKNOWN, IntentCategory.INFORMATION)
    assert classify_tier(result) == ModelTier.TIER_2
