"""Tests for the agent's validation gate, post-write hint, and pip_install / python_execute changes.

Covers:
- ``WriteFileTool`` emits a POST_WRITE_VALIDATION_REQUIRED hint for executable
  files but not for plain text / unknown extensions.
- ``PythonExecutionTool`` no longer rejects legitimate code via the
  substring blacklist that used to block ``open(``, ``input``, ``compile``,
  etc., and still surfaces non-zero exit codes correctly.
- ``PipInstallTool`` validates package specs against PEP 508, refuses
  shell-injection attempts, appends to ``requirements.txt`` only when the
  package is not already listed (idempotency), and preserves existing lines.
- ``DeileAgent._detect_unvalidated_writes`` flags writes without a following
  ``bash_execute`` / ``python_execute`` and clears them when validation
  follows. ``pip_install`` alone does NOT count as validation.
- ``DeileAgent._contains_promise_pattern`` matches Portuguese and English
  action-promise wording without false positives on benign text.
- ``DeileAgent._apply_validation_gate`` re-invokes ``_process_iterative_function_calling``
  exactly once when the gate fires, threads the synthetic prompt through
  ``conversation_history``, and is a no-op when ``_validation_gate_active``
  is set (preventing recursion).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from deile.core.agent import AgentSession, DeileAgent
from deile.tools.base import ToolContext, ToolResult, ToolStatus
from deile.tools.execution_tools import PipInstallTool, PythonExecutionTool
from deile.tools.file_tools import WriteFileTool, _post_write_validation_hint


# ---------------------------------------------------------------------------
# write_file post-write hint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, expected_kind",
    [
        ("app.py", "python_syntax"),
        ("run.sh", "bash_syntax"),
        ("data.json", "json_parse"),
        ("config.yaml", "yaml_parse"),
        ("config.yml", "yaml_parse"),
        ("index.js", "node_syntax"),
        ("src/main.ts", "typescript_check"),
    ],
)
def test_post_write_hint_for_executable_extensions(filename, expected_kind):
    hint = _post_write_validation_hint(filename)
    assert hint is not None
    assert hint["kind"] == expected_kind
    assert filename in hint["command"]


@pytest.mark.parametrize("filename", ["README.md", "notes.txt", "image.png", "no_extension"])
def test_post_write_hint_skipped_for_non_executable(filename):
    assert _post_write_validation_hint(filename) is None


def test_write_file_attaches_validation_metadata_for_python(tmp_path):
    tool = WriteFileTool()
    target = tmp_path / "demo.py"
    ctx = ToolContext(
        user_input="",
        parsed_args={"file_path": str(target), "content": "print('hi')\n"},
        working_directory=str(tmp_path),
    )
    result = tool.execute_sync(ctx)
    assert result.is_success
    assert result.metadata.get("post_write_validation_required") is True
    assert "py_compile" in result.metadata.get("post_write_validation_command", "")
    assert "POST_WRITE_VALIDATION_REQUIRED" in result.message


def test_write_file_no_validation_metadata_for_markdown(tmp_path):
    tool = WriteFileTool()
    target = tmp_path / "notes.md"
    ctx = ToolContext(
        user_input="",
        parsed_args={"file_path": str(target), "content": "# title\n"},
        working_directory=str(tmp_path),
    )
    result = tool.execute_sync(ctx)
    assert result.is_success
    assert "post_write_validation_required" not in result.metadata
    assert "POST_WRITE_VALIDATION_REQUIRED" not in result.message


# ---------------------------------------------------------------------------
# python_execute — blacklist removed, real exits surfaced
# ---------------------------------------------------------------------------


def test_python_execute_runs_code_that_old_blacklist_blocked(tmp_path):
    tool = PythonExecutionTool()
    # The old blacklist would have rejected this for containing "open(", "compile",
    # and "input" as substrings — none of which are actually dangerous here.
    ctx = ToolContext(
        user_input="",
        parsed_args={
            "code": (
                "data = {'open(': 1, 'compile_target': 2, 'input_field': 3}\n"
                "print(sum(data.values()))\n"
            )
        },
        working_directory=str(tmp_path),
    )
    result = tool.execute_sync(ctx)
    assert result.is_success, f"expected success, got {result.message}"
    assert result.metadata["exit_code"] == 0
    assert result.metadata["stdout"].strip() == "6"


def test_python_execute_surfaces_nonzero_exit(tmp_path):
    tool = PythonExecutionTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={"code": "import sys; sys.exit(2)"},
        working_directory=str(tmp_path),
    )
    result = tool.execute_sync(ctx)
    assert not result.is_success
    assert result.metadata["exit_code"] == 2


def test_python_execute_requires_code():
    tool = PythonExecutionTool()
    ctx = ToolContext(user_input="", parsed_args={}, working_directory=".")
    result = tool.execute_sync(ctx)
    assert result.is_error
    assert "code is required" in str(result.error)


# ---------------------------------------------------------------------------
# pip_install — spec validation, requirements.txt idempotency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec, valid",
    [
        ("requests", True),
        ("requests==2.31.0", True),
        ("pillow[webp]>=10.0", True),
        ("foo-bar.baz", True),
        ("pkg ; rm -rf /", False),
        ("pkg && evil", False),
        ("pkg | nc attacker", False),
        ("pkg`whoami`", False),
        ("pkg$(id)", False),
        ("pkg with spaces", False),
        ("../../../etc/passwd", False),
        ("'pkg'", False),
    ],
)
def test_pip_install_spec_validation(spec, valid):
    tool = PipInstallTool()
    assert bool(tool._SPEC_RE.match(spec)) is valid


def test_pip_install_normalize_pkg_name():
    norm = PipInstallTool._normalize_pkg_name
    assert norm("requests") == "requests"
    assert norm("requests==2.31.0") == "requests"
    assert norm("Pillow[webp]>=10.0") == "pillow"
    assert norm("python-Dotenv") == "python-dotenv"
    assert norm("python_dotenv") == "python-dotenv"
    assert norm("Python.dotenv") == "python-dotenv"


def test_pip_install_requirements_idempotency(tmp_path):
    """Running pip_install twice for the same package must not duplicate the
    line in requirements.txt; existing lines (for unrelated packages) are
    preserved verbatim. We stub the subprocess call so the test does not
    actually hit pip / network."""
    tool = PipInstallTool()
    req = tmp_path / "requirements.txt"
    req.write_text("rich>=13.0\n# comment line\nclick==8.1.0\n", encoding="utf-8")

    fake_ok = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Successfully installed requests-2.31.0\n", stderr=""
    )

    import deile.tools.execution_tools as exec_mod

    real_run = exec_mod.subprocess.run
    exec_mod.subprocess.run = lambda *a, **kw: fake_ok
    try:
        ctx = ToolContext(
            user_input="",
            parsed_args={"package": "requests"},
            working_directory=str(tmp_path),
        )
        first = tool.execute_sync(ctx)
        assert first.is_success
        assert first.metadata["requirements_updated"] is True

        second = tool.execute_sync(ctx)
        assert second.is_success
        assert second.metadata["requirements_updated"] is False
    finally:
        exec_mod.subprocess.run = real_run

    contents = req.read_text(encoding="utf-8")
    # Existing lines preserved
    assert "rich>=13.0" in contents
    assert "click==8.1.0" in contents
    # New line added exactly once
    assert contents.count("\nrequests\n") == 1 or contents.endswith("requests\n")
    # Total requests-named entries: exactly 1
    matches = [
        line for line in contents.splitlines()
        if line.strip() and not line.strip().startswith("#")
        and PipInstallTool._normalize_pkg_name(line) == "requests"
    ]
    assert len(matches) == 1


def test_pip_install_creates_requirements_when_absent(tmp_path):
    tool = PipInstallTool()
    req = tmp_path / "requirements.txt"
    assert not req.exists()

    fake_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    import deile.tools.execution_tools as exec_mod
    real_run = exec_mod.subprocess.run
    exec_mod.subprocess.run = lambda *a, **kw: fake_ok
    try:
        ctx = ToolContext(
            user_input="",
            parsed_args={"package": "httpx"},
            working_directory=str(tmp_path),
        )
        result = tool.execute_sync(ctx)
    finally:
        exec_mod.subprocess.run = real_run

    assert result.is_success
    assert req.exists()
    assert "httpx" in req.read_text(encoding="utf-8")


def test_pip_install_rejects_shell_injection():
    tool = PipInstallTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={"package": "requests; rm -rf /"},
        working_directory=".",
    )
    result = tool.execute_sync(ctx)
    assert result.is_error
    assert "PEP 508" in result.message


def test_pip_install_propagates_pip_failure(tmp_path):
    tool = PipInstallTool()
    fake_fail = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="ERROR: Could not find a version"
    )
    import deile.tools.execution_tools as exec_mod
    real_run = exec_mod.subprocess.run
    exec_mod.subprocess.run = lambda *a, **kw: fake_fail
    try:
        ctx = ToolContext(
            user_input="",
            parsed_args={"package": "definitely-not-a-real-package-zzz"},
            working_directory=str(tmp_path),
        )
        result = tool.execute_sync(ctx)
    finally:
        exec_mod.subprocess.run = real_run

    assert result.is_error
    assert result.metadata["exit_code"] == 1
    assert "Could not find a version" in result.message


def test_pip_install_skip_requirements_when_disabled(tmp_path):
    tool = PipInstallTool()
    req = tmp_path / "requirements.txt"
    req.write_text("rich>=13\n", encoding="utf-8")

    fake_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    import deile.tools.execution_tools as exec_mod
    real_run = exec_mod.subprocess.run
    exec_mod.subprocess.run = lambda *a, **kw: fake_ok
    try:
        ctx = ToolContext(
            user_input="",
            parsed_args={"package": "requests", "update_requirements": False},
            working_directory=str(tmp_path),
        )
        result = tool.execute_sync(ctx)
    finally:
        exec_mod.subprocess.run = real_run

    assert result.is_success
    assert "requests" not in req.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Detector + validation gate (agent layer)
# ---------------------------------------------------------------------------


def _write_result(path: str = "/tmp/foo.py") -> ToolResult:
    return ToolResult(
        status=ToolStatus.SUCCESS,
        data=path,
        message="wrote",
        metadata={
            "function_name": "write_file",
            "file_path": path,
            "post_write_validation_required": True,
            "post_write_validation_command": f"python -m py_compile {path}",
        },
    )


def _named_result(name: str, exit_code: int = 0) -> ToolResult:
    return ToolResult(
        status=ToolStatus.SUCCESS if exit_code == 0 else ToolStatus.ERROR,
        message="ok",
        metadata={"function_name": name, "exit_code": exit_code},
    )


def test_detector_flags_write_without_validation():
    flagged = DeileAgent._detect_unvalidated_writes([_write_result()])
    assert len(flagged) == 1


def test_detector_clears_when_bash_execute_followed():
    flagged = DeileAgent._detect_unvalidated_writes(
        [_write_result(), _named_result("bash_execute")]
    )
    assert flagged == []


def test_detector_clears_when_python_execute_followed():
    flagged = DeileAgent._detect_unvalidated_writes(
        [_write_result(), _named_result("python_execute")]
    )
    assert flagged == []


def test_detector_pip_install_alone_is_not_validation():
    """pip_install fixes a missing dep but doesn't *prove* the file works.
    The model must still run the file afterwards — so the gate must still fire."""
    flagged = DeileAgent._detect_unvalidated_writes(
        [_write_result(), _named_result("pip_install")]
    )
    assert len(flagged) == 1


def test_detector_ignores_non_executable_writes():
    md_write = ToolResult(
        status=ToolStatus.SUCCESS,
        message="wrote",
        metadata={"function_name": "write_file", "file_path": "/tmp/notes.md"},
    )
    flagged = DeileAgent._detect_unvalidated_writes([md_write])
    assert flagged == []


@pytest.mark.parametrize(
    "text",
    [
        "Vou testar agora!",
        "Vou rodar isso pra ver",
        "Deixa eu rodar o programa",
        "Vamos validar",
        "I'll run it now",
        "Let me verify the import",
        "Let me install it",
        "Testing it now",
        "Running that for you",
    ],
)
def test_promise_pattern_detected(text):
    assert DeileAgent._contains_promise_pattern(text)


@pytest.mark.parametrize(
    "text",
    [
        "Pronto, criei o arquivo conforme pedido.",
        "O programa está em /tmp/foo.py.",
        "Aqui está o código que você pediu.",
        "I created the file at the requested path.",
        "",
        "Done — see the diff above.",
    ],
)
def test_promise_pattern_no_false_positive(text):
    assert not DeileAgent._contains_promise_pattern(text)


@pytest.mark.asyncio
async def test_validation_gate_reentrancy_guard():
    """When _validation_gate_active is set on the session, the gate must NOT
    re-invoke — that's how we prevent recursion when the gate's own retry
    runs into the same condition."""
    agent = DeileAgent.__new__(DeileAgent)  # bypass __init__
    session = AgentSession(session_id="t")
    session.context_data["_validation_gate_active"] = True
    content_in = "Vou testar agora"  # would normally trigger
    content_out, results_out = await agent._apply_validation_gate(
        user_input="",
        parse_result=None,
        session=session,
        content=content_in,
        tool_results=[],
    )
    assert content_out == content_in
    assert results_out == []


@pytest.mark.asyncio
async def test_validation_gate_re_invokes_once_on_unvalidated_write():
    """When a write_file is detected without validation, the gate calls
    _process_iterative_function_calling exactly once with the synthetic
    prompt, and concatenates the resulting tool_results."""
    agent = DeileAgent.__new__(DeileAgent)
    session = AgentSession(session_id="t")

    async def fake_iter(*, user_input, parse_result, session):
        # Verify the synthetic prompt mentions the gate AND the file
        assert "INTERNAL_VALIDATION_GATE" in user_input
        assert "/tmp/foo.py" in user_input
        return "validated, exit 0", [_named_result("bash_execute")]

    agent._process_iterative_function_calling = fake_iter

    new_content, new_results = await agent._apply_validation_gate(
        user_input="orig",
        parse_result=None,
        session=session,
        content="created file",
        tool_results=[_write_result()],
    )

    assert new_content == "validated, exit 0"
    assert len(new_results) == 2  # original write + new bash_execute
    assert new_results[0].metadata["function_name"] == "write_file"
    assert new_results[1].metadata["function_name"] == "bash_execute"
    # Gate marker must be cleared after the retry
    assert "_validation_gate_active" not in session.context_data
    # History contains the pre-gate assistant turn AND the synthetic user prompt
    roles = [e["role"] for e in session.conversation_history]
    assert roles == ["assistant", "user"]
    assert session.conversation_history[0]["metadata"].get("validation_gate_pre") is True
    assert session.conversation_history[1]["metadata"].get("validation_gate") is True


@pytest.mark.asyncio
async def test_validation_gate_no_op_when_validated():
    agent = DeileAgent.__new__(DeileAgent)
    session = AgentSession(session_id="t")

    # _process_iterative_function_calling must NOT be called
    agent._process_iterative_function_calling = AsyncMock(side_effect=AssertionError("should not run"))

    content_out, results_out = await agent._apply_validation_gate(
        user_input="orig",
        parse_result=None,
        session=session,
        content="all good",
        tool_results=[_write_result(), _named_result("bash_execute")],
    )
    assert content_out == "all good"
    assert len(results_out) == 2
    # No history was added — gate was a no-op
    assert session.conversation_history == []
