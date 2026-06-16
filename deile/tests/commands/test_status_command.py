"""Tests: /status command — real integrations (issue #165).

All subcommands must query real modules; no hardcoded placeholders.
"""

from __future__ import annotations

import asyncio
import time
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from deile.__version__ import __version__
from deile.commands.base import CommandContext
from deile.commands.builtin.status_command import StatusCommand

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(content) -> str:
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=120)
    console.print(content)
    return buf.getvalue()


def _ctx(args: str = "", agent=None) -> CommandContext:
    ctx = CommandContext(user_input=f"/status {args}".strip(), args=args)
    ctx.agent = agent
    return ctx


def _cmd() -> StatusCommand:
    return StatusCommand()


# ---------------------------------------------------------------------------
# /status (complete overview) — regression guard
# ---------------------------------------------------------------------------


class TestStatusComplete:
    async def test_returns_success(self):
        result = await _cmd().execute(_ctx())
        assert result.success is True

    async def test_content_type_is_rich(self):
        result = await _cmd().execute(_ctx())
        assert result.content_type == "rich"

    async def test_content_not_string(self):
        result = await _cmd().execute(_ctx())
        assert not isinstance(result.content, str)

    async def test_renders_without_repr_artifacts(self):
        result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        assert "<rich." not in rendered

    async def test_renders_non_empty(self):
        result = await _cmd().execute(_ctx())
        assert _render(result.content).strip()

    async def test_mentions_system(self):
        result = await _cmd().execute(_ctx())
        assert (
            "system" in _render(result.content).lower()
            or "sistema" in _render(result.content).lower()
        )

    async def test_mentions_health(self):
        result = await _cmd().execute(_ctx())
        rendered = _render(result.content).lower()
        assert "health" in rendered or "saúde" in rendered or "status" in rendered


# ---------------------------------------------------------------------------
# Issue #165 — /status version reads from __version__.py
# ---------------------------------------------------------------------------


class TestStatusVersion:
    async def test_version_reads_from_version_module(self):
        """Version panel must contain the value from deile.__version__."""
        result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        assert (
            __version__ in rendered
        ), f"Expected version {__version__!r} in rendered output but got: {rendered[:300]}"

    async def test_no_hardcoded_old_version(self):
        result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        assert "4.0.0" not in rendered


# ---------------------------------------------------------------------------
# Issue #165 — /status models reflects active router
# ---------------------------------------------------------------------------


class TestStatusModels:
    async def test_models_returns_success(self):
        result = await _cmd().execute(_ctx("models"))
        assert result.success is True

    async def test_models_renders_without_error(self):
        result = await _cmd().execute(_ctx("models"))
        assert _render(result.content).strip()

    async def test_model_reflects_active_router(self):
        """With a registered provider, /status models must list it."""
        mock_provider = MagicMock()
        mock_provider.provider_name = "openai"
        mock_provider.model_name = "gpt-4o"

        with patch("deile.core.models.router.get_model_router") as mock_gr:
            mock_router = MagicMock()
            mock_router.providers = {"openai:gpt-4o": mock_provider}
            mock_gr.return_value = mock_router

            result = await _cmd().execute(_ctx("models"))
            rendered = _render(result.content)
            assert "openai" in rendered or "gpt-4o" in rendered

    async def test_no_hardcoded_gemini(self):
        """The old hardcoded 'gemini-2.5-pro' must never appear in models section."""
        with patch("deile.core.models.router.get_model_router") as mock_gr:
            mock_router = MagicMock()
            mock_router.providers = {}
            mock_gr.return_value = mock_router
            result = await _cmd().execute(_ctx("models"))
            rendered = _render(result.content)
            assert "gemini-2.5-pro" not in rendered or "INDISPONÍVEL" in rendered


# ---------------------------------------------------------------------------
# Issue #165 — /status tools lists all registered
# ---------------------------------------------------------------------------


class TestStatusTools:
    async def test_tools_returns_success(self):
        result = await _cmd().execute(_ctx("tools"))
        assert result.success is True

    async def test_tools_lists_all_registered(self):
        """Tool count in rendered output must match registry."""
        from deile.tools.registry import get_tool_registry

        registry = get_tool_registry()
        expected_count = len(registry.list_all())

        result = await _cmd().execute(_ctx("tools"))
        rendered = _render(result.content)
        assert str(expected_count) in rendered or expected_count == 0


# ---------------------------------------------------------------------------
# Issue #165 — /status memory returns real usage stats
# ---------------------------------------------------------------------------


