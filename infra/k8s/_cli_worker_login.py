"""bootstrap_cli_worker_oauth — captura a credencial OAuth de um CLI worker do
host e instala o worker (paridade com ``_claude_install`` / ``claude-login``).

Generaliza o fluxo ``claude-login`` para qualquer CLI da frota cujo adapter é
**oauth-capable** — declara um :class:`~cli_adapters.base.OAuthSpec` (atributo
``oauth`` não-None), INDEPENDENTE do ``auth_mode`` default. Cobre dois casos:

* adapters ``auth_mode="oauth_file"`` (claude-like — OAuth é o único modo);
* adapters env-default mas oauth-capable (codex: ``auth_mode="env"`` +
  ``OAuthSpec``), cujo OAuth é OPT-IN via ``DEILE_<KIND>_AUTH=oauth``. Instalar
  por este verb coloca o worker em MODO OAUTH: o manifest é renderizado com os
  blocos OAuth (``oauth_mode=True``) e o env ``DEILE_<KIND>_AUTH=oauth`` é setado
  no Deployment. Workers SEM ``OAuthSpec`` são rejeitados (vão para
  ``cli-worker-install``, auth por chave de API).

Onde o ``claude-login`` é hard-coded para o Claude (Keychain macOS, Secret
``claude-credentials``, manifests 47/49/50), este módulo lê TUDO do adapter:

* o caminho da credencial no host (``OAuthSpec.cred_path``, com expansão de ``~``
  e do env var de home do CLI quando aplicável — ex.: ``CODEX_HOME``);
* o comando de login interativo (``OAuthSpec.login_cmd`` — ``codex login
  --device-auth``), que o OPERADOR completa no browser/device-auth;
* o nome do Secret de credencial (``OAuthSpec.secret_name`` ou derivado);
* a geração do manifest (PVC + initContainer ``bootstrap-creds`` + mount),
  delegada a ``_cli_worker_gen`` (mesmo template dos workers env, com os blocos
  OAuth condicionais ativados pelo ``auth_mode``).

Sequência (cada etapa idempotente; espelha ``bootstrap_claude_worker``):

1. detecta a credencial OAuth no host (lê o arquivo declarado pelo adapter);
2. se ausente OU ``force_relogin`` E ``interactive`` → roda o ``login_cmd`` no
   host (o operador faz o device-auth) e re-lê; sem cred + ``interactive=False``
   → fail-fast;
3. aplica o Secret de credencial (conteúdo bruto do arquivo, sob a chave =
   basename do path — ex.: ``auth.json``);
4. sincroniza o bearer ``<kind>-worker-bearer`` (reusa o ``worker-bearer``);
5. propaga as ``auth_env_keys`` env (ex.: nenhuma no codex-oauth) ao Secret
   compartilhado, e seta ``DEILE_<KIND>_AUTH=oauth`` no Deployment;
6. gera + aplica o manifest (Deployment/Service/NetworkPolicy/PVC/CronJob);
7. escala a 1 réplica.

**Segurança (princípio 08):** o conteúdo da credencial NUNCA é logado — só o
comprimento, como evidência de leitura sem expor o segredo. O Secret é criado
via ``kubectl create secret --dry-run | apply`` (sem materializar o valor em
disco fora do cluster).
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

#: Mapa de env var de HOME por CLI — quando setada no host, sobrepõe o ``~`` do
#: ``cred_path``. Codex respeita ``CODEX_HOME``; outros CLIs OAuth se adicionam
#: aqui conforme entrarem na frota.
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
    """Resolve o caminho ABSOLUTO da credencial OAuth no HOST (função pura).

    Expande ``OAuthSpec.cred_path`` usando, nesta ordem: o env var de home do
    CLI (ex.: ``CODEX_HOME``) quando o ``cred_path`` é relativo ao home do CLI;
    senão o ``~`` → ``$HOME``. Não toca o filesystem (só monta o path) — a
    existência é checada pelo chamador (testável sem cluster).

    Args:
        kind: kind do adapter.
        adapter: instância do adapter (lê ``adapter.oauth.cred_path``).
        env: fonte de env vars (default ``os.environ``).
        home: HOME do operador (default ``$HOME``).

    Returns:
        O ``Path`` resolvido, ou ``None`` se o adapter não declara ``oauth``.
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

    # ``cred_path`` tipicamente é ``~/.codex/auth.json``: a parte após o
    # diretório-home do CLI é o que o env var de home sobrepõe.
    if cred_path.startswith("~/"):
        rel = cred_path[2:]  # ex.: ".codex/auth.json"
        if cli_home:
            # ``CODEX_HOME`` já É ``~/.codex`` → a credencial é o basename sob ele.
            basename = rel.rsplit("/", 1)[-1]
            return Path(cli_home) / basename
        return home / rel
    if cred_path == "~":
        return home
    return Path(cred_path).expanduser()


