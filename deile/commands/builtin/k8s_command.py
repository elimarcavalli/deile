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
    up, build, down, scale, start, stop, setup, create-namespace,
    claude-login, claude-renew, test, clone, panel
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from pathlib import Path
from typing import List, Optional, Tuple

from rich.console import Group
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

_K8S_DELEGATE_TIMEOUT_SHORT = 300.0   # seconds for non-long verbs
_K8S_DELEGATE_TIMEOUT_LONG = 1800.0  # seconds for up/build/down

_V2_LONG_VERBS = frozenset({"up", "build", "down"})
_V2_SHORT_VERBS = frozenset({
    "scale", "start", "stop", "setup", "create-namespace",
    "claude-login", "claude-renew", "test", "clone",
})

# Verbs that are always destructive (no extra flags needed)
_ALWAYS_DESTRUCTIVE = frozenset({"down"})
# Verbs destructive only when certain flags are present
_CONDITIONALLY_DESTRUCTIVE: dict = {
    "build": "--restart",
    "claude-login": "--switch",
}
# Interactive verbs that can't be piped via PIPE (block on stdin)
_INTERACTIVE_NO_VARIANT = frozenset({"setup"})

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

_V2_VERBS = [
    "up", "build", "down", "scale", "start", "stop", "setup",
    "create-namespace", "claude-login", "claude-renew", "test", "clone", "panel",
]

# ---------------------------------------------------------------------------
# Pod detection
# ---------------------------------------------------------------------------


def _running_in_pod() -> bool:
    """Detecta se está rodando dentro de um pod Kubernetes.

    Dois sinais canônicos: a variável KUBERNETES_SERVICE_HOST (injetada
    automaticamente em todo pod) ou o token de serviceaccount montado pelo
    kubelet. Qualquer um verdadeiro → in-pod.
    """
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    return Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists()


def _find_deploy_py() -> Optional[Path]:
    """Localiza ``infra/k8s/deploy.py`` caminhando para cima a partir deste arquivo.

    Sobe a árvore de diretórios a partir de ``__file__`` procurando por
    ``infra/k8s/deploy.py``. Retorna o Path se encontrado, ou None.
    """
    candidate = Path(__file__).resolve()
    for parent in [candidate, *candidate.parents]:
        deploy = parent / "infra" / "k8s" / "deploy.py"
        if deploy.is_file():
            return deploy
    return None


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
# V2 live-streaming subprocess helper
# ---------------------------------------------------------------------------


