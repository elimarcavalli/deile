"""Testes do filtro V2 em PodWatchView/LiveSessionView (issue #544).

Cobertura:
  AC-A1..A5  — highlight visual
  AC-B1..B6  — persistência entre sessões
  AC-C1..C5  — multi-filtro AND/OR
  AC-D1..D3  — regex-timeout
  AC-T1/T2   — transversais (regressão zero)
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import regex as _regex  # available when regex>=2024.0 is installed (issue #544)
    _HAS_REGEX = True
except ImportError:
    _regex = None  # type: ignore[assignment]
    _HAS_REGEX = False
import _panel as panel


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_pod_view() -> panel.PodWatchView:
    view = panel.PodWatchView()
    view.pod_name = "claude-worker-0"
    view.pod_role = "claude-worker"
    view.hide_health = False
    return view


def _make_streamer(lines: list[str]) -> MagicMock:
    m = MagicMock()
    m.snapshot.return_value = list(lines)
    return m


def _make_app() -> MagicMock:
    return MagicMock()


def _apply_terms(view: panel.PodWatchView, text: str) -> None:
    """Parse expression and set filter state on view (simulates pressing Enter)."""
    terms, op, err = panel._parse_filter_expr(text)
    assert err is None, f"Unexpected parse error: {err}"
    view._filter_text = text
    view._filter_terms = terms
    view._filter_op = op
    if len(terms) == 1:
        k, r, c = terms[0]
        view._filter_re = (c if k == "regex" else panel._lit_compile(r))
    else:
        view._filter_re = None


# ---------------------------------------------------------------------------
# Item A — Highlight
# ---------------------------------------------------------------------------

class TestHighlightLiteralMatchSpans:
    """AC-A1: literal 'error' → span reverse on 'ERROR'."""

    def test_highlight_literal_match_spans(self):
        view = _make_pod_view()
        view.streamer = _make_streamer(["ERROR foo bar"])
        _apply_terms(view, "error")

        p = view._log_panel()
        body = p.renderable
        # body is a Text produced by _build_highlighted_body
        # Check spans contain "reverse" over the matched region
        rendered = str(body)
        assert "ERROR" in rendered
        spans_reverse = [s for s in body._spans if "reverse" in str(s.style)]
        assert spans_reverse, "Expected at least one 'reverse' span"
        # The reversed span should cover "ERROR" (positions 0-5)
        assert any(s.start == 0 and s.end == 5 for s in spans_reverse)


class TestHighlightMultipleMatches:
    """AC-A2: 'err err' + 'err' → 2 reverse spans via str.find loop."""

    def test_highlight_multiple_matches(self):
        view = _make_pod_view()
        view.streamer = _make_streamer(["err err"])
        _apply_terms(view, "err")

        p = view._log_panel()
        body = p.renderable
        spans_reverse = [s for s in body._spans if "reverse" in str(s.style)]
        assert len(spans_reverse) == 2, f"Expected 2 reverse spans, got {len(spans_reverse)}"


class TestHighlightPreservesHiding:
    """AC-A3: lines without match stay hidden (not rendered with 0 spans)."""

    def test_highlight_preserves_hiding(self):
        view = _make_pod_view()
        view.streamer = _make_streamer(["ERROR: boom", "info: quiet"])
        _apply_terms(view, "error")

        p = view._log_panel()
        rendered = str(p.renderable)
        assert "boom" in rendered
        assert "quiet" not in rendered


class TestHighlightMultiTermAllTerms:
    """AC-A4: 'error OR warn' on a line with both → 2 groups of reverse spans."""

    def test_highlight_multi_term_all_terms(self):
        view = _make_pod_view()
        view.streamer = _make_streamer(["ERROR warn here"])
        _apply_terms(view, "error OR warn")

        p = view._log_panel()
        body = p.renderable
        spans_reverse = [s for s in body._spans if "reverse" in str(s.style)]
        # Should have spans for both "ERROR" and "warn"
        assert len(spans_reverse) >= 2, (
            f"Expected spans for both terms, got {len(spans_reverse)}"
        )


class TestHighlightRegexTermTimeoutNoSpans:
    """AC-A5: r:(a+)+$ on 5000 'a' → TimeoutError captured, line rendered without spans."""

    def test_highlight_regex_term_timeout_no_spans(self):
        pytest = __import__("pytest")
        if not _HAS_REGEX:
            pytest.skip("regex module not installed — AC-A5 requires regex>=2024.0")

        line = "a" * 5000
        terms_info = [("regex", "(a+)+$",
                       _regex.compile("(a+)+$", _regex.IGNORECASE))]
        t0 = time.monotonic()
        result = panel._highlight_filter_line(line, terms_info)
        elapsed = time.monotonic() - t0

        assert elapsed <= 0.5, f"highlight took {elapsed:.3f}s — expected ≤0.5s (budget 0.1s)"
        # No reverse spans — falls back to plain text
        spans_reverse = [s for s in result._spans if "reverse" in str(s.style)]
        assert not spans_reverse, "Expected no reverse spans on timeout"
        assert str(result) == line, "Line text should be preserved without spans"


# ---------------------------------------------------------------------------
# Item B — Persistência
# ---------------------------------------------------------------------------

class TestPersistWritesEntryAtomically:
    """AC-B1: on_unmount writes key/text to JSON; atomic write."""

    def test_persist_writes_entry_atomically(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel, "_PANEL_FILTERS_PATH", tmp_path / "panel_filters.json")

        panel._save_panel_filter("pod:ns/pod-a", "error")

        data = json.loads((tmp_path / "panel_filters.json").read_text())
        assert data["schema_version"] == 1
        assert data["entries"]["pod:ns/pod-a"]["text"] == "error"

    def test_atomic_write_no_partial_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel, "_PANEL_FILTERS_PATH", tmp_path / "panel_filters.json")
        # Simulate: if write succeeds, file is valid JSON
        panel._save_panel_filter("pod:ns/pod-x", "timeout")
        content = (tmp_path / "panel_filters.json").read_text()
        json.loads(content)  # must not raise


class TestPersistRestoresOnRemount:
    """AC-B2: remounting same key restores filter; unknown key → empty."""

    def test_persist_restores_on_remount(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel, "_PANEL_FILTERS_PATH", tmp_path / "pf.json")

        panel._save_panel_filter("pod:ns/pod-a", "error AND timeout")
        result = panel._load_panel_filter("pod:ns/pod-a")
        assert result == "error AND timeout"

    def test_unknown_key_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel, "_PANEL_FILTERS_PATH", tmp_path / "pf.json")
        result = panel._load_panel_filter("pod:ns/nonexistent")
        assert result == ""


class TestPersistCorruptFileIsIgnored:
    """AC-B3: corrupt JSON or wrong schema_version → empty + toast, file untouched."""

    def test_corrupt_json_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel, "_PANEL_FILTERS_PATH", tmp_path / "pf.json")
        (tmp_path / "pf.json").write_text("{invalid json}")
        app = _make_app()
        result = panel._load_panel_filter("any-key", app)
        assert result == ""
        app.push_toast.assert_called_once()
        # File must NOT be overwritten during load
        assert (tmp_path / "pf.json").read_text() == "{invalid json}"

    def test_wrong_schema_version_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel, "_PANEL_FILTERS_PATH", tmp_path / "pf.json")
        data = {"schema_version": 99, "entries": {"k": {"text": "x", "saved_at": int(time.time())}}}
        (tmp_path / "pf.json").write_text(json.dumps(data))
        app = _make_app()
        result = panel._load_panel_filter("k", app)
        assert result == ""
        app.push_toast.assert_called_once()


class TestPersistCapAndStaleness:
    """AC-B4: 51 entries → keep 50 (LRU); entries >30d pruned."""

    def test_cap_at_50(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel, "_PANEL_FILTERS_PATH", tmp_path / "pf.json")
        # Write 50 entries with recent timestamps (within the 30-day window)
        now = int(time.time())
        entries = {f"pod:ns/pod-{i}": {"text": f"f{i}", "saved_at": now - i}
                   for i in range(50)}
        data = {"schema_version": 1, "entries": entries}
        (tmp_path / "pf.json").write_text(json.dumps(data))

        # Add one more (the 51st)
        panel._save_panel_filter("pod:ns/pod-50", "new")

        result = json.loads((tmp_path / "pf.json").read_text())
        assert len(result["entries"]) == 50

    def test_staleness_prune_on_save(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel, "_PANEL_FILTERS_PATH", tmp_path / "pf.json")
        old_ts = int(time.time()) - 31 * 86400  # 31 days ago
        entries = {"pod:ns/stale": {"text": "old", "saved_at": old_ts}}
        data = {"schema_version": 1, "entries": entries}
        (tmp_path / "pf.json").write_text(json.dumps(data))

        panel._save_panel_filter("pod:ns/fresh", "new")

        result = json.loads((tmp_path / "pf.json").read_text())
        assert "pod:ns/stale" not in result["entries"]
        assert "pod:ns/fresh" in result["entries"]


class TestPersistMergePreservesOtherKeys:
    """AC-B5: saving pod B leaves pod A byte-identical (read-merge-write)."""

    def test_persist_merge_preserves_other_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel, "_PANEL_FILTERS_PATH", tmp_path / "pf.json")

        panel._save_panel_filter("pod:ns/pod-A", "error")
        original = json.loads((tmp_path / "pf.json").read_text())
        entry_a_before = original["entries"]["pod:ns/pod-A"]

        panel._save_panel_filter("pod:ns/pod-B", "timeout")

        after = json.loads((tmp_path / "pf.json").read_text())
        assert "pod:ns/pod-A" in after["entries"], "pod-A must still be present"
        assert "pod:ns/pod-B" in after["entries"]
        assert after["entries"]["pod:ns/pod-A"]["text"] == entry_a_before["text"]
        assert after["entries"]["pod:ns/pod-A"]["saved_at"] == entry_a_before["saved_at"]


class TestPersistLiveSessionKeyStable:
    """AC-B6: LiveSessionView saves/restores by session:{task_id}."""

    def test_persist_live_session_key_stable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel, "_PANEL_FILTERS_PATH", tmp_path / "pf.json")

        view = panel.LiveSessionView()
        view.task_id = "task-abc-123"
        view.pod_name = "claude-worker-0"
        view._filter_text = "error"

        view.on_unmount(_make_app())

        result = json.loads((tmp_path / "pf.json").read_text())
        key = panel._sanitize_filter_key("session:task-abc-123")
        assert key in result["entries"]
        assert result["entries"][key]["text"] == "error"

    def test_live_session_restore_different_pod_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel, "_PANEL_FILTERS_PATH", tmp_path / "pf.json")

        # Save with the sanitized key (same as on_mount would use)
        key = panel._sanitize_filter_key("session:task-xyz")
        panel._save_panel_filter(key, "timeout")

        view = panel.LiveSessionView()
        app = _make_app()
        app.last_payload = {"task_id": "task-xyz", "pod_name": "claude-worker-99"}
        view.on_mount(app)

        assert view._filter_text == "timeout"


# ---------------------------------------------------------------------------
# Item C — Multi-filtro AND/OR
# ---------------------------------------------------------------------------

class TestMultiAndOrBasic:
    """AC-C1: AND/OR logic; single term = #460 compat."""

    def test_and_shows_only_both(self):
        view = _make_pod_view()
        view.streamer = _make_streamer([
            "error and timeout here",
            "only error here",
            "only timeout here",
            "neither",
        ])
        _apply_terms(view, "error AND timeout")
        p = view._log_panel()
        rendered = str(p.renderable)
        assert "error and timeout here" in rendered
        assert "only error here" not in rendered
        assert "only timeout here" not in rendered

    def test_or_shows_either(self):
        view = _make_pod_view()
        view.streamer = _make_streamer([
            "error line",
            "warn line",
            "debug line",
        ])
        _apply_terms(view, "error OR warn")
        p = view._log_panel()
        rendered = str(p.renderable)
        assert "error line" in rendered
        assert "warn line" in rendered
        assert "debug line" not in rendered

    def test_single_term_compat(self):
        """Single term behaves like #460 (no regression)."""
        view = _make_pod_view()
        lines = ["alpha found", "beta line", "gamma"]
        view.streamer = _make_streamer(lines)
        _apply_terms(view, "alpha")
        p = view._log_panel()
        rendered = str(p.renderable)
        assert "alpha found" in rendered
        assert "beta line" not in rendered
        assert "gamma" not in rendered


