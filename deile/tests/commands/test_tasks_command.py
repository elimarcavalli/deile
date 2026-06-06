"""Tests for /tasks command (issue #416).

Covers:
- Pure formatting helpers (_fmt_age, _fmt_time, _build_output) — zero I/O.
- TasksCommand constructor (registration, aliases, category).
- TasksCommand.execute() degrading gracefully when the observability module
  is absent or when the pipeline-status pod is unreachable.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.commands.base import CommandContext
from deile.commands.builtin.tasks_command import (
    TasksCommand,
    _build_output,
    _fmt_age,
    _fmt_time,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(args: str = "") -> CommandContext:
    ctx = CommandContext(user_input=f"/tasks {args}".strip(), args=args)
    ctx.agent = None
    return ctx


def _iso_now(delta_minutes: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# _fmt_age
# ---------------------------------------------------------------------------


class TestFmtAge:
    def test_none_returns_empty(self):
        assert _fmt_age(None) == ""

    def test_empty_string_returns_empty(self):
        assert _fmt_age("") == ""

    def test_just_now(self):
        ts = _iso_now(delta_minutes=0)
        assert _fmt_age(ts) == "agora"

    def test_one_minute_ago(self):
        ts = _iso_now(delta_minutes=-1)
        assert _fmt_age(ts) == "há 1min"

    def test_many_minutes_ago(self):
        ts = _iso_now(delta_minutes=-42)
        assert _fmt_age(ts) == "há 42min"

    def test_z_suffix_accepted(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _fmt_age(ts)
        assert result in ("agora", "há 1min")

    def test_invalid_string_returned_truncated(self):
        result = _fmt_age("not-a-date")
        assert result == "not-a-date"

    def test_invalid_short_string_returned_as_is(self):
        result = _fmt_age("bad")
        assert result == "bad"


# ---------------------------------------------------------------------------
# _fmt_time
# ---------------------------------------------------------------------------


class TestFmtTime:
    def test_none_returns_dash(self):
        assert _fmt_time(None) == "—"

    def test_empty_returns_dash(self):
        assert _fmt_time("") == "—"

    def test_extracts_hhmmss(self):
        result = _fmt_time("2026-01-15T14:30:45+00:00")
        assert result == "14:30:45"

    def test_z_suffix_accepted(self):
        result = _fmt_time("2026-01-15T09:05:00Z")
        assert result == "09:05:00"

    def test_invalid_string_truncated_to_8(self):
        result = _fmt_time("ABCDEFGHIJ")
        assert result == "ABCDEFGH"

    def test_very_short_invalid_string_returned_as_is(self):
        result = _fmt_time("ABC")
        assert result == "ABC"


# ---------------------------------------------------------------------------
# _build_output
# ---------------------------------------------------------------------------


class TestBuildOutput:
    _STATUS = {
        "ticks_total": 7,
        "errors_total": 0,
        "last_tick_at": "2026-01-15T10:00:00Z",
        "next_tick_at": "2026-01-15T10:30:00Z",
        "pods_seen": ["deile-pipeline-abc"],
    }
    _LEDGER_EMPTY = {"ledger": {}}
    _LEDGER_ONE = {
        "ledger": {
            "issue:42": {
                "stage": "implement",
                "started_at": "2026-01-15T09:50:00Z",
                "worker": "deile-worker",
            }
        }
    }

    def test_header_contains_tick_number(self):
        out = _build_output(self._STATUS, self._LEDGER_EMPTY)
        assert "tick #7" in out

    def test_header_contains_pod_name(self):
        out = _build_output(self._STATUS, self._LEDGER_EMPTY)
        assert "deile-pipeline-abc" in out

    def test_empty_ledger_shows_nenhuma(self):
        out = _build_output(self._STATUS, self._LEDGER_EMPTY)
        assert "nenhuma tarefa ativa" in out

    def test_one_active_item_shown(self):
        out = _build_output(self._STATUS, self._LEDGER_ONE)
        assert "issue" in out
        assert "#42" in out
        assert "implement" in out
        assert "deile-worker" in out

    def test_verbose_shows_task_id_label(self):
        ledger = {
            "ledger": {
                "pr:10": {
                    "stage": "pr_review",
                    "task_id": "task-abc-123",
                    "session_id": "sess-xyz",
                    "attempt": 1,
                    "worker": "claude-worker",
                }
            }
        }
        out = _build_output(self._STATUS, ledger, verbose=True)
        assert "task_id=task-abc-123" in out
        assert "session_id=sess-xyz" in out

    def test_verbose_hint_absent_when_verbose_true(self):
        out = _build_output(self._STATUS, self._LEDGER_EMPTY, verbose=True)
        assert "--verbose" not in out

    def test_verbose_hint_present_when_not_verbose(self):
        out = _build_output(self._STATUS, self._LEDGER_EMPTY, verbose=False)
        assert "--verbose" in out

    def test_backlog_absent_by_default(self):
        out = _build_output(self._STATUS, self._LEDGER_EMPTY)
        assert "Backlog" not in out

    def test_backlog_section_shown_when_provided(self):
        backlog = {"backlog": [{"kind": "issue", "ref": "7"}, {"kind": "issue", "ref": "8"}]}
        out = _build_output(self._STATUS, self._LEDGER_EMPTY, backlog_data=backlog)
        assert "Backlog" in out
        assert "#7" in out

    def test_empty_backlog_shows_vazio(self):
        out = _build_output(self._STATUS, self._LEDGER_EMPTY, backlog_data={"backlog": []})
        assert "backlog vazio" in out

    def test_no_pods_shows_dash(self):
        status = dict(self._STATUS, pods_seen=[])
        out = _build_output(status, self._LEDGER_EMPTY)
        assert "—" in out

    def test_missing_pods_key_shows_dash(self):
        status = {k: v for k, v in self._STATUS.items() if k != "pods_seen"}
        out = _build_output(status, self._LEDGER_EMPTY)
        assert "—" in out

    def test_non_dict_ledger_treated_as_empty(self):
        out = _build_output(self._STATUS, {"ledger": "not-a-dict"})
        assert "nenhuma tarefa ativa" in out

    def test_non_dict_ledger_entry_skipped(self):
        ledger = {"ledger": {"issue:1": "bad-value"}}
        out = _build_output(self._STATUS, ledger)
        # Should not crash; active count shows 1 but entry is skipped in body
        assert "Em andamento" in out


# ---------------------------------------------------------------------------
# TasksCommand constructor
# ---------------------------------------------------------------------------


class TestTasksCommandInit:
    def test_name_is_tasks(self):
        cmd = TasksCommand()
        assert cmd.name == "tasks"

    def test_aliases_include_tarefas(self):
        cmd = TasksCommand()
        assert "tarefas" in cmd.aliases

    def test_category_is_orchestration(self):
        cmd = TasksCommand()
        assert cmd.category == "orchestration"

    def test_cli_flag_is_set(self):
        cmd = TasksCommand()
        assert cmd.cli_flag == "--tasks"


# ---------------------------------------------------------------------------
# TasksCommand.execute — import-error degradation
# ---------------------------------------------------------------------------


class TestTasksCommandExecuteMissingModule:
    """When the observability subpackage is absent, the command must surface
    a clear error instead of crashing with an unhandled ImportError."""

    async def test_returns_error_when_obs_module_missing(self, monkeypatch):
        fake_name = "deile.ui.panel.observability"
        real_module = sys.modules.pop(fake_name, None)

        class _Boom:
            def __getattr__(self, name):
                raise ImportError(f"forced: {name}")

        monkeypatch.setitem(sys.modules, fake_name, _Boom())
        try:
            result = await TasksCommand().execute(_ctx())
            assert result.success is False
            assert result.content is not None
            assert "observabilidade" in result.content.lower() or "módulo" in result.content.lower()
        finally:
            sys.modules.pop(fake_name, None)
            if real_module is not None:
                sys.modules[fake_name] = real_module


# ---------------------------------------------------------------------------
# TasksCommand.execute — network-error degradation
# ---------------------------------------------------------------------------


class TestTasksCommandExecuteNetworkErrors:
    """The command must return a helpful error when the pipeline-status pod
    is unreachable (connection refused / timeout → status_code == 0)."""

    # tasks_command does: `from ...ui.panel.observability import client as obs_client`
    # Python resolves this from sys.modules["deile.ui.panel.observability"].client, so
    # we must patch BOTH the submodule key AND set `.client` on the parent mock.
    _CLIENT_KEY = "deile.ui.panel.observability.client"
    _PARENT_KEY = "deile.ui.panel.observability"

    def _patch_ctx(self, status_reply, ledger_reply=None):
        """Return a patch.dict context manager that injects mock client."""
        from deile.ui.panel.observability import client as real_client

        ApiError = real_client.ApiError

        mock_pipeline = AsyncMock()
        mock_pipeline.get_status = AsyncMock(return_value=status_reply)
        mock_pipeline.get_ledger = AsyncMock(
            return_value=ledger_reply if ledger_reply is not None else {"ledger": {}}
        )

        mock_cli_instance = MagicMock()
        mock_cli_instance.pipeline = mock_pipeline

        mock_client_cls = MagicMock()
        mock_client_cls.from_endpoints.return_value = mock_cli_instance

        mock_client_module = MagicMock()
        mock_client_module.ClusterObservabilityClient = mock_client_cls
        mock_client_module.ApiError = ApiError

        mock_parent = MagicMock()
        mock_parent.client = mock_client_module

        return patch.dict(sys.modules, {
            self._CLIENT_KEY: mock_client_module,
            self._PARENT_KEY: mock_parent,
        })

    async def test_unreachable_pipeline_returns_error(self, monkeypatch):
        from deile.ui.panel.observability import client as real_client

        api_error = real_client.ApiError(status=0, message="connection refused")
        monkeypatch.setenv("DEILE_PIPELINE_STATUS_ENDPOINT", "http://fake:8768")
        monkeypatch.setenv("DEILE_CLAUDE_WORKER_ENDPOINT", "http://fake:8767")

        with self._patch_ctx(status_reply=api_error):
            result = await TasksCommand().execute(_ctx())

        assert result.success is False
        assert "inacessível" in (result.content or "") or "pipeline-status" in (result.content or "")

    async def test_http_401_returns_auth_error_message(self, monkeypatch):
        from deile.ui.panel.observability import client as real_client

        api_error = real_client.ApiError(status=401, message="Unauthorized")
        monkeypatch.setenv("DEILE_PIPELINE_STATUS_ENDPOINT", "http://fake:8768")
        monkeypatch.setenv("DEILE_CLAUDE_WORKER_ENDPOINT", "http://fake:8767")

        with self._patch_ctx(status_reply=api_error):
            result = await TasksCommand().execute(_ctx())

        assert result.success is False
        assert "401" in (result.content or "") or "autenticação" in (result.content or "").lower()

    async def test_successful_response_returns_success(self, monkeypatch):
        status_payload = {
            "ticks_total": 3,
            "errors_total": 0,
            "last_tick_at": "2026-01-15T10:00:00Z",
            "next_tick_at": "2026-01-15T10:30:00Z",
            "pods_seen": ["pod-a"],
        }
        ledger_payload = {"ledger": {}}

        monkeypatch.setenv("DEILE_PIPELINE_STATUS_ENDPOINT", "http://fake:8768")
        monkeypatch.setenv("DEILE_CLAUDE_WORKER_ENDPOINT", "http://fake:8767")

        with self._patch_ctx(status_reply=status_payload, ledger_reply=ledger_payload):
            result = await TasksCommand().execute(_ctx())

        assert result.success is True
        assert "tick #3" in (result.content or "")
