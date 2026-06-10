"""Execution tools for DEILE — subprocess-based Python, pip, and pytest runners."""

import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from ..core.exceptions import ValidationError
from .base import (SecurityLevel, SyncTool, ToolCategory, ToolContext,
                   ToolResult, ToolSchema, ToolStatus)

logger = logging.getLogger(__name__)


def _run_subprocess(
    cmd: list[str],
    *,
    timeout: int,
    cwd: str | None,
    timeout_msg: str,
    timeout_error_msg: str,
    generic_err_prefix: str,
) -> tuple[subprocess.CompletedProcess | None, ToolResult | None]:
    """Run ``cmd`` capturing stdout/stderr.

    Returns ``(proc, None)`` on a successful spawn (regardless of returncode),
    or ``(None, error_result)`` when the subprocess timed out or raised any
    other exception — in which case the caller just returns ``error_result``.

    Keeps the timeout/exception envelope identical across the three execution
    tools so each tool only has to interpret ``returncode`` to build its
    success/error ``ToolResult``.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return proc, None
    except subprocess.TimeoutExpired:
        return None, ToolResult.error_result(
            message=timeout_msg,
            error=TimeoutError(timeout_error_msg),
        )
    except Exception as exc:  # noqa: BLE001 — every path returns an error result
        return None, ToolResult.error_result(
            message=f"{generic_err_prefix}: {exc}",
            error=exc,
        )


class PythonExecutionTool(SyncTool):
    """Run a Python snippet in a subprocess with timeout.

    Isolation: subprocess + cwd bound to the session's working directory + timeout.
    """

    @property
    def name(self) -> str:
        return "python_execute"

    @property
    def description(self) -> str:
        return "Executes a Python snippet in a subprocess and returns stdout/stderr/exit_code"

    @property
    def category(self) -> str:
        return "execution"

    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa código Python via subprocess."""
        code = context.parsed_args.get("code")
        timeout = context.parsed_args.get("timeout", 30)

        if not code:
            return ToolResult.error_result(
                message="No Python code provided",
                error=ValidationError("code is required"),
            )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            temp_file = f.name

        try:
            result, err = _run_subprocess(
                [sys.executable, temp_file],
                timeout=timeout,
                cwd=context.working_directory,
                timeout_msg=f"Python code timed out after {timeout} seconds",
                timeout_error_msg=f"Execution timeout: {timeout}s",
                generic_err_prefix="Error executing Python code",
            )
            if err is not None:
                return err

            stdout = result.stdout or ""
            stderr = result.stderr or ""

            if result.returncode == 0:
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    data=stdout.strip(),
                    message=stdout.strip() or "Python executed successfully (no stdout)",
                    metadata={
                        "exit_code": result.returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                    },
                )
            return ToolResult.error_result(
                data=stderr.strip(),
                message=(
                    f"Python failed with exit code {result.returncode}.\n"
                    f"stderr:\n{stderr.strip()}"
                ),
                metadata={
                    "exit_code": result.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )
        finally:
            try:
                os.unlink(temp_file)
            except OSError:
                pass


class PipInstallTool(SyncTool):
    """Install a Python package via pip and (optionally) persist it to requirements.txt.

    The agent's primary recovery for ModuleNotFoundError. Validates the
    package spec to block shell injection, runs `pip install` in a
    subprocess, and updates requirements.txt by appending the spec if no
    line for that package already exists (case-insensitive PEP 503 name
    normalization). Idempotent: running twice does not duplicate lines.
    """

    _SPEC_RE = re.compile(
        r"^[A-Za-z0-9][A-Za-z0-9._\-]*"
        r"(?:\[[A-Za-z0-9._,\-]+\])?"
        r"(?:[<>=!~]=?[A-Za-z0-9._\-+*]+(?:,[<>=!~]=?[A-Za-z0-9._\-+*]+)*)?$"
    )

    @property
    def name(self) -> str:
        return "pip_install"

    @property
    def description(self) -> str:
        return (
            "Installs a Python package via pip and (by default) appends it to "
            "requirements.txt if not already present. Use this in response to "
            "ModuleNotFoundError or when adding a new third-party dependency."
        )

    @property
    def category(self) -> str:
        return "execution"

    @staticmethod
    def _normalize_pkg_name(spec: str) -> str:
        """Return the canonical PEP 503 name from a spec like 'requests>=2.0[extras]'."""
        from packaging.utils import canonicalize_name
        bare = re.sub(r"\[.*?\]", "", spec)
        bare = re.split(r"[<>=!~]", bare, maxsplit=1)[0]
        return canonicalize_name(bare.strip())

    @classmethod
    def _requirements_already_lists(cls, req_path: Path, package_spec: str) -> bool:
        """True if requirements.txt has a line for the same normalized package name."""
        if not req_path.exists():
            return False
        target = cls._normalize_pkg_name(package_spec)
        try:
            for raw in req_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                if "#" in line:
                    line = line.split("#", 1)[0].strip()
                if cls._normalize_pkg_name(line) == target:
                    return True
        except OSError:
            return False
        return False

    @staticmethod
    def _append_to_requirements(req_path: Path, package_spec: str) -> None:
        """Append a spec to requirements.txt, creating the file if needed."""
        existing = ""
        if req_path.exists():
            try:
                existing = req_path.read_text(encoding="utf-8")
            except OSError:
                existing = ""
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        req_path.write_text(existing + sep + package_spec + "\n", encoding="utf-8")

    def execute_sync(self, context: ToolContext) -> ToolResult:
        package = context.parsed_args.get("package")
        version = context.parsed_args.get("version")
        update_requirements = context.parsed_args.get("update_requirements", True)
        requirements_file = context.parsed_args.get("requirements_file", "requirements.txt")
        upgrade = context.parsed_args.get("upgrade", False)
        timeout = context.parsed_args.get("timeout", 120)

        if not package:
            return ToolResult.error_result(
                message="No package provided",
                error=ValidationError("package is required"),
            )

        spec = f"{package}=={version}" if version else package

        if not self._SPEC_RE.match(spec):
            return ToolResult.error_result(
                message=(
                    f"Refused to install: {spec!r} does not match PEP 508 package "
                    "spec format. Allowed: name + optional [extras] + optional "
                    "version specifier (no whitespace, no shell metachars)."
                ),
                error=ValidationError("invalid package spec"),
            )

        cmd = [sys.executable, "-m", "pip", "install"]
        if upgrade:
            cmd.append("--upgrade")
        cmd.append(spec)

        result, err = _run_subprocess(
            cmd,
            timeout=timeout,
            cwd=context.working_directory,
            timeout_msg=f"pip install {spec} timed out after {timeout} seconds",
            timeout_error_msg=f"pip timeout: {timeout}s",
            generic_err_prefix="Failed to invoke pip",
        )
        if err is not None:
            return err

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        if result.returncode != 0:
            return ToolResult.error_result(
                data=stderr.strip(),
                message=(
                    f"pip install {spec} failed with exit code {result.returncode}.\n"
                    f"stderr:\n{stderr.strip()}"
                ),
                metadata={
                    "exit_code": result.returncode,
                    "spec": spec,
                    "stdout": stdout,
                    "stderr": stderr,
                    "requirements_updated": False,
                },
            )

        requirements_updated = False
        requirements_note = ""
        if update_requirements:
            req_path = Path(context.working_directory) / requirements_file
            if self._requirements_already_lists(req_path, spec):
                requirements_note = (
                    f"{requirements_file} already lists {self._normalize_pkg_name(spec)} "
                    "(no change)."
                )
            else:
                try:
                    self._append_to_requirements(req_path, spec)
                    requirements_updated = True
                    requirements_note = f"Appended {spec!r} to {requirements_file}."
                except OSError as e:
                    requirements_note = f"WARNING: could not update {requirements_file}: {e}"

        msg = f"Installed {spec}."
        if requirements_note:
            msg += f" {requirements_note}"

        return ToolResult(
            status=ToolStatus.SUCCESS,
            data=stdout.strip(),
            message=msg,
            metadata={
                "exit_code": 0,
                "spec": spec,
                "stdout": stdout,
                "stderr": stderr,
                "requirements_updated": requirements_updated,
                "requirements_file": requirements_file,
            },
        )


class TestRunnerTool(SyncTool):
    """Run a project's test suite (pytest / unittest / nose) in a subprocess.

    Until this tool carried a :class:`ToolSchema` it was registered but
    invisible to every LLM function-calling exporter (they skip tools whose
    ``schema`` is ``None``), so the agent could never invoke it even though
    ``validation_gate`` already treated ``run_tests`` as a real validation
    tool. The inline schema below makes it callable.
    """

    @property
    def name(self) -> str:
        return "run_tests"

    @property
    def description(self) -> str:
        return "Runs project tests and returns results"

    @property
    def category(self) -> str:
        return "testing"

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="run_tests",
                description=(
                    "Run the project's test suite in a subprocess and return "
                    "stdout/stderr plus pass/fail status. Use after editing code "
                    "to verify nothing regressed. Defaults to pytest against "
                    "'tests/'; pass `test_path` to scope to a file or directory "
                    "and `verbose=true` for per-test output (pytest only)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "test_path": {
                            "type": "string",
                            "description": (
                                "File or directory to test (e.g. "
                                "'deile/tests/tools/'). Defaults to 'tests/'. "
                                "Use '.' to run everything from the working dir."
                            ),
                        },
                        "test_type": {
                            "type": "string",
                            "enum": ["pytest", "unittest", "nose"],
                            "description": "Test runner to use. Defaults to pytest.",
                        },
                        "verbose": {
                            "type": "boolean",
                            "description": (
                                "Add -v for per-test output (pytest only). "
                                "Defaults to false."
                            ),
                        },
                    },
                },
                required=[],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.EXECUTION,
                max_execution_time=300,
            )
        )

    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa testes do projeto"""
        test_path = context.parsed_args.get("test_path", "tests/")
        test_type = context.parsed_args.get("test_type", "pytest")
        verbose = context.parsed_args.get("verbose", False)

        test_commands = {
            "pytest": ["python3", "-m", "pytest"],
            "unittest": ["python3", "-m", "unittest"],
            "nose": ["nosetests"]
        }

        if test_type not in test_commands:
            return ToolResult.error_result(
                message=f"Unsupported test type: {test_type}",
                error=ValidationError(f"test_type must be one of: {list(test_commands.keys())}")
            )

        cmd = test_commands[test_type].copy()

        if test_path and test_path != ".":
            cmd.append(test_path)

        if verbose and test_type == "pytest":
            cmd.append("-v")

        result, err = _run_subprocess(
            cmd,
            timeout=300,
            cwd=context.working_directory,
            timeout_msg="Tests timed out after 5 minutes",
            timeout_error_msg="Test execution timeout",
            generic_err_prefix="Error running tests",
        )
        if err is not None:
            return err

        output = result.stdout.strip() if result.stdout else ""
        error_output = result.stderr.strip() if result.stderr else ""

        if test_type == "pytest":
            if "failed" in output.lower() or result.returncode != 0:
                status = ToolStatus.ERROR
                message = "Tests failed"
            else:
                status = ToolStatus.SUCCESS
                message = "All tests passed"
        else:
            status = ToolStatus.SUCCESS if result.returncode == 0 else ToolStatus.ERROR
            message = "Tests completed" if result.returncode == 0 else "Tests failed"

        return ToolResult(
            status=status,
            data=output,
            message=message,
            metadata={
                "test_type": test_type,
                "test_path": test_path,
                "exit_code": result.returncode,
                "stdout": output,
                "stderr": error_output
            }
        )
