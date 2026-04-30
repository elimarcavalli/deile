"""Model tier definitions for the multi-provider router."""

from enum import Enum


class ModelTier(Enum):
    """Capability/cost tiers used to select the right provider cascade."""

    TIER_1 = "tier_1"  # complex coding / refactor / architecture
    TIER_2 = "tier_2"  # default coding / tool use
    TIER_3 = "tier_3"  # fast / classification / simple Q&A
    TIER_4 = "tier_4"  # bulk / batch / cost-critical
