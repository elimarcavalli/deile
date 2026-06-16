"""Maps IntentAnalysisResult → ModelTier for the multi-provider router."""

from __future__ import annotations

from deile.core.intent_analyzer import IntentAnalysisResult, IntentCategory, IntentType
from deile.core.models.tier import ModelTier

_TYPE_MAP: dict[IntentType, ModelTier] = {
    IntentType.WORKFLOW_REQUIRED: ModelTier.TIER_1,
    IntentType.COMPLEX_ANALYSIS: ModelTier.TIER_1,
    IntentType.MULTI_STEP: ModelTier.TIER_2,
    IntentType.SIMPLE_TASK: ModelTier.TIER_3,
    IntentType.INFORMATION_QUERY: ModelTier.TIER_3,
    IntentType.UNKNOWN: ModelTier.TIER_2,
}

# Implementation tasks should never fall below TIER_2 even if intent_type says SIMPLE
_CATEGORY_FLOOR: dict[IntentCategory, ModelTier] = {
    IntentCategory.IMPLEMENTATION: ModelTier.TIER_2,
    IntentCategory.WORKFLOW: ModelTier.TIER_1,
}


def classify_tier(result: IntentAnalysisResult) -> ModelTier:
    """Return the ModelTier most appropriate for *result*.

    Primary classification is driven by intent_type.
    category_floor raises the floor for certain categories (e.g., IMPLEMENTATION
    is always at least TIER_2 regardless of detected complexity).
    """
    tier = _TYPE_MAP.get(result.intent_type, ModelTier.TIER_2)

    # Apply category floor — never go below the minimum for this category
    floor = _CATEGORY_FLOOR.get(result.primary_category)
    if floor is not None:
        # Lower tier value = higher tier (TIER_1 < TIER_2 numerically in enum definition order)
        tier_order = [
            ModelTier.TIER_1,
            ModelTier.TIER_2,
            ModelTier.TIER_3,
            ModelTier.TIER_4,
        ]
        if tier_order.index(tier) > tier_order.index(floor):
            tier = floor

    return tier