async def _live_stream_subprocess(
    cmd: List[str],
    *,
    timeout: float,
    console,
) -> Tuple[int, List[str]]:
    """Executa um subprocess e faz streaming linha a linha com Rich Live.

    Usa ``rich.live.Live(auto_refresh=False)`` para renderizar cada linha
    recebida, possibilitando reflow enquanto o processo está em andamento.
    Fallback para ``console.print`` por linha em ambiente não-TTY.

    Args:
        cmd: argv do subprocess (sem shell).
        timeout: Limite em segundos para o processo terminar.
        console: Console Rich onde renderizar.

    Returns:
        Tupla (returncode, all_lines) onde all_lines é a lista de todas as
        linhas recebidas do stdout+stderr combinados.
    """
    from deile.ui.dynamic_render import is_interactive_tty

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    all_lines: List[str] = []

    async def _read_lines() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            all_lines.append(line)

    if is_interactive_tty():
        from rich.live import Live

        with Live(
            Text(""),
            console=console,
            auto_refresh=False,
            transient=False,
        ) as live:
            async def _stream_live() -> None:
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").rstrip("\n")
                    all_lines.append(line)
                    live.update(Text("\n".join(all_lines)))
                    live.refresh()

            try:
                await asyncio.wait_for(_stream_live(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return -1, all_lines
    else:
        try:
            await asyncio.wait_for(_read_lines(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, all_lines
        for line in all_lines:
            console.print(line)

    await proc.wait()
    return proc.returncode or 0, all_lines


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
    if deployment.lower() == "all":
        targets = list(K8S_DEPLOYMENTS)
    else:
        targets = [deployment]

    lines = []
    all_ok = True

    for dep in targets:
        ok, _, stderr = await _run_kubectl(
            ["-n", namespace, "rollout", "restart", f"deployment/{dep}"]
        )
        if not ok:
            lines.append(Text(f"[✗] rollout restart {dep}: {stderr.strip()[:200]}", style="red"))
            all_ok = False
            continue

        lines.append(Text(f"[~] kubectl -n {namespace} rollout restart deployment/{dep}", style="dim"))

        ok2, out2, err2 = await _run_kubectl(
            ["-n", namespace, "rollout", "status", f"deployment/{dep}", "--timeout=180s"],
            timeout=200.0,
        )
        if ok2:
            lines.append(Text(f"[OK] deployment \"{dep}\" rollout concluído", style="green"))
        else:
            msg = (err2 or out2).strip()[:200]
            lines.append(Text(f"[✗] rollout status {dep} falhou: {msg}", style="red"))
            all_ok = False

    lines.append(Text(""))
    n = len(targets)
    if all_ok:
        lines.append(Text(f"[OK] {n}/{n} deployments concluíram rollout", style="green"))
    else:
        lines.append(Text("Alguns rollouts falharam (ver detalhes acima)", style="red"))

    return CommandResult(
        success=all_ok,
        content=Group(*lines),
        content_type="rich",
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
# V2 verb delegation
# ---------------------------------------------------------------------------


def _is_destructive(verb: str, extra: str) -> bool:
    """Retorna True se o verbo+extra forma uma operação destrutiva."""
    if verb in _ALWAYS_DESTRUCTIVE:
        return True
    flag = _CONDITIONALLY_DESTRUCTIVE.get(verb)
    if flag and flag in extra:
        return True
    return False


async def _cmd_v2_delegate(
    verb: str,
    extra: str,
    namespace: str,
    *,
    confirmed: bool = False,
) -> CommandResult:
    """Delega verbos V2 para ``python3 infra/k8s/deploy.py k8s <verb>``.

    Host-only: retorna erro se detectar ambiente in-pod. Verbs longos
    (up/build/down) usam timeout de 1800s; demais usam 300s. Output é
    transmitido linha a linha via Rich Live (com reflow).

    Verbos destrutivos (down, build --restart, claude-login --switch) exigem
    ``confirmed=True`` — sem isso retorna erro com instrução de re-emissão.
    ``setup`` é interativo (getpass, sem variante não-interativa) e retorna
    erro apontando ``create-namespace`` como alternativa scriptável.
    ``claude-login`` recebe ``--no-interactive`` automaticamente.
    """
    from deile.security.audit_logger import AuditEventType, SeverityLevel, get_audit_logger
    from rich.console import Console

    audit = get_audit_logger()

    if _running_in_pod():
        return CommandResult.error_result(
            f"verbo `{verb}` é host-only: requer `infra/k8s/deploy.py`, "
            "ausente na imagem. Detectado ambiente in-pod (KUBERNETES_SERVICE_HOST)."
        )

    # setup is interactive without a non-interactive variant — never pipe it
    if verb in _INTERACTIVE_NO_VARIANT:
        return CommandResult.error_result(
            f"verbo `{verb}` é interativo (usa getpass) e não possui variante "
            "não-interativa. Use `/k8s create-namespace` para provisionamento "
            "scriptável (todos os parâmetros via CLI)."
        )

    deploy = _find_deploy_py()
    if deploy is None:
        return CommandResult.error_result(
            f"verbo `{verb}` é host-only: `infra/k8s/deploy.py` não localizado."
        )

    # Destructive gating — must re-emit with --confirm
    if _is_destructive(verb, extra) and not confirmed:
        flag_hint = f"--confirm"
        reissue = f"/k8s {verb} {extra} {flag_hint}".strip() if extra.strip() else f"/k8s {verb} {flag_hint}"
        return CommandResult.error_result(
            f"[!] `{verb}` APAGA dados de forma irreversível. "
            f"Para confirmar, re-execute:\n  {reissue}"
        )

    # claude-login: inject --no-interactive to avoid blocking on stdin
    extra_tokens = shlex.split(extra) if extra.strip() else []
    if verb == "claude-login" and "--no-interactive" not in extra_tokens and "--from-env-only" not in extra_tokens:
        extra_tokens = ["--no-interactive"] + extra_tokens

    timeout = _K8S_DELEGATE_TIMEOUT_LONG if verb in _V2_LONG_VERBS else _K8S_DELEGATE_TIMEOUT_SHORT

    argv = ["python3", str(deploy), "k8s", verb, *extra_tokens]

    # AuditEvent BEFORE exec (order: audit-antes-de-exec per spec)
    audit.log_event(
        event_type=AuditEventType.COMMAND_EXECUTED,
        severity=SeverityLevel.WARNING if _is_destructive(verb, extra) else SeverityLevel.INFO,
        actor="user",
        resource="k8s",
        action=f"v2_{verb}",
        result="started",
        details={"verb": verb, "namespace": namespace, "extra": extra, "confirmed": confirmed},
    )

    console = Console()
    logger.info("k8s V2 delegate: %s", " ".join(argv))

    try:
        rc, all_lines = await _live_stream_subprocess(argv, timeout=timeout, console=console)
    except OSError as exc:
        return CommandResult.error_result(f"Falha ao iniciar `{verb}`: {exc}")

    output = "\n".join(all_lines)
    if rc == 0:
        return CommandResult.success_result(
            output or f"verbo `{verb}` concluído (rc=0)", "text"
        )
    if rc == -1:
        return CommandResult.error_result(
            f"verbo `{verb}` excedeu timeout de {timeout:.0f}s\n{output}"
        )
    return CommandResult.error_result(
        f"verbo `{verb}` encerrado com rc={rc}\n{output}"
    )


async def _cmd_panel(extra: str, namespace: str) -> CommandResult:
    """Hand-off do TTY para ``python3 infra/k8s/deploy.py k8s panel``.

    Host-only. Sem timeout — painel TUI interativo. Captura termios ANTES
    de claim_stdin_for_panel e restaura no finally.
    """
    from deile.security.audit_logger import AuditEventType, SeverityLevel, get_audit_logger
    from deile.ui._stdin_owner import (
        claim_stdin_for_panel,
        prime_termios_snapshot,
        release_stdin_for_panel,
        restore_termios_now,
    )

    if _running_in_pod():
        return CommandResult.error_result(
            "verbo `panel` é host-only: requer `infra/k8s/deploy.py` + `_panel`, "
            "ausentes na imagem. Detectado ambiente in-pod (KUBERNETES_SERVICE_HOST)."
        )

    deploy = _find_deploy_py()
    if deploy is None:
        return CommandResult.error_result(
            "verbo `panel` é host-only: `infra/k8s/deploy.py` não localizado."
        )

    audit = get_audit_logger()
    audit.log_event(
        event_type=AuditEventType.COMMAND_EXECUTED,
        severity=SeverityLevel.INFO,
        actor="user",
        resource="k8s",
        action="panel_open",
        result="started",
        details={"verb": "panel", "namespace": namespace},
    )

    # Captura termios ANTES do claim (estado cooked, antes de qualquer cbreak)
    prime_termios_snapshot()

    extra_tokens = shlex.split(extra) if extra.strip() else []
    argv = ["python3", str(deploy), "k8s", "panel", *extra_tokens]

    rc: Optional[int] = None
    try:
        claim_stdin_for_panel()
        proc = await asyncio.create_subprocess_exec(*argv)
        rc = await proc.wait()
    except Exception as exc:
        audit.log_event(
            event_type=AuditEventType.COMMAND_EXECUTED,
            severity=SeverityLevel.ERROR,
            actor="user",
            resource="k8s",
            action="panel_error",
            result="failed",
            details={"verb": "panel", "namespace": namespace, "error": str(exc)},
        )
        return CommandResult.error_result(f"Falha ao lançar painel: {exc}")
    finally:
        release_stdin_for_panel()
        restore_termios_now()

    audit.log_event(
        event_type=AuditEventType.COMMAND_EXECUTED,
        severity=SeverityLevel.INFO,
        actor="user",
        resource="k8s",
        action="panel_close",
        result="completed",
        details={"verb": "panel", "namespace": namespace, "returncode": rc},
    )

    if rc == 0:
        return CommandResult.success_result("painel encerrado (rc=0) · terminal restaurado", "text")
    return CommandResult.error_result(f"painel encerrado com rc={rc}")


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

        # Extract --confirm and --save-namespace global flags before subcommand dispatch
        confirmed = "--confirm" in args
        save_namespace = "--save-namespace" in args
        # Strip these meta-flags before further parsing
        clean_args = args.replace("--confirm", "").replace("--save-namespace", "").strip()

        parts = clean_args.split(None, 1)
        sub = parts[0].lower() if parts else ""
        tail = parts[1] if len(parts) > 1 else ""

        namespace = await _detect_namespace()

        if save_namespace:
            try:
                from deile.commands.settings_manager import SettingsManager
                mgr = SettingsManager()
                mgr.set_setting("k8s.namespace", namespace)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to persist k8s_namespace: %s", exc)

        if not sub:
            return await _cmd_discovery(namespace)

        if sub == "panel":
            return await _cmd_panel(tail, namespace)

        if sub in _V2_LONG_VERBS | _V2_SHORT_VERBS:
            return await _cmd_v2_delegate(sub, tail, namespace, confirmed=confirmed)

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
        valid = "restart, status, logs, list, " + ", ".join(sorted(_V2_LONG_VERBS | _V2_SHORT_VERBS)) + ", panel"
        return CommandResult.error_result(
            f"Unknown subcommand '{sub}'. Valid: {valid}. Run /k8s for help."
        )

    def get_help(self) -> str:
        return """/k8s -- kubectl operations for DEILE cluster

Usage:
  /k8s                           show available verbs (discovery panel)
  /k8s restart [--deployment <name|all>]
                                 rollout restart (default: deile-pipeline)
  /k8s status                    kubectl get pods,deployments,services
  /k8s logs [target] [--tail N]  recent logs (default: pipeline, tail 50)
  /k8s list                      list DEILE-managed namespaces
  /k8s panel                     open live TUI panel (TTY hand-off)
  /k8s up|build|down|scale|start|stop|setup|create-namespace|
       claude-login|claude-renew|test|clone
                                 V2 verbs delegated to infra/k8s/deploy.py

Log targets: bot, worker, pipeline (default), claude-worker, shell, all
Deployments: """ + " | ".join(K8S_DEPLOYMENTS)