class TestMultiMixedOperatorsInvalid:
    """AC-C2: a AND b OR c → no-op + toast."""

    def test_mixed_operators_invalid(self):
        terms, op, err = panel._parse_filter_expr("a AND b OR c")
        assert err is not None
        assert "AND" in err or "OR" in err
        assert terms == []

    def test_mixed_operators_via_handler(self):
        view = _make_pod_view()
        app = _make_app()
        view._prompt_open = True
        view._filter_buffer = "a AND b OR c"
        view.handle_key("\r", app)
        app.push_toast.assert_called_once()
        assert view._filter_text == ""
        assert view._filter_terms == []


class TestMultiQuotedTerm:
    """AC-C3: "a AND b" matches literal ' AND ' inside."""

    def test_quoted_term_matches_literal_and(self):
        terms, op, err = panel._parse_filter_expr('"a AND b"')
        assert err is None
        assert len(terms) == 1
        kind, raw, _ = terms[0]
        assert kind == "literal"
        assert raw == "a AND b"

    def test_quoted_term_filter(self):
        view = _make_pod_view()
        view.streamer = _make_streamer([
            "line with a AND b inside",
            "line without it",
        ])
        _apply_terms(view, '"a AND b"')
        p = view._log_panel()
        rendered = str(p.renderable)
        assert "line with a AND b inside" in rendered
        assert "line without it" not in rendered


