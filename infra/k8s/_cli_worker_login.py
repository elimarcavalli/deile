"""bootstrap_cli_worker_oauth — captura credencial OAuth do host e instala um CLI worker oauth-capable.

Generaliza ``claude-login`` para qualquer adapter com ``OAuthSpec`` (atributo ``oauth`` não-None),
independente do ``auth_mode`` default. Workers sem ``OAuthSpec`` vão para ``cli-worker-install``
(auth por chave de API).

Adapters env-default mas oauth-capable (ex.: codex) entram em MODO OAUTH: manifest renderizado com
blocos OAuth e ``DEILE_<KIND>_AUTH=oauth`` setado no Deployment.

**Segurança (princípio 08):** o conteúdo da credencial NUNCA é logado — só o comprimento. O Secret
é criado via ``kubectl create secret --dry-run | apply`` (valor nunca toca o disco do host).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent

#: Env var de HOME por CLI — sobrepõe o ``~`` do ``cred_path`` quando setada no host.
#: Codex respeita ``CODEX_HOME``; adicionar aqui para novos CLIs OAuth.
_HOME_ENV_BY_KIND: Dict[str, str] = {
    "codex": "CODEX_HOME",
}


@dataclass
class CliWorkerLoginResult:
    """Resultado de :func:`bootstrap_cli_worker_oauth` — status + flags por etapa."""

    ok: bool
    kind: str = ""
    cred_detected: bool = False
    cred_secret_applied: bool = False
    bearer_applied: bool = False
    manifest_applied: bool = False
    auth_mode_set: bool = False
    scaled: bool = False
    error: Optional[str] = None
    missing_keys: List[str] = field(default_factory=list)


def _ensure_on_path() -> None:
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))


def _adapter(kind: str):
    _ensure_on_path()
    import cli_adapters  # noqa: PLC0415

    return cli_adapters.get_adapter(kind)


def resolve_host_cred_path(
    kind: str, adapter, *, env: Optional[Dict[str, str]] = None,
    home: Optional[Path] = None,
) -> Optional[Path]:
    """Resolve o caminho absoluto da credencial OAuth no host (função pura, não toca o filesystem).

    Expande ``OAuthSpec.cred_path`` usando o env var de home do CLI (ex.: ``CODEX_HOME``) quando
    setado, ou ``$HOME`` como fallback. Retorna ``None`` se o adapter não declara ``oauth``.
    """
    oauth = getattr(adapter, "oauth", None)
    cred_path = getattr(oauth, "cred_path", None) if oauth else None
    if not cred_path:
        return None
    env = os.environ if env is None else env
    home = home or Path(env.get("HOME", str(Path.home())))
    cred_path = cred_path.strip()

    home_env_name = _HOME_ENV_BY_KIND.get(kind)
    cli_home = (env.get(home_env_name, "").strip() if home_env_name else "")

    if cred_path.startswith("~/"):
        rel = cred_path[2:]  # ex.: ".codex/auth.json"
        if cli_home:
            # CODEX_HOME já É o diretório-pai (ex.: ~/.codex) → credencial é só o basename.
            basename = rel.rsplit("/", 1)[-1]
            return Path(cli_home) / basename
        return home / rel
    if cred_path == "~":
        return home
    return Path(cred_path).expanduser()


def cred_secret_name(kind: str, adapter) -> str:
    """Nome do Secret de credencial OAuth — delega a ``_cli_worker_gen`` (fonte única)."""
    _ensure_on_path()
    from _cli_worker_gen import cred_secret_name as _gen_name  # noqa: PLC0415

    return _gen_name(adapter, kind=kind)


def read_host_credential(cred_path: Path) -> Optional[str]:
    """Lê a credencial OAuth do host; retorna ``None`` se ausente/ilegível.

    Loga só o comprimento — nunca o conteúdo (princípio 08).
    """
    try:
        if not cred_path.is_file():
            return None
        content = cred_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("falha ao ler credencial %s: %s", cred_path, exc)
        return None
    if not content.strip():
        return None
    logger.info(
        "credencial OAuth detectada em %s (len=%d) — capturando para o Secret",
        cred_path, len(content),
    )
    return content


def build_cred_secret_payload(content: str, *, cred_path: Path) -> Dict[str, str]:
    """Monta ``{basename: conteúdo}`` do Secret (função pura).

    Chave = basename do path (ex.: ``auth.json``) para que o initContainer
    encontre ``/run/secrets/<kind>-oauth/<basename>``.
    """
    return {cred_path.name: content}


def _kubectl_apply_cred_secret(
    secret_name: str, payload: Dict[str, str], *, namespace: str,
) -> bool:
    """Cria/atualiza o Secret de credencial OAuth via dry-run|apply (idempotente).

    Valor via ``--from-literal`` no pipe — nunca toca o disco do host nem é logado.
    """
    if not payload:
        logger.error("payload do Secret %s vazio — nada a aplicar", secret_name)
        return False
    literals: List[str] = [f"--from-literal={k}={v}" for k, v in payload.items()]
    try:
        dry = subprocess.run(
            ["kubectl", "create", "secret", "generic", secret_name,
             *literals, "-n", namespace, "--dry-run=client", "-o", "yaml"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except subprocess.TimeoutExpired:
        logger.error("dry-run Secret %s timed out (15s)", secret_name)
        return False
    except FileNotFoundError:
        logger.error("kubectl não encontrado no PATH")
        return False
    if dry.returncode != 0:
        logger.error("dry-run Secret %s falhou: %s", secret_name, dry.stderr)
        return False
    try:
        apply = subprocess.run(
            ["kubectl", "-n", namespace, "apply", "-f", "-"],
            input=dry.stdout, capture_output=True, text=True,
            check=False, timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.error("apply Secret %s timed out (30s)", secret_name)
        return False
    if apply.returncode != 0:
        logger.error("apply Secret %s falhou: %s", secret_name, apply.stderr)
        return False
    return True


def _run_login_cmd(login_cmd: List[str], *, inherit_stdio: bool = True) -> bool:
    """Roda o ``login_cmd`` do adapter no host aguardando o device-auth do operador.

    Timeout 5 min. NÃO loga output (pode conter o código de device — princípio 08).
    """
    kwargs: dict = ({"check": False} if inherit_stdio
                    else {"capture_output": True, "text": True, "check": False})
    logger.info(
        "rodando `%s` — complete o device-auth no browser (timeout 5min)",
        " ".join(login_cmd),
    )
    try:
        result = subprocess.run(login_cmd, timeout=300, **kwargs)
    except subprocess.TimeoutExpired:
        logger.error("`%s` excedeu 5min — device-auth não foi completado",
                     " ".join(login_cmd))
        return False
    except FileNotFoundError:
        logger.error("comando de login não encontrado no PATH: %s", login_cmd[0])
        return False
    return result.returncode == 0


def bootstrap_cli_worker_oauth(
    kind: str,
    *,
    namespace: str = "deile",
    force_relogin: bool = False,
    interactive: bool = True,
    replicas: int = 1,
    home: Optional[Path] = None,
    inherit_stdio: bool = True,
    env: Optional[Dict[str, str]] = None,
) -> CliWorkerLoginResult:
    """Captura credencial OAuth do host e instala o CLI worker oauth-capable.

    Idempotente — rerodar sem flags é noop quando tudo já está pronto.
    ``interactive=False`` falha-rápido se a credencial estiver ausente (CI).
    ``force_relogin`` força o ``login_cmd`` mesmo com credencial presente.
    """
    try:
        adapter = _adapter(kind)
    except KeyError as exc:
        return CliWorkerLoginResult(ok=False, kind=kind, error=str(exc))

    # Rejeita workers sem OAuthSpec — vão para `cli-worker-install` (chave de API).
    oauth = getattr(adapter, "oauth", None)
    if oauth is None or not getattr(oauth, "login_cmd", None):
        return CliWorkerLoginResult(
            ok=False, kind=kind,
            error=(
                f"adapter {kind!r} não declara um OAuthSpec com login_cmd — não é "
                "oauth-capable. `cli-worker-login` cobre só workers OAuth. Para "
                "auth por chave de API use `cli-worker-install`."
            ),
        )
    # Adapters env-default mas oauth-capable (ex.: codex) entram em modo OAuth:
    # manifest com blocos OAuth + DEILE_<KIND>_AUTH=oauth no Deployment.
    oauth_mode = True

    result = CliWorkerLoginResult(ok=False, kind=kind)
    worker = f"{kind}-worker"
    cred_path = resolve_host_cred_path(kind, adapter, env=env, home=home)
    if cred_path is None:
        result.error = f"adapter {kind!r} sem cred_path no OAuthSpec"
        return result

    # 1/2. Detecta credencial; roda login se ausente/forçado e interativo.
    content = read_host_credential(cred_path)
    need_login = force_relogin or content is None
    if need_login:
        if not interactive:
            result.error = (
                f"credencial OAuth de {kind!r} ausente em {cred_path} E "
                "interactive=False. Rode sem --no-interactive (o operador faz o "
                f"device-auth) ou execute `{' '.join(oauth.login_cmd)}` no host."
            )
            return result
        if not _run_login_cmd(list(oauth.login_cmd), inherit_stdio=inherit_stdio):
            result.error = f"`{' '.join(oauth.login_cmd)}` falhou"
            return result
        content = read_host_credential(cred_path)
        if content is None:
            result.error = (
                f"`{' '.join(oauth.login_cmd)}` retornou OK mas a credencial não "
                f"apareceu em {cred_path}. Verifique o login e re-rode."
            )
            return result
    result.cred_detected = True

    # 3. Secret de credencial OAuth.
    secret_name = cred_secret_name(kind, adapter)
    payload = build_cred_secret_payload(content, cred_path=cred_path)
    if not _kubectl_apply_cred_secret(secret_name, payload, namespace=namespace):
        result.error = f"falha ao aplicar Secret {secret_name}"
        return result
    result.cred_secret_applied = True

    # 4-7. Bearer + auth_env_keys + manifest + auth-mode env + scale (reusa _cli_worker_install).
    _ensure_on_path()
    from _cli_worker_install import (  # noqa: PLC0415
        _kubectl_apply_keys_secret,
        _kubectl_apply_manifest,
        _kubectl_scale,
        _kubectl_sync_bearer,
        _resolve_auth_keys,
    )

    auth_values = _resolve_auth_keys(adapter, kind=kind)
    declared = list(getattr(adapter, "auth_env_keys", []) or [])
    result.missing_keys = [k for k in declared if k not in auth_values]
    if not _kubectl_apply_keys_secret(auth_values, namespace=namespace):
        result.error = "falha ao aplicar Secret cli-worker-keys"
        return result

    if not _kubectl_sync_bearer(worker, namespace=namespace):
        result.error = f"falha ao sincronizar {worker}-bearer"
        return result
    result.bearer_applied = True

    try:
        if not _kubectl_apply_manifest(
            kind, namespace=namespace, oauth_mode=oauth_mode,
        ):
            result.error = f"falha ao aplicar manifest de {worker}"
            return result
    except Exception as exc:  # noqa: BLE001 — render/apply pode estourar
        result.error = f"erro ao gerar/aplicar manifest: {exc}"
        return result
    result.manifest_applied = True

    # Opt-in OAuth em runtime: adapter env-default precisa desta var para usar o modo OAuth.
    if not _kubectl_set_auth_mode(worker, kind, namespace=namespace):
        result.error = f"falha ao setar DEILE_{kind.upper()}_AUTH=oauth"
        return result
    result.auth_mode_set = True

    if not _kubectl_scale(worker, replicas, namespace=namespace):
        result.error = f"manifest aplicado mas scale de {worker} falhou"
        return result
    result.scaled = True
    result.ok = True
    return result


def _kubectl_set_auth_mode(worker: str, kind: str, *, namespace: str) -> bool:
    """Seta ``DEILE_<KIND>_AUTH=oauth`` no Deployment via ``kubectl set env`` (idempotente)."""
    var = f"DEILE_{kind.upper()}_AUTH"
    try:
        result = subprocess.run(
            ["kubectl", "-n", namespace, "set", "env",
             f"deployment/{worker}", f"{var}=oauth"],
            capture_output=True, text=True, check=False, timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.error("set env %s falhou: %s", var, exc)
        return False
    if result.returncode != 0:
        logger.error("set env %s falhou: %s", var, result.stderr)
        return False
    return True


__all__ = [
    "CliWorkerLoginResult",
    "resolve_host_cred_path",
    "cred_secret_name",
    "read_host_credential",
    "build_cred_secret_payload",
    "bootstrap_cli_worker_oauth",
]
