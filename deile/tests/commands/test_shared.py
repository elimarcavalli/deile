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

from deile.commands._sentinels import (POST_SWITCH_ACTION_KEY,
                                       SWITCH_SESSION_KEY)
from deile.commands.builtin._shared import (FLAG_DESCRICOES_PTBR,
                                            PROJECT_LINKS, _colored_panel,
                                            _resolve_patches_dir,
                                            emit_audit_event, error_panel,
                                            export_timestamp,
                                            get_memory_manager, indisponivel,
                                            split_args, success_panel,
                                            truncate_oneline, warning_panel)

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
        panel = _colored_panel("hello", "Title", "red")
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
        panel = _colored_panel("msg", None, "blue")
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


_AUDIT_PATCH = "deile.security.audit_logger.get_audit_logger"
_AUDIT_KWARGS = {"event_type": "EVT", "severity": "INFO", "resource": "/x", "action": "run"}


def _capturing_logger() -> tuple[dict, object]:
    """Return (captured dict, logger-instance) that records log_event kwargs."""
    captured: dict = {}

    class _Recorder:
        def log_event(self, **kw):
            captured.update(kw)

    return captured, _Recorder()


class TestEmitAuditEvent:
    def test_happy_path_calls_log_event(self):
        captured, logger = _capturing_logger()
        with patch(_AUDIT_PATCH, return_value=logger):
            emit_audit_event(**_AUDIT_KWARGS, details={"a": 1})

        assert captured["event_type"] == "EVT"
        assert captured["severity"] == "INFO"
        assert captured["resource"] == "/x"
        assert captured["action"] == "run"
        assert captured["actor"] == "user"
        assert captured["result"] == "initiated"
        assert captured["details"] == {"a": 1}

    def test_swallows_logger_exception_silently(self, caplog):
        class _BoomLogger:
            def log_event(self, **kw):
                raise RuntimeError("boom")

        with patch(_AUDIT_PATCH, return_value=_BoomLogger()):
            with caplog.at_level("DEBUG"):
                emit_audit_event(**_AUDIT_KWARGS)  # must NOT raise
        assert any("emit_audit_event falhou" in rec.message for rec in caplog.records)

    def test_swallows_import_error_silently(self, caplog):
        with patch(_AUDIT_PATCH, side_effect=ImportError("module gone")):
            with caplog.at_level("DEBUG"):
                emit_audit_event(**_AUDIT_KWARGS)
        assert any("emit_audit_event falhou" in rec.message for rec in caplog.records)

    def test_default_details_is_empty_dict(self):
        captured, logger = _capturing_logger()
        with patch(_AUDIT_PATCH, return_value=logger):
            emit_audit_event(**_AUDIT_KWARGS)
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


# ---------------------------------------------------------------------------
# _resolve_patches_dir
# ---------------------------------------------------------------------------


class TestResolvePatchesDir:
    def test_novo_default_aponta_para_deile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _resolve_patches_dir()
        assert result == tmp_path / ".deile" / "patches"

    def test_legado_preservado_quando_nao_vazio(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        legacy = tmp_path / "PATCHES"
        legacy.mkdir()
        (legacy / "fix.patch").touch()
        result = _resolve_patches_dir()
        assert result == legacy


# ---------------------------------------------------------------------------
# truncate_oneline
# ---------------------------------------------------------------------------


class TestTruncateOneline:
    def test_none_returns_empty(self):
        assert truncate_oneline(None, 10) == ""

    def test_falsy_list_returns_empty(self):
        assert truncate_oneline([], 10) == ""

    def test_empty_string_returns_empty(self):
        assert truncate_oneline("", 10) == ""

    def test_short_text_unchanged(self):
        assert truncate_oneline("hello", 10) == "hello"

    def test_exactly_max_chars_not_truncated(self):
        assert truncate_oneline("abcde", 5) == "abcde"

    def test_one_over_max_chars_truncated_with_ellipsis(self):
        result = truncate_oneline("abcdef", 5)
        assert result == "abcde…"

    def test_newlines_flattened_to_spaces(self):
        assert truncate_oneline("line1\nline2", 50) == "line1 line2"

    def test_surrounding_whitespace_stripped(self):
        assert truncate_oneline("  spaced  ", 50) == "spaced"

    def test_whitespace_only_returns_empty(self):
        # Truthy (skips the `if not text` guard) but collapses to "" via strip().
        assert truncate_oneline("   ", 10) == ""

    def test_non_string_coerced_via_str(self):
        assert truncate_oneline(12345, 50) == "12345"

    def test_non_string_coerced_then_truncated(self):
        assert truncate_oneline(123456, 3) == "123…"

    def test_max_chars_zero_truncates_everything(self):
        assert truncate_oneline("anything", 0) == "…"

    def test_whitespace_only_string_returns_empty(self):
        assert truncate_oneline("   ", 50) == ""


# ---------------------------------------------------------------------------
# indisponivel
# ---------------------------------------------------------------------------


class TestIndisponivel:
    def test_wraps_reason_in_marker(self):
        assert indisponivel("sem dados") == "[INDISPONÍVEL: sem dados]"

    def test_empty_reason(self):
        assert indisponivel("") == "[INDISPONÍVEL: ]"


# ---------------------------------------------------------------------------
# session-switch sentinels
# ---------------------------------------------------------------------------


class TestSentinels:
    def test_switch_session_key_literal(self):
        assert SWITCH_SESSION_KEY == "_switch_session"

    def test_post_switch_action_key_literal(self):
        assert POST_SWITCH_ACTION_KEY == "_post_switch_action"

    def test_cli_class_attributes_match_sentinels(self):
        """The CLI must source the sentinels from _sentinels — no drift."""
        from deile.cli import _DeileCLI
        assert _DeileCLI._SWITCH_SESSION_KEY == SWITCH_SESSION_KEY
        assert _DeileCLI._POST_SWITCH_ACTION_KEY == POST_SWITCH_ACTION_KEY