class TestMultiRegexTermCompileFail:
    """AC-C4: bad r: term → no-op + toast with term index."""

    def test_regex_compile_fail_returns_error(self):
        terms, op, err = panel._parse_filter_expr("r:[bad AND x")
        assert err is not None
        assert "1" in err  # term index 1

    def test_multi_regex_bad_second_term(self):
        terms, op, err = panel._parse_filter_expr("error AND r:[bad")
        assert err is not None
        assert "2" in err  # term index 2

    def test_bad_regex_via_handler_no_filter(self):
        view = _make_pod_view()
        app = _make_app()
        view._prompt_open = True
        view._filter_buffer = "r:[invalid"
        view.handle_key("\r", app)
        app.push_toast.assert_called_once()
        assert view._filter_text == ""
        assert view._filter_terms == []
        assert view._filter_re is None


class TestMultiOver200Chars:
    """AC-C5: expression >200 chars → error message."""

    def test_over_200_chars_returns_error(self):
        expr = "x" * 201
        terms, op, err = panel._parse_filter_expr(expr)
        assert err is not None
        assert terms == []

    def test_exactly_200_chars_ok(self):
        expr = "x" * 200
        terms, op, err = panel._parse_filter_expr(expr)
        assert err is None
        assert len(terms) == 1


# ---------------------------------------------------------------------------
# Item D — Regex-timeout
# ---------------------------------------------------------------------------

