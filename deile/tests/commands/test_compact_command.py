"""Tests: /compact command — real subsystem wiring, no hardcoded data (issue #171).

Mocks represent the external systems (MemoryManager, SessionStore) to isolate
command logic. The mocks match the real API contracts established by issue #171.

Criteria verified:
  - summary reads from MemoryManager + SessionStore (not hardcoded stats)
  - compress calls consolidate() and reports real returned entries count
  - purge shows real session count before confirm, deletes only with --confirm
  - analyze uses real sessions or honest "dados insuficientes" message
  - execute signature is async and returns CommandResult (compatible with SlashCommand)
  - no hardcoded topic lists anywhere in command output
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from rich.console import Console

from deile.commands.base import CommandContext, CommandResult
from deile.commands.builtin.compact_command import CompactCommand

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(content) -> str:
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=120)
    console.print(content)
    return buf.getvalue()


def _ctx(args: str = "", agent=None) -> CommandContext:
    ctx = CommandContext(user_input=f"/compact {args}".strip(), args=args)
    ctx.agent = agent
    return ctx


def _cmd() -> CompactCommand:
    return CompactCommand()


def _mock_agent(memory_manager=None, session_store=None):
    agent = MagicMock()
    agent.memory_manager = memory_manager

    async def fake_get_session_store():
        return session_store

    agent._get_session_store = fake_get_session_store
    return agent


def _mock_memory_manager(total_mb: float = 5.0, entries: int = 3, cleaned: int = 1):
    mm = MagicMock()
    mm.get_memory_usage = AsyncMock(
        return_value={
            "total_memory_mb": total_mb,
            "components": {
                "working_memory": {
                    "total_entries": entries,
                    "ttl": 3600,
                    "memory_mb": total_mb,
                }
            },
            "manager_stats": {},
            "consolidation_active": False,
        }
    )
    mm.consolidate = AsyncMock(
        return_value={
            "older_than_days": 7,
            "entries_before": entries,
            "entries_processed": cleaned,
            "total_time_s": 0.01,
        }
    )
    return mm


def _mock_session_store(
    sessions=None,
    count: int = 2,
    count_before: int = 1,
):
    ss = MagicMock()
    if sessions is None:
        sessions = [
            {"session_id": "s1", "last_used_at": "2026-01-01T10:00:00.000000Z"},
            {"session_id": "s2", "last_used_at": "2026-01-02T11:00:00.000000Z"},
        ]
    ss.get_stats = AsyncMock(
        return_value={
            "session_count": count,
            "oldest_last_used": sessions[0]["last_used_at"] if sessions else None,
            "newest_last_used": sessions[-1]["last_used_at"] if sessions else None,
        }
    )
    ss.list_all = AsyncMock(return_value=sessions)
    ss.count_sessions_before = AsyncMock(return_value=count_before)
    ss.delete_sessions_before = AsyncMock(return_value=count_before)
    ss.upsert = AsyncMock()
    return ss


# ---------------------------------------------------------------------------
# /compact summary
# ---------------------------------------------------------------------------


class TestCompactSummary:
    async def test_summary_reads_from_memory_manager(self):
        mm = _mock_memory_manager(total_mb=12.5, entries=7)
        agent = _mock_agent(memory_manager=mm)
        result = await _cmd().execute(_ctx("summary", agent))
        assert result.success
        mm.get_memory_usage.assert_awaited_once()

    async def test_summary_reads_from_session_store(self):
        ss = _mock_session_store(count=5)
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("summary", agent))
        assert result.success
        ss.get_stats.assert_awaited_once()

    async def test_summary_no_agent_returns_success(self):
        result = await _cmd().execute(_ctx("summary"))
        assert result.success

    async def test_summary_content_type_is_rich(self):
        result = await _cmd().execute(_ctx("summary"))
        assert result.content_type == "rich"

    async def test_summary_default_action_no_args(self):
        result = await _cmd().execute(_ctx(""))
        assert result.success

    async def test_summary_renders_non_empty(self):
        result = await _cmd().execute(_ctx("summary"))
        assert _render(result.content).strip()


# ---------------------------------------------------------------------------
# /compact compress
# ---------------------------------------------------------------------------


class TestCompactCompress:
    async def test_compress_calls_consolidate(self):
        mm = _mock_memory_manager(entries=10, cleaned=3)
        agent = _mock_agent(memory_manager=mm)
        result = await _cmd().execute(_ctx("compress 7", agent))
        assert result.success
        mm.consolidate.assert_awaited_once_with(older_than_days=7)

    async def test_compress_result_shown_is_real(self):
        mm = _mock_memory_manager(entries=10, cleaned=3)
        agent = _mock_agent(memory_manager=mm)
        result = await _cmd().execute(_ctx("compress 7", agent))
        assert result.success
        assert result.metadata.get("entries_processed") == 3

    async def test_compress_entries_before_is_real(self):
        mm = _mock_memory_manager(entries=10, cleaned=3)
        agent = _mock_agent(memory_manager=mm)
        result = await _cmd().execute(_ctx("compress 7", agent))
        assert result.metadata.get("entries_before") == 10

    async def test_compress_no_memory_manager_returns_error(self):
        result = await _cmd().execute(_ctx("compress 7"))
        assert not result.success

    async def test_compress_default_days(self):
        mm = _mock_memory_manager()
        agent = _mock_agent(memory_manager=mm)
        result = await _cmd().execute(_ctx("compress", agent))
        assert result.success
        mm.consolidate.assert_awaited_once()

    async def test_compress_custom_days(self):
        mm = _mock_memory_manager()
        agent = _mock_agent(memory_manager=mm)
        await _cmd().execute(_ctx("compress 14", agent))
        mm.consolidate.assert_awaited_once_with(older_than_days=14)


# ---------------------------------------------------------------------------
# /compact purge
# ---------------------------------------------------------------------------


class TestCompactPurge:
    async def test_purge_shows_real_count_before_confirm(self):
        ss = _mock_session_store(count_before=3)
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("purge 30", agent))
        assert result.success
        assert result.metadata.get("sessions_to_delete") == 3
        ss.count_sessions_before.assert_awaited_once()

    async def test_purge_without_confirmation_deletes_nothing(self):
        ss = _mock_session_store(count_before=5)
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("purge 30", agent))
        assert result.success
        assert result.metadata.get("purged_count") == 0
        ss.delete_sessions_before.assert_not_awaited()

    async def test_purge_with_confirmation_deletes_real_sessions(self):
        ss = _mock_session_store(count_before=4)
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("purge 30 --confirm", agent))
        assert result.success
        assert result.metadata.get("purged_count") == 4
        ss.delete_sessions_before.assert_awaited_once()

    async def test_purge_no_session_store_returns_error(self):
        result = await _cmd().execute(_ctx("purge 30"))
        assert not result.success

    async def test_purge_confirm_flag_sim(self):
        ss = _mock_session_store(count_before=2)
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("purge 14 sim", agent))
        assert result.success
        assert result.metadata.get("purged_count") == 2
        ss.delete_sessions_before.assert_awaited_once()

    async def test_purge_confirmed_shows_deleted_count_in_metadata(self):
        ss = _mock_session_store(count_before=7)
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("purge 30 --confirm", agent))
        assert result.metadata.get("confirmed") is True
        assert result.metadata.get("purged_count") == 7

    async def test_purge_unconfirmed_shows_confirmed_false(self):
        ss = _mock_session_store(count_before=1)
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("purge 30", agent))
        assert result.metadata.get("confirmed") is False


# ---------------------------------------------------------------------------
# /compact analyze
# ---------------------------------------------------------------------------


class TestCompactAnalyze:
    async def test_analyze_uses_real_sessions(self):
        sessions = [
            {"session_id": f"s{i}", "last_used_at": f"2026-0{(i%3)+1}-{(i%28)+1:02d}T10:00:00.000000Z"}
            for i in range(5)
        ]
        ss = _mock_session_store(sessions=sessions, count=5)
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("analyze", agent))
        assert result.success
        assert result.metadata.get("session_count") == 5
        ss.list_all.assert_awaited_once()

    async def test_analyze_insufficient_data_honest_message(self):
        ss = _mock_session_store(sessions=[], count=0)
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("analyze", agent))
        assert result.success
        assert result.metadata.get("session_count") == 0

    async def test_analyze_no_hardcoded_topic_lists(self):
        sessions = [
            {"session_id": f"s{i}", "last_used_at": f"2026-01-{i+1:02d}T10:00:00.000000Z"}
            for i in range(3)
        ]
        ss = _mock_session_store(sessions=sessions, count=3)
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("analyze", agent))
        assert result.success
        content_str = str(result.content)
        assert "Python Development" not in content_str
        assert "Bug Fixes" not in content_str
        assert "Code Review" not in content_str

    async def test_analyze_no_session_store_returns_error(self):
        result = await _cmd().execute(_ctx("analyze"))
        assert not result.success

    async def test_analyze_renders_non_empty(self):
        sessions = [{"session_id": "s1", "last_used_at": "2026-01-01T10:00:00.000000Z"}]
        ss = _mock_session_store(sessions=sessions, count=1)
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("analyze", agent))
        assert _render(result.content).strip()


# ---------------------------------------------------------------------------
# execute() signature compatibility
# ---------------------------------------------------------------------------


class TestExecuteSignature:
    async def test_execute_signature_compatible_with_slash_command(self):
        import inspect

        from deile.commands.base import SlashCommand

        cmd = _cmd()
        assert isinstance(cmd, SlashCommand)
        assert inspect.iscoroutinefunction(cmd.execute)
        ctx = _ctx()
        result = await cmd.execute(ctx)
        assert isinstance(result, CommandResult)

    async def test_execute_returns_command_result_not_dict(self):
        result = await _cmd().execute(_ctx())
        assert isinstance(result, CommandResult)
        assert not isinstance(result, dict)

    async def test_execute_unknown_action_returns_error(self):
        result = await _cmd().execute(_ctx("nonexistent"))
        assert not result.success

    async def test_execute_invalid_days_returns_error(self):
        result = await _cmd().execute(_ctx("compress notanumber"))
        assert not result.success


# ---------------------------------------------------------------------------
# /compact config
# ---------------------------------------------------------------------------


class TestCompactConfig:
    async def test_show_config_returns_success(self):
        result = await _cmd().execute(_ctx("config"))
        assert result.success
        assert result.content_type == "rich"

    async def test_set_config_updates_value(self):
        cmd = _cmd()
        result = await cmd.execute(_ctx("config compress_threshold_days 14"))
        assert result.success
        assert cmd.compact_config["compress_threshold_days"] == 14

    async def test_set_config_unknown_key_returns_error(self):
        result = await _cmd().execute(_ctx("config nonexistent_key 42"))
        assert not result.success

    async def test_set_config_invalid_value_returns_error(self):
        result = await _cmd().execute(_ctx("config compress_threshold_days notanumber"))
        assert not result.success


# ---------------------------------------------------------------------------
# /compact export + import (integration with SessionStore)
# ---------------------------------------------------------------------------


class TestCompactExport:
    async def test_export_no_session_store_returns_error(self):
        result = await _cmd().execute(_ctx("export json"))
        assert not result.success

    async def test_export_empty_sessions_returns_error(self):
        ss = _mock_session_store(sessions=[])
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("export json", agent))
        assert not result.success

    async def test_export_json_writes_real_sessions(self, tmp_path):
        sessions = [
            {"session_id": "abc", "last_used_at": "2026-01-01T10:00:00.000000Z"},
        ]
        ss = _mock_session_store(sessions=sessions, count=1)
        agent = _mock_agent(session_store=ss)
        fname = str(tmp_path / "out.json")
        result = await _cmd().execute(_ctx(f"export json {fname}", agent))
        assert result.success
        data = json.loads(Path(fname).read_text())
        assert data[0]["session_id"] == "abc"

    async def test_export_invalid_format_returns_error(self):
        ss = _mock_session_store()
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("export xml", agent))
        assert not result.success


class TestCompactImport:
    async def test_import_no_filename_returns_error(self):
        result = await _cmd().execute(_ctx("import"))
        assert not result.success

    async def test_import_missing_file_returns_error(self):
        ss = _mock_session_store()
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx("import /nonexistent/file.json", agent))
        assert not result.success

    async def test_import_json_calls_upsert(self, tmp_path):
        sessions = [{"session_id": "xyz", "working_directory": "/tmp"}]
        f = tmp_path / "import.json"
        f.write_text(json.dumps(sessions), encoding="utf-8")
        ss = _mock_session_store()
        agent = _mock_agent(session_store=ss)
        result = await _cmd().execute(_ctx(f"import {f}", agent))
        assert result.success
        ss.upsert.assert_awaited_once()
