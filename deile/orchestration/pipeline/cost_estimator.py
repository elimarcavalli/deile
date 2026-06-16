"""Stage cost estimator — issue #392.

Estimates per-run USD cost for a given stage + model, using historical
usage data from ``UsageRepository`` and per-token pricing from
``model_providers.yaml``. Falls back to conservative heuristics when no
history is available for the stage.

Consumed by ``StageBudgetGuard`` in ``deile/storage/usage_repository.py``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Heuristic fallbacks (tokens) when UsageRepository has no history for stage
# ---------------------------------------------------------------------------

#: Conservative token-count estimates for stages with no recorded history.
#: Tuned to be slightly above the p50 observed in production so that the cap
#: check does not fire on normal runs — operators set caps above these.
_FALLBACK_TOKENS: dict[str, tuple[int, int]] = {
    # (prompt_tokens, completion_tokens)
    "classify": (2_000, 500),
    "refine": (5_000, 2_000),
    "implement": (30_000, 15_000),
    "pr_review": (20_000, 5_000),
    "follow_ups": (5_000, 2_000),
}

# Default fallback for unknown stages (should not happen in practice).
_DEFAULT_FALLBACK = (5_000, 2_000)

# Number of recent runs to average when estimating tokens from history.
_HISTORY_WINDOW = 10


class PricingProvider:
    """Resolves per-token pricing for a model slug.

    Reads ``deile/config/model_providers.yaml`` and returns
    ``(input_usd_per_token, output_usd_per_token)``.  Returns ``(0, 0)``
    when the slug is not found — missing pricing never blocks dispatch.

    Results are cached in-process (the YAML does not change at runtime).
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[Decimal, Decimal]] = {}
        self._loaded: bool = False

    def _load(self) -> dict:
        """Parse ``model_providers.yaml`` once and return the raw dict."""
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            return {}
        try:
            # The YAML lives in the ``deile.config`` package directory.
            import deile.config as _cfg_pkg  # noqa: PLC0415

            pkg_path = getattr(_cfg_pkg, "__path__", [None])[0]
            if pkg_path is None:
                return {}
            from pathlib import Path as _Path  # noqa: PLC0415

            yaml_file = _Path(pkg_path) / "model_providers.yaml"
            if not yaml_file.is_file():
                return {}
            with open(yaml_file) as fh:
                return yaml.safe_load(fh) or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "PricingProvider: could not load model_providers.yaml: %s", exc
            )
            return {}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        data = self._load()
        models = data.get("models", {})
        if not isinstance(models, dict):
            return
        for slug, attrs in models.items():
            if not isinstance(attrs, dict):
                continue
            pricing = attrs.get("pricing", {})
            if not isinstance(pricing, dict):
                continue
            try:
                inp = Decimal(str(pricing.get("input_per_token", 0) or 0))
                out = Decimal(str(pricing.get("output_per_token", 0) or 0))
                self._cache[str(slug)] = (inp, out)
            except Exception:  # noqa: BLE001
                pass

    def get_pricing(self, model_slug: str) -> tuple[Decimal, Decimal]:
        """Return ``(input_usd_per_token, output_usd_per_token)`` for *model_slug*.

        Returns ``(Decimal(0), Decimal(0))`` when the slug is unknown — missing
        pricing never blocks dispatch (cost guard stays silent).
        """
        self._ensure_loaded()
        return self._cache.get(model_slug, (Decimal(0), Decimal(0)))


# Module-level singleton — reused across calls within the same process.
_pricing_provider: Optional[PricingProvider] = None


def get_pricing_provider() -> PricingProvider:
    """Return the singleton PricingProvider."""
    global _pricing_provider
    if _pricing_provider is None:
        _pricing_provider = PricingProvider()
    return _pricing_provider


def reset_pricing_provider() -> None:
    """Reset the singleton (test helper)."""
    global _pricing_provider
    _pricing_provider = None


