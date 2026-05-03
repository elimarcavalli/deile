"""Foundation E2E fixtures.

These tests use FakeProviderAdapter + FakeBridge by default. A `real_agent`
fixture is provided for tests that *want* to invoke a live model — those
must be marked `slow` or `manual` (and require `DEEPSEEK_API_KEY`).
"""

from __future__ import annotations

import os

import pytest

from deile_bot._testing import FakeAgentMetaProvider, FakeProviderAdapter
from deile_bot.foundation.agent_bridge import (AgentBridge, AgentInvocation,
                                               AgentResponse)
from deile_bot.foundation.audit import BotAuditLogger
from deile_bot.foundation.capabilities import CapabilityCatalog
from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.dlq import DeadLetterQueue
from deile_bot.foundation.event_bus import BotEventBus
from deile_bot.foundation.identity import IdentityResolver
from deile_bot.foundation.intent import HeuristicIntentClassifier
from deile_bot.foundation.metrics import MetricsCollector
from deile_bot.foundation.output_formatter import PlainTextFormatter
from deile_bot.foundation.permissions import PermissionGate
from deile_bot.foundation.persona_selector import PersonaSelector
from deile_bot.foundation.pipeline import EgressPipeline, IngressPipeline
from deile_bot.foundation.rate_limit import RateLimiter
from deile_bot.foundation.settings import BotSettings


class CapturingFakeBridge(AgentBridge):
    """Bridge that captures all invocations for assertions."""

    def __init__(self, response_text="resposta de teste — pelo menos 50 caracteres aqui ok!"):
        self.response_text = response_text
        self.invocations = []

    async def invoke(self, inv: AgentInvocation) -> AgentResponse:
        from deile.common.markup_ast import MarkupAST

        self.invocations.append(inv)
        return AgentResponse(
            text=self.response_text,
            markup=MarkupAST.from_plain(self.response_text),
            elapsed_ms=20,
            model_used="fake",
        )


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "e2e.sqlite")
    await s.init()
    yield s
    await s.close()


@pytest.fixture
async def fake_adapter():
    return FakeProviderAdapter()


@pytest.fixture
async def fake_bridge():
    return CapturingFakeBridge()


@pytest.fixture
async def settings():
    return BotSettings()


@pytest.fixture
async def metrics():
    return MetricsCollector()


@pytest.fixture
async def pipeline(store, fake_adapter, fake_bridge, settings, metrics):
    identity = IdentityResolver(store)
    perms = PermissionGate(settings, identity)
    rl = RateLimiter(settings)
    audit = BotAuditLogger(store)
    bus = BotEventBus()
    dlq = DeadLetterQueue(store, settings)
    egress = EgressPipeline(
        formatters={fake_adapter.name: PlainTextFormatter()},
        rate_limit=rl,
        store=store,
        audit=audit,
        event_bus=bus,
        metrics=metrics,
        dlq=dlq,
    )
    p = IngressPipeline(
        identity=identity,
        permissions=perms,
        rate_limit=rl,
        store=store,
        intent=HeuristicIntentClassifier(),
        bridge=fake_bridge,
        capability_catalog=CapabilityCatalog(),
        persona_selector=PersonaSelector(settings, identity),
        audit=audit,
        event_bus=bus,
        metrics=metrics,
        egress=egress,
        agent_meta=FakeAgentMetaProvider(),
    )
    return p


def _has_key(name: str) -> bool:
    return bool(os.environ.get(name))


@pytest.fixture
async def real_agent():
    """Returns a real DeileAgent if DEEPSEEK_API_KEY is set; else None.

    Tests that depend on this should `pytest.skip` when None.
    """
    if not _has_key("DEEPSEEK_API_KEY"):
        return None
    try:
        from deile.config.manager import ConfigManager
        from deile.core.agent import DeileAgent
        from deile.core.models.bootstrap import bootstrap_providers
        from deile.core.models.router import get_model_router
    except Exception:  # pragma: no cover
        return None
    try:
        ConfigManager().load_config()
        model_router = get_model_router()
        bootstrap_providers(router=model_router)
        agent = DeileAgent(model_router=model_router)
        await agent.initialize()
        return agent
    except Exception:  # pragma: no cover
        return None
