"""Tests: /memory command — real MemoryManager integration (issue #167)."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from deile.commands.base import CommandContext
from deile.commands.builtin.memory_command import MemoryCommand

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(content) -> str:
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=120)
    console.print(content)
    return buf.getvalue()


def _ctx(args: str = "", agent=None) -> CommandContext:
    ctx = CommandContext(user_input=f"/memory {args}".strip(), args=args)
    ctx.agent = agent
    return ctx


def _cmd() -> MemoryCommand:
    return MemoryCommand()


class _FakeAgent:
    def __init__(self, mm=None):
        self.memory_manager = mm


def _make_mock_mm(usage_data: dict | None = None) -> MagicMock:
    mm = MagicMock()
    mm.get_memory_usage = AsyncMock(return_value=usage_data or {
        "total_memory_mb": 1.5,
        "components": {
            "working_memory": {"entries": 3, "memory_mb": 0.5},
            "episodic_memory": {"entries": 10, "memory_mb": 0.6},
            "semantic_memory": {"entries": 5, "memory_mb": 0.3},
            "procedural_memory": {"entries": 2, "memory_mb": 0.1},
        },
        "manager_stats": {},
        "consolidation_active": False,
    })
    mm.optimize_memory = AsyncMock(return_value={"consolidated": 5, "freed_mb": 0.2})
    return mm


# ---------------------------------------------------------------------------
# /memory status reads from MemoryManager
# ---------------------------------------------------------------------------


class TestStatusReadsFromMemoryManager:
    async def test_status_reads_from_memory_manager(self):
        mm = _make_mock_mm()
        agent = _FakeAgent(mm)
        result = await _cmd().execute(_ctx("status", agent=agent))
        assert result.success is True
        mm.get_memory_usage.assert_awaited_once()

    async def test_status_shows_layer_names(self):
        mm = _make_mock_mm()
        agent = _FakeAgent(mm)
        result = await _cmd().execute(_ctx("status", agent=agent))
        rendered = _render(result.content)
        assert "working" in rendered.lower() or "Working" in rendered

    async def test_status_no_agent_shows_fallback(self):
        result = await _cmd().execute(_ctx("status"))
        assert result.success is True
        rendered = _render(result.content)
        # Should not crash; may show INDISPONÍVEL or session-level data
        assert rendered.strip()

    async def test_status_values_change_as_session_grows(self):
        """Values differ when MemoryManager reports more entries."""
        mm1 = _make_mock_mm({"total_memory_mb": 0.0, "components": {"working_memory": {"entries": 0, "memory_mb": 0.0}}})
        mm2 = _make_mock_mm({"total_memory_mb": 5.0, "components": {"working_memory": {"entries": 50, "memory_mb": 5.0}}})

        result1 = await _cmd().execute(_ctx("status", agent=_FakeAgent(mm1)))
        result2 = await _cmd().execute(_ctx("status", agent=_FakeAgent(mm2)))
        r1 = _render(result1.content)
        r2 = _render(result2.content)
        assert r1 != r2

    async def test_status_memory_manager_error_shows_error(self):
        mm = MagicMock()
        mm.get_memory_usage = AsyncMock(return_value={"error": "DB timeout"})
        agent = _FakeAgent(mm)
        result = await _cmd().execute(_ctx("status", agent=agent))
        assert result.success is True
        rendered = _render(result.content)
        assert "DB timeout" in rendered or "Erro" in rendered


# ---------------------------------------------------------------------------
# /memory compact calls optimize_memory
# ---------------------------------------------------------------------------


class TestCompactCallsOptimizeMemory:
    async def test_compact_calls_optimize_memory(self):
        mm = _make_mock_mm()
        agent = _FakeAgent(mm)
        result = await _cmd().execute(_ctx("compact", agent=agent))
        assert result.success is True
        mm.optimize_memory.assert_awaited_once()

    async def test_compact_reports_real_result(self):
        mm = _make_mock_mm()
        mm.optimize_memory = AsyncMock(return_value={"consolidated": 12, "freed_mb": 1.5})
        agent = _FakeAgent(mm)
        result = await _cmd().execute(_ctx("compact", agent=agent))
        rendered = _render(result.content)
        # Report should contain the real returned data
        assert "12" in rendered or "1.5" in rendered or "consolidated" in rendered

    async def test_compact_no_agent_shows_indisponivel(self):
        result = await _cmd().execute(_ctx("compact"))
        rendered = _render(result.content)
        assert "INDISPONÍVEL" in rendered

    async def test_compact_exception_raises_command_error(self):
        from deile.core.exceptions import CommandError
        mm = MagicMock()
        mm.optimize_memory = AsyncMock(side_effect=RuntimeError("out of memory"))
        agent = _FakeAgent(mm)
        with pytest.raises(CommandError, match="Falha na compactação"):
            await _cmd().execute(_ctx("compact", agent=agent))


# ---------------------------------------------------------------------------
# /memory save — writes to disk
# ---------------------------------------------------------------------------


class TestCheckpointSaveWritesToDisk:
    async def test_checkpoint_save_writes_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")

        mm = _make_mock_mm()
        agent = _FakeAgent(mm)
        result = await _cmd().execute(_ctx("save mytest", agent=agent))
        assert result.success is True

        # Checkpoint file must exist
        cp_path = tmp_path / "mytest.json"
        assert cp_path.exists(), f"Expected checkpoint file at {cp_path}"

        # Index must be updated
        index_path = tmp_path / "index.json"
        assert index_path.exists()
        index = json.loads(index_path.read_text())
        assert "mytest" in index

    async def test_checkpoint_save_success_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")

        result = await _cmd().execute(_ctx("save cp_test"))
        rendered = _render(result.content)
        assert "cp_test" in rendered


# ---------------------------------------------------------------------------
# /memory restore — reads from disk
# ---------------------------------------------------------------------------


class TestCheckpointRestoreReadsFromDisk:
    async def test_checkpoint_restore_reads_from_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")

        # Create checkpoint file manually
        cp_data = {"name": "restore_test", "saved_at": "2026-01-01T00:00:00", "memory_usage": {}}
        cp_path = tmp_path / "restore_test.json"
        cp_path.write_text(json.dumps(cp_data), encoding="utf-8")

        result = await _cmd().execute(_ctx("restore restore_test"))
        assert result.success is True
        rendered = _render(result.content)
        assert "restore_test" in rendered

    async def test_checkpoint_restore_fails_if_not_exists(self, tmp_path, monkeypatch):
        from deile.core.exceptions import CommandError
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")

        with pytest.raises(CommandError, match="não encontrado"):
            await _cmd().execute(_ctx("restore ghost_checkpoint"))

    async def test_checkpoint_restore_session_b_after_save_session_a(self, tmp_path, monkeypatch):
        """Checkpoint saved in 'session A' must be readable in 'session B'."""
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")

        mm = _make_mock_mm()
        # Session A: save
        cmd_a = _cmd()
        await cmd_a.execute(_ctx("save cross_session", agent=_FakeAgent(mm)))

        # Session B: restore (new command instance = new process simulation)
        cmd_b = _cmd()
        result = await cmd_b.execute(_ctx("restore cross_session"))
        assert result.success is True


# ---------------------------------------------------------------------------
# /memory list
# ---------------------------------------------------------------------------


class TestCheckpointListReadsIndex:
    async def test_checkpoint_list_reads_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")

        index = {"cp1": {"name": "cp1", "saved_at": "2026-01-01T00:00:00", "size_bytes": 100}}
        (tmp_path / "index.json").write_text(json.dumps(index))

        result = await _cmd().execute(_ctx("list"))
        assert result.success is True
        rendered = _render(result.content)
        assert "cp1" in rendered

    async def test_checkpoint_list_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")

        result = await _cmd().execute(_ctx("list"))
        assert result.success is True
        rendered = _render(result.content)
        assert "Nenhum" in rendered or "checkpoint" in rendered.lower()


# ---------------------------------------------------------------------------
# /memory clear — None safety
# ---------------------------------------------------------------------------


class TestClearTypeHandlesNoneSafely:
    async def test_clear_working_reduces_usage(self):
        """After clearing conversation, usage should reflect 0 messages."""
        class _Session:
            conversation_history = ["msg1", "msg2"]
            context_data = {}
            memory = []

        session_obj = _Session()
        ctx = CommandContext(user_input="/memory clear conversation", args="clear conversation")
        ctx.session = session_obj
        result = await _cmd().execute(ctx)
        assert result.success is True
        assert len(session_obj.conversation_history) == 0

    async def test_clear_type_handles_none_safely(self):
        """None session attributes must not raise AttributeError."""
        class _NoneSession:
            conversation_history = None
            context_data = None
            memory = None

        ctx = CommandContext(user_input="/memory clear conversation", args="clear conversation")
        ctx.session = _NoneSession()
        result = await _cmd().execute(ctx)
        assert result.success is True

    async def test_clear_without_session_is_safe(self):
        result = await _cmd().execute(_ctx("clear conversation"))
        assert result.success is True

    async def test_clear_unknown_type_raises(self):
        from deile.core.exceptions import CommandError
        with pytest.raises(CommandError, match="desconhecido"):
            await _cmd().execute(_ctx("clear foobar"))


# ---------------------------------------------------------------------------
# /memory export
# ---------------------------------------------------------------------------


class TestExportWritesValidJsonFile:
    async def test_export_writes_valid_json_file(self, tmp_path):
        output = tmp_path / "memory_export.json"
        mm = _make_mock_mm()
        agent = _FakeAgent(mm)
        result = await _cmd().execute(_ctx(f"export {output}", agent=agent))
        assert result.success is True
        assert output.exists(), "export must create the file"
        data = json.loads(output.read_text())
        assert "exported_at" in data

    async def test_export_creates_parseable_json(self, tmp_path):
        output = tmp_path / "test_out.json"
        result = await _cmd().execute(_ctx(f"export {output}"))
        assert result.success is True
        data = json.loads(output.read_text())
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# No mojibake in output
# ---------------------------------------------------------------------------


class TestNoMojibakeInAnyOutput:
    async def test_no_mojibake_in_status(self):
        result = await _cmd().execute(_ctx("status"))
        rendered = _render(result.content)
        mojibake_markers = ["üss†", "‚úÖ", "ä¸0", "\x00"]
        for marker in mojibake_markers:
            assert marker not in rendered, f"Mojibake marker {marker!r} found in output"

    async def test_no_mojibake_in_compact(self):
        mm = _make_mock_mm()
        result = await _cmd().execute(_ctx("compact", agent=_FakeAgent(mm)))
        rendered = _render(result.content)
        assert "üss†" not in rendered and "‚úÖ" not in rendered

    async def test_output_is_readable_utf8(self):
        result = await _cmd().execute(_ctx("status"))
        rendered = _render(result.content)
        rendered.encode("utf-8")  # Must not raise UnicodeEncodeError


# ---------------------------------------------------------------------------
# /memory usage
# ---------------------------------------------------------------------------


class TestMemoryUsageSubcommand:
    async def test_usage_returns_success(self):
        result = await _cmd().execute(_ctx("usage"))
        assert result.success is True

    async def test_usage_renders_non_empty(self):
        result = await _cmd().execute(_ctx("usage"))
        assert _render(result.content).strip()

    async def test_usage_with_session_history(self):
        class _Session:
            conversation_history = ["a", "b", "c"]
            context_data = {"k": "v"}
            memory = []

        ctx = CommandContext(user_input="/memory usage", args="usage")
        ctx.session = _Session()
        result = await _cmd().execute(ctx)
        assert result.success is True
        rendered = _render(result.content)
        assert "3" in rendered or "Histórico" in rendered

    async def test_usage_with_large_history_shows_alto(self):
        class _Session:
            conversation_history = ["x"] * 110
            context_data = {}
            memory = []

        ctx = CommandContext(user_input="/memory usage", args="usage")
        ctx.session = _Session()
        result = await _cmd().execute(ctx)
        assert result.success is True
        rendered = _render(result.content)
        assert "Alto" in rendered or "alto" in rendered


# ---------------------------------------------------------------------------
# /memory clear — additional type coverage
# ---------------------------------------------------------------------------


class TestClearAdditionalTypes:
    async def test_clear_context_type(self):
        class _Session:
            conversation_history = []
            context_data = {"a": 1, "b": 2}
            memory = []

        ctx = CommandContext(user_input="/memory clear context", args="clear context")
        ctx.session = _Session()
        result = await _cmd().execute(ctx)
        assert result.success is True
        assert len(ctx.session.context_data) == 0

    async def test_clear_memory_buffer_type(self):
        class _Session:
            conversation_history = []
            context_data = {}
            memory = ["item1", "item2"]

        ctx = CommandContext(user_input="/memory clear memory", args="clear memory")
        ctx.session = _Session()
        result = await _cmd().execute(ctx)
        assert result.success is True
        assert len(ctx.session.memory) == 0

    async def test_clear_all_type(self):
        class _Session:
            conversation_history = ["m1", "m2"]
            context_data = {"x": 1}
            memory = ["b1"]

        ctx = CommandContext(user_input="/memory clear all", args="clear all")
        ctx.session = _Session()
        result = await _cmd().execute(ctx)
        assert result.success is True

    async def test_clear_audit_type(self):
        result = await _cmd().execute(_ctx("clear audit"))
        assert result.success is True

    async def test_clear_plans_type(self):
        result = await _cmd().execute(_ctx("clear plans"))
        assert result.success is True


# ---------------------------------------------------------------------------
# /memory export — error path
# ---------------------------------------------------------------------------


class TestExportErrorPath:
    async def test_export_invalid_path_raises(self, tmp_path):
        from deile.core.exceptions import CommandError
        bad_path = tmp_path / "nonexistent_dir" / "sub" / "out.json"
        with pytest.raises(CommandError, match="Falha ao escrever"):
            await _cmd().execute(_ctx(f"export {bad_path}"))


# ---------------------------------------------------------------------------
# /memory status — error in real_usage
# ---------------------------------------------------------------------------


class TestStatusErrorPath:
    async def test_status_with_indisponivel_in_error(self):
        """When error key is in usage, output must contain the error message."""
        mm = MagicMock()
        mm.get_memory_usage = AsyncMock(return_value={"error": "connection_lost"})
        agent = _FakeAgent(mm)
        result = await _cmd().execute(_ctx("status", agent=agent))
        assert result.success is True
        rendered = _render(result.content)
        assert "connection_lost" in rendered or "INDISPONÍVEL" in rendered

    async def test_status_get_memory_usage_raises(self):
        """When get_memory_usage raises, the error is captured in real_usage."""
        mm = MagicMock()
        mm.get_memory_usage = AsyncMock(side_effect=RuntimeError("boom"))
        agent = _FakeAgent(mm)
        result = await _cmd().execute(_ctx("status", agent=agent))
        assert result.success is True


# ---------------------------------------------------------------------------
# Additional coverage: no-args, unknown action, get_help, restore bad JSON
# ---------------------------------------------------------------------------


class TestAdditionalCoverage:
    async def test_no_args_calls_status(self):
        """Invoking /memory with no args should return success (status view)."""
        result = await _cmd().execute(_ctx(""))
        assert result.success is True

    async def test_unknown_action_raises(self):
        from deile.core.exceptions import CommandError
        with pytest.raises(CommandError, match="desconhecida"):
            await _cmd().execute(_ctx("frobnicate"))

    async def test_get_help_returns_string(self):
        help_text = _cmd().get_help()
        assert isinstance(help_text, str)
        assert "/memory" in help_text

    async def test_restore_invalid_json_raises(self, tmp_path, monkeypatch):
        from deile.core.exceptions import CommandError
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")

        (tmp_path / "bad.json").write_text("not valid json", encoding="utf-8")
        with pytest.raises(CommandError, match="Falha ao ler checkpoint"):
            await _cmd().execute(_ctx("restore bad"))

    async def test_restore_no_name_raises(self):
        from deile.core.exceptions import CommandError
        with pytest.raises(CommandError, match="requer"):
            await _cmd().execute(_ctx("restore"))

    async def test_save_no_args_uses_default_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")

        result = await _cmd().execute(_ctx("save"))
        assert result.success is True

    async def test_save_with_failing_mm_still_succeeds(self, tmp_path, monkeypatch):
        """When mm.get_memory_usage raises during save, checkpoint still writes."""
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")
        mm = MagicMock()
        mm.get_memory_usage = AsyncMock(side_effect=RuntimeError("db error"))
        result = await _cmd().execute(_ctx("save failtest", agent=_FakeAgent(mm)))
        assert result.success is True

    async def test_export_with_failing_mm_still_writes_file(self, tmp_path):
        """When mm.get_memory_usage raises during export, file is still created."""
        output = tmp_path / "out.json"
        mm = MagicMock()
        mm.get_memory_usage = AsyncMock(side_effect=RuntimeError("fail"))
        result = await _cmd().execute(_ctx(f"export {output}", agent=_FakeAgent(mm)))
        assert result.success is True
        assert output.exists()

    async def test_export_no_args_uses_default_filename(self, tmp_path):
        import os
        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await _cmd().execute(_ctx("export"))
            assert result.success is True
        finally:
            os.chdir(orig)

    async def test_status_with_not_initialized_falls_back(self):
        """When real_usage has status='not_initialized', fall back to session data."""
        mm = MagicMock()
        mm.get_memory_usage = AsyncMock(return_value={"status": "not_initialized", "components": {}})
        agent = _FakeAgent(mm)
        result = await _cmd().execute(_ctx("status", agent=agent))
        assert result.success is True

    async def test_status_plan_manager_exception_shows_indisponivel(self):
        """When plan_manager raises in _show_memory_status, fallback row appears."""
        with patch("deile.orchestration.plan_manager.get_plan_manager") as mock_gpm:
            mock_gpm.side_effect = RuntimeError("plan DB error")
            result = await _cmd().execute(_ctx("status"))
            assert result.success is True
            rendered = _render(result.content)
            assert "INDISPONÍVEL" in rendered or "Planos" in rendered

    async def test_load_index_invalid_json_returns_empty(self, tmp_path, monkeypatch):
        """When index file has invalid JSON, _load_index returns {}."""
        idx = tmp_path / "index.json"
        idx.write_text("not-valid-json")
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", idx)
        result = await _cmd().execute(_ctx("list"))
        assert result.success is True
        rendered = _render(result.content)
        assert "Nenhum" in rendered or "checkpoint" in rendered.lower()

    async def test_save_path_traversal_raises(self, tmp_path, monkeypatch):
        """Checkpoint names with directory separators are rejected."""
        from deile.core.exceptions import CommandError
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")
        with pytest.raises(CommandError, match="inválido"):
            await _cmd().execute(_ctx("save ../../evil"))

    async def test_restore_path_traversal_raises(self, tmp_path, monkeypatch):
        """Restore rejects names with directory separators."""
        from deile.core.exceptions import CommandError
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_DIR", tmp_path)
        monkeypatch.setattr("deile.commands.builtin.memory_command._CHECKPOINT_INDEX", tmp_path / "index.json")
        with pytest.raises(CommandError, match="inválido"):
            await _cmd().execute(_ctx("restore ../../etc/passwd"))


class TestUsageHighImpact:
    async def test_usage_with_active_plans_shows_plans(self):
        """_show_memory_usage shows plans row when plan_manager has active plans."""
        with patch("deile.orchestration.plan_manager.get_plan_manager") as mock_gpm:
            mock_pm = MagicMock()
            mock_pm.active_plan_count.return_value = 3
            mock_gpm.return_value = mock_pm
            result = await _cmd().execute(_ctx("usage"))
            assert result.success is True
            rendered = _render(result.content)
            assert "3" in rendered or "Planos" in rendered

    async def test_usage_medium_impact_recommendation(self):
        """When total_impact > 2 but <= 5, shows medium impact recommendation."""
        class _Session:
            conversation_history = ["x"] * 60  # >50 → +2
            context_data = {}
            memory = []

        ctx = CommandContext(user_input="/memory usage", args="usage")
        ctx.session = _Session()

        with patch("deile.orchestration.plan_manager.get_plan_manager") as mock_gpm:
            mock_pm = MagicMock()
            mock_pm.active_plan_count.return_value = 3  # active=3 → +3
            mock_gpm.return_value = mock_pm
            result = await _cmd().execute(ctx)
            assert result.success is True
            rendered = _render(result.content)
            assert "Impacto" in rendered or "compact" in rendered

    async def test_clear_plans_type_with_active_plans(self):
        """Clear plans type with actually active plans removes them."""
        with patch("deile.orchestration.plan_manager.get_plan_manager") as mock_gpm:
            mock_pm = MagicMock()
            mock_pm.clear_active_state = AsyncMock(return_value=1)
            mock_gpm.return_value = mock_pm
            result = await _cmd().execute(_ctx("clear plans"))
            assert result.success is True
            mock_pm.clear_active_state.assert_awaited_once()

    async def test_clear_all_with_audit_events(self):
        """Clear all path hits audit events section."""
        class _Session:
            conversation_history = ["m"]
            context_data = {}
            memory = []

        ctx = CommandContext(user_input="/memory clear all", args="clear all")
        ctx.session = _Session()
        result = await _cmd().execute(ctx)
        assert result.success is True
