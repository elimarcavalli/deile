"""Sandbox honesty tests — issues #54, #55, #56, #57.

These pin four honesty invariants:

* `bash_execute`'s `sandbox` parameter description does not promise
  isolation (issue #57).
* `PluginSandbox` is documented as a non-isolating skeleton and is
  not invoked by `PluginManager` (issue #54).
* `SafetySandbox` is gone and `ImprovementLoop.start()` refuses to
  run without `experimental=True` (issue #56).
* `DockerSandboxManager` is gone and `SandboxCommand` neither imports
  the `docker` SDK nor advertises Docker subcommands (issue #55).

A regression on any of these would re-introduce the misleading
guarantees these issues were filed to remove.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

from deile.commands.builtin.sandbox_command import SandboxCommand
from deile.evolution.improvement_loop import ImprovementLoop
from deile.plugins.sandbox import PluginSandbox
from deile.tools.bash_tool import BashExecuteTool


def _render_rich(renderable) -> str:
    console = Console(file=io.StringIO(), record=True, force_terminal=False, width=120)
    console.print(renderable)
    return console.export_text()


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
    """Issue #54: PluginManager must not import or call PluginSandbox.

    The class exists as a skeleton; if a future change wires it into
    PluginManager, this test should be updated alongside the docs that
    promise isolation. We parse the AST so an honest mention of
    `PluginSandbox` in a docstring (e.g. "PluginSandbox does not isolate")
    does not count as wiring.
    """
    plugin_manager_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "plugin_manager.py"
    )
    tree = ast.parse(plugin_manager_path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            module = getattr(node, "module", "") or ""
            assert "PluginSandbox" not in names, (
                "plugin_manager.py imports PluginSandbox — issue #54 forbids "
                "wiring without first updating the docs that call it a skeleton."
            )
            assert "sandbox" not in module.split("."), (
                "plugin_manager.py imports from .sandbox — issue #54."
            )
        if isinstance(node, ast.Name) and node.id == "PluginSandbox":
            raise AssertionError(
                "plugin_manager.py references the PluginSandbox name in code — "
                "issue #54 forbids wiring without updating the skeleton docs."
            )
        if isinstance(node, ast.Attribute) and node.attr == "PluginSandbox":
            raise AssertionError(
                "plugin_manager.py references PluginSandbox via attribute access "
                "— issue #54 forbids wiring without updating the skeleton docs."
            )


@pytest.mark.security
def test_safety_sandbox_is_gone():
    """Issue #56: SafetySandbox stub removed; module must not exist or import."""
    safety_sandbox_path = (
        Path(__file__).resolve().parents[2]
        / "evolution"
        / "safety_sandbox.py"
    )
    assert not safety_sandbox_path.exists(), (
        "deile/evolution/safety_sandbox.py reappeared — issue #56 stub must "
        "stay deleted."
    )

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


# ---------------------------------------------------------------------------
# Issue #55 — DockerSandboxManager dead code removed
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_sandbox_command_module_does_not_import_docker():
    """Issue #55: removing DockerSandboxManager also removes the `docker` import.

    A reintroduced `import docker` (or `from docker ...`) signals that the
    dead manager has come back. Re-introducing the import is allowed only
    if a real wiring lands at the same time — in which case this test
    should be updated alongside the documentation that promises isolation.
    """
    sandbox_module_path = (
        Path(__file__).resolve().parents[2]
        / "commands"
        / "builtin"
        / "sandbox_command.py"
    )
    tree = ast.parse(sandbox_module_path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "docker", (
                    "sandbox_command.py imports `docker` — issue #55 forbids "
                    "the dead Docker SDK dependency from coming back without "
                    "real wiring into bash_execute or python_execute."
                )
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module != "docker" and not module.startswith("docker."), (
                "sandbox_command.py imports from `docker` — issue #55 forbids "
                "the dead Docker SDK dependency from coming back without "
                "real wiring into bash_execute or python_execute."
            )


@pytest.mark.security
def test_docker_sandbox_manager_class_is_gone():
    """Issue #55: the DockerSandboxManager class must not be re-exported.

    The class was 340 LOC of unwired infrastructure; bringing it back as
    a name in the module is the regression we are pinning against.
    """
    import deile.commands.builtin.sandbox_command as sandbox_module

    assert not hasattr(sandbox_module, "DockerSandboxManager"), (
        "DockerSandboxManager reappeared in sandbox_command.py — issue #55 "
        "removed it because no execution tool ever called it. Re-introduce "
        "only with a real wiring into bash_execute / python_execute."
    )


@pytest.mark.security
async def test_sandbox_command_does_not_advertise_docker_subcommands():
    """Issue #55: help text and rendered status/config panels must not promise
    Docker setup/cleanup/stats.

    The deleted ``_setup_sandbox`` / ``_manage_docker`` methods rendered their
    promises in Rich panels, not in ``get_help()`` — so we walk the full
    rendered surface (help text + status panel + config panel) to catch
    regressions in either place.
    """
    cmd = SandboxCommand()
    help_text = cmd.get_help().lower()
    status_text = _render_rich((await cmd._show_sandbox_status()).content).lower()
    config_text = _render_rich((await cmd._show_sandbox_config()).content).lower()

    forbidden_promises = (
        "docker setup",
        "docker cleanup",
        "docker stats",
        "/sandbox docker",
    )
    for phrase in forbidden_promises:
        for surface_name, surface in (
            ("get_help()", help_text),
            ("_show_sandbox_status()", status_text),
            ("_show_sandbox_config()", config_text),
        ):
            assert phrase not in surface, (
                f"{surface_name} advertises {phrase!r} — issue #55 removed that "
                "subcommand because it pointed to a manager class that nothing "
                "ever invoked."
            )


@pytest.mark.security
async def test_sandbox_command_status_marks_itself_informational():
    """Issue #55/#57: the rendered status panel must declare the toggle informational."""
    cmd = SandboxCommand()
    status_text = _render_rich((await cmd._show_sandbox_status()).content).lower()

    assert "informational only" in status_text, (
        "_show_sandbox_status() must render the phrase 'informational only' so "
        "the user-facing surface keeps matching the bash_tool sandbox flag's "
        "honesty contract — issue #55 forbids silent regression to the old "
        "promise of Docker isolation."
    )
