"""Multi-provider bootstrap — conditional provider registration from model_providers.yaml."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, List, Optional

import yaml

from deile.core.models.catalog import ModelCatalog
from deile.core.models.provider_config import ProviderConfig

logger = logging.getLogger(__name__)

_DEFAULT_YAML = Path(__file__).parents[2] / "config" / "model_providers.yaml"

_PROVIDER_CLASSES = {
    "anthropic": "deile.core.models.anthropic_provider.AnthropicProvider",
    "openai": "deile.core.models.openai_provider.OpenAIProvider",
    "deepseek": "deile.core.models.deepseek_provider.DeepSeekProvider",
    "gemini": "deile.core.models.gemini_provider.GeminiProvider",
}


def _import_provider_class(dotted: str):
    module_path, class_name = dotted.rsplit(".", 1)
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


def _bootstrap_legacy_gemini_only(router) -> List[str]:
    """Legacy single-provider path used when use_legacy_gemini_only is set.

    Registers GeminiProvider in the router only if GOOGLE_API_KEY is set.
    """
    if not os.getenv("GOOGLE_API_KEY"):
        return []
    if router is None:
        logger.debug(
            "bootstrap: skipping legacy gemini registration, router is None"
        )
        return []
    from deile.core.models.gemini_provider import GeminiProvider
    router.register_provider(GeminiProvider(), priority=1)
    return ["gemini"]


def bootstrap_providers(
    yaml_path: Optional[Path] = None,
    router=None,
) -> List[str]:
    """Conditionally instantiate and register providers from YAML.

    Returns a list of successfully registered provider_ids.
    Logs warnings for providers with missing API keys.

    Honors the ``feature_flags.use_legacy_gemini_only`` toggle: when set,
    only the Gemini provider is registered (and only if ``GOOGLE_API_KEY``
    is present), bypassing the multi-provider catalog path entirely.
    """
    path = yaml_path or _DEFAULT_YAML

    with open(path) as f:
        data = yaml.safe_load(f)

    if bool(data.get("feature_flags", {}).get("use_legacy_gemini_only", False)):
        return _bootstrap_legacy_gemini_only(router)

    catalog = ModelCatalog.from_yaml(path)
    providers_cfg = data.get("providers", {})

    registered: List[str] = []

    for provider_id, cfg in providers_cfg.items():
        if not cfg.get("enabled", True):
            logger.debug("bootstrap: provider %s disabled in YAML, skipping", provider_id)
            continue

        api_key_env = cfg.get("api_key_env", "")
        api_key = os.getenv(api_key_env) if api_key_env else None

        if not api_key:
            logger.warning(
                "⚠ Provider %s disabled: %s not set",
                provider_id,
                api_key_env or "(no api_key_env configured)",
            )
            continue

        cls_path = _PROVIDER_CLASSES.get(provider_id)
        if cls_path is None:
            logger.warning("bootstrap: no provider class registered for %s, skipping", provider_id)
            continue

        handles = catalog.list_by_provider(provider_id) if hasattr(catalog, "list_by_provider") else [
            h for h in catalog.list_all() if h.provider_id == provider_id
        ]

        if not handles:
            logger.warning("bootstrap: no model handles for provider %s in catalog", provider_id)
            continue

        provider_config = ProviderConfig(
            provider_id=provider_id,
            api_key_env=api_key_env,
            base_url=cfg.get("base_url"),
            sdk_kwargs=cfg.get("sdk_kwargs", {}),
            enabled=True,
        )

        try:
            cls = _import_provider_class(cls_path)
        except Exception as exc:
            logger.error("bootstrap: failed to import %s: %s", provider_id, exc)
            continue

        # Register ONE provider instance per (provider_id, model_id) handle in BOTH
        # the legacy ModelRouter and the TierRouter. The TierRouter is now keyed by
        # full provider:model_id, so each tier cascade can resolve to the right model
        # (haiku for tier_3, opus for tier_1, etc.) within the same provider.
        instances: List[Any] = []
        for idx, handle in enumerate(handles):
            try:
                inst = cls(handle, provider_config)
            except Exception as exc:
                logger.error(
                    "bootstrap: failed to instantiate %s:%s: %s", provider_id, handle.model_id, exc
                )
                continue
            instances.append(inst)
            if router is not None:
                try:
                    router.register_provider(inst, priority=1 if idx == 0 else 0)
                except Exception as exc:
                    logger.warning(
                        "bootstrap: could not register %s:%s in legacy router: %s",
                        provider_id, handle.model_id, exc,
                    )

        # Register every instance in TierRouter under its full provider:model key
        if router is not None and instances:
            try:
                from deile.core.models.tier_router import get_tier_router
                tier_router = get_tier_router(yaml_path=path)
                for inst in instances:
                    tier_router.register_provider(inst)
            except Exception as exc:
                logger.debug(
                    "bootstrap: TierRouter registration skipped for %s: %s", provider_id, exc
                )

        if not instances:
            continue

        registered.append(provider_id)
        logger.info(
            "bootstrap: registered provider %s (%d model instance(s))",
            provider_id, len(instances),
        )

    return registered
