"""Unit tests for CronCreateTool / CronListTool / CronDeleteTool."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deile.tools.base import ToolContext, ToolStatus
from deile.tools.cron_create_tool import CronCreateTool
from deile.tools.cron_delete_tool import CronDeleteTool
from deile.tools.cron_list_tool import CronListTool


@pytest.fixture
def cron_db(tmp_path, monkeypatch):
    """Point all cron tools at an isolated SQLite under tmp_path."""
    db_path = tmp_path / "cron.db"
    monkeypatch.setenv("DEILE_CRON_DB_PATH", str(db_path))
    return db_path


def _ctx(**args) -> ToolContext:
    return ToolContext(user_input="", parsed_args=args)


# ---------------------------------------------------------------------------
# Schema sanity (catches the "type: null" regression we shipped on PipelineTool)
# ---------------------------------------------------------------------------

class TestToolSchemas:
    @pytest.mark.parametrize("cls", [CronCreateTool, CronListTool, CronDeleteTool])
    def test_parameters_are_object_schema(self, cls):
        schema = cls()._schema
        assert schema.parameters["type"] == "object"
        assert "properties" in schema.parameters
        # Round-trip through to_openai_function.
        fn = schema.to_openai_function()
        assert fn["function"]["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# CronCreateTool
# ---------------------------------------------------------------------------

class TestCronCreate:
    async def test_create_recurring(self, cron_db):
        tool = CronCreateTool()
        r = await tool.execute(_ctx(prompt="say hi", cron="*/5 * * * *"))
        assert r.status == ToolStatus.SUCCESS
        assert r.data["id"].startswith("cron-")
        assert r.data["is_oneshot"] is False
        assert r.data["next_fire_at"] is not None

    async def test_create_oneshot(self, cron_db):
        tool = CronCreateTool()
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        r = await tool.execute(_ctx(prompt="run report", run_at=future))
        assert r.status == ToolStatus.SUCCESS
        assert r.data["is_oneshot"] is True

    async def test_missing_prompt(self, cron_db):
        tool = CronCreateTool()
        r = await tool.execute(_ctx(cron="*/5 * * * *"))
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "MISSING_PROMPT"

    async def test_both_cron_and_run_at_rejected(self, cron_db):
        tool = CronCreateTool()
        r = await tool.execute(_ctx(prompt="x", cron="* * * * *",
                                    run_at="2030-01-01T00:00:00Z"))
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "AMBIGUOUS_SCHEDULE"

    async def test_neither_cron_nor_run_at_rejected(self, cron_db):
        tool = CronCreateTool()
        r = await tool.execute(_ctx(prompt="x"))
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "MISSING_SCHEDULE"

    async def test_invalid_run_at(self, cron_db):
        tool = CronCreateTool()
        r = await tool.execute(_ctx(prompt="x", run_at="banana"))
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "INVALID_DATETIME"

    async def test_invalid_cron(self, cron_db):
        tool = CronCreateTool()
        r = await tool.execute(_ctx(prompt="x", cron="not a cron"))
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "CRON_STORE"

    async def test_explicit_id_used(self, cron_db):
        tool = CronCreateTool()
        r = await tool.execute(_ctx(prompt="x", cron="*/5 * * * *", id="my-id"))
        assert r.data["id"] == "my-id"

    async def test_duplicate_id_rejected(self, cron_db):
        tool = CronCreateTool()
        await tool.execute(_ctx(prompt="x", cron="*/5 * * * *", id="dup"))
        r = await tool.execute(_ctx(prompt="y", cron="*/10 * * * *", id="dup"))
        assert r.status == ToolStatus.ERROR


# ---------------------------------------------------------------------------
# CronListTool
# ---------------------------------------------------------------------------

class TestCronList:
    async def test_empty(self, cron_db):
        r = await CronListTool().execute(_ctx())
        assert r.status == ToolStatus.SUCCESS
        assert r.data["count"] == 0

    async def test_lists_added(self, cron_db):
        await CronCreateTool().execute(_ctx(prompt="a", cron="*/5 * * * *"))
        await CronCreateTool().execute(_ctx(prompt="b", cron="*/10 * * * *"))
        r = await CronListTool().execute(_ctx())
        assert r.data["count"] == 2

    async def test_filter_by_creator(self, cron_db):
        await CronCreateTool().execute(_ctx(
            prompt="a", cron="*/5 * * * *", created_by="discord:1"
        ))
        await CronCreateTool().execute(_ctx(
            prompt="b", cron="*/10 * * * *", created_by="discord:2"
        ))
        r = await CronListTool().execute(_ctx(created_by="discord:2"))
        assert r.data["count"] == 1
        assert r.data["entries"][0]["prompt"] == "b"

    async def test_only_enabled(self, cron_db):
        # Add two, disable one, request only_enabled.
        c1 = await CronCreateTool().execute(_ctx(
            prompt="a", cron="*/5 * * * *", id="r1",
        ))
        await CronCreateTool().execute(_ctx(
            prompt="b", cron="*/10 * * * *", id="r2",
        ))
        await CronDeleteTool().execute(_ctx(id="r2", disable_only=True))
        r = await CronListTool().execute(_ctx(only_enabled=True))
        assert r.data["count"] == 1
        assert r.data["entries"][0]["id"] == "r1"


# ---------------------------------------------------------------------------
# CronDeleteTool
# ---------------------------------------------------------------------------

class TestCronDelete:
    async def test_delete_existing(self, cron_db):
        await CronCreateTool().execute(_ctx(prompt="x", cron="*/5 * * * *", id="r1"))
        r = await CronDeleteTool().execute(_ctx(id="r1"))
        assert r.status == ToolStatus.SUCCESS
        assert r.data["action"] == "removed"
        # gone
        listing = await CronListTool().execute(_ctx())
        assert listing.data["count"] == 0

    async def test_disable_preserves_entry(self, cron_db):
        await CronCreateTool().execute(_ctx(prompt="x", cron="*/5 * * * *", id="r1"))
        r = await CronDeleteTool().execute(_ctx(id="r1", disable_only=True))
        assert r.data["action"] == "disabled"
        listing = await CronListTool().execute(_ctx())
        assert listing.data["count"] == 1
        assert listing.data["entries"][0]["enabled"] is False

    async def test_missing_id(self, cron_db):
        r = await CronDeleteTool().execute(_ctx())
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "MISSING_ID"

    async def test_unknown_id(self, cron_db):
        r = await CronDeleteTool().execute(_ctx(id="nope"))
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Auto-discover registration
# ---------------------------------------------------------------------------

class TestAutoDiscover:
    def test_three_tools_registered(self):
        from deile.tools.registry import ToolRegistry
        r = ToolRegistry()
        r.auto_discover()
        for name in ("cron_create", "cron_list", "cron_delete"):
            assert name in r._tools, f"{name} not auto-registered"
