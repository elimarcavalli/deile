"""Wire-format tests for ``DispatchPayload.cli_model`` (frota multi-CLI, B2).

``cli_model`` é o campo do **model-id NATIVO do CLI** (string LIVRE), separado de
``preferred_model`` (``provider:model`` do deile-worker). Adicionar campo novo —
em vez de relaxar o validator ``provider:model`` — preserva a fronteira de wire do
deile-worker: este aceita ids livres como ``openrouter/deepseek/deepseek-chat``,
``qwen3-coder-plus``, ``gpt-5.5-codex`` que o regex ``provider:model`` rejeitaria.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from deile.infrastructure.deile_worker_client import (
    DispatchPayload,
    build_dispatch_payload,
)


class TestCliModelValidator:
    @pytest.mark.parametrize(
        "free",
        [
            "openrouter/deepseek/deepseek-chat",
            "qwen3-coder-plus",
            "gpt-5.5-codex",
            "anthropic/claude-3.7-sonnet",
            "Mixed-Case/Model.v2",  # CLI ids podem ter maiúsculas — slug regex não.
        ],
    )
    def test_accepts_free_string(self, free):
        p = DispatchPayload(brief="x", channel_id="c", cli_model=free)
        assert p.cli_model == free

    def test_does_not_require_provider_colon(self):
        # Justamente o que o validator de preferred_model REJEITARIA.
        p = DispatchPayload(brief="x", channel_id="c", cli_model="qwen3-coder-plus")
        assert p.cli_model == "qwen3-coder-plus"
        # E o mesmo valor em preferred_model é rejeitado — provando que o
        # campo separado não enfraqueceu a fronteira do deile-worker.
        with pytest.raises(ValidationError):
            DispatchPayload(
                brief="x", channel_id="c", preferred_model="qwen3-coder-plus"
            )

    def test_none_and_default_stay_none(self):
        assert DispatchPayload(brief="x", channel_id="c").cli_model is None
        assert (
            DispatchPayload(brief="x", channel_id="c", cli_model=None).cli_model is None
        )

    def test_empty_and_whitespace_collapse_to_none(self):
        assert (
            DispatchPayload(brief="x", channel_id="c", cli_model="").cli_model is None
        )
        assert (
            DispatchPayload(brief="x", channel_id="c", cli_model="   ").cli_model
            is None
        )

    def test_strips_surrounding_whitespace(self):
        p = DispatchPayload(brief="x", channel_id="c", cli_model="  qwen3-coder-plus  ")
        assert p.cli_model == "qwen3-coder-plus"

    def test_enforces_max_length(self):
        with pytest.raises(ValidationError):
            DispatchPayload(brief="x", channel_id="c", cli_model="m" * 300)

    def test_cli_model_and_preferred_model_independent(self):
        # Os dois campos coexistem sem interferência (deile-worker ignora
        # cli_model; CLI worker ignora preferred_model).
        p = DispatchPayload(
            brief="x",
            channel_id="c",
            preferred_model="deepseek:deepseek-v4-pro",
            cli_model="openrouter/deepseek/deepseek-chat",
        )
        assert p.preferred_model == "deepseek:deepseek-v4-pro"
        assert p.cli_model == "openrouter/deepseek/deepseek-chat"


class TestBuildDispatchPayloadCliModel:
    def test_adds_cli_model_when_truthy(self):
        p = build_dispatch_payload(
            brief="x",
            channel_id="c",
            cli_model="openrouter/deepseek/deepseek-chat",
        )
        assert p["cli_model"] == "openrouter/deepseek/deepseek-chat"

    def test_omits_cli_model_when_none(self):
        p = build_dispatch_payload(brief="x", channel_id="c", cli_model=None)
        assert "cli_model" not in p

    def test_omits_cli_model_when_default(self):
        p = build_dispatch_payload(brief="x", channel_id="c")
        assert "cli_model" not in p

    def test_omits_when_empty_string(self):
        p = build_dispatch_payload(brief="x", channel_id="c", cli_model="")
        assert "cli_model" not in p

    def test_validates_through_payload(self):
        # build_dispatch_payload monta o dict; DispatchPayload.model_validate o
        # aceita (sem regex) — fluxo ponta-a-ponta do campo livre.
        raw = build_dispatch_payload(
            brief="x",
            channel_id="c",
            cli_model="qwen3-coder-plus",
        )
        validated = DispatchPayload.model_validate(raw)
        assert validated.cli_model == "qwen3-coder-plus"