def cred_secret_name(kind: str, adapter) -> str:
    """Nome do Secret de credencial OAuth (single source: ``_cli_worker_gen``)."""
    _ensure_on_path()
    from _cli_worker_gen import cred_secret_name as _gen_name  # noqa: PLC0415

    return _gen_name(adapter, kind=kind)


def read_host_credential(cred_path: Path) -> Optional[str]:
    """Lê o conteúdo bruto da credencial OAuth do host (não loga o conteúdo).

    Returns o texto do arquivo (preservado byte-a-byte para o pod consumir o
    mesmo shape que o CLI gravou) ou ``None`` se ausente/ilegível. Loga apenas o
    comprimento — princípio 08 (segredos não entram em log).
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
    """Monta o payload ``{<basename>: <conteúdo>}`` do Secret (função pura).

    A chave do Secret é o basename do path da credencial (ex.: ``auth.json``),
    para que o initContainer encontre ``/run/secrets/<kind>-oauth/<basename>``.
    Não materializa o valor em nenhum lugar além do dict devolvido.
    """
    return {cred_path.name: content}


def _kubectl_apply_cred_secret(
    secret_name: str, payload: Dict[str, str], *, namespace: str,
) -> bool:
    """Cria/atualiza o Secret de credencial OAuth (dry-run | apply, idempotente).

    O valor é passado via ``--from-literal`` num pipe ``dry-run -o yaml | apply``
    — o conteúdo nunca toca o disco do host fora do cluster e nunca é logado.
    Timeouts: 15s (dry-run) + 30s (apply).
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
    """Roda o ``login_cmd`` do adapter no host (device-auth interativo).

    O operador completa o device-auth no browser; este processo aguarda o
    comando retornar. ``inherit_stdio=True`` (CLI direto) deixa o operador ver a
    URL/código de device-auth no terminal. Timeout 5min (device-auth pode
    demorar). NÃO loga o output (pode conter o código de device).
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
    """Captura a credencial OAuth do host + instala o CLI worker ``oauth_file``.

    Espelha ``bootstrap_claude_worker`` generalizado pelo ``OAuthSpec`` do
    adapter. Idempotente: rerodar sem flags é noop quando tudo está pronto.

    Args:
        kind: kind do adapter (deve ser oauth-capable — ter um ``OAuthSpec`` em
            ``oauth``, independente do ``auth_mode`` default).
        namespace: namespace k8s alvo.
        force_relogin: força rodar o ``login_cmd`` mesmo com credencial presente
            (trocar de conta).
        interactive: ``False`` (CI) falha-rápido se a credencial estiver ausente
            (não roda o ``login_cmd``).
        replicas: réplicas finais do Deployment (default 1).
        home / env: injeção para testes (default ``$HOME`` / ``os.environ``).
        inherit_stdio: stdio do ``login_cmd`` herdado pro terminal (CLI direto).

    Returns:
        :class:`CliWorkerLoginResult` com flags por etapa + erro opcional.
    """
    try:
        adapter = _adapter(kind)
    except KeyError as exc:
        return CliWorkerLoginResult(ok=False, kind=kind, error=str(exc))

    # `cli-worker-login` cobre QUALQUER worker oauth-capable: ou `auth_mode`
    # estático `oauth_file` (claude-like), ou env-default COM um `OAuthSpec`
    # opt-in (codex, que roda OAuth via `DEILE_CODEX_AUTH=oauth`). Rejeita só
    # quem não tem OAuthSpec — esse vai para `cli-worker-install` (chave de API).
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
    # Quando o adapter é env-default mas oauth-capable, o worker entra em MODO
    # OAUTH: o manifest é renderizado com os blocos OAuth e o env
    # `DEILE_<KIND>_AUTH=oauth` é setado no Deployment (etapa 6 abaixo).
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

    # 4/5/6/7. Bearer + auth_env_keys + manifest + auth-mode env + scale —
    # reusa o instalador env-only (que já faz bearer/keys/manifest/scale).
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

    # Seta DEILE_<KIND>_AUTH=oauth no Deployment (seleção do modo OAuth em
    # runtime — o adapter declara o modo padrão env; o opt-in é por env var).
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
    """``kubectl set env`` ``DEILE_<KIND>_AUTH=oauth`` no Deployment (idempotente)."""
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
