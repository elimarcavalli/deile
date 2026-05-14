"""Execution tools for DEILE — subprocess-based command, Python, pip, and pytest runners."""

import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict

from ..core.exceptions import ValidationError
from ._shell_security import is_blocked
from .base import SyncTool, ToolContext, ToolResult, ToolStatus

logger = logging.getLogger(__name__)


class EnhancedExecutionTool(SyncTool):
    """Subprocess-based shell command execution with timeout and basic safety screen."""

    @property
    def name(self) -> str:
        return "execute_command_enhanced"

    @property
    def description(self) -> str:
        return "Executes a shell command via subprocess with timeout and a basic safety screen"

    @property
    def category(self) -> str:
        return "execution"

    def execute_sync(self, context: ToolContext) -> ToolResult:
        command = context.parsed_args.get("command")
        timeout = context.parsed_args.get("timeout", 30)
        env_vars = context.parsed_args.get("env", {})

        if not command:
            return ToolResult.error_result(
                message="No command provided",
                error=ValidationError("command is required"),
            )

        if not self._is_command_safe(command):
            return ToolResult.error_result(
                message=f"Potentially dangerous command blocked: {command}",
                error=PermissionError("Command blocked by security policy"),
            )

        try:
            return self._execute_standard(command, context, timeout, env_vars)
        except Exception as e:
            logger.error("Enhanced execution error: %s", e)
            return ToolResult.error_result(
                message=f"Execution failed: {str(e)}",
                error=e,
            )

    def _execute_standard(self, command: str, context: ToolContext,
                          timeout: int, env_vars: Dict[str, str]) -> ToolResult:
        try:
            full_env = {**os.environ, **env_vars}
            result = subprocess.run(
                command,
                shell=True,  # nosec B602 — execution tool; command validated upstream
                cwd=context.working_directory,
                env=full_env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            output = result.stdout.strip() if result.stdout else ""
            error_output = result.stderr.strip() if result.stderr else ""
            status = ToolStatus.SUCCESS if result.returncode == 0 else ToolStatus.ERROR

            return ToolResult(
                status=status,
                data=output,
                message=f"Command executed with exit code {result.returncode}",
                metadata={
                    "command": command,
                    "exit_code": result.returncode,
                    "stdout": output,
                    "stderr": error_output,
                    "working_directory": context.working_directory,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult.error_result(
                message=f"Command timed out after {timeout} seconds",
                error=TimeoutError(f"Command timeout: {timeout}s"),
            )

    def _is_command_safe(self, command: str) -> bool:
        """Reject commands matching any DANGEROUS pattern in `_shell_security`."""
        return not is_blocked(command)


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

        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as f:
                f.write(code)
                temp_file = f.name

            try:
                result = subprocess.run(
                    [sys.executable, temp_file],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=context.working_directory,
                )

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

        except subprocess.TimeoutExpired:
            return ToolResult.error_result(
                message=f"Python code timed out after {timeout} seconds",
                error=TimeoutError(f"Execution timeout: {timeout}s"),
            )
        except Exception as e:
            return ToolResult.error_result(
                message=f"Error executing Python code: {str(e)}",
                error=e,
            )


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

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=context.working_directory,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.error_result(
                message=f"pip install {spec} timed out after {timeout} seconds",
                error=TimeoutError(f"pip timeout: {timeout}s"),
            )
        except Exception as e:
            return ToolResult.error_result(
                message=f"Failed to invoke pip: {e}",
                error=e,
            )

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
    """Ferramenta para execução de testes (implementação futura)"""

    @property
    def name(self) -> str:
        return "run_tests"

    @property
    def description(self) -> str:
        return "Runs project tests and returns results"

    @property
    def category(self) -> str:
        return "testing"

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

        try:
            cmd = test_commands[test_type].copy()

            if test_path and test_path != ".":
                cmd.append(test_path)

            if verbose and test_type == "pytest":
                cmd.append("-v")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=context.working_directory
            )

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

        except subprocess.TimeoutExpired:
            return ToolResult.error_result(
                message="Tests timed out after 5 minutes",
                error=TimeoutError("Test execution timeout")
            )
        except Exception as e:
            return ToolResult.error_result(
                message=f"Error running tests: {str(e)}",
                error=e
            )