class StageCostEstimator:
    """Estimates per-run USD cost for a pipeline stage.

    Algorithm:
    1. Query ``UsageRepository`` for the last ``_HISTORY_WINDOW`` records
       of ``(model_id, stage)`` (stage encoded in session_id prefix).
    2. Average ``prompt_tokens`` / ``completion_tokens`` from those records.
    3. If fewer than 2 records exist, fall back to ``_FALLBACK_TOKENS``.
    4. Multiply by the per-token price from ``PricingProvider``.

    The estimate is intentionally conservative (uses average, not p50) so
    that cost-cap checks do not block normal runs. Operators should set caps
    above the typical run cost.

    Notes:
    - ``payload_size_tokens`` is a hint from the caller (size of the brief/
      prompt being dispatched). When > 0 it replaces the historical
      ``prompt_tokens`` average — useful for stages like ``implement`` where
      the prompt size varies widely per issue.
    - When pricing data is missing (``Decimal(0)``), the estimate is ``0``
      and the guard never fires — absence of pricing = no enforcement.
    """

    def __init__(
        self,
        usage_repo: "UsageRepository",  # noqa: F821
        pricing_provider: Optional[PricingProvider] = None,
    ) -> None:
        self._repo = usage_repo
        self._pricing = pricing_provider or get_pricing_provider()

    def estimate_run_cost(
        self,
        stage: str,
        model_slug: str,
        payload_size_tokens: int = 0,
    ) -> Decimal:
        """Return USD estimate for one run of *stage* with *model_slug*.

        Args:
            stage: canonical stage name (classify/refine/implement/
                pr_review/follow_ups).
            model_slug: provider:model string (e.g. ``anthropic:claude-opus-4-8``).
            payload_size_tokens: estimated token count of the dispatch payload;
                overrides the historical prompt-token average when > 0.

        Returns:
            Decimal USD estimate. Returns ``Decimal(0)`` when pricing is
            unknown — callers treat 0 as "cannot estimate; skip enforcement".
        """
        # Resolve pricing first — if unknown, return 0 immediately.
        input_price, output_price = self._pricing.get_pricing(model_slug)
        if input_price == 0 and output_price == 0:
            logger.debug(
                "StageCostEstimator: no pricing for model %r — estimate=0",
                model_slug,
            )
            return Decimal(0)

        prompt_avg, completion_avg = self._average_tokens(stage, model_slug)

        # Override prompt tokens from payload hint when available.
        if payload_size_tokens > 0:
            prompt_tokens = Decimal(payload_size_tokens)
        else:
            prompt_tokens = Decimal(prompt_avg)

        completion_tokens = Decimal(completion_avg)

        cost = prompt_tokens * input_price + completion_tokens * output_price
        logger.debug(
            "StageCostEstimator: stage=%s model=%s "
            "prompt=%s completion=%s → est=$%s",
            stage,
            model_slug,
            prompt_tokens,
            completion_tokens,
            cost,
        )
        return cost

    def _average_tokens(self, stage: str, model_slug: str) -> tuple[int, int]:
        """Return ``(avg_prompt, avg_completion)`` from recent history.

        Falls back to ``_FALLBACK_TOKENS`` when fewer than 2 records exist.
        Stage is matched by ``session_id`` prefix ``pipeline-<stage>-``
        (canonical format used by WorkerImplementer when building channel_id).
        """
        try:
            records = self._repo.records_for_stage_model(
                stage=stage,
                model_id=model_slug,
                limit=_HISTORY_WINDOW,
            )
        except AttributeError:
            # UsageRepository may not have records_for_stage_model yet
            # (e.g. old DB without the stage column); fall back gracefully.
            records = []
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "StageCostEstimator: error fetching history for %s/%s: %s",
                stage,
                model_slug,
                exc,
            )
            records = []

        if len(records) < 2:
            fallback = _FALLBACK_TOKENS.get(stage, _DEFAULT_FALLBACK)
            logger.debug(
                "StageCostEstimator: insufficient history for %s/%s "
                "(%d records) — using fallback %s",
                stage,
                model_slug,
                len(records),
                fallback,
            )
            return fallback

        total_prompt = sum(getattr(r, "prompt_tokens", 0) for r in records)
        total_completion = sum(getattr(r, "completion_tokens", 0) for r in records)
        n = len(records)
        return int(total_prompt / n), int(total_completion / n)
