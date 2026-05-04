"""Sandbox honesty tests — issues #54, #56, #57.

These pin three honesty invariants:

* `bash_execute`'s `sandbox` parameter description does not promise
  isolation (issue #57).
* `PluginSandbox` is documented as a non-isolating skeleton and is
  not invoked by `PluginManager` (issue #54).
* `SafetySandbox` is gone and `ImprovementLoop.start()` refuses to
  run without `experimental=True` (issue #56).

A regression on any of these would re-introduce the misleading
guarantees these issues were filed to remove.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from deile.evolution.improvement_loop import ImprovementLoop
from deile.plugins.sandbox import PluginSandbox
from deile.tools.bash_tool import BashExecuteTool


@pytest.mark.security
def test_bash_sandbox_param_description_does_not_promise_isolation():
    """Issue #57: schema description must say it does NOT isolate."""
    tool = BashExecuteTool()
    schema = tool.get_schema()
    desc = schema["parameters"]["properties"]["sandbox"]["description"].lower()

    assert "does not provide isolation" in desc, (
        "sandbox parameter description must explicitly say it does not isolate; "
        f"got: {desc!r}"
    )
    assert "pty" in desc, (
        "sandbox parameter description must explain it controls PTY only; "
        f"got: {desc!r}"
    )


@pytest.mark.security
def test_plugin_sandbox_docstring_marks_skeleton():
    """Issue #54: PluginSandbox docstring must declare it does not isolate."""
    doc = (PluginSandbox.__doc__ or "").lower()

    assert "skeleton" in doc, (
        f"PluginSandbox class docstring must declare itself a skeleton; got: {doc!r}"
    )
    assert "não fornece isolamento" in doc or "does not" in doc, (
        f"PluginSandbox docstring must be explicit that it does not isolate; got: {doc!r}"
    )


@pytest.mark.security
def test_plugin_manager_does_not_invoke_plugin_sandbox():
    """Issue #54: PluginManager source must not import or call PluginSandbox.

    The class exists as a skeleton; if a future change wires it into
    PluginManager, this test should be updated alongside the doc that
    promises isolation.
    """
    plugin_manager_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "plugin_manager.py"
    )
    src = plugin_manager_path.read_text(encoding="utf-8")

    assert "PluginSandbox" not in src, (
        "plugin_manager.py imports/uses PluginSandbox — but PluginSandbox is "
        "documented as a non-isolating skeleton (issue #54). Either update the "
        "skeleton docs or remove the wiring; do not silently reintroduce the "
        "false-isolation drift."
    )


@pytest.mark.security
def test_safety_sandbox_is_gone():
    """Issue #56: SafetySandbox stub removed; module must not be importable."""
    with pytest.raises(ImportError):
        import deile.evolution.safety_sandbox  # noqa: F401

    from deile import evolution

    assert not hasattr(evolution, "SafetySandbox"), (
        "deile.evolution.SafetySandbox must not be re-exported (issue #56)."
    )


@pytest.mark.security
async def test_improvement_loop_refuses_to_start_without_experimental_flag():
    """Issue #56: start() must raise without explicit experimental=True."""
    self_analyzer = AsyncMock()
    loop = ImprovementLoop(self_analyzer=self_analyzer)

    with pytest.raises(RuntimeError, match="experimental=True"):
        await loop.start()

    self_analyzer.start.assert_not_called()
