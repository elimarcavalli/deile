"""Testes para `deile/commands/builtin/_shared.py`.

Módulo crítico compartilhado pelos comandos builtin — qualquer
regressão aqui afeta `permissions`, `status`, `compact`, `memory`,
`apply`, `cost`, `version`, `welcome`, `export`, `patch`, `context`.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import patch

from rich.panel import Panel

from deile.commands.builtin._shared import (FLAG_DESCRICOES_PTBR,
                                            PROJECT_LINKS, colored_panel,
                                            emit_audit_event, error_panel,
                                            export_timestamp,
                                            get_memory_manager, split_args,
                                            success_panel, warning_panel)

# ---------------------------------------------------------------------------
# split_args
# ---------------------------------------------------------------------------


class TestSplitArgs:
    def test_none_args_returns_empty_list(self):
        ctx = SimpleNamespace(args=None)
        assert split_args(ctx) == []

    def test_empty_args_returns_empty_list(self):
        ctx = SimpleNamespace(args="")
        assert split_args(ctx) == []

    def test_whitespace_only_returns_empty_list(self):
        ctx = SimpleNamespace(args="   \t  ")
        assert split_args(ctx) == []

    def test_simple_words(self):
        ctx = SimpleNamespace(args="foo bar baz")
        assert split_args(ctx) == ["foo", "bar", "baz"]

    def test_collapses_whitespace(self):
        ctx = SimpleNamespace(args="  foo   bar  ")
        assert split_args(ctx) == ["foo", "bar"]

    def test_missing_args_attribute_returns_empty(self):
        ctx = SimpleNamespace()  # no `args` attribute
        assert split_args(ctx) == []


# ---------------------------------------------------------------------------
# Panel helpers
# ---------------------------------------------------------------------------


class TestPanelHelpers:
    def test_colored_panel_returns_panel(self):
        panel = colored_panel("hello", "Title", "red")
        assert isinstance(panel, Panel)
        assert panel.border_style == "red"
        assert panel.title == "Title"

    def test_error_panel_default_title_is_erro(self):
        panel = error_panel("falha")
        assert panel.border_style == "red"
        assert panel.title == "Erro"

    def test_error_panel_explicit_title(self):
        panel = error_panel("falha", title="Custom")
        assert panel.title == "Custom"

    def test_warning_panel_default_title(self):
        panel = warning_panel("cuidado")
        assert panel.border_style == "yellow"
        assert panel.title == "Aviso"

    def test_success_panel_default_title(self):
        panel = success_panel("ok")
        assert panel.border_style == "green"
        assert panel.title == "Sucesso"

    def test_panel_accepts_none_title(self):
        panel = colored_panel("msg", None, "blue")
        assert panel.title is None


# ---------------------------------------------------------------------------
# export_timestamp
# ---------------------------------------------------------------------------


class TestExportTimestamp:
    def test_format_is_yyyymmdd_hhmmss(self):
        ts = export_timestamp()
        assert re.fullmatch(r"\d{8}_\d{6}", ts), f"unexpected format: {ts}"

    def test_returns_string(self):
        assert isinstance(export_timestamp(), str)


# ---------------------------------------------------------------------------
# emit_audit_event
# ---------------------------------------------------------------------------


class TestEmitAuditEvent:
    def test_happy_path_calls_log_event(self):
        called = {}

        class _FakeLogger:
            def log_event(self, **kw):
                called.update(kw)

        with patch(
            "deile.security.audit_logger.get_audit_logger", return_value=_FakeLogger()
        ):
            emit_audit_event(
                event_type="EVT",
                severity="INFO",
                resource="/x",
                action="run",
                details={"a": 1},
            )

        assert called["event_type"] == "EVT"
        assert called["severity"] == "INFO"
        assert called["resource"] == "/x"
        assert called["action"] == "run"
        assert called["actor"] == "user"
        assert called["result"] == "initiated"
        assert called["details"] == {"a": 1}

    def test_swallows_logger_exception_silently(self, caplog):
        class _BoomLogger:
            def log_event(self, **kw):
                raise RuntimeError("boom")

        with patch(
            "deile.security.audit_logger.get_audit_logger", return_value=_BoomLogger()
        ):
            with caplog.at_level("DEBUG"):
                # Must NOT raise
                emit_audit_event(
                    event_type="EVT",
                    severity="INFO",
                    resource="/x",
                    action="run",
                )
        # Failure was logged at DEBUG (per pillar 03 §6 fix)
        assert any("emit_audit_event falhou" in rec.message for rec in caplog.records)

    def test_swallows_import_error_silently(self, caplog):
        with patch(
            "deile.security.audit_logger.get_audit_logger",
            side_effect=ImportError("module gone"),
        ):
            with caplog.at_level("DEBUG"):
                emit_audit_event(
                    event_type="EVT",
                    severity="INFO",
                    resource="/x",
                    action="run",
                )
        assert any("emit_audit_event falhou" in rec.message for rec in caplog.records)

    def test_default_details_is_empty_dict(self):
        captured = {}

        class _Recorder:
            def log_event(self, **kw):
                captured.update(kw)

        with patch(
            "deile.security.audit_logger.get_audit_logger", return_value=_Recorder()
        ):
            emit_audit_event(
                event_type="EVT", severity="INFO", resource="/x", action="run"
            )
        assert captured["details"] == {}


# ---------------------------------------------------------------------------
# get_memory_manager
# ---------------------------------------------------------------------------


class TestGetMemoryManager:
    def test_no_agent_returns_none(self):
        ctx = SimpleNamespace(agent=None)
        assert get_memory_manager(ctx) is None

    def test_missing_agent_attribute_returns_none(self):
        ctx = SimpleNamespace()
        assert get_memory_manager(ctx) is None

    def test_agent_without_memory_manager_returns_none(self):
        ctx = SimpleNamespace(agent=SimpleNamespace())
        assert get_memory_manager(ctx) is None

    def test_returns_agent_memory_manager_when_present(self):
        sentinel = object()
        ctx = SimpleNamespace(agent=SimpleNamespace(memory_manager=sentinel))
        assert get_memory_manager(ctx) is sentinel


# ---------------------------------------------------------------------------
# Constant maps
# ---------------------------------------------------------------------------


class TestConstants:
    def test_project_links_keys(self):
        assert set(PROJECT_LINKS) == {
            "Repositório",
            "Documentação",
            "Licença",
            "Issues",
        }

    def test_project_links_values_are_strings(self):
        assert all(isinstance(v, str) for v in PROJECT_LINKS.values())

    def test_flag_descricoes_keys_are_lowercase_snake(self):
        for k in FLAG_DESCRICOES_PTBR:
            assert k == k.lower()

    def test_flag_descricoes_covers_active_features(self):
        import deile.__version__ as version_mod
        active = [k for k, v in version_mod.FEATURES.items() if v]
        for flag in active:
            assert flag in FLAG_DESCRICOES_PTBR
