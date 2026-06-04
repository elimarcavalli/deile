"""``/k8s`` command — kubectl operations against DEILE cluster namespaces.

Usage:
    /k8s                        show available verbs (discovery panel)
    /k8s restart [--deployment <name|all>]
                                rollout restart one or all deployments
                                (default deployment: deile-pipeline)
    /k8s status                 kubectl get pods,deployments,services
    /k8s logs [target] [--tail N]
                                recent logs (default: pipeline, tail 50)
    /k8s list                   namespaces managed by DEILE

Targets for /k8s logs:
    bot         -> deilebot
    worker      -> deile-worker
    pipeline    -> deile-pipeline  (default)
    claude-worker -> claude-worker
    shell       -> deile-shell
    all         -> each deployment

V2 verbs (host-only, require deploy.py on the host):
    start, stop, restart, status, logs
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path
from typing import List, Optional, Tuple

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand
from ...config.manager import CommandConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_KUBECTL = str(Path.home() / ".rd" / "bin" / "kubectl")
_DEFAULT_NAMESPACE = "deile"
_DEFAULT_TAIL = 50
_DEFAULT_DEPLOYMENT = "deile-pipeline"
_KUBECTL_TIMEOUT = 30.0

K8S_DEPLOYMENTS = [
    "deilebot",
    "deile-worker",
    "deile-shell",
    "deile-pipeline",
    "claude-worker",
]

_LOG_TARGET_MAP = {
    "bot": "deilebot",
    "worker": "deile-worker",
    "pipeline": "deile-pipeline",
    "claude-worker": "claude-worker",
    "shell": "deile-shell",
}

_V1_VERBS = [
    ("restart", "rollout restart [--deployment <name|all>]  (padrão: deile-pipeline)"),
    ("status", "pods, deployments e services"),
    ("logs", "logs recentes [bot|worker|pipeline|claude-worker|shell|all]"),
    ("list", "namespaces DEILE detectados no cluster"),
]

_V2_VERBS = ["start", "stop", "restart", "status", "logs"]

# ---------------------------------------------------------------------------
# Namespace auto-detection
# ---------------------------------------------------------------------------


async def _detect_namespace() -> str:
    """Detect DEILE-managed namespace via kubectl label selector.

    Returns the first matching namespace, or 'deile' as fallback.
    """
    import shutil
    kubectl = shutil.which("kubectl") or _KUBECTL
    cmd = [
        kubectl,
        "get", "namespaces",
        "-l", "app.kubernetes.io/managed-by=deile",
        "-o", "jsonpath={.items[*].metadata.name}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=_KUBECTL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return _DEFAULT_NAMESPACE
    except OSError:
        return _DEFAULT_NAMESPACE

    names = stdout_b.decode(errors="replace").split()
    if not names:
        return _DEFAULT_NAMESPACE
    return names[0]


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


async def _run_kubectl(
    args: List[str],
    timeout: float = _KUBECTL_TIMEOUT,
) -> Tuple[bool, str, str]:
    """Run kubectl with the given args. Returns (ok, stdout, stderr)."""
    import shutil
    kubectl = shutil.which("kubectl") or _KUBECTL
    cmd = [kubectl] + args
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "", f"kubectl timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, "", f"Failed to start kubectl: {exc}"

    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    ok = proc.returncode == 0
    return ok, stdout, stderr


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------


def _parse_restart_args(raw: str):
    """Parse --deployment flag from restart subcommand tail.

    Returns the deployment name (string) or 'all'.
    """
    tokens = shlex.split(raw) if raw.strip() else []
    deployment = _DEFAULT_DEPLOYMENT
    i = 0
    while i < len(tokens):
        if tokens[i] in ("--deployment", "-d") and i + 1 < len(tokens):
            deployment = tokens[i + 1]
            i += 2
        else:
            i += 1
    return deployment


def _parse_logs_args(raw: str):
    """Parse [target] [--tail N] from logs subcommand tail.

    Returns (target_key, tail_n).
    """
    tokens = shlex.split(raw) if raw.strip() else []
    target = "pipeline"
    tail = _DEFAULT_TAIL
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--tail" and i + 1 < len(tokens):
            try:
                tail = int(tokens[i + 1])
            except ValueError:
                pass
            i += 2
        elif not tok.startswith("-"):
            target = tok
            i += 1
        else:
            i += 1
    return target, tail


# ---------------------------------------------------------------------------
# Sub-command implementations
# ---------------------------------------------------------------------------


async def _cmd_discovery(namespace: str) -> CommandResult:
    """Show the /k8s discovery panel."""
    v1_table = Table(show_header=True, header_style="bold cyan", box=None)
    v1_table.add_column("Verbo", style="cyan", no_wrap=True)
    v1_table.add_column("Descricao")

    for verb, desc in _V1_VERBS:
        v1_table.add_row(verb, desc)

    v2_line = Text("-- host-only (V2, requer deploy.py no host) --", style="dim")
    v2_verbs_text = Text("  " + "  ".join(_V2_VERBS), style="yellow")

    footer = Text(
        "Deployments: " + " . ".join(K8S_DEPLOYMENTS),
        style="dim",
    )

    content = Group(v1_table, v2_line, v2_verbs_text, footer)
    panel = Panel(
        content,
        title=f"/k8s -- verbos disponiveis (namespace ativo: {namespace})",
        border_style="blue",
    )
    return CommandResult.success_result(panel, "rich")


async def _cmd_restart(namespace: str, deployment: str) -> CommandResult:
    """Restart one or all deployments."""
    console = Console(no_color=False)

    if deployment.lower() == "all":
        targets = K8S_DEPLOYMENTS
    else:
        targets = [deployment]

    results = []
    for dep in targets:
        console.print(f"[cyan]Restarting deployment/{dep}...[/cyan]")
        ok, stdout, stderr = await _run_kubectl(
            ["-n", namespace, "rollout", "restart", f"deployment/{dep}"]
        )
        if not ok:
            results.append((dep, False, stderr.strip()))
            console.print(f"[red]restart deployment/{dep} failed: {stderr.strip()}[/red]")
            continue

        # Wait for rollout status
        console.print(f"[cyan]Waiting for rollout status of {dep}...[/cyan]")
        ok2, stdout2, stderr2 = await _run_kubectl(
            ["-n", namespace, "rollout", "status", f"deployment/{dep}", "--timeout=180s"],
            timeout=200.0,
        )
        if ok2:
            results.append((dep, True, stdout2.strip()))
            console.print(f"[green]deployment/{dep}: {stdout2.strip() or 'success'}[/green]")
        else:
            results.append((dep, False, stderr2.strip() or stdout2.strip()))
            console.print(f"[red]deployment/{dep} rollout failed: {stderr2.strip()}[/red]")

    all_ok = all(ok for _, ok, _ in results)
    lines = [
        ("[green]OK[/green]" if ok else "[red]FAIL[/red]") + f"  {dep}: {msg}"
        for dep, ok, msg in results
    ]
    summary = "\n".join(lines)

    if all_ok:
        return CommandResult.success_result(
            f"Restart completed.\n{summary}",
            "text",
        )
    return CommandResult(
        success=False,
        content=f"One or more restarts failed.\n{summary}",
        content_type="text",
    )


async def _cmd_status(namespace: str) -> CommandResult:
    """Get pods, deployments and services."""
    ok, stdout, stderr = await _run_kubectl(
        ["-n", namespace, "get", "pods,deployments,services"]
    )
    if not ok:
        return CommandResult.error_result(
            f"kubectl get failed:\n{stderr.strip()}"
        )
    return CommandResult.success_result(stdout or "(empty output)", "text")


async def _cmd_logs(namespace: str, target_key: str, tail: int) -> CommandResult:
    """Fetch recent logs from one or all deployments."""
    if target_key == "all":
        targets = list(K8S_DEPLOYMENTS)
    else:
        resolved = _LOG_TARGET_MAP.get(target_key)
        if resolved is None:
            # Maybe user passed the full deployment name directly
            if target_key in K8S_DEPLOYMENTS:
                resolved = target_key
            else:
                valid = ", ".join(sorted(_LOG_TARGET_MAP.keys()) + ["all"])
                return CommandResult.error_result(
                    f"Unknown log target '{target_key}'. Valid: {valid}"
                )
        targets = [resolved]

    sections = []
    for dep in targets:
        ok, stdout, stderr = await _run_kubectl(
            ["-n", namespace, "logs", f"deployment/{dep}", f"--tail={tail}"]
        )
        header = f"=== {dep} (--tail={tail}) ==="
        if not ok:
            sections.append(f"{header}\n[ERROR] {stderr.strip()}")
        else:
            sections.append(f"{header}\n{stdout or '(no output)'}")

    return CommandResult.success_result("\n\n".join(sections), "text")


async def _cmd_list(namespace: str) -> CommandResult:
    """List DEILE-managed namespaces."""
    ok, stdout, stderr = await _run_kubectl(
        ["get", "namespaces", "-l", "app.kubernetes.io/managed-by=deile"]
    )
    if not ok:
        return CommandResult.error_result(
            f"kubectl get namespaces failed:\n{stderr.strip()}"
        )
    return CommandResult.success_result(stdout or "(no DEILE namespaces found)", "text")


# ---------------------------------------------------------------------------
# Command class
# ---------------------------------------------------------------------------


class K8sCommand(DirectCommand):
    """``/k8s {restart|status|logs|list}`` -- kubectl operations for DEILE cluster.

    With no args, shows a discovery panel with available verbs and deployments.
    """

    cli_flag = None
    cli_requires_provider = False

    def __init__(self) -> None:
        super().__init__(
            CommandConfig(
                name="k8s",
                description=(
                    "Operacoes kubectl no cluster DEILE "
                    "(restart|status|logs|list)"
                ),
                action="k8s",
            )
        )
        self.category = "infrastructure"

    async def execute(self, context: CommandContext) -> CommandResult:
        args = context.args.strip() if context.args else ""
        parts = args.split(None, 1)
        sub = parts[0].lower() if parts else ""
        tail = parts[1] if len(parts) > 1 else ""

        namespace = await _detect_namespace()

        if not sub:
            return await _cmd_discovery(namespace)

        if sub == "restart":
            deployment = _parse_restart_args(tail)
            return await _cmd_restart(namespace, deployment)

        if sub == "status":
            return await _cmd_status(namespace)

        if sub == "logs":
            target, tail_n = _parse_logs_args(tail)
            return await _cmd_logs(namespace, target, tail_n)

        if sub == "list":
            return await _cmd_list(namespace)

        # Unknown subcommand — show discovery panel with a hint
        valid = "restart, status, logs, list"
        return CommandResult.error_result(
            f"Unknown subcommand '{sub}'. Valid: {valid}. Run /k8s for help."
        )

    async def get_help(self) -> str:
        return """/k8s -- kubectl operations for DEILE cluster

Usage:
  /k8s                           show available verbs (discovery panel)
  /k8s restart [--deployment <name|all>]
                                 rollout restart (default: deile-pipeline)
  /k8s status                    kubectl get pods,deployments,services
  /k8s logs [target] [--tail N]  recent logs (default: pipeline, tail 50)
  /k8s list                      list DEILE-managed namespaces

Log targets: bot, worker, pipeline (default), claude-worker, shell, all
Deployments: """ + " | ".join(K8S_DEPLOYMENTS)
