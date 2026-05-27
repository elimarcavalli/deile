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


def _run_claude_login(*, logout_first: bool = False,
                      inherit_stdio: bool = False) -> bool:
    """Spawn `claude auth login` no host. Returns True se completou OK.

    :param inherit_stdio: ``True`` (CLI mode — ``deploy.py k8s claude-login``)
        deixa stdout/stderr inherit pro terminal, operador vê URL OAuth e
        prompts diretamente. ``False`` (painel TUI background thread)
        captura para não corromper o display Rich; output vai pros logs
        via ``logger.info``.

    O subcomando correto é ``claude auth login`` — ``claude login`` (sem
    ``auth``) é interpretado pelo CLI como prompt posicional (``[prompt]``)
    e abre uma sessão interativa com o texto "login", em vez de iniciar
    OAuth. Mesmo trato para ``claude auth logout``.

    Se logout_first=True, faz `claude auth logout` antes (descarta conta atual).
    Timeout 5 min — OAuth interativo pode demorar.

    IMPORTANTE: ``stdout``/``stderr`` são CAPTURADOS (não inherit). Em
    background thread (fix do bug #309 fase 2 #2), claude CLI escreveria
    sobre o display Rich do painel TUI — corrompendo a UI e dando aparência
    de freeze. Output capturado é logado via ``logger.info`` para o
    operador inspecionar pelos logs do painel se precisar. O browser ainda
    é aberto automaticamente pelo claude CLI (a abertura usa o `open(1)`/
    xdg-open e não depende de stdout estar livre).
    """
    # kwargs comuns entre os 2 modos — só stdio difere.
    capture_kwargs: dict = ({"check": False}
                            if inherit_stdio
                            else {"capture_output": True, "text": True,
                                  "check": False})

    if logout_first:
        logger.info("running `claude auth logout` (force relogin)")
        try:
            logout_res = subprocess.run(
                ["claude", "auth", "logout"], timeout=30, **capture_kwargs,
            )
            if not inherit_stdio:
                if (logout_res.stdout or "").strip():
                    logger.info("claude logout stdout: %s",
                                logout_res.stdout.strip()[:500])
                if (logout_res.stderr or "").strip():
                    logger.info("claude logout stderr: %s",
                                logout_res.stderr.strip()[:500])
        except subprocess.TimeoutExpired:
            logger.warning("`claude auth logout` timed out after 30s — "
                           "prosseguindo com login mesmo assim")
        except FileNotFoundError:
            logger.error("claude CLI not in PATH")
            return False

    logger.info(
        "running `claude auth login` — uma janela do navegador deve abrir; "
        "complete o OAuth nela (timeout 5min)"
    )
    try:
        result = subprocess.run(
            ["claude", "auth", "login"], timeout=300, **capture_kwargs,
        )
        if not inherit_stdio:
            if (result.stdout or "").strip():
                logger.info("claude login stdout: %s",
                            result.stdout.strip()[:1000])
            if (result.stderr or "").strip():
                logger.info("claude login stderr: %s",
                            result.stderr.strip()[:1000])
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error("`claude auth login` timed out after 5min — "
                     "OAuth não foi completado no browser")
        return False
    except FileNotFoundError:
        logger.error(
            "claude CLI not in PATH; install with "
            "`npm install -g @anthropic-ai/claude-code`"
        )
        return False


