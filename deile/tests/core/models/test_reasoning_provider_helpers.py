"""Base-provider reasoning helpers: pop + merge into extra_body.

Verifies that any provider built on ``ModelProvider`` translates a normalized
``reasoning_effort`` kwarg into the native ``extra_body`` payload, pops it so it
never leaks to the SDK, and stays fail-open on unsupported levels.
"""

from __future__ import annotations

from typing import List

import pytest

from deile.core.models.base import ModelProvider, ModelSize, ModelType


class _StubProvider(ModelProvider):
    """Minimal concrete provider for exercising the base reasoning helpers."""

    def __init__(self, provider_id: str, model_name: str):
        super().__init__(model_name=model_name)
        self._pid = provider_id

    @property
    def provider_name(self) -> str:
        return self._pid

    @property
    def provider_id(self) -> str:
        return self._pid

    @property
    def supported_types(self) -> List[ModelType]:
        return [ModelType.CHAT]

    @property
    def model_size(self) -> ModelSize:
        return ModelSize.MEDIUM

    async def generate(self, messages, system_instruction=None, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def generate_stream(self, messages, system_instruction=None, tools=None, **kwargs):  # pragma: no cover
        yield None


@pytest.mark.unit
def test_pop_removes_kwarg_and_returns_native_body():
    p = _StubProvider("openai", "gpt-5.5")
    kwargs = {"reasoning_effort": "high", "max_tokens": 100}
    extra = p._pop_reasoning_extra_body(kwargs)
    assert extra == {"reasoning_effort": "high"}
    assert "reasoning_effort" not in kwargs  # popped → never reaches the SDK
    assert kwargs["max_tokens"] == 100


@pytest.mark.unit
def test_anthropic_maps_to_output_config():
    p = _StubProvider("anthropic", "claude-opus-4-8")
    assert p._pop_reasoning_extra_body({"reasoning_effort": "max"}) == {
        "output_config": {"effort": "max"}
    }


@pytest.mark.unit
def test_no_effort_returns_empty():
    p = _StubProvider("anthropic", "claude-opus-4-8")
    assert p._pop_reasoning_extra_body({}) == {}
    assert p._pop_reasoning_extra_body({"reasoning_effort": None}) == {}


@pytest.mark.unit
def test_apply_merges_into_existing_extra_body():
    create_kwargs = {"extra_body": {"foo": 1}, "model": "x"}
    ModelProvider._apply_reasoning_extra_body(create_kwargs, {"reasoning_effort": "low"})
    assert create_kwargs["extra_body"] == {"foo": 1, "reasoning_effort": "low"}


@pytest.mark.unit
def test_apply_noop_on_empty():
    create_kwargs = {"model": "x"}
    ModelProvider._apply_reasoning_extra_body(create_kwargs, {})
    assert "extra_body" not in create_kwargs
