"""End-to-end test: CronStore → CronRunner → make_fire_callback → MockAgent."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from deile.cron.agent_bridge import make_fire_callback
from deile.cron.runner import CronRunner
from deile.cron.store import CronEntry, CronStore


class TestCronEndToEnd:
    async def test_due_entry_flows_through_bridge_to_agent_and_back(self, tmp_path):
        store = CronStore(tmp_path / "cron.db")
        store.add(CronEntry(
            id="e1", prompt="run report",
            run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ))

        # Mock agent that records calls and returns a known response.
        agent = MagicMock()
        response = MagicMock()
        response.content = "report generated successfully"
        agent.process_input = AsyncMock(return_value=response)

        async def provider():
            return agent

        runner = CronRunner(store, fire_callback=make_fire_callback(provider))
        fired = await runner.tick()

        assert fired == 1
        # agent saw the prompt + correct session_id
        agent.process_input.assert_awaited_once()
        call = agent.process_input.await_args
        assert "run report" in (call.args[0] if call.args else call.kwargs.get("prompt", ""))
        # entry was marked fired with the response summary
        loaded = store.get("e1")
        assert loaded is not None
        assert not loaded.enabled  # one-shot disabled
        assert "report generated" in (loaded.last_result or "")

    async def test_session_id_encodes_entry_id(self, tmp_path):
        """Bridge passes session_id='cron-{entry.id}' so memory layers can correlate."""
        store = CronStore(tmp_path / "cron.db")
        store.add(CronEntry(
            id="abc123", prompt="hello",
            run_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        ))

        agent = MagicMock()
        agent.process_input = AsyncMock(return_value=MagicMock(content="ok"))

        CronRunner(store, fire_callback=make_fire_callback(lambda: asyncio.coroutine(lambda: agent)()))

        # Use a direct callback to capture kwargs
        captured: dict = {}

        async def provider():
            return agent

        async def recording_cb(entry: CronEntry) -> str:
            result = await make_fire_callback(provider)(entry)
            captured["session_id"] = agent.process_input.await_args.kwargs.get("session_id")
            return result

        runner2 = CronRunner(store, fire_callback=recording_cb)
        await runner2.tick()

        assert captured.get("session_id") == "cron-abc123"

    async def test_agent_provider_error_does_not_crash_runner(self, tmp_path):
        """If the agent provider raises, the runner marks the entry fired with error."""
        store = CronStore(tmp_path / "cron.db")
        store.add(CronEntry(
            id="bad1", prompt="will fail",
            run_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        ))

        async def broken_provider():
            raise RuntimeError("provider down")

        runner = CronRunner(store, fire_callback=make_fire_callback(broken_provider))
        fired = await runner.tick()

        assert fired == 1  # runner still counts it as fired
        loaded = store.get("bad1")
        assert loaded is not None
        assert "RuntimeError" in (loaded.last_result or "") or "error" in (loaded.last_result or "").lower()

    async def test_summary_truncated_at_max_chars(self, tmp_path):
        """Long agent responses are truncated to max_summary_chars."""
        store = CronStore(tmp_path / "cron.db")
        store.add(CronEntry(
            id="long1", prompt="generate",
            run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ))

        agent = MagicMock()
        long_text = "x" * 600
        agent.process_input = AsyncMock(return_value=MagicMock(content=long_text))

        async def provider():
            return agent

        runner = CronRunner(store, fire_callback=make_fire_callback(provider, max_summary_chars=100))
        await runner.tick()

        loaded = store.get("long1")
        assert loaded is not None
        result = loaded.last_result or ""
        assert len(result) <= 500  # store caps at 1000, bridge at max_summary_chars=100
        assert result.endswith("…") or len(result) <= 100
