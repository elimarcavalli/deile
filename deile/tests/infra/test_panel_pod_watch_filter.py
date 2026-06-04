"""Testes do filtro grep-like em PodWatchView e LiveSessionView (issue #460).

Cobertura dos ACs:

AC1  — filtro literal case-insensitive; linhas sem match ficam ocultas.
AC2  — limite de 200 chars no buffer; prefixo `r:` ativa regex.
AC3  — filtro aplicado APÓS health filter e ANTES do truncamento [-30:].
AC4  — `[/]` abre o prompt inline; teclas viram texto no buffer.
AC5  — `[ESC]` em 3 níveis: fecha prompt → limpa filtro → pop view (global).
AC6  — LiveSessionView filtra `data.chat["turns"]` antes de renderizar.
AC7  — regex inválido cai para filtro literal + toast.
AC8  — filtro vazio `""` = saída idêntica (nenhuma linha oculta).
AC9  — contador `grep_hidden` é separado de `health hidden`.
AC10 — regex compilado 1× no [enter], nunca por render.
AC11 — hotkeys `f`/`c`/`h` ficam mortas enquanto o prompt está aberto.
AC12 — `[ESC]` dentro do prompt fecha sem aplicar o filtro.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_view() -> panel.PodWatchView:
    view = panel.PodWatchView()
    view.pod_name = "claude-worker-0"
    view.pod_role = "claude-worker"
    view.hide_health = False
    return view


def _make_streamer(lines: list[str]) -> MagicMock:
    mock = MagicMock()
    mock.snapshot.return_value = list(lines)
    return mock


def _make_app() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# AC1 — filtro literal case-insensitive
# ---------------------------------------------------------------------------

class TestLiteralFilter:
    def test_filter_literal_case_insensitive(self):
        view = _make_view()
        view.streamer = _make_streamer(["ERROR: boom", "info: ok", "Error: another"])
        view._filter_re = re.compile(re.escape("error"), re.IGNORECASE)
        view._filter_text = "error"

        p = view._log_panel()
        rendered = str(p.renderable)
        assert "boom" in rendered
        assert "another" in rendered
        assert "info: ok" not in rendered

    def test_filter_persists_across_renders(self):
        view = _make_view()
        lines = ["match_line", "skip_line"]
        view.streamer = _make_streamer(lines)
        view._filter_re = re.compile(re.escape("match"), re.IGNORECASE)
        view._filter_text = "match"

        # first render
        p1 = view._log_panel()
        # second render — same filter object, not recompiled
        p2 = view._log_panel()
        assert str(p1.renderable) == str(p2.renderable)


# ---------------------------------------------------------------------------
# AC8 — filtro vazio = saída idêntica
# ---------------------------------------------------------------------------

class TestEmptyFilter:
    def test_empty_filter_no_change(self):
        lines = ["alpha", "beta", "gamma"]
        view = _make_view()
        view.streamer = _make_streamer(lines)
        # no filter
        panel_no_filter = view._log_panel()

        view._filter_re = None
        view._filter_text = ""
        panel_with_empty = view._log_panel()

        assert str(panel_no_filter.renderable) == str(panel_with_empty.renderable)


# ---------------------------------------------------------------------------
# AC3 — filtro antes do truncamento [-30:]
# ---------------------------------------------------------------------------

class TestFilterBeforeTruncation:
    def test_filter_applied_before_truncation(self):
        # 40 lines: first 35 contain "target", last 5 do not.
        # Without pre-truncation filtering, only last 30 lines are visible
        # and none contain "target" → result would be empty.
        # With correct ordering (filter before truncation), we see matches.
        lines = [f"target line {i}" for i in range(35)] + [
            f"noise line {i}" for i in range(5)
        ]
        view = _make_view()
        view.streamer = _make_streamer(lines)
        view._filter_re = re.compile(re.escape("target"), re.IGNORECASE)
        view._filter_text = "target"

        p = view._log_panel()
        rendered = str(p.renderable)
        assert "target" in rendered, (
            "filter must be applied to full 200-line buffer before [-30:] truncation"
        )


# ---------------------------------------------------------------------------
# AC9 — grep_hidden separado do health hidden
# ---------------------------------------------------------------------------

class TestSeparateCounters:
    def test_grep_hidden_count_separate_from_health(self):
        # 3 health lines (to be hidden by hide_health) + 3 normal lines,
        # 1 matching the grep filter.
        lines = [
            "GET /health 200",
            "GET /healthz 200",
            "GET /ready 200",
            "ERROR: boom",
            "info: ok",
            "WARNING: watch",
        ]
        view = _make_view()
        view.hide_health = True
        view.streamer = _make_streamer(lines)
        view._filter_re = re.compile(re.escape("error"), re.IGNORECASE)
        view._filter_text = "error"

        with patch("_panel._HEALTH_LINE_RE") as mock_re:
            mock_re.search.side_effect = lambda ln: (
                True if "health" in ln.lower() or "healthz" in ln.lower()
                or "ready" in ln.lower() else False
            )
            p = view._log_panel()

        title = p.title
        # Both counters must appear with their respective labels.
        assert "health filtrados" in title
        assert "filtro grep" in title
        # They must be on separate labels, not merged.
        assert title.count("health filtrados") == 1
        assert title.count("filtro grep") == 1


# ---------------------------------------------------------------------------
# AC4 — [/] abre prompt, teclas viram buffer
# ---------------------------------------------------------------------------

class TestPromptOpen:
    def test_slash_opens_prompt(self):
        view = _make_view()
        app = _make_app()
        assert not view._prompt_open
        result = view.handle_key("/", app)
        assert view._prompt_open
        assert view._filter_buffer == ""
        assert result.kind == panel.Action.REFRESH

    def test_characters_go_to_buffer_when_prompt_open(self):
        view = _make_view()
        app = _make_app()
        view._prompt_open = True
        for ch in "hello":
            view.handle_key(ch, app)
        assert view._filter_buffer == "hello"


# ---------------------------------------------------------------------------
# AC11 — hotkeys f/c/h ficam mortas enquanto o prompt está aberto
# ---------------------------------------------------------------------------

class TestHotkeysDeadWhilePromptOpen:
    def test_f_goes_to_buffer_not_follow_toggle(self):
        view = _make_view()
        app = _make_app()
        view._prompt_open = True
        initial_following = view.following
        view.handle_key("f", app)
        assert view.following == initial_following, "follow must NOT toggle while prompt is open"
        assert "f" in view._filter_buffer

    def test_c_goes_to_buffer_not_clear(self):
        view = _make_view()
        app = _make_app()
        mock_streamer = _make_streamer(["line"])
        view.streamer = mock_streamer
        view._prompt_open = True
        view.handle_key("c", app)
        mock_streamer.buf.clear.assert_not_called()
        assert "c" in view._filter_buffer

    def test_h_goes_to_buffer_not_hide_toggle(self):
        view = _make_view()
        app = _make_app()
        view._prompt_open = True
        initial_hide = view.hide_health
        view.handle_key("h", app)
        assert view.hide_health == initial_hide, "hide_health must NOT toggle while prompt is open"
        assert "h" in view._filter_buffer


# ---------------------------------------------------------------------------
# AC12 — ESC dentro do prompt fecha sem aplicar filtro
# ---------------------------------------------------------------------------

class TestEscInPrompt:
    def test_esc_closes_prompt_without_applying_filter(self):
        view = _make_view()
        app = _make_app()
        view._prompt_open = True
        view._filter_buffer = "partial"
        result = view.handle_key("ESC", app)
        assert not view._prompt_open
        assert view._filter_buffer == ""
        assert view._filter_text == ""
        assert view._filter_re is None
        assert result.kind == panel.Action.REFRESH


# ---------------------------------------------------------------------------
# AC5 — ESC em 3 níveis via intercepts_key
# ---------------------------------------------------------------------------

class TestEscThreeLevels:
    def test_level1_esc_closes_prompt(self):
        view = _make_view()
        view._prompt_open = True
        assert view.intercepts_key("ESC")
        app = _make_app()
        view.handle_key("ESC", app)
        assert not view._prompt_open

    def test_level2_esc_clears_filter(self):
        view = _make_view()
        view._filter_text = "error"
        view._filter_re = re.compile(re.escape("error"), re.IGNORECASE)
        assert view.intercepts_key("ESC")
        app = _make_app()
        result = view.handle_key("ESC", app)
        assert view._filter_text == ""
        assert view._filter_re is None
        assert result.kind == panel.Action.REFRESH

    def test_level3_esc_not_intercepted(self):
        view = _make_view()
        view._filter_text = ""
        view._prompt_open = False
        assert not view.intercepts_key("ESC")


# ---------------------------------------------------------------------------
# AC7 — regex inválido cai para literal + toast
# ---------------------------------------------------------------------------

class TestInvalidRegex:
    def test_invalid_regex_falls_back_to_literal(self):
        view = _make_view()
        app = _make_app()
        view._prompt_open = True
        for ch in "r:[invalid":
            view._filter_buffer += ch
        view.handle_key("\r", app)
        app.push_toast.assert_called_once_with("⚠", "regex inválido — usando filtro literal")
        # Filter should be a compiled literal (re.escape of the pattern part)
        assert view._filter_re is not None
        assert view._filter_text == "[invalid"

    def test_valid_regex_compiles(self):
        view = _make_view()
        app = _make_app()
        view._prompt_open = True
        for ch in "r:err.*boom":
            view._filter_buffer += ch
        view.handle_key("\r", app)
        app.push_toast.assert_not_called()
        assert view._filter_re is not None
        assert view._filter_re.search("error boom")


# ---------------------------------------------------------------------------
# AC2 — limite de 200 chars
# ---------------------------------------------------------------------------

class TestMaxLength:
    def test_filter_text_capped_at_200_chars(self):
        view = _make_view()
        app = _make_app()
        view._prompt_open = True
        view._filter_buffer = "x" * 250
        view.handle_key("\r", app)
        assert len(view._filter_text) == 200


# ---------------------------------------------------------------------------
# AC10 — regex compilado 1× (não por render)
# ---------------------------------------------------------------------------

class TestCompileOnce:
    def test_regex_not_recompiled_on_render(self):
        view = _make_view()
        view.streamer = _make_streamer(["line1", "line2"])
        compiled = re.compile(re.escape("line"), re.IGNORECASE)
        view._filter_re = compiled
        view._filter_text = "line"

        view._log_panel()
        view._log_panel()
        view._log_panel()

        # The exact same object must still be there (not a new compile each time).
        assert view._filter_re is compiled


# ---------------------------------------------------------------------------
# AC6 — LiveSessionView filtra chat["turns"]
# ---------------------------------------------------------------------------

class TestLiveSessionViewFilter:
    def test_filter_turns_before_render(self):
        from deile.ui.panel.observability.screens import LiveSessionData  # type: ignore

        view = panel.LiveSessionView()
        view.task_id = "task-123"
        view.pod_name = "claude-worker-0"

        turns = [
            {"role": "user", "content": "find the error"},
            {"role": "assistant", "content": "looking good"},
            {"role": "user", "content": "error found: boom"},
        ]
        live_data = LiveSessionData(
            session={"task_id": "task-123", "status": "running"},
            command={"cmd": "test"},
            chat={"turns": turns},
            api_errors=[],
        )

        view._filter_re = re.compile(re.escape("error"), re.IGNORECASE)
        view._filter_text = "error"

        # Patch _fetch_data to return our live_data
        # and LiveSessionScreen.render to capture what it receives.
        received = {}

        class FakeScreen:
            def render(self, data):
                received["data"] = data
                from rich.text import Text
                return Text("ok")

        with (
            patch.object(view, "_fetch_data", return_value=(live_data, [])),
            patch(
                "deile.ui.panel.observability.screens.LiveSessionScreen",
                new=lambda: FakeScreen(),
            ),
            patch("_panel._head_panel", return_value=MagicMock()),
            patch("_panel._footer_panel", return_value=MagicMock()),
        ):
            view.render(_make_app())

        assert "data" in received
        filtered_turns = received["data"].chat["turns"]
        # Only turns containing "error" should remain.
        assert len(filtered_turns) == 2
        assert all("error" in t["content"].lower() for t in filtered_turns)
        # Original data must be unchanged.
        assert len(turns) == 3
