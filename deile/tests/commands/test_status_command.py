"""Tests: /status command — StatusCommand.

Regression guard for the bug where _show_complete_status built the final
display via an f-string that called str() on Rich objects, producing output
like:

    <rich.columns.Columns object at 0x…>
    <rich.panel.Panel object at 0x…>

instead of actually rendering them.
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from deile.commands.base import CommandContext
from deile.commands.builtin.status_command import StatusCommand

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(content) -> str:
    """Render a Rich renderable to plain text. Raises if content is not renderable."""
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=120)
    console.print(content)
    return buf.getvalue()


def _ctx(args: str = "") -> CommandContext:
    return CommandContext(user_input=f"/status {args}".strip(), args=args)


def _make_cmd() -> StatusCommand:
    return StatusCommand()


# ---------------------------------------------------------------------------
# /status (no args) — complete overview
# ---------------------------------------------------------------------------


class TestStatusComplete:
    async def test_returns_success(self):
        result = await _make_cmd().execute(_ctx())
        assert result.success is True

    async def test_content_type_is_rich(self):
        result = await _make_cmd().execute(_ctx())
        assert result.content_type == "rich"

    async def test_content_is_not_string(self):
        """The fix: content must NOT be a plain string (i.e. the f-string repr bug)."""
        result = await _make_cmd().execute(_ctx())
        assert not isinstance(result.content, str), (
            "content must be a Rich renderable, not a str produced by f-string interpolation"
        )

    async def test_content_has_no_repr_artifacts(self):
        """Rendered output must not contain object repr strings like '<rich.'."""
        result = await _make_cmd().execute(_ctx())
        rendered = _render(result.content)
        assert "<rich." not in rendered, (
            f"Rendered output contains object repr: {rendered[:200]}"
        )

    async def test_content_renders_without_error(self):
        """Rich must be able to render the content directly (the original crash scenario)."""
        result = await _make_cmd().execute(_ctx())
        rendered = _render(result.content)
        assert rendered  # non-empty

    async def test_rendered_output_mentions_system(self):
        result = await _make_cmd().execute(_ctx())
        assert "system" in _render(result.content).lower()

    async def test_rendered_output_mentions_health(self):
        result = await _make_cmd().execute(_ctx())
        rendered = _render(result.content).lower()
        assert "health" in rendered or "status" in rendered


# ---------------------------------------------------------------------------
# /status system — sub-command
# ---------------------------------------------------------------------------


class TestStatusSystem:
    async def test_returns_success(self):
        result = await _make_cmd().execute(_ctx("system"))
        assert result.success is True

    async def test_content_type_is_rich(self):
        result = await _make_cmd().execute(_ctx("system"))
        assert result.content_type == "rich"

    async def test_content_renders_without_error(self):
        result = await _make_cmd().execute(_ctx("system"))
        rendered = _render(result.content)
        assert rendered

    async def test_content_not_a_string(self):
        result = await _make_cmd().execute(_ctx("system"))
        assert not isinstance(result.content, str)


# ---------------------------------------------------------------------------
# /status <placeholder sub-commands>
# ---------------------------------------------------------------------------


class TestStatusSubCommands:
    async def test_models_returns_success(self):
        result = await _make_cmd().execute(_ctx("models"))
        assert result.success is True
        _render(result.content)

    async def test_tools_returns_success(self):
        result = await _make_cmd().execute(_ctx("tools"))
        assert result.success is True
        _render(result.content)

    async def test_memory_returns_success(self):
        result = await _make_cmd().execute(_ctx("memory"))
        assert result.success is True
        _render(result.content)

    async def test_plans_returns_success(self):
        result = await _make_cmd().execute(_ctx("plans"))
        assert result.success is True
        _render(result.content)

    async def test_connectivity_returns_success(self):
        result = await _make_cmd().execute(_ctx("connectivity"))
        assert result.success is True
        _render(result.content)

    async def test_performance_returns_success(self):
        result = await _make_cmd().execute(_ctx("performance"))
        assert result.success is True
        _render(result.content)