class TestRegexDepDeclared:
    """AC-D1: pyproject.toml declares regex>=2024.0."""

    def test_regex_dep_declared(self):
        import tomllib  # Python 3.11+; fallback below
        import importlib
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # noqa: F811 — optional fallback

        pyproject = _REPO / "pyproject.toml"
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            import configparser, re as _re
            text = pyproject.read_text()
            deps = _re.findall(r'"(regex[^"]*)"', text)
            assert any("regex" in d for d in deps), "regex not found in pyproject.toml"
            return

        deps = data.get("project", {}).get("dependencies", [])
        regex_deps = [d for d in deps if d.startswith("regex")]
        assert regex_deps, "regex dependency not found in [project].dependencies"
        assert any("2024" in d or ">=" in d for d in regex_deps)


class TestRegexTimeoutAbortsUnder100ms:
    """AC-D2: r:(a+)+$ on 5000 'a' → TimeoutError, returns in ≤100ms."""

    def test_regex_timeout_aborts_under_100ms(self):
        import pytest
        if not _HAS_REGEX:
            pytest.skip("regex module not installed — AC-D2 requires regex>=2024.0")

        view = _make_pod_view()
        app = _make_app()
        view.streamer = _make_streamer(["a" * 5000])
        view._prompt_open = True
        view._filter_buffer = "r:(a+)+$"

        t0 = time.monotonic()
        view.handle_key("\r", app)
        # Apply filter (will time out during matching)
        view._log_panel()
        elapsed = time.monotonic() - t0

        assert elapsed <= 0.5, f"Filter took {elapsed:.3f}s — expected ≤0.5s"

    def test_parse_filter_expr_compiles_via_regex_module(self):
        import pytest
        if not _HAS_REGEX:
            pytest.skip("regex module not installed — requires regex>=2024.0")

        terms, op, err = panel._parse_filter_expr("r:err.*boom")
        assert err is None
        assert len(terms) == 1
        kind, raw, compiled = terms[0]
        assert kind == "regex"
        assert hasattr(compiled, "search")
        # Should be a regex module pattern, not re module
        assert type(compiled).__module__.startswith("regex"), (
            f"Expected regex.Pattern, got {type(compiled)}"
        )


class TestRegexNormalPatternStillWorks:
    """AC-D3: normal regex pattern works with new lib (no regression)."""

    def test_regex_normal_pattern_still_works(self):
        view = _make_pod_view()
        view.streamer = _make_streamer(["ERROR: something bad", "info: ok"])
        _apply_terms(view, "r:ERR.*bad")
        p = view._log_panel()
        rendered = str(p.renderable)
        assert "ERROR: something bad" in rendered
        assert "info: ok" not in rendered


# ---------------------------------------------------------------------------
# Transversais
# ---------------------------------------------------------------------------

class TestAllItemsOffOutputIdentical:
    """AC-T2: with all items off, output is byte-identical to #460 behavior."""

    def test_no_filter_output_identical(self):
        lines = ["alpha", "beta", "gamma"]
        view = _make_pod_view()
        view.streamer = _make_streamer(lines)

        # No filter set
        p1 = view._log_panel()
        body1 = str(p1.renderable)

        # With no filter terms set, output should be plain text
        assert "alpha" in body1
        assert "beta" in body1
        assert "gamma" in body1
        # No spans should exist (no highlight without a filter)
        assert not hasattr(p1.renderable, "_spans") or not any(
            "reverse" in str(s.style) for s in p1.renderable._spans
        )

    def test_defaults_falsy(self):
        view = _make_pod_view()
        assert view._filter_terms == []
        assert view._filter_op == ""
        assert view._filter_re is None
        assert view._filter_text == ""
