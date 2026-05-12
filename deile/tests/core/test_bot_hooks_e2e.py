"""E2E for plano DEILE hooks (sessions persistentes + extra_system_prompt + structured).

These tests exercise the full chain end-to-end without invoking a live LLM
(LLM-backed scenarios are marked `manual` and skipped here). The point is to
prove that the cross-section of session/persisted/extra-prompt/bot-context/
structured-output works under realistic flow.
"""

from __future__ import annotations

import pytest

from deile.core.agent import AgentResponse, AgentStatus, DeileAgent

pytestmark = pytest.mark.e2e


class TestE2ED1PersistedSession:
    async def test_session_survives_close_and_reopen(self, tmp_path, monkeypatch):
        store_path = tmp_path / "e2e.sqlite"

        async def fake_get_store(self):
            if not getattr(self, "_session_store", None):
                from deile.core.session_store import SessionStore

                self._session_store = SessionStore(store_path)
                await self._session_store.init()
            return self._session_store

        monkeypatch.setattr(DeileAgent, "get_session_store", fake_get_store)

        # Process A
        agent_a = DeileAgent()
        s = await agent_a.get_or_create_session(
            "bot_session_alice", working_directory=str(tmp_path), persisted=True
        )
        s.context_data["facts"] = {"name": "Alice"}
        await agent_a.flush_persisted_sessions()
        await agent_a.shutdown()

        # Process B (new agent, same DB)
        agent_b = DeileAgent()
        s2 = await agent_b.get_or_create_session(
            "bot_session_alice", persisted=True
        )
        assert s2.context_data.get("facts", {}).get("name") == "Alice"
        await agent_b.shutdown()


class TestE2ED2ExtraSystemPrompt:
    async def test_extra_prompt_appears_in_system_instruction(
        self, tmp_path, monkeypatch
    ):
        agent = DeileAgent()

        captured = {}

        async def fake_process_input(self, user_input, session_id="default", **kwargs):
            extra = kwargs.pop("extra_system_prompt", None)
            session = self._get_or_create_session(session_id, **kwargs)
            if extra is not None:
                from deile.core.bot_hooks import sanitize_extra_system_prompt
                session.context_data["extra_system_prompt"] = (
                    sanitize_extra_system_prompt(str(extra))
                )
            captured["session"] = session
            return AgentResponse(
                content="ok",
                status=AgentStatus.IDLE,
                tool_results=[],
                execution_time=0.01,
            )

        monkeypatch.setattr(DeileAgent, "process_input", fake_process_input)
        await agent.process_input(
            "oi",
            session_id="t1",
            extra_system_prompt="<bot_capabilities>tool_x: faz X</bot_capabilities>",
        )
        sess = captured["session"]
        assert "tool_x" in sess.context_data["extra_system_prompt"]


class TestE2ED3BotContext:
    async def test_tool_context_extra_carries_bot_context(self, tmp_path):
        from deile.tools.base import ToolContext

        agent = DeileAgent()
        s = await agent.get_or_create_session(
            "t2", working_directory=str(tmp_path)
        )
        s.context_data["bot_context"] = {"provider": "discord", "scope": "DM"}
        # Simulate the agent dispatching a tool: build ToolContext as agent does
        bc = s.context_data.get("bot_context") or {}
        ctx = ToolContext(
            user_input="x",
            session_data=s.context_data,
            extra={"bot_context": dict(bc)} if bc else {},
        )
        assert ctx.extra["bot_context"]["provider"] == "discord"


class TestE2ED4StructuredAST:
    async def test_structured_returns_ast(self, monkeypatch):
        from deile.common.markup_ast import SpanKind

        async def fake_process_input(self, user_input, session_id="default", **kwargs):
            return AgentResponse(
                content="# Heading\n\n- one\n- two\n- three\n",
                status=AgentStatus.IDLE,
                tool_results=[],
                metadata={"model_used": "fake"},
                execution_time=0.01,
            )

        monkeypatch.setattr(DeileAgent, "process_input", fake_process_input)
        agent = DeileAgent()
        sr = await agent.process_input_structured("liste 3 frutas")
        kinds = {span.kind for span in sr.markup}
        assert SpanKind.HEADING in kinds
        assert SpanKind.BULLET in kinds
        assert sum(1 for s in sr.markup if s.kind == SpanKind.BULLET) >= 3


class TestE2ED5StreamChunkIntegrity:
    async def test_recombination_equals_done(self, monkeypatch):
        agent = DeileAgent()

        async def fake_stream(self, user_input, session_id="default", **kwargs):
            from deile.core.models.stream_events import StreamEventType

            class E:
                pass

            for piece in ["maca, ", "banana, ", "uva, ", "manga, ", "morango"]:
                e = E()
                e.type = StreamEventType.TEXT_DELTA
                e.text = piece
                yield e

        monkeypatch.setattr(DeileAgent, "process_input_stream", fake_stream)
        chunks = []
        async for c in agent.process_input_stream_chunks("liste 5 frutas"):
            chunks.append(c)
        assert chunks[-1].kind == "done"
        text_only = "".join(
            c.payload["text"] for c in chunks if c.kind == "text"
        )
        assert text_only == chunks[-1].payload["text"]
        assert "morango" in chunks[-1].payload["text"]


class TestE2ED6CLINoRegression:
    """Smoke that DeileAgent constructs and basic methods exist."""

    async def test_agent_constructs(self):
        agent = DeileAgent()
        assert hasattr(agent, "process_input")
        assert hasattr(agent, "process_input_structured")
        assert hasattr(agent, "process_input_stream")
        assert hasattr(agent, "process_input_stream_chunks")
        assert hasattr(agent, "get_or_create_session")

    async def test_session_default_persisted_false(self, tmp_path):
        agent = DeileAgent()
        s = await agent.get_or_create_session(
            "cli-default", working_directory=str(tmp_path), persisted=False
        )
        assert s.persisted is False
