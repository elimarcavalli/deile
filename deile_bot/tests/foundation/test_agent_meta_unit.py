"""Unit tests for DeileAgentMetaProvider — exercises introspection paths."""

from __future__ import annotations

from deile_bot.foundation.agent_meta import DeileAgentMetaProvider, ToolMeta


class _FakeTool:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description


class _FakeRegistry:
    def __init__(self, tools):
        self._tools = tools

    def list_enabled(self):
        return self._tools


class _FakeProvider:
    def __init__(self, models):
        self.available_models = models


class _FakeRouter:
    def __init__(self, providers):
        self.providers = providers


class _FakePersonaMgr:
    def __init__(self, personas):
        self._personas = personas

    def list_personas(self):
        return self._personas


class _FakeAgent:
    def __init__(self, *, registry=None, router=None, persona_manager=None):
        self.tool_registry = registry
        self.model_router = router
        self.persona_manager = persona_manager


class TestDeileAgentMetaProvider:
    async def test_list_tools_from_registry(self):
        reg = _FakeRegistry([_FakeTool("read", "reads files"), _FakeTool("bash")])
        agent = _FakeAgent(registry=reg)
        meta = DeileAgentMetaProvider(agent)
        tools = await meta.list_tools()
        names = [t.name for t in tools]
        assert "read" in names
        assert "bash" in names
        assert all(isinstance(t, ToolMeta) for t in tools)

    async def test_list_tools_no_registry(self):
        agent = _FakeAgent(registry=None)
        meta = DeileAgentMetaProvider(agent)
        assert await meta.list_tools() == []

    async def test_list_tools_swallows_errors(self):
        class Boom:
            def list_enabled(self):
                raise RuntimeError("kaboom")

        agent = _FakeAgent(registry=Boom())
        meta = DeileAgentMetaProvider(agent)
        # exception path returns []
        assert await meta.list_tools() == []

    async def test_list_models_from_router(self):
        router = _FakeRouter({
            "anthropic": _FakeProvider(["claude-sonnet-4-6"]),
            "deepseek": _FakeProvider(["deepseek-chat"]),
        })
        agent = _FakeAgent(router=router)
        meta = DeileAgentMetaProvider(agent)
        models = await meta.list_models()
        assert "anthropic:claude-sonnet-4-6" in models
        assert "deepseek:deepseek-chat" in models

    async def test_list_models_no_router(self):
        agent = _FakeAgent(router=None)
        meta = DeileAgentMetaProvider(agent)
        assert await meta.list_models() == []

    async def test_list_models_provider_without_models(self):
        class _NoModels:
            available_models = None

        router = _FakeRouter({"x": _NoModels()})
        agent = _FakeAgent(router=router)
        meta = DeileAgentMetaProvider(agent)
        models = await meta.list_models()
        assert models == ["x:default"]

    async def test_list_personas_via_method(self):
        mgr = _FakePersonaMgr(["developer", "host"])
        agent = _FakeAgent(persona_manager=mgr)
        meta = DeileAgentMetaProvider(agent)
        personas = await meta.list_personas()
        assert personas == ["developer", "host"]

    async def test_list_personas_via_dict_attribute(self):
        class _Mgr:
            personas = {"a": object(), "b": object()}

        agent = _FakeAgent(persona_manager=_Mgr())
        meta = DeileAgentMetaProvider(agent)
        personas = await meta.list_personas()
        assert set(personas) == {"a", "b"}

    async def test_list_personas_no_manager(self):
        agent = _FakeAgent(persona_manager=None)
        meta = DeileAgentMetaProvider(agent)
        assert await meta.list_personas() == []
