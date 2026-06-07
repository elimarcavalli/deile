"""Tests: OpenRouter deploy.py propagation (OR4) + panel picker listing (OR6).

OR4 — ``OPENROUTER_API_KEY`` is part of ``LLM_KEYS`` (single source of the
``.env``-scan that feeds ``deile-secrets`` + ``bot-secrets``), the wrapper's
``_SENSITIVE_KEYS`` pop-list, and the create-namespace CLI parity.

OR6 — the panel's ``ModelsProvider`` reads ``model_providers.yaml`` and exposes
every model; the new ``openrouter:*`` entries therefore appear in the per-stage
picker with no panel-code change.
"""

from __future__ import annotations

import sys
from pathlib import Path

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))


# ---------------------------------------------------------------------------
# OR4 — deploy.py / wrapper key propagation
# ---------------------------------------------------------------------------


class TestDeployKeyPropagation:
    def test_openrouter_in_llm_keys(self):
        import deploy  # noqa: PLC0415
        assert "OPENROUTER_API_KEY" in deploy.LLM_KEYS

    def test_create_namespace_accepts_openrouter_key(self):
        import deploy  # noqa: PLC0415
        cfg = deploy.CreateNamespaceConfig(openrouter_key="sk-or-x")
        assert cfg.openrouter_key == "sk-or-x"

    def test_create_namespace_flag_mapping(self):
        # The CLI flag must map onto the cfg field (parity with the other keys).
        import inspect

        import deploy  # noqa: PLC0415
        src = inspect.getsource(deploy)
        assert '"--openrouter-key":' in src
        assert '"openrouter_key"' in src

    def test_wrapper_strips_openrouter_after_bootstrap(self):
        import wrapper  # noqa: PLC0415

        # After bootstrap, the key must be popped from os.environ so it never
        # lands in /proc/self/environ (parity with the other LLM keys).
        assert "OPENROUTER_API_KEY" in wrapper._SENSITIVE_KEYS


# ---------------------------------------------------------------------------
# OR6 — panel per-stage model picker
# ---------------------------------------------------------------------------


class TestPanelModelsListing:
    def test_models_provider_lists_openrouter_slugs(self):
        from _panel_data import ModelsProvider  # noqa: PLC0415

        models = ModelsProvider().get(force=True)
        slugs = {f"{m.provider_id}:{m.model_id}" for m in models}
        assert "openrouter:deepseek/deepseek-chat" in slugs
        assert "openrouter:anthropic/claude-sonnet-4.6" in slugs

    def test_openrouter_models_carry_pricing_and_tier(self):
        from _panel_data import ModelsProvider  # noqa: PLC0415

        models = ModelsProvider().get(force=True)
        ors = [m for m in models if m.provider_id == "openrouter"]
        assert ors, "expected openrouter models in the picker"
        for m in ors:
            assert m.tier and m.tier != "—"
            assert m.output_cost_per_1m > 0  # picker shows a real fallback price
