"""Tests for /panel command (issue #347).

The command is the operator's entry point into the observability panel.
These tests verify it builds the client correctly and degrades gracefully
when the optional ``screens.run_panel`` orchestrator is missing.
"""

from __future__ import annotations

from deile.commands.base import CommandContext
from deile.commands.builtin.panel_command import PanelCommand


def _ctx(args: str = "") -> CommandContext:
    ctx = CommandContext(user_input=f"/panel {args}".strip(), args=args)
    ctx.agent = None
    return ctx


async def test_panel_command_builds_client_without_typeerror(monkeypatch):
    """Regression: ``/panel`` must construct a working
    ``ClusterObservabilityClient`` via ``from_endpoints``.

    PR #352 first-fix attempt called the dataclass constructor directly
    with kwargs that did not match its fields, so every invocation died
    with ``TypeError: __init__() got an unexpected keyword argument
    'pipeline_status_url'`` before reaching the (deferred) screens loop.
    """
    monkeypatch.setenv("DEILE_PIPELINE_STATUS_ENDPOINT", "http://pip:8768")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ENDPOINT", "http://cw:8767")
    monkeypatch.setenv("DEILE_PIPELINE_STATUS_AUTH_TOKEN", "pt")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_AUTH_TOKEN", "ct")

    result = await PanelCommand().execute(_ctx())
    # ``run_panel`` is intentionally not implemented yet — the command
    # surfaces that as a clear error, but it must reach that point
    # (i.e. the client constructor must NOT raise).
    assert result.success is False
    assert "run_panel" in (result.content or "")
    assert "pipeline_status_url" not in (result.content or "")


async def test_panel_command_handles_missing_module(monkeypatch):
    """When the observability subpackage is absent, the command surfaces
    a clear ImportError-style message instead of crashing the shell."""
    import sys

    # Force the import branch to fail by hiding the subpackage from
    # ``sys.modules`` and shadowing it with an unimportable stub.
    fake_name = "deile.ui.panel.observability"
    real_module = sys.modules.pop(fake_name, None)

    class _Boom:
        def __getattr__(self, name):
            raise ImportError(f"forced: {name}")

    monkeypatch.setitem(sys.modules, fake_name, _Boom())
    try:
        result = await PanelCommand().execute(_ctx())
        assert result.success is False
        assert "painel" in (result.content or "").lower()
    finally:
        # Restore the real module so other tests still see it.
        sys.modules.pop(fake_name, None)
        if real_module is not None:
            sys.modules[fake_name] = real_module
