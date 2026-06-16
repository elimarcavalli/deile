"""Tests for DeileAgent.get_or_create_session (external/persisted sessions)."""

from __future__ import annotations

from deile.core.agent import AgentSession, DeileAgent


class TestSnapshot:
    def test_snapshot_roundtrip(self, tmp_path):
        s = AgentSession(
            session_id="x",
            working_directory=tmp_path,
            context_data={"k": "v"},
        )
        snap = s.snapshot()
        s2 = AgentSession.from_snapshot(snap)
        assert s2.session_id == "x"
        assert s2.context_data == {"k": "v"}
        assert s2.persisted is True


class TestGetOrCreate:
    async def test_in_memory_creates(self, tmp_path):
        agent = DeileAgent()
        agent._session_store = None
        s1 = await agent.get_or_create_session(
            "test-1", working_directory=str(tmp_path)
        )
        assert s1.session_id == "test-1"
        s2 = await agent.get_or_create_session("test-1")
        assert s1 is s2

    async def test_persisted_uses_external_store(self, tmp_path, monkeypatch):
        store_path = tmp_path / "ext.sqlite"

        async def fake_get_store(self):
            if not getattr(self, "_session_store", None):
                from deile.core.session_store import SessionStore as SS

                store = SS(store_path)
                await store.init()
                self._session_store = store
            return self._session_store

        monkeypatch.setattr(DeileAgent, "get_session_store", fake_get_store)

        agent = DeileAgent()
        s1 = await agent.get_or_create_session(
            "bot_session_alice", working_directory=str(tmp_path), persisted=True
        )
        assert s1.persisted
        s1.context_data["name"] = "Alice"
        await agent.flush_persisted_sessions()
        await agent.shutdown()

        agent2 = DeileAgent()
        agent2._session_store = None
        s2 = await fake_get_store(agent2)
        row = await s2.get("bot_session_alice")
        assert row is not None
        assert row.context_data.get("name") == "Alice"
        await agent2.shutdown()
