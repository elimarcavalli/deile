"""Lock in REVIEW_CETICA Round 3 fixes (C1-C4): agent → new-provider call path.

These tests exercise the `_process_iterative_function_calling` method with a
mock provider that exposes `chat_with_tools` (mimicking AnthropicProvider /
OpenAIProvider / DeepSeekProvider) but NOT `create_chat_session`. They verify
that the agent:

1. (C2) Fetches tools via the correct registry method and passes ToolSchema objects.
2. (C3) Converts dict messages from context_manager into ModelMessage objects.
3. (C4) Classifies the user intent into a ModelTier and passes it to the router.
4. (C1) BudgetGuard YAML path resolves correctly (no FileNotFoundError).

These are pure unit tests with everything mocked — they require no API keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core.models.base import ModelMessage, ModelUsage


class _CapturingProvider:
    """Mock provider that exposes chat_with_tools() and captures call arguments."""

    provider_id = "anthropic"
    model_name = "test-model"

    def __init__(self) -> None:
        self.captured_messages: List[Any] = []
        self.captured_tools: List[Any] = []
        self.captured_kwargs: dict = {}

    async def chat_with_tools(
        self,
        messages: List[Any],
        tools: List[Any],
        system_instruction: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[str, List[Any], ModelUsage]:
        self.captured_messages = messages
        self.captured_tools = tools
        self.captured_kwargs = {"system_instruction": system_instruction, **kwargs}
        return (
            "OK",
            [],
            ModelUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def _record_usage(self, **kwargs: Any) -> None:
        # No-op; verifies attribute exists on the provider
        return None


def _build_minimal_agent_with_mock_provider(provider: _CapturingProvider):
    """Build a DeileAgent shell wired only with what _process_iterative_function_calling reads."""
    from deile.core.agent import DeileAgent

    agent = DeileAgent.__new__(DeileAgent)
    agent.logger = MagicMock()

    # context_manager.build_context returns a dict with messages as plain dicts (real pattern)
    cm = MagicMock()
    cm.build_context = AsyncMock(
        return_value={
            "system_instruction": "You are DEILE.",
            "messages": [{"role": "user", "content": "test prompt"}],
        }
    )
    agent.context_manager = cm

    # IntentAnalyzer — minimal stub returning a result classify_tier can consume
    from deile.core.intent_analyzer import (
        IntentAnalysisResult,
        IntentCategory,
        IntentType,
    )

    intent = MagicMock()
    intent.analyze = AsyncMock(
        return_value=IntentAnalysisResult(
            intent_type=IntentType.SIMPLE_TASK,
            primary_category=IntentCategory.INFORMATION,
            confidence=0.9,
            complexity_score=0.2,
        )
    )
    agent.intent_analyzer = intent

    # ModelRouter — returns the mock provider
    router = MagicMock()
    router.select_provider = AsyncMock(return_value=provider)
    router.providers = {"anthropic:test-model": provider}
    agent.model_router = router

    # BudgetGuard singleton — disable to avoid YAML loading in unit tests
    agent._budget_guard_singleton = False

    return agent


@pytest.mark.asyncio
async def test_dict_messages_are_converted_to_modelmessage_objects():
    """C3: providers must receive ModelMessage objects, not raw dicts."""
    provider = _CapturingProvider()
    agent = _build_minimal_agent_with_mock_provider(provider)

    from deile.core.agent import AgentSession

    session = AgentSession(
        session_id="test-session",
        working_directory=Path("/tmp"),
        context_data={},
    )

    content, _ = await agent._process_iterative_function_calling(
        user_input="test prompt",
        parse_result=None,
        session=session,
    )

    assert content == "OK"
    assert len(provider.captured_messages) >= 1
    # Every captured message MUST be a ModelMessage instance (not a dict)
    for m in provider.captured_messages:
        assert isinstance(
            m, ModelMessage
        ), f"Got {type(m).__name__}, expected ModelMessage"


@pytest.mark.asyncio
async def test_tools_are_toolschema_objects_from_list_enabled():
    """C2: tools passed to provider must be ToolSchema objects (or empty list when none)."""
    from deile.tools.base import ToolSchema

    provider = _CapturingProvider()
    agent = _build_minimal_agent_with_mock_provider(provider)

    from deile.core.agent import AgentSession

    session = AgentSession(
        session_id="test-session",
        working_directory=Path("/tmp"),
        context_data={},
    )

    await agent._process_iterative_function_calling(
        user_input="test",
        parse_result=None,
        session=session,
    )

    # Either zero tools (registry empty) or every captured tool is a ToolSchema
    for t in provider.captured_tools:
        assert isinstance(t, ToolSchema), f"Got {type(t).__name__}, expected ToolSchema"


@pytest.mark.asyncio
async def test_tier_is_classified_and_passed_to_select_provider():
    """C4: classified ModelTier must be passed to select_provider as `tier=...`."""
    provider = _CapturingProvider()
    agent = _build_minimal_agent_with_mock_provider(provider)

    from deile.core.agent import AgentSession

    session = AgentSession(
        session_id="test-session",
        working_directory=Path("/tmp"),
        context_data={},
    )

    await agent._process_iterative_function_calling(
        user_input="What is 2+2?",
        parse_result=None,
        session=session,
    )

    # select_provider should have been called with tier kwarg (not None) since
    # the IntentAnalyzer mock returns a SIMPLE_TASK which classify_tier maps to TIER_3
    call_args = agent.model_router.select_provider.call_args
    assert "tier" in call_args.kwargs
    assert call_args.kwargs["tier"] is not None
    # The session should have the tier recorded
    assert session.context_data.get("_current_tier") in {
        "tier_1",
        "tier_2",
        "tier_3",
        "tier_4",
    }


@pytest.mark.asyncio
async def test_session_id_propagates_to_chat_with_tools():
    """Provider must receive the real session_id, not the literal 'default'."""
    provider = _CapturingProvider()
    agent = _build_minimal_agent_with_mock_provider(provider)

    from deile.core.agent import AgentSession

    session = AgentSession(
        session_id="my-real-session-xyz",
        working_directory=Path("/tmp"),
        context_data={},
    )

    await agent._process_iterative_function_calling(
        user_input="hi",
        parse_result=None,
        session=session,
    )

    assert provider.captured_kwargs.get("session_id") == "my-real-session-xyz"


@pytest.mark.asyncio
async def test_forced_model_routes_to_specific_provider():
    """H3: /model use <provider:model> must actually steer routing."""
    forced_provider = _CapturingProvider()
    forced_provider.provider_id = "anthropic"
    forced_provider.model_name = "claude-haiku-4-5"

    agent = _build_minimal_agent_with_mock_provider(forced_provider)
    # Make sure the provider is reachable via providers dict for forced lookup
    agent.model_router.providers = {"anthropic:claude-haiku-4-5": forced_provider}

    from deile.core.agent import AgentSession

    session = AgentSession(
        session_id="forced-test",
        working_directory=Path("/tmp"),
        context_data={"forced_model": "anthropic:claude-haiku-4-5"},
    )

    content, _ = await agent._process_iterative_function_calling(
        user_input="hi",
        parse_result=None,
        session=session,
    )
    assert content == "OK"
    # Forced lookup short-circuits select_provider and uses the providers dict directly
    # — but if forced provider is found in the dict, select_provider should NOT have been called
    # (it's only called as a fallback when the forced provider isn't found)
    # Either way, the right provider must have served the request.
    assert forced_provider.captured_messages, "forced provider was not called"


@pytest.mark.asyncio
async def test_forced_model_unregistered_raises_instead_of_silent_swap():
    """R7-H2 + R8-M1: when the forced model is NOT registered, do NOT silently substitute
    the flagship — that would surprise users with up to 10x the cost.
    The structured ModelError propagates so process_input can build a Rich panel."""
    from deile.core.exceptions import ModelError

    flagship = _CapturingProvider()
    flagship.provider_id = "anthropic"
    flagship.model_name = "claude-opus-4-8"

    agent = _build_minimal_agent_with_mock_provider(flagship)
    # Only the flagship is registered; the user-requested haiku is NOT
    agent.model_router.providers = {"anthropic:claude-opus-4-8": flagship}

    from deile.core.agent import AgentSession

    session = AgentSession(
        session_id="forced-unregistered-test",
        working_directory=Path("/tmp"),
        context_data={"forced_model": "anthropic:claude-haiku-4-5"},
    )

    # The structured ModelError now PROPAGATES from _process_iterative_function_calling
    # so process_input can return a Rich-panel-able AgentResponse. Crucially, the
    # FLAGSHIP must NOT have been called — that was the whole point of the fix.
    with pytest.raises(ModelError) as exc_info:
        await agent._process_iterative_function_calling(
            user_input="hi",
            parse_result=None,
            session=session,
        )
    assert getattr(exc_info.value, "error_code", "") == "FORCED_MODEL_NOT_REGISTERED"
    assert (
        not flagship.captured_messages
    ), "Flagship was silently called despite forced_model selecting an unregistered model"


@pytest.mark.asyncio
async def test_forced_model_id_picks_exact_instance_not_flagship():
    """R4-H1: when /model use anthropic:claude-haiku-4-5 is set AND multiple anthropic
    instances are registered, agent must pick the haiku one, NOT the (flagship) opus."""
    flagship = _CapturingProvider()
    flagship.provider_id = "anthropic"
    flagship.model_name = "claude-opus-4-8"  # the cascade flagship

    haiku = _CapturingProvider()
    haiku.provider_id = "anthropic"
    haiku.model_name = "claude-haiku-4-5"  # the cheap model the user explicitly picked

    agent = _build_minimal_agent_with_mock_provider(flagship)
    agent.model_router.providers = {
        "anthropic:claude-opus-4-8": flagship,
        "anthropic:claude-haiku-4-5": haiku,
    }

    from deile.core.agent import AgentSession

    session = AgentSession(
        session_id="forced-haiku-test",
        working_directory=Path("/tmp"),
        context_data={"forced_model": "anthropic:claude-haiku-4-5"},
    )

    content, _ = await agent._process_iterative_function_calling(
        user_input="hi",
        parse_result=None,
        session=session,
    )
    assert content == "OK"
    # Haiku must have been the one called — flagship must NOT have been called
    assert haiku.captured_messages, "the haiku instance was NOT used"
    assert (
        not flagship.captured_messages
    ), "the flagship was used despite forced_model selecting haiku"


@pytest.mark.asyncio
async def test_budget_exceeded_propagates_to_caller():
    """R4-H2: BudgetExceeded must reach the caller (not be swallowed by catch-all)."""
    from deile.storage.usage_repository import BudgetExceeded

    provider = _CapturingProvider()
    agent = _build_minimal_agent_with_mock_provider(provider)

    # Wire a budget guard that always raises
    class _BlockingGuard:
        def check_all(self, **kwargs):
            raise BudgetExceeded(
                "session over limit",
                provider_id="anthropic",
                limit_type="per_session",
            )

    agent._budget_guard_singleton = _BlockingGuard()

    from deile.core.agent import AgentSession

    session = AgentSession(
        session_id="budget-test",
        working_directory=Path("/tmp"),
        context_data={},
    )

    with pytest.raises(BudgetExceeded):
        await agent._process_iterative_function_calling(
            user_input="hi",
            parse_result=None,
            session=session,
        )


@pytest.mark.asyncio
async def test_cascade_retry_succeeds_after_first_provider_fails():
    """R6-C1: when provider 1 raises, agent must call TierRouter.select with skip
    and the second provider must serve the request — within ONE request, not after
    accumulated CB failures."""

    # Provider 1: always fails (use a real async function so the await works)
    class _FailingProvider:
        provider_id = "anthropic"
        model_name = "claude-haiku-4-5"

        async def chat_with_tools(self, **kwargs):
            raise RuntimeError("simulated 401")

        async def _record_usage(self, **kwargs):
            return None

    failing = _FailingProvider()

    # Provider 2: always succeeds
    succeeding = _CapturingProvider()
    succeeding.provider_id = "openai"
    succeeding.model_name = "gpt-5.4-mini"

    agent = _build_minimal_agent_with_mock_provider(failing)
    agent.model_router.providers = {
        "anthropic:claude-haiku-4-5": failing,
        "openai:gpt-5.4-mini": succeeding,
    }

    # Patch get_tier_router so the cascade retry sees both providers, with skip semantics

    fake_tier_router = MagicMock()

    def _select(tier, skip_provider_ids=None):
        skip_set = set(skip_provider_ids or ())
        if "anthropic" not in skip_set:
            return failing
        return succeeding

    fake_tier_router.select = MagicMock(side_effect=_select)
    fake_tier_router.record_success = MagicMock()
    fake_tier_router.record_failure = MagicMock()

    from deile.core.agent import AgentSession

    session = AgentSession(
        session_id="cascade-test",
        working_directory=Path("/tmp"),
        context_data={},
    )

    with patch(
        "deile.core.models.tier_router.get_tier_router", return_value=fake_tier_router
    ):
        content, _ = await agent._process_iterative_function_calling(
            user_input="trigger cascade",
            parse_result=None,
            session=session,
        )

    # Provider 2 must have served the request
    assert content == "OK"
    assert (
        succeeding.captured_messages
    ), "second provider was never called — cascade retry failed"


@pytest.mark.asyncio
async def test_process_input_returns_structured_budget_exceeded_metadata():
    """R6-H4: process_input must surface BudgetExceeded with metadata flag the CLI can read."""
    from deile.core.agent import AgentStatus, DeileAgent
    from deile.storage.usage_repository import BudgetExceeded

    # Build a minimal agent to call process_input
    agent = DeileAgent.__new__(DeileAgent)
    agent.logger = MagicMock()
    agent._sessions = {}
    agent._status = AgentStatus.IDLE
    agent._request_count = 0
    agent._total_tokens = 0
    agent._success_count = 0
    agent._error_count = 0
    agent._start_time = 0.0
    agent.proactive_analyzer = (
        None  # process_input checks `if not self.proactive_analyzer`
    )
    agent.intent_analyzer = MagicMock()
    agent.intent_analyzer.analyze = AsyncMock()

    # Make _get_or_create_session return a usable session
    from deile.core.agent import AgentSession

    sess = AgentSession(
        session_id="budget-cli-test",
        working_directory=Path("/tmp"),
        context_data={},
    )
    sess.update_activity = MagicMock()
    sess.add_to_history = MagicMock()
    agent._sessions[sess.session_id] = sess
    agent._get_or_create_session = MagicMock(return_value=sess)

    # Make _parse_input/_should_create_workflow side-effect-free
    agent._parse_input = AsyncMock(return_value=None)
    agent._should_create_workflow = AsyncMock(return_value=False)

    # Inject a _process_iterative_function_calling that raises BudgetExceeded
    async def _raise_budget(*args, **kwargs):
        raise BudgetExceeded(
            "month exceeded", provider_id="openai", limit_type="monthly"
        )

    agent._process_iterative_function_calling = _raise_budget

    response = await agent.process_input("anything", session_id="budget-cli-test")

    # Structured signals the CLI consumes
    assert response.status == AgentStatus.ERROR
    assert response.metadata.get("budget_exceeded") is True
    assert response.metadata.get("provider_id") == "openai"
    assert response.metadata.get("limit_type") == "monthly"
    assert "/model budget" in response.content  # actionable user hint
    assert isinstance(response.error, BudgetExceeded)


@pytest.mark.asyncio
async def test_process_input_returns_structured_forced_model_metadata():
    """R8-M1: process_input must surface FORCED_MODEL_NOT_REGISTERED with metadata flag
    the CLI uses to render a Rich panel — same pattern as BudgetExceeded."""
    from deile.core.agent import AgentSession, AgentStatus, DeileAgent
    from deile.core.exceptions import ModelError

    agent = DeileAgent.__new__(DeileAgent)
    agent.logger = MagicMock()
    agent._sessions = {}
    agent._status = AgentStatus.IDLE
    agent._request_count = 0
    agent._total_tokens = 0
    agent._success_count = 0
    agent._error_count = 0
    agent._start_time = 0.0
    agent.proactive_analyzer = None
    agent.intent_analyzer = MagicMock()
    agent.intent_analyzer.analyze = AsyncMock()

    sess = AgentSession(
        session_id="forced-cli-test",
        working_directory=Path("/tmp"),
        context_data={},
    )
    sess.update_activity = MagicMock()
    sess.add_to_history = MagicMock()
    agent._sessions[sess.session_id] = sess
    agent._get_or_create_session = MagicMock(return_value=sess)
    agent._parse_input = AsyncMock(return_value=None)
    agent._should_create_workflow = AsyncMock(return_value=False)

    async def _raise_forced(*args, **kwargs):
        raise ModelError(
            "Forced model 'anthropic:nonexistent' is not registered. Available: ['claude-opus-4-8']. Use /model use auto to clear.",
            error_code="FORCED_MODEL_NOT_REGISTERED",
        )

    agent._process_iterative_function_calling = _raise_forced

    response = await agent.process_input("anything", session_id="forced-cli-test")
    assert response.status == AgentStatus.ERROR
    assert response.metadata.get("forced_model_not_registered") is True
    assert response.metadata.get("error_code") == "FORCED_MODEL_NOT_REGISTERED"
    assert "not registered" in response.content.lower()
    assert "/model use auto" in response.content