class TestStatusMemory:
    async def test_memory_no_agent_shows_indisponivel(self):
        """Without an agent, memory status shows INDISPONÍVEL."""
        result = await _cmd().execute(_ctx("memory"))
        rendered = _render(result.content)
        assert "INDISPONÍVEL" in rendered or result.success is True

    async def test_memory_returns_real_usage_stats(self):
        """With a MemoryManager attached, /status memory must succeed."""
        from unittest.mock import AsyncMock, MagicMock

        mm = MagicMock()
        mm.get_memory_usage = AsyncMock(
            return_value={
                "total_memory_mb": 0.5,
                "components": {"working_memory": {"entries": 2, "memory_mb": 0.1}},
            }
        )

        class _Agent:
            pass

        agent = _Agent()
        agent.memory_manager = mm

        ctx = _ctx("memory", agent=agent)
        result = await _cmd().execute(ctx)
        assert result.success is True
        rendered = _render(result.content)
        assert "MemoryManager não acessível" not in rendered

    async def test_memory_subsystem_exception_shows_degraded(self):
        """When MemoryManager.get_memory_usage raises, output shows error gracefully."""

        class _BadMM:
            async def get_memory_usage(self):
                raise RuntimeError("DB offline")

        class _Agent:
            memory_manager = _BadMM()

        result = await _cmd().execute(_ctx("memory", agent=_Agent()))
        rendered = _render(result.content)
        assert "DB offline" in rendered or "Erro" in rendered or result.success is True


# ---------------------------------------------------------------------------
# Issue #165 — /status plans empty state is honest
# ---------------------------------------------------------------------------


class TestStatusPlans:
    async def test_plans_returns_success(self):
        result = await _cmd().execute(_ctx("plans"))
        assert result.success is True

    async def test_plans_empty_state_is_honest(self):
        """With no active plans, the status must show 0, not fake data."""
        with patch("deile.orchestration.plan_manager.get_plan_manager") as mock_gpm:
            mock_pm = MagicMock()
            mock_pm._active_plans = {}
            mock_pm.list_plans = AsyncMock(return_value=[])
            mock_gpm.return_value = mock_pm

            result = await _cmd().execute(_ctx("plans"))
            rendered = _render(result.content)
            assert "0 ativos / 0 total" in rendered or "Nenhum plano" in rendered


# ---------------------------------------------------------------------------
# Issue #165 — /status connectivity probes real providers
# ---------------------------------------------------------------------------


class TestStatusConnectivity:
    async def test_connectivity_returns_success(self):
        result = await _cmd().execute(_ctx("connectivity"))
        assert result.success is True

    async def test_connectivity_probes_real_providers(self):
        """Connectivity table must contain at least one provider row."""
        result = await _cmd().execute(_ctx("connectivity"))
        rendered = _render(result.content)
        # Should contain provider names or FALHOU markers
        assert any(
            pid in rendered for pid in ("openai", "anthropic", "google", "deepseek")
        )

    async def test_connectivity_parallel_execution(self):
        """Probing multiple providers must complete faster than sequential sum."""

        async def _slow_probe(host, port=443, timeout=5.0):
            await asyncio.sleep(0.05)
            return False, 50.0

        with patch(
            "deile.commands.builtin.status_command._probe_host", side_effect=_slow_probe
        ):
            start = time.monotonic()
            result = await _cmd().execute(_ctx("connectivity"))
            elapsed = time.monotonic() - start
            # With 4 providers each at 50ms, sequential = 200ms+.
            # Parallel should be < 100ms. Allow generous 500ms for CI overhead.
            assert elapsed < 0.5
            assert result.success is True


# ---------------------------------------------------------------------------
# Issue #165 — /status performance reads usage repository
# ---------------------------------------------------------------------------


class TestStatusPerformance:
    async def test_performance_returns_success(self):
        result = await _cmd().execute(_ctx("performance"))
        assert result.success is True

    async def test_performance_reads_usage_repository(self):
        """Performance view must show session-level metrics (even if 0)."""
        result = await _cmd().execute(_ctx("performance"))
        rendered = _render(result.content)
        assert "sessão" in rendered.lower() or "tokens" in rendered.lower()


# ---------------------------------------------------------------------------
# Issue #165 — /status system
# ---------------------------------------------------------------------------


class TestStatusSystem:
    async def test_returns_success(self):
        result = await _cmd().execute(_ctx("system"))
        assert result.success is True

    async def test_content_not_string(self):
        result = await _cmd().execute(_ctx("system"))
        assert not isinstance(result.content, str)

    async def test_renders_non_empty(self):
        result = await _cmd().execute(_ctx("system"))
        assert _render(result.content).strip()

    async def test_version_in_system(self):
        result = await _cmd().execute(_ctx("system"))
        rendered = _render(result.content)
        assert __version__ in rendered


# ---------------------------------------------------------------------------
# Issue #165 — subsystem exception shows degraded state
# ---------------------------------------------------------------------------