def _kubectl_apply_secret(creds: dict, *, namespace: str) -> bool:
    """Apply/update Secret claude-credentials com credentials.json content.

    Timeouts: 15s (dry-run, só serializa) + 30s (apply, fala com API server).
    Sem timeout, kubectl pendurado em DNS/auth issue trava o painel/CLI
    indefinidamente.
    """
    creds_json = json.dumps(creds)

    # 1. Generate manifest via dry-run
    dry_run_cmd = [
        "kubectl", "create", "secret", "generic", "claude-credentials",
        f"--from-literal=credentials.json={creds_json}",
        "-n", namespace,
        "--dry-run=client", "-o", "yaml",
    ]
    try:
        dry = subprocess.run(
            dry_run_cmd, capture_output=True, text=True, check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        logger.error("kubectl create secret dry-run timed out (15s)")
        return False
    except FileNotFoundError:
        logger.error("kubectl não encontrado no PATH")
        return False
    if dry.returncode != 0:
        logger.error("kubectl create secret dry-run failed: %s", dry.stderr)
        return False

    # 2. Apply via `kubectl apply -f -`
    try:
        apply = subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=dry.stdout, capture_output=True, text=True, check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.error("kubectl apply secret timed out (30s)")
        return False
    if apply.returncode != 0:
        logger.error("kubectl apply secret failed: %s", apply.stderr)
        return False
    return True


def _kubectl_sync_bearer_token(*, namespace: str) -> bool:
    """Popula ``claude-worker-bearer`` reusando o token do ``worker-bearer``
    (deile-worker).

    Por que reusar: o ``DeileWorkerClient`` (caller do pipeline) lê o token
    de ``/run/secrets/worker/AUTH_TOKEN`` e envia em ``Authorization:
    Bearer …`` em TODA chamada — independente de o destino ser
    ``deile-worker`` ou ``claude-worker``. Em V1, ter tokens distintos
    exigiria refator do cliente; reusar o mesmo simplifica e mantém ambos
    os pods atrás da mesma boundary de confiança (já igualmente cobertos
    pela NetworkPolicy ingress whitelist do pipeline).

    Idempotente: se ``worker-bearer`` ainda não foi criado (cluster sem
    ``deploy.py k8s up`` rodado), retorna ``True`` e logger.warning —
    o operador vai precisar rodar ``up`` primeiro (o Deployment
    claude-worker vai ficar pending no rollout até o secret existir).

    Returns ``False`` apenas em erro real de I/O com kubectl.
    """
    # 1. Lê o token do worker-bearer (Secret existente do deile-worker).
    try:
        get = subprocess.run(
            ["kubectl", "-n", namespace, "get", "secret", "worker-bearer",
             "-o", "jsonpath={.data.AUTH_TOKEN}"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.error("kubectl get secret worker-bearer falhou: %s", exc)
        return False
    if get.returncode != 0 or not get.stdout.strip():
        logger.warning(
            "secret worker-bearer ausente — rode `deploy.py k8s up` antes "
            "do `claude-login` (claude-worker rollout vai ficar pending até "
            "o secret existir)"
        )
        return True  # não-fatal — manifests aplicam, rollout que falha depois.

    # 2. base64 decode (jsonpath devolve raw base64 do .data).
    import base64  # noqa: PLC0415 — só usado aqui
    try:
        token = base64.b64decode(get.stdout.strip()).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        logger.error("worker-bearer AUTH_TOKEN não é base64 ascii: %s", exc)
        return False

    # 3. Apply do claude-worker-bearer com o mesmo token (via dry-run|apply
    # pra ser idempotente — update se já existe, create se não).
    try:
        dry = subprocess.run(
            ["kubectl", "create", "secret", "generic", "claude-worker-bearer",
             f"--from-literal=CLAUDE_WORKER_BEARER_TOKEN={token}",
             "-n", namespace,
             "--dry-run=client", "-o", "yaml"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except subprocess.TimeoutExpired:
        logger.error("kubectl create secret claude-worker-bearer dry-run timed out")
        return False
    if dry.returncode != 0:
        logger.error("dry-run claude-worker-bearer failed: %s", dry.stderr)
        return False

    try:
        apply = subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=dry.stdout, capture_output=True, text=True, check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.error("kubectl apply claude-worker-bearer timed out")
        return False
    if apply.returncode != 0:
        logger.error("apply claude-worker-bearer failed: %s", apply.stderr)
        return False
    logger.info("claude-worker-bearer sincronizado com worker-bearer token")
    return True


def _kubectl_apply_manifests(*, namespace: str) -> bool:
    """Apply manifests 47 (allowed-repos ConfigMap), 48 (bearer Secret),
    49 (PVC), 50 (Deployment+Service), 40 (NetworkPolicy update).

    Sequencial e idempotente (``kubectl apply -f`` é declarativo). Sem
    rollback automático em caso de falha intermediária — apply parcial
    pode ocorrer (ex.: ConfigMap criado mas Deployment falha). O operador
    pode re-rodar ``bootstrap_claude_worker`` (apply é idempotente) ou
    rodar ``deploy.py k8s down`` para wipe completo.

    Timeout 30s por manifest — API server lento não trava painel/CLI.
    """
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
        try:
            result = subprocess.run(
                ["kubectl", "apply", "-f", str(f), "-n", namespace],
                capture_output=True, text=True, check=False, timeout=30,
            )
        except subprocess.TimeoutExpired:
            logger.error("kubectl apply %s timed out (30s) — applies "
                         "anteriores ficam aplicados (idempotência cobre "
                         "re-tentativa)", f.name)
            return False
        except FileNotFoundError:
            logger.error("kubectl não encontrado no PATH")
            return False
        if result.returncode != 0:
            logger.error("kubectl apply %s failed: %s", f.name, result.stderr)
            return False
        logger.info("applied %s", f.name)
    return True


def _kubectl_wait_rollout(*, namespace: str, timeout_s: int = 180) -> bool:
    """Wait claude-worker Deployment Ready.

    Timeout subprocess = ``timeout_s + 30`` (margem pra kubectl propagar
    o resultado após o --timeout do server-side terminar).
    """
    try:
        result = subprocess.run(
            ["kubectl", "rollout", "status", "deployment/claude-worker",
             "-n", namespace, f"--timeout={timeout_s}s"],
            capture_output=True, text=True, check=False,
            timeout=timeout_s + 30,
        )
    except subprocess.TimeoutExpired:
        logger.error("kubectl rollout status timed out (subprocess %ds)",
                     timeout_s + 30)
        return False
    except FileNotFoundError:
        logger.error("kubectl não encontrado no PATH")
        return False
    if result.returncode != 0:
        logger.error(
            "kubectl rollout status failed: %s\n%s",
            result.stderr, result.stdout,
        )
    return result.returncode == 0


def uninstall_claude_worker(*, namespace: str = "deile") -> ClaudeLoginResult:
    """Limpa toda a stack do claude-worker do cluster.

    Útil quando rollout falhou na metade e o operador quer reinstalar
    do zero. Idempotente — recursos já ausentes geram WARN, não fatal.

    Remove (ordem de menor pra maior dependência):

    1. Deployment ``claude-worker`` (mata os Pods)
    2. Service ``claude-worker`` (remove do DNS interno)
    3. PVC ``claude-worker-home`` (apaga credentials.json em-disco)
    4. Secret ``claude-credentials`` (creds OAuth do claude)
    5. Secret ``claude-worker-bearer`` (token de auth do dispatch)
    6. ConfigMap ``claude-worker-allowed-repos`` (allowlist)
    7. **NÃO mexe na NetworkPolicy** — ela é compartilhada com outros
       pods (deile-worker, etc); revert dela é responsabilidade do
       ``deploy.py k8s down`` (wipe completo).

    Returns:
        ClaudeLoginResult.ok=True se TODOS os deletes foram NotFound ou OK;
        False se algum delete falhou por outro motivo (erro real de API
        server / RBAC). Recursos individuais que não existem (404) viram
        ``logger.info`` e não falham — uninstall é idempotente.
    """
    resources = [
        ("deployment", "claude-worker"),
        ("service", "claude-worker"),
        ("pvc", "claude-worker-home"),
        ("secret", "claude-credentials"),
        ("secret", "claude-worker-bearer"),
        ("configmap", "claude-worker-allowed-repos"),
    ]
    failures = []
    for kind, name in resources:
        try:
            result = subprocess.run(
                ["kubectl", "-n", namespace, "delete", kind, name,
                 "--ignore-not-found=true", "--wait=false"],
                capture_output=True, text=True, check=False, timeout=30,
            )
        except subprocess.TimeoutExpired:
            failures.append(f"{kind}/{name}: delete timed out (30s)")
            continue
        except FileNotFoundError:
            return ClaudeLoginResult(
                ok=False, error="kubectl não encontrado no PATH",
            )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            failures.append(f"{kind}/{name}: {err}")
        else:
            out = (result.stdout or "").strip()
            if out:
                logger.info("delete %s/%s: %s", kind, name, out)

    if failures:
        return ClaudeLoginResult(
            ok=False,
            error="; ".join(failures),
        )
    return ClaudeLoginResult(
        ok=True,
        secret_applied=False,  # após uninstall, nada está aplicado
        deployment_applied=False,
        rollout_ready=False,
    )


def bootstrap_claude_worker(
    *,
    namespace: str = "deile",
    force_relogin: bool = False,
    interactive: bool = True,
    home: Optional[Path] = None,
    inherit_stdio: bool = True,
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

    :param inherit_stdio: ``True`` (default — uso via CLI direto pelo
        ``deploy.py k8s claude-login``) deixa stdout/stderr do ``claude
        auth login`` inherit pro terminal, operador vê URL OAuth e prompts
        diretamente. ``False`` (uso via painel TUI background thread)
        captura o output pra não corromper o display Rich.

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
        if not _run_claude_login(logout_first=force_relogin,
                                  inherit_stdio=inherit_stdio):
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

    # 3. Manifests (cria stub do claude-worker-bearer entre outros)
    if not _kubectl_apply_manifests(namespace=namespace):
        return ClaudeLoginResult(
            ok=False,
            account_email=email,
            secret_applied=True,
            error="failed to apply claude-worker manifests",
        )

    # 3b. Sincroniza claude-worker-bearer com o token do worker-bearer
    # (não-fatal — só warn se worker-bearer não existir; rollout vai
    # detectar). Faz AQUI (após apply do stub vazio do manifest 48) pra
    # garantir overwrite com o token real.
    if not _kubectl_sync_bearer_token(namespace=namespace):
        return ClaudeLoginResult(
            ok=False,
            account_email=email,
            secret_applied=True,
            deployment_applied=True,
            error="failed to sync claude-worker-bearer token",
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
