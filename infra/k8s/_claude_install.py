"""bootstrap_claude_worker — instala/atualiza credentials + Deployment do
claude-worker. Compartilhado entre CLI verb (`deploy.py k8s claude-login`)
e painel TUI (DispatchMatrixView Task 20).

Issue #309 fase 2.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ClaudeLoginResult:
    """Resultado de bootstrap_claude_worker — status + flags por etapa."""

    ok: bool
    account_email: Optional[str] = None
    secret_applied: bool = False
    deployment_applied: bool = False
    rollout_ready: bool = False
    error: Optional[str] = None


def _check_claude_logged_in() -> Optional[dict]:
    """Returns dict de ``claude auth status --json`` se loggedIn=true; senão None.

    Idempotente — primeira coisa que ``bootstrap_claude_worker`` chama, evita
    abrir browser desnecessariamente quando claude já está logado.
    """
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("loggedIn"):
        return None
    return data


def _read_credentials_from_keychain() -> Optional[dict]:
    """macOS Keychain: extrai JSON do service 'Claude Code-credentials'.

    Returns o JSON parseado (contém ``claudeAiOauth`` com access_token,
    refresh_token, scopes etc) ou None se ausente / não-Darwin / falha.

    NÃO loga o conteúdo — segredos não entram em log.
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        logger.warning("Keychain content not JSON: %s", exc)
        return None


def _read_credentials_from_file(home: Path) -> Optional[dict]:
    """Linux/headless fallback: lê ~/.claude/credentials.json se existir."""
    cred_path = home / ".claude" / "credentials.json"
    if not cred_path.exists():
        return None
    try:
        return json.loads(cred_path.read_text())
    except Exception as exc:  # noqa: BLE001 — log + None é OK
        logger.warning("failed to parse %s: %s", cred_path, exc)
        return None


def _read_credentials(home: Path) -> Optional[dict]:
    """Retorna credenciais Claude OAuth do host.

    Estratégia em camadas (primeira que retornar venceu):
      1. macOS Keychain (``security find-generic-password -s 'Claude Code-credentials'``)
         — onde claude CLI armazena por padrão no macOS.
      2. ``~/.claude/credentials.json`` — fallback Linux/headless, ou
         operadores que usam apiKeyHelper.

    Returns None se nenhuma camada produzir credenciais válidas.
    """
    # macOS: Keychain primeiro (storage default do claude CLI)
    creds = _read_credentials_from_keychain()
    if creds is not None:
        return creds
    # Linux ou headless: arquivo
    return _read_credentials_from_file(home)


def _run_claude_login(*, logout_first: bool = False) -> bool:
    """Spawn `claude auth login` no host. Returns True se completou OK.

    O subcomando correto é ``claude auth login`` — ``claude login`` (sem
    ``auth``) é interpretado pelo CLI como prompt posicional (``[prompt]``)
    e abre uma sessão interativa com o texto "login", em vez de iniciar
    OAuth. Mesmo trato para ``claude auth logout``.

    Se logout_first=True, faz `claude auth logout` antes (descarta conta atual).
    Timeout 5 min — OAuth interativo pode demorar.
    """
    if logout_first:
        logger.info("running `claude auth logout` (force relogin)")
        subprocess.run(
            ["claude", "auth", "logout"], check=False, timeout=30,
        )

    logger.info(
        "running `claude auth login` — browser will open; complete OAuth there"
    )
    try:
        result = subprocess.run(
            ["claude", "auth", "login"], check=False, timeout=300,
        )
        return result.returncode == 0
    except FileNotFoundError:
        logger.error(
            "claude CLI not in PATH; install with "
            "`npm install -g @anthropic-ai/claude-code`"
        )
        return False


def _kubectl_apply_secret(creds: dict, *, namespace: str) -> bool:
    """Apply/update Secret claude-credentials com credentials.json content."""
    creds_json = json.dumps(creds)

    # 1. Generate manifest via dry-run
    dry_run_cmd = [
        "kubectl", "create", "secret", "generic", "claude-credentials",
        f"--from-literal=credentials.json={creds_json}",
        "-n", namespace,
        "--dry-run=client", "-o", "yaml",
    ]
    dry = subprocess.run(
        dry_run_cmd, capture_output=True, text=True, check=False,
    )
    if dry.returncode != 0:
        logger.error("kubectl create secret dry-run failed: %s", dry.stderr)
        return False

    # 2. Apply via `kubectl apply -f -`
    apply = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=dry.stdout, capture_output=True, text=True, check=False,
    )
    if apply.returncode != 0:
        logger.error("kubectl apply secret failed: %s", apply.stderr)
        return False
    return True


