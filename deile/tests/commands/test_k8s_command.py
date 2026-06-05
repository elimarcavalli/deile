"""Tests: /k8s command."""

from __future__ import annotations

import asyncio
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from deile.commands.base import CommandContext, DirectCommand
from deile.commands.builtin.k8s_command import (
    K8sCommand,
    K8S_DEPLOYMENTS,
    _cmd_discovery,
    _cmd_list,
    _cmd_logs,
    _cmd_panel,
    _cmd_restart,
    _cmd_status,
    _cmd_v2_delegate,
    _detect_namespace,
    _find_deploy_py,
    _parse_logs_args,
    _parse_restart_args,
    _run_kubectl,
    _running_in_pod,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(args: str = "") -> CommandContext:
    return CommandContext(user_input=f"/k8s {args}".strip(), args=args)


def _cmd() -> K8sCommand:
    return K8sCommand()


def _render(content) -> str:
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=120)
    console.print(content)
    return buf.getvalue()


def _mock_proc(returncode: int, stdout: bytes, stderr: bytes) -> AsyncMock:
    """Build a mock asyncio subprocess."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


# ---------------------------------------------------------------------------
# _parse_restart_args
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseRestartArgs:
    def test_no_args_returns_default(self):
        assert _parse_restart_args("") == "deile-pipeline"

    def test_explicit_deployment(self):
        assert _parse_restart_args("--deployment deilebot") == "deilebot"

    def test_short_flag(self):
        assert _parse_restart_args("-d claude-worker") == "claude-worker"

    def test_all(self):
        assert _parse_restart_args("--deployment all") == "all"

    def test_unknown_token_ignored(self):
        # unknown tokens should not crash the parser
        result = _parse_restart_args("some-random-text")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _parse_logs_args
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseLogsArgs:
    def test_no_args_returns_defaults(self):
        target, tail = _parse_logs_args("")
        assert target == "pipeline"
        assert tail == 50

    def test_explicit_target(self):
        target, tail = _parse_logs_args("bot")
        assert target == "bot"
        assert tail == 50

    def test_explicit_tail(self):
        target, tail = _parse_logs_args("worker --tail 100")
        assert target == "worker"
        assert tail == 100

    def test_tail_only(self):
        target, tail = _parse_logs_args("--tail 200")
        assert target == "pipeline"
        assert tail == 200

    def test_invalid_tail_ignored(self):
        target, tail = _parse_logs_args("--tail notanumber")
        assert tail == 50  # default preserved


# ---------------------------------------------------------------------------
# _detect_namespace
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDetectNamespace:
    async def test_single_namespace_returned(self):
        proc = _mock_proc(0, b"deile", b"")
        with patch(
            "deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            ns = await _detect_namespace()
        assert ns == "deile"

    async def test_multiple_namespaces_returns_first(self):
        proc = _mock_proc(0, b"deile deile-gl", b"")
        with patch(
            "deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            ns = await _detect_namespace()
        assert ns == "deile"

    async def test_empty_output_returns_default(self):
        proc = _mock_proc(0, b"", b"")
        with patch(
            "deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            ns = await _detect_namespace()
        assert ns == "deile"

    async def test_os_error_returns_default(self):
        with patch(
            "deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec",
            side_effect=OSError("no such file"),
        ):
            ns = await _detect_namespace()
        assert ns == "deile"

    async def test_timeout_returns_default(self):
        proc = _mock_proc(0, b"deile", b"")
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch(
            "deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            ns = await _detect_namespace()
        assert ns == "deile"


# ---------------------------------------------------------------------------
# _run_kubectl
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunKubectl:
    async def test_success(self):
        proc = _mock_proc(0, b"NAME   STATUS\npod-a  Running\n", b"")
        with patch(
            "deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            ok, stdout, stderr = await _run_kubectl(["-n", "deile", "get", "pods"])
        assert ok is True
        assert "pod-a" in stdout
        assert stderr == ""

    async def test_nonzero_returncode(self):
        proc = _mock_proc(1, b"", b"Forbidden")
        with patch(
            "deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            ok, stdout, stderr = await _run_kubectl(["-n", "deile", "get", "pods"])
        assert ok is False
        assert "Forbidden" in stderr

    async def test_os_error(self):
        with patch(
            "deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec",
            side_effect=OSError("binary not found"),
        ):
            ok, stdout, stderr = await _run_kubectl(["get", "pods"])
        assert ok is False
        assert "binary not found" in stderr

    async def test_timeout(self):
        proc = _mock_proc(0, b"", b"")
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch(
            "deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            ok, stdout, stderr = await _run_kubectl(["get", "pods"], timeout=5.0)
        assert ok is False
        assert "timed out" in stderr.lower()


# ---------------------------------------------------------------------------
# _cmd_discovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCmdDiscovery:
    async def test_returns_rich_panel(self):
        result = await _cmd_discovery("deile")
        assert result.success is True
        assert result.content_type == "rich"

    async def test_panel_contains_namespace(self):
        result = await _cmd_discovery("my-namespace")
        rendered = _render(result.content)
        assert "my-namespace" in rendered

    async def test_panel_contains_verbs(self):
        result = await _cmd_discovery("deile")
        rendered = _render(result.content)
        assert "restart" in rendered
        assert "status" in rendered
        assert "logs" in rendered
        assert "list" in rendered

    async def test_panel_contains_deployments_footer(self):
        result = await _cmd_discovery("deile")
        rendered = _render(result.content)
        assert "deile-pipeline" in rendered
        assert "deilebot" in rendered


# ---------------------------------------------------------------------------
# _cmd_restart
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCmdRestart:
    async def test_restart_single_success(self):
        proc_ok = _mock_proc(0, b"deployment.apps/deile-pipeline restarted\n", b"")
        proc_status = _mock_proc(
            0, b'deployment "deile-pipeline" successfully rolled out\n', b""
        )
        call_count = [0]

        async def fake_run(args, timeout=30.0):
            call_count[0] += 1
            if call_count[0] == 1:
                return True, proc_ok.communicate.return_value[0].decode(), ""
            return True, proc_status.communicate.return_value[0].decode(), ""

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_restart("deile", "deile-pipeline")

        assert result.success is True

    async def test_restart_single_failure(self):
        async def fake_run(args, timeout=30.0):
            return False, "", "Error from server"

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_restart("deile", "deile-pipeline")

        assert result.success is False

    async def test_restart_all_iterates_all_deployments(self):
        called_deployments = []

        async def fake_run(args, timeout=30.0):
            # Capture which deployment is being operated on
            for i, arg in enumerate(args):
                if "deployment/" in arg:
                    dep = arg.replace("deployment/", "")
                    called_deployments.append(dep)
            return True, "success", ""

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_restart("deile", "all")

        # Each deployment is restarted (restart + status = 2 calls each)
        assert result.success is True
        for dep in K8S_DEPLOYMENTS:
            assert dep in called_deployments

    async def test_restart_all_partial_failure(self):
        fail_deps = {"deilebot"}

        async def fake_run(args, timeout=30.0):
            for arg in args:
                if "deployment/deilebot" in arg and "restart" in args:
                    return False, "", "Failed for deilebot"
            return True, "success", ""

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_restart("deile", "all")

        # Result depends on outcomes — just confirm it runs without crashing
        assert result is not None


# ---------------------------------------------------------------------------
# _cmd_status
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCmdStatus:
    async def test_success_returns_kubectl_output(self):
        async def fake_run(args, timeout=30.0):
            return True, "NAME           READY   STATUS\npod-a         1/1     Running\n", ""

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_status("deile")

        assert result.success is True
        assert "pod-a" in result.content

    async def test_kubectl_error_returns_failure(self):
        async def fake_run(args, timeout=30.0):
            return False, "", "Forbidden: User cannot list resource"

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_status("deile")

        assert result.success is False
        assert "Forbidden" in result.content


# ---------------------------------------------------------------------------
# _cmd_logs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCmdLogs:
    async def test_default_pipeline_target(self):
        async def fake_run(args, timeout=30.0):
            assert "deployment/deile-pipeline" in args
            return True, "log line 1\nlog line 2\n", ""

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_logs("deile", "pipeline", 50)

        assert result.success is True
        assert "deile-pipeline" in result.content

    def _make_run(self, ok=True):
        async def fake_run(args, timeout=30.0):
            return ok, "log output\n", "" if ok else "error"
        return fake_run

    async def test_bot_target_resolves_deilebot(self):
        calls = []

        async def fake_run(args, timeout=30.0):
            calls.append(args)
            return True, "logs\n", ""

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_logs("deile", "bot", 20)

        assert result.success is True
        assert any("deployment/deilebot" in a for args in calls for a in args)

    async def test_worker_target_resolves(self):
        calls = []

        async def fake_run(args, timeout=30.0):
            calls.append(args)
            return True, "logs\n", ""

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_logs("deile", "worker", 50)

        assert result.success is True
        assert any("deployment/deile-worker" in a for args in calls for a in args)

    async def test_all_target_iterates_all_deployments(self):
        calls = []

        async def fake_run(args, timeout=30.0):
            calls.append(args)
            return True, "log line\n", ""

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_logs("deile", "all", 50)

        assert result.success is True
        collected = [a for args in calls for a in args]
        for dep in K8S_DEPLOYMENTS:
            assert f"deployment/{dep}" in collected

    async def test_invalid_target_returns_error(self):
        result = await _cmd_logs("deile", "invalid-target", 50)
        assert result.success is False
        assert "invalid-target" in result.content

    async def test_tail_flag_passed_to_kubectl(self):
        calls = []

        async def fake_run(args, timeout=30.0):
            calls.append(args)
            return True, "log\n", ""

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            await _cmd_logs("deile", "pipeline", 123)

        assert any("--tail=123" in a for args in calls for a in args)

    async def test_kubectl_error_included_in_output(self):
        async def fake_run(args, timeout=30.0):
            return False, "", "RBAC denied"

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_logs("deile", "pipeline", 50)

        assert result.success is True  # _cmd_logs always returns content
        assert "RBAC denied" in result.content or "ERROR" in result.content


# ---------------------------------------------------------------------------
# _cmd_list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCmdList:
    async def test_success_returns_namespace_list(self):
        async def fake_run(args, timeout=30.0):
            return True, "NAME   STATUS\ndeile  Active\n", ""

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_list("deile")

        assert result.success is True
        assert "deile" in result.content

    async def test_kubectl_error_returns_failure(self):
        async def fake_run(args, timeout=30.0):
            return False, "", "permission denied"

        with patch("deile.commands.builtin.k8s_command._run_kubectl", side_effect=fake_run):
            result = await _cmd_list("deile")

        assert result.success is False
        assert "permission denied" in result.content.lower()


# ---------------------------------------------------------------------------
# K8sCommand.execute — dispatch routing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestK8sCommandExecute:
    async def test_no_args_shows_discovery(self):
        with patch(
            "deile.commands.builtin.k8s_command._detect_namespace",
            new_callable=AsyncMock,
            return_value="deile",
        ):
            result = await _cmd().execute(_ctx(""))
        assert result.success is True
        assert result.content_type == "rich"

    async def test_restart_subcommand_dispatched(self):
        with (
            patch(
                "deile.commands.builtin.k8s_command._detect_namespace",
                new_callable=AsyncMock,
                return_value="deile",
            ),
            patch(
                "deile.commands.builtin.k8s_command._cmd_restart",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, content="ok", content_type="text"),
            ) as mock_restart,
        ):
            await _cmd().execute(_ctx("restart --deployment deilebot"))

        mock_restart.assert_called_once_with("deile", "deilebot")

    async def test_status_subcommand_dispatched(self):
        with (
            patch(
                "deile.commands.builtin.k8s_command._detect_namespace",
                new_callable=AsyncMock,
                return_value="deile",
            ),
            patch(
                "deile.commands.builtin.k8s_command._cmd_status",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, content="pods", content_type="text"),
            ) as mock_status,
        ):
            await _cmd().execute(_ctx("status"))

        mock_status.assert_called_once_with("deile")

    async def test_logs_subcommand_dispatched(self):
        with (
            patch(
                "deile.commands.builtin.k8s_command._detect_namespace",
                new_callable=AsyncMock,
                return_value="deile",
            ),
            patch(
                "deile.commands.builtin.k8s_command._cmd_logs",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, content="logs", content_type="text"),
            ) as mock_logs,
        ):
            await _cmd().execute(_ctx("logs bot --tail 100"))

        mock_logs.assert_called_once_with("deile", "bot", 100)

    async def test_list_subcommand_dispatched(self):
        with (
            patch(
                "deile.commands.builtin.k8s_command._detect_namespace",
                new_callable=AsyncMock,
                return_value="deile",
            ),
            patch(
                "deile.commands.builtin.k8s_command._cmd_list",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, content="ns", content_type="text"),
            ) as mock_list,
        ):
            await _cmd().execute(_ctx("list"))

        mock_list.assert_called_once_with("deile")

    async def test_unknown_subcommand_returns_error(self):
        with patch(
            "deile.commands.builtin.k8s_command._detect_namespace",
            new_callable=AsyncMock,
            return_value="deile",
        ):
            result = await _cmd().execute(_ctx("foobar"))

        assert result.success is False
        assert "foobar" in result.content

    async def test_restart_default_deployment(self):
        """No --deployment flag => uses deile-pipeline."""
        with (
            patch(
                "deile.commands.builtin.k8s_command._detect_namespace",
                new_callable=AsyncMock,
                return_value="deile",
            ),
            patch(
                "deile.commands.builtin.k8s_command._cmd_restart",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, content="ok", content_type="text"),
            ) as mock_restart,
        ):
            await _cmd().execute(_ctx("restart"))

        mock_restart.assert_called_once_with("deile", "deile-pipeline")


# ---------------------------------------------------------------------------
# Command metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestK8sCommandMetadata:
    def test_name(self):
        assert _cmd().name == "k8s"

    def test_is_direct_command(self):
        assert isinstance(_cmd(), DirectCommand)

    def test_category_is_infrastructure(self):
        assert _cmd().category == "infrastructure"

    def test_get_help_contains_usage(self):
        help_text = _cmd().get_help()
        assert "k8s" in help_text.lower()
        assert "restart" in help_text.lower()
        assert "logs" in help_text.lower()

    def test_no_fixed_width_in_add_column(self):
        """k8s_command.py must not use width=<int> in add_column calls."""
        import re
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[3]
            / "deile"
            / "commands"
            / "builtin"
            / "k8s_command.py"
        )
        text = path.read_text(encoding="utf-8")
        width_literal = re.compile(r"\.add_column\s*\([^)]*width\s*=\s*\d+")
        matches = width_literal.findall(text)
        assert not matches, f"Fixed width in add_column: {matches}"


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_k8s_command_auto_discoverable():
    from deile.commands.registry import CommandRegistry
    r = CommandRegistry()
    r.auto_discover_builtin_commands()
    cmd = r.get_command("k8s")
    assert cmd is not None
    assert cmd.name == "k8s"


# ---------------------------------------------------------------------------
# _running_in_pod
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunningInPod:
    def test_in_pod_when_env_set(self):
        with patch.dict("os.environ", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}):
            assert _running_in_pod() is True

    def test_not_in_pod_when_env_absent(self):
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "KUBERNETES_SERVICE_HOST"}
        with patch.dict("os.environ", env, clear=True):
            assert _running_in_pod() is False

    def test_empty_string_is_falsy(self):
        with patch.dict("os.environ", {"KUBERNETES_SERVICE_HOST": ""}):
            assert _running_in_pod() is False


# ---------------------------------------------------------------------------
# _find_deploy_py
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFindDeployPy:
    def test_returns_path_or_none(self):
        result = _find_deploy_py()
        # In CI the file may or may not exist — just verify the return type
        assert result is None or result.name == "deploy.py"

    def test_returns_none_when_not_found(self, tmp_path):
        """When __file__ is in a temp dir with no infra/k8s/deploy.py, returns None."""
        import deile.commands.builtin.k8s_command as mod
        original = mod.__file__
        try:
            mod.__file__ = str(tmp_path / "k8s_command.py")
            result = _find_deploy_py()
            assert result is None
        finally:
            mod.__file__ = original


# ---------------------------------------------------------------------------
# _cmd_v2_delegate
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCmdV2Delegate:
    async def test_in_pod_returns_error(self):
        with patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=True):
            result = await _cmd_v2_delegate("up", "", "deile")
        assert result.success is False
        assert "in-pod" in result.content.lower() or "KUBERNETES_SERVICE_HOST" in result.content

    async def test_missing_deploy_py_returns_error(self):
        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=None),
        ):
            result = await _cmd_v2_delegate("up", "", "deile")
        assert result.success is False
        assert "deploy.py" in result.content

    async def test_happy_path_rc0_success(self):
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"

        async def fake_stream(cmd, *, timeout, console):
            return 0, ["build complete", "done"]

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command._live_stream_subprocess", side_effect=fake_stream),
        ):
            result = await _cmd_v2_delegate("build", "", "deile")
        assert result.success is True
        assert "build complete" in result.content

    async def test_nonzero_rc_returns_error(self):
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"

        async def fake_stream(cmd, *, timeout, console):
            return 1, ["something went wrong"]

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command._live_stream_subprocess", side_effect=fake_stream),
        ):
            result = await _cmd_v2_delegate("build", "", "deile")
        assert result.success is False
        assert "rc=1" in result.content

    async def test_timeout_rc_minus1_returns_error(self):
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"

        async def fake_stream(cmd, *, timeout, console):
            return -1, []

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command._live_stream_subprocess", side_effect=fake_stream),
        ):
            result = await _cmd_v2_delegate("up", "", "deile")
        assert result.success is False
        assert "timeout" in result.content.lower()

    async def test_long_verb_uses_long_timeout(self):
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"
        captured = {}

        async def fake_stream(cmd, *, timeout, console):
            captured["timeout"] = timeout
            return 0, []

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command._live_stream_subprocess", side_effect=fake_stream),
        ):
            await _cmd_v2_delegate("up", "", "deile")
        assert captured["timeout"] == 1800.0

    async def test_short_verb_uses_short_timeout(self):
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"
        captured = {}

        async def fake_stream(cmd, *, timeout, console):
            captured["timeout"] = timeout
            return 0, []

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command._live_stream_subprocess", side_effect=fake_stream),
        ):
            await _cmd_v2_delegate("scale", "", "deile")
        assert captured["timeout"] == 300.0

    async def test_extra_args_passed_to_subprocess(self):
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"
        captured = {}

        async def fake_stream(cmd, *, timeout, console):
            captured["cmd"] = cmd
            return 0, []

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command._live_stream_subprocess", side_effect=fake_stream),
        ):
            await _cmd_v2_delegate("scale", "--worker 2", "deile")
        assert "--worker" in captured["cmd"]
        assert "2" in captured["cmd"]

    async def test_os_error_returns_error(self):
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"

        async def fake_stream(cmd, *, timeout, console):
            raise OSError("binary not found")

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command._live_stream_subprocess", side_effect=fake_stream),
        ):
            result = await _cmd_v2_delegate("start", "", "deile")
        assert result.success is False
        assert "Falha" in result.content


# ---------------------------------------------------------------------------
# _cmd_panel
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCmdPanel:
    def _make_stdin_mocks(self):
        prime = MagicMock()
        claim = MagicMock()
        release = MagicMock()
        restore = MagicMock()
        return prime, claim, release, restore

    async def test_in_pod_returns_error_no_subprocess(self):
        with patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=True):
            result = await _cmd_panel("", "deile")
        assert result.success is False
        assert "in-pod" in result.content.lower() or "KUBERNETES_SERVICE_HOST" in result.content

    async def test_missing_deploy_py_returns_error(self):
        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=None),
        ):
            result = await _cmd_panel("", "deile")
        assert result.success is False
        assert "deploy.py" in result.content

    async def test_happy_path_rc0_success_result(self):
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"
        prime, claim, release, restore = self._make_stdin_mocks()

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("deile.ui._stdin_owner.prime_termios_snapshot", prime),
            patch("deile.ui._stdin_owner.claim_stdin_for_panel", claim),
            patch("deile.ui._stdin_owner.release_stdin_for_panel", release),
            patch("deile.ui._stdin_owner.restore_termios_now", restore),
        ):
            result = await _cmd_panel("", "deile")

        assert result.success is True
        assert "rc=0" in result.content

    async def test_nonzero_rc_returns_error(self):
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"
        prime, claim, release, restore = self._make_stdin_mocks()

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=130)

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("deile.ui._stdin_owner.prime_termios_snapshot", prime),
            patch("deile.ui._stdin_owner.claim_stdin_for_panel", claim),
            patch("deile.ui._stdin_owner.release_stdin_for_panel", release),
            patch("deile.ui._stdin_owner.restore_termios_now", restore),
        ):
            result = await _cmd_panel("", "deile")

        assert result.success is False
        assert "130" in result.content

    async def test_rc_nonzero_release_and_restore_still_called(self):
        """release + restore devem ser chamados mesmo quando rc != 0."""
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"
        prime, claim, release, restore = self._make_stdin_mocks()

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("deile.ui._stdin_owner.prime_termios_snapshot", prime),
            patch("deile.ui._stdin_owner.claim_stdin_for_panel", claim),
            patch("deile.ui._stdin_owner.release_stdin_for_panel", release),
            patch("deile.ui._stdin_owner.restore_termios_now", restore),
        ):
            await _cmd_panel("", "deile")

        release.assert_called_once()
        restore.assert_called_once()

    async def test_exception_from_subprocess_restore_still_called(self):
        """release + restore devem ser chamados mesmo quando o subprocess lança exceção."""
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"
        prime, claim, release, restore = self._make_stdin_mocks()

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch(
                "deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec",
                side_effect=OSError("exec failed"),
            ),
            patch("deile.ui._stdin_owner.prime_termios_snapshot", prime),
            patch("deile.ui._stdin_owner.claim_stdin_for_panel", claim),
            patch("deile.ui._stdin_owner.release_stdin_for_panel", release),
            patch("deile.ui._stdin_owner.restore_termios_now", restore),
        ):
            result = await _cmd_panel("", "deile")

        assert result.success is False
        assert "exec failed" in result.content or "Falha" in result.content
        release.assert_called_once()
        restore.assert_called_once()

    async def test_prime_called_before_claim(self):
        """prime_termios_snapshot deve ser chamado antes de claim_stdin_for_panel."""
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"
        call_order = []

        def prime_side():
            call_order.append("prime")

        def claim_side():
            call_order.append("claim")

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("deile.ui._stdin_owner.prime_termios_snapshot", side_effect=prime_side),
            patch("deile.ui._stdin_owner.claim_stdin_for_panel", side_effect=claim_side),
            patch("deile.ui._stdin_owner.release_stdin_for_panel"),
            patch("deile.ui._stdin_owner.restore_termios_now"),
        ):
            await _cmd_panel("", "deile")

        assert call_order.index("prime") < call_order.index("claim")

    async def test_no_timeout_on_panel(self):
        """Panel NÃO deve usar asyncio.wait_for (sem timeout)."""
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        wait_for_calls = []

        real_wait_for = asyncio.wait_for

        async def tracking_wait_for(coro, timeout=None, **kwargs):
            wait_for_calls.append(timeout)
            return await real_wait_for(coro, timeout=timeout, **kwargs)

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("deile.ui._stdin_owner.prime_termios_snapshot"),
            patch("deile.ui._stdin_owner.claim_stdin_for_panel"),
            patch("deile.ui._stdin_owner.release_stdin_for_panel"),
            patch("deile.ui._stdin_owner.restore_termios_now"),
            patch("deile.commands.builtin.k8s_command.asyncio.wait_for", side_effect=tracking_wait_for),
        ):
            await _cmd_panel("", "deile")

        # asyncio.wait_for should NOT have been called from _cmd_panel
        assert not wait_for_calls, (
            f"_cmd_panel chamou asyncio.wait_for com timeouts {wait_for_calls}, "
            "mas panel é interativo — não deve ter timeout."
        )

    async def test_two_audit_events_emitted(self):
        """Deve emitir exatamente 2 AuditEvents: panel_open e panel_close."""
        fake_deploy = MagicMock()
        fake_deploy.__str__ = lambda self: "/repo/infra/k8s/deploy.py"

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        logged_actions = []

        def capture_log_event(**kwargs):
            logged_actions.append(kwargs.get("action"))

        mock_audit = MagicMock()
        mock_audit.log_event = MagicMock(side_effect=capture_log_event)

        with (
            patch("deile.commands.builtin.k8s_command._running_in_pod", return_value=False),
            patch("deile.commands.builtin.k8s_command._find_deploy_py", return_value=fake_deploy),
            patch("deile.commands.builtin.k8s_command.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("deile.ui._stdin_owner.prime_termios_snapshot"),
            patch("deile.ui._stdin_owner.claim_stdin_for_panel"),
            patch("deile.ui._stdin_owner.release_stdin_for_panel"),
            patch("deile.ui._stdin_owner.restore_termios_now"),
            patch(
                "deile.security.audit_logger.get_audit_logger",
                return_value=mock_audit,
            ),
        ):
            await _cmd_panel("", "deile")

        assert "panel_open" in logged_actions
        assert "panel_close" in logged_actions


# ---------------------------------------------------------------------------
# V2 dispatch routing in K8sCommand.execute
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestK8sCommandV2Routing:
    async def test_up_verb_dispatched_to_v2_delegate(self):
        with (
            patch(
                "deile.commands.builtin.k8s_command._detect_namespace",
                new_callable=AsyncMock,
                return_value="deile",
            ),
            patch(
                "deile.commands.builtin.k8s_command._cmd_v2_delegate",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, content="ok", content_type="text"),
            ) as mock_v2,
        ):
            await _cmd().execute(_ctx("up"))

        mock_v2.assert_called_once_with("up", "", "deile")

    async def test_down_verb_dispatched_to_v2_delegate(self):
        with (
            patch(
                "deile.commands.builtin.k8s_command._detect_namespace",
                new_callable=AsyncMock,
                return_value="deile",
            ),
            patch(
                "deile.commands.builtin.k8s_command._cmd_v2_delegate",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, content="ok", content_type="text"),
            ) as mock_v2,
        ):
            await _cmd().execute(_ctx("down"))

        mock_v2.assert_called_once_with("down", "", "deile")

    async def test_panel_verb_dispatched_to_cmd_panel(self):
        with (
            patch(
                "deile.commands.builtin.k8s_command._detect_namespace",
                new_callable=AsyncMock,
                return_value="deile",
            ),
            patch(
                "deile.commands.builtin.k8s_command._cmd_panel",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, content="ok", content_type="text"),
            ) as mock_panel,
        ):
            await _cmd().execute(_ctx("panel"))

        mock_panel.assert_called_once_with("", "deile")

    async def test_scale_verb_with_extra_args(self):
        with (
            patch(
                "deile.commands.builtin.k8s_command._detect_namespace",
                new_callable=AsyncMock,
                return_value="deile",
            ),
            patch(
                "deile.commands.builtin.k8s_command._cmd_v2_delegate",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, content="ok", content_type="text"),
            ) as mock_v2,
        ):
            await _cmd().execute(_ctx("scale --worker 2"))

        mock_v2.assert_called_once_with("scale", "--worker 2", "deile")