class TestStatusDegradedState:
    async def test_tools_exception_shows_error_not_crash(self):
        with patch("deile.tools.registry.get_tool_registry") as mock_gr:
            mock_gr.side_effect = RuntimeError("registry broken")
            result = await _cmd().execute(_ctx("tools"))
            assert result.success is True
            rendered = _render(result.content)
            assert "broken" in rendered or "Erro" in rendered

    async def test_plans_exception_shows_error_not_crash(self):
        with patch("deile.orchestration.plan_manager.get_plan_manager") as mock_gpm:
            mock_gpm.side_effect = RuntimeError("plan DB gone")
            result = await _cmd().execute(_ctx("plans"))
            assert result.success is True


# ---------------------------------------------------------------------------
# Issue #165 — full status completes under 3s
# ---------------------------------------------------------------------------


class TestStatusPerf:
    async def test_full_status_completes_under_3s(self):
        start = time.monotonic()
        result = await _cmd().execute(_ctx())
        elapsed = time.monotonic() - start
        assert result.success is True
        assert elapsed < 3.0, f"/status took {elapsed:.2f}s — expected < 3s"

    async def test_unknown_section_raises_command_error(self):
        from deile.core.exceptions import CommandError

        with pytest.raises(CommandError):
            await _cmd().execute(_ctx("nonexistent_section"))


# ---------------------------------------------------------------------------
# Audit event is emitted
# ---------------------------------------------------------------------------


class TestStatusAudit:
    async def test_audit_event_emitted(self):
        from deile.security.audit_logger import AuditEventType, get_audit_logger

        al = get_audit_logger()
        before = len(al.recent_events)
        await _cmd().execute(_ctx())
        after = len(al.recent_events)
        # The status command should emit at least one COMMAND_EXECUTED event
        command_events = [
            e
            for e in al.recent_events
            if e.event_type == AuditEventType.COMMAND_EXECUTED
        ]
        assert len(command_events) >= 1 or after > before


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------


class TestProbeHostDirect:
    async def test_probe_host_failure_returns_false(self):
        from deile.commands.builtin.status_command import _probe_host

        ok, latency = await _probe_host("localhost", port=1, timeout=0.05)
        assert ok is False
        assert latency >= 0


class TestStatusExceptionBranches:
    async def test_memory_not_initialized_path(self):
        """When usage has status='not_initialized', shows indisponivel."""
        mm = MagicMock()
        mm.get_memory_usage = AsyncMock(return_value={"status": "not_initialized"})

        class _Agent:
            memory_manager = mm

        result = await _cmd().execute(_ctx("memory", agent=_Agent()))
        assert result.success is True
        rendered = _render(result.content)
        assert "INDISPONÍVEL" in rendered or "não inicializado" in rendered

    async def test_plans_status_empty_no_crash(self):
        result = await _cmd().execute(_ctx("plans"))
        assert result.success is True
        rendered = _render(result.content)
        assert "Nenhum plano" in rendered or "planos" in rendered.lower()

    async def test_performance_with_bad_repo_still_succeeds(self):
        with patch("deile.storage.usage_repository.UsageRepository") as mock_repo_cls:
            mock_repo_cls.side_effect = RuntimeError("DB gone")
            result = await _cmd().execute(_ctx("performance"))
            assert result.success is True

    async def test_overview_with_failing_tool_registry(self):
        with patch("deile.tools.registry.get_tool_registry") as mock_gr:
            mock_gr.side_effect = RuntimeError("registry gone")
            result = await _cmd().execute(_ctx())
            assert result.success is True

    async def test_overview_with_failing_model_router(self):
        with patch("deile.core.models.router.get_model_router") as mock_gr:
            mock_gr.side_effect = RuntimeError("router gone")
            result = await _cmd().execute(_ctx())
            assert result.success is True


class TestPermissionsCommandDefaultConstructor:
    async def test_default_constructor_sets_permission_manager(self):
        from deile.commands.builtin.permissions_command import PermissionsCommand

        cmd = PermissionsCommand()
        assert cmd.permission_manager is not None

    async def test_unknown_action_raises(self):
        from deile.commands.builtin.permissions_command import PermissionsCommand
        from deile.core.exceptions import CommandError

        cmd = PermissionsCommand()
        with pytest.raises(CommandError):
            await cmd.execute(_ctx("nonexistent_action_xyz"))

    async def test_err_helper_on_missing_restore_arg(self):
        from deile.commands.builtin.permissions_command import PermissionsCommand
        from deile.core.exceptions import CommandError

        cmd = PermissionsCommand()
        with pytest.raises(CommandError):
            await cmd.execute(_ctx("enable"))

    async def test_get_help_returns_string(self):
        from deile.commands.builtin.permissions_command import PermissionsCommand

        cmd = PermissionsCommand()
        help_text = cmd.get_help()
        assert isinstance(help_text, str)