def _kubectl_apply_manifests(*, namespace: str) -> bool:
    """Apply manifests 47 (allowed-repos ConfigMap), 48 (bearer Secret),
    49 (PVC), 50 (Deployment+Service), 40 (NetworkPolicy update)."""
    manifests_dir = Path(__file__).parent / "manifests"
    files = [
        manifests_dir / "47-claude-worker-allowed-repos.yaml",
        manifests_dir / "48-claude-worker-bearer-secret.yaml",
        manifests_dir / "49-claude-worker-pvc.yaml",
        manifests_dir / "50-claude-worker-deployment.yaml",
        manifests_dir / "40-network-policy.yaml",
    ]
    for f in files:
        if not f.exists():
            logger.error("manifest missing: %s", f)
            return False
        result = subprocess.run(
            ["kubectl", "apply", "-f", str(f), "-n", namespace],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            logger.error("kubectl apply %s failed: %s", f.name, result.stderr)
            return False
        logger.info("applied %s", f.name)
    return True


def _kubectl_wait_rollout(*, namespace: str, timeout_s: int = 180) -> bool:
    """Wait claude-worker Deployment Ready."""
    result = subprocess.run(
        ["kubectl", "rollout", "status", "deployment/claude-worker",
         "-n", namespace, f"--timeout={timeout_s}s"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        logger.error(
            "kubectl rollout status failed: %s\n%s",
            result.stderr, result.stdout,
        )
    return result.returncode == 0


def bootstrap_claude_worker(
    *,
    namespace: str = "deile",
    force_relogin: bool = False,
    interactive: bool = True,
    home: Optional[Path] = None,
) -> ClaudeLoginResult:
    """Setup do claude-worker no cluster. Idempotente.

    Etapas:
    1. Detect credentials no host (~/.claude/credentials.json).
    2. Se force_relogin OR ausente E interactive=True → claude login.
       Se sem creds + interactive=False → fail fast.
    3. Read credentials.json, extract email (opcional).
    4. Apply Secret claude-credentials via kubectl.
    5. Apply manifests 47/48/49/50 + 40 (NetworkPolicy).
    6. Wait rollout status (180s default).

    Returns:
        ClaudeLoginResult com flags por etapa + error opcional.
    """
    home = home or Path(os.environ.get("HOME", str(Path.home())))

    # 1. Credentials — fluxo idempotente que evita loop OAuth:
    #
    #    1a. Pre-check: `claude auth status --json` — se já logado, evita
    #        browser ride desnecessário (a menos que force_relogin=True).
    #    1b. Read credenciais (Keychain macOS / file Linux).
    #    1c. Se ausente E interactive=True → claude auth login + re-read.
    #
    # IMPORTANTE: `_read_credentials` agora tem Keychain support no macOS;
    # antes assumia ~/.claude/credentials.json (que NÃO EXISTE no macOS —
    # claude CLI usa Keychain) → causava loop infinito de OAuth.
    auth_status = _check_claude_logged_in()  # None ou dict com loggedIn=true
    creds = _read_credentials(home)

    need_login = force_relogin or (auth_status is None and creds is None)
    if need_login:
        if not interactive:
            return ClaudeLoginResult(
                ok=False,
                error=(
                    "Sem credenciais Claude detectadas (Keychain/file ausentes) "
                    "E interactive=False. Rode com --interactive ou faça "
                    "`claude auth login` no host primeiro."
                ),
            )
        if not _run_claude_login(logout_first=force_relogin):
            return ClaudeLoginResult(ok=False, error="`claude auth login` falhou")
        # Re-check após login. Se ainda ausente, ABORT (evita loop infinito).
        auth_status = _check_claude_logged_in()
        creds = _read_credentials(home)
        if auth_status is None and creds is None:
            return ClaudeLoginResult(
                ok=False,
                error=(
                    "`claude auth login` reportou sucesso mas credenciais ainda "
                    "não foram detectadas no Keychain/file. Possíveis causas: "
                    "(a) login interrompido antes de salvar; (b) keychain "
                    "lock; (c) plataforma não suportada. NÃO vou re-tentar "
                    "(evita loop OAuth)."
                ),
            )

    # Prioridade pra email: status > credentials > None
    email = None
    if auth_status and isinstance(auth_status, dict):
        email = auth_status.get("email")
    if not email and isinstance(creds, dict):
        # Procura email em campos comuns (root, ou aninhado em oauth)
        email = creds.get("email")
        if not email and isinstance(creds.get("claudeAiOauth"), dict):
            email = creds["claudeAiOauth"].get("email")

    # Hard requirement: precisamos do JSON pra montar Secret. Se chegamos
    # aqui sem creds (raro: auth_status=true mas Keychain extract falhou —
    # access denied etc), aborta limpo.
    if creds is None:
        return ClaudeLoginResult(
            ok=False, account_email=email,
            error=(
                "`claude auth status` reportou loggedIn=true mas não foi "
                "possível extrair credentials do Keychain (macOS) ou do "
                "arquivo ~/.claude/credentials.json (Linux). Verifique "
                "permissões do Keychain ou re-rode `claude auth login`."
            ),
        )

    # 2. Secret
    if not _kubectl_apply_secret(creds, namespace=namespace):
        return ClaudeLoginResult(
            ok=False,
            account_email=email,
            error="failed to apply claude-credentials Secret",
        )

    # 3. Manifests
    if not _kubectl_apply_manifests(namespace=namespace):
        return ClaudeLoginResult(
            ok=False,
            account_email=email,
            secret_applied=True,
            error="failed to apply claude-worker manifests",
        )

    # 4. Wait rollout
    if not _kubectl_wait_rollout(namespace=namespace):
        return ClaudeLoginResult(
            ok=False,
            account_email=email,
            secret_applied=True,
            deployment_applied=True,
            error="claude-worker rollout did not become ready in time",
        )

    return ClaudeLoginResult(
        ok=True,
        account_email=email,
        secret_applied=True,
        deployment_applied=True,
        rollout_ready=True,
    )
