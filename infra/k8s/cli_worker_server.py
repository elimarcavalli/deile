#!/usr/bin/env python3
"""cli_worker_server — servidor genérico de CLI worker da frota multi-worker.

Espelha o contrato HTTP do ``claude_worker_server.py`` (lease, heartbeat,
subprocess one-shot, ``/v1/dispatch``, ``/v1/health``, ``/v1/progress``) mas é
**agnóstico do CLI concreto**: o comportamento que diverge entre CLIs
(opencode, codex, qwen, aider, goose, ...) vive num *adapter* selecionado por
``DEILE_CLI_WORKER_KIND`` e descoberto pelo registro ``cli_adapters.ADAPTERS``.

Toda a maquinaria genérica é REUSADA de :mod:`_worker_core` (sem duplicação):
lease atômico multi-réplica, heartbeat, ``run_subprocess_with_progress`` com
persistência de stdout/stderr no PVC, Bearer auth middleware e validação de
task_id. O claude mantém seu server dedicado (por causa do OAuth); este server
serve todos os demais CLIs e o claude conceitualmente vira "só mais um adapter".

Diferenças deliberadas em relação ao claude-worker:

* **Modelo:** chega como ``cli_model`` (string livre, model-id nativo do CLI),
  não ``preferred_model`` (``provider:model``). O adapter consome a string.
* **Brief em arquivo:** o brief é gravado em ``<workdir>/.brief.md`` e o caminho
  é passado ao ``build_argv`` (opencode ``-f``, aider ``--message-file``, etc.).
* **Gate pós-run:** exit-code dos CLIs não é confiável → ``WorkResult.ok`` do
  adapter é combinado com um gate de git (commit novo + push) conforme o
  ``git_strategy`` do adapter, antes de declarar sucesso.
* **``/v1/models``:** lista os modelos que o adapter suporta (catálogo ou
  dinâmico), alimentando o picker do painel.

Sem OAuth, sem ``--max-budget-usd`` nativo (controle de custo = timeout do pod +
modelo barato), sem ultracode/effort-jargon claude.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import List, Optional

import _worker_core as _core
from aiohttp import web
from cli_adapters import ADAPTERS, get_adapter
from cli_adapters.base import CliAdapter, ResumeCtx, WorkResult

logger = logging.getLogger("deile.cli_worker")

#: Tamanho do task_id (hex) — alinhado ao claude-worker (``secrets.token_hex(8)``).
_TASK_ID_BYTES = 8

#: Lease TTL/heartbeat — defaults do core, overridáveis por env (paridade com o
#: claude-worker, que usa as mesmas semânticas). Constantes de módulo para
#: permitir monkeypatch nos testes.
_LEASE_TTL_S: int = int(os.environ.get("DEILE_CLI_LEASE_TTL_S", "30"))
_LEASE_HEARTBEAT_S: int = int(os.environ.get("DEILE_CLI_LEASE_HEARTBEAT_S", "5"))

#: Cache de ``/v1/models`` (TTL) — list dinâmico pode tocar a rede.
_MODELS_CACHE_TTL_S: float = float(
    os.environ.get("DEILE_CLI_MODELS_CACHE_TTL_S", "600")
)

#: TTL do cache de ``GET /v1/models`` por kind: ``{kind: (fetched_at, [ModelInfo])}``.
_models_cache: dict = {}

#: Limite de tail na resposta — paridade com o claude-worker.
_STDOUT_TAIL = 50_000
_STDERR_TAIL = 10_000


# --------------------------------------------------------------------------- #
# Lease wrappers — finos, repassam as constantes monkeypatcháveis ao core.
# (Mesmo padrão do claude_worker_server: o core recebe ttl/heartbeat por
#  parâmetro; o server injeta a constante de módulo.)
# --------------------------------------------------------------------------- #


async def _acquire_lease(
    workspace: Path, *, channel: str = "", session_id: str = "",
) -> Optional[dict]:
    return await _core.acquire_lease(
        workspace, ttl_s=_LEASE_TTL_S, channel=channel, session_id=session_id,
    )


async def _heartbeat_loop(lease_path: Path, stop_event: asyncio.Event) -> None:
    await _core.heartbeat_loop(
        lease_path, stop_event, heartbeat_s=_LEASE_HEARTBEAT_S,
    )


async def _release_lease(lease_path: Path) -> None:
    await _core.release_lease(lease_path)


# Re-binds diretos do core (genéricos, sem constante de módulo a injetar).
SubprocessResult = _core.SubprocessResult
run_subprocess_with_progress = _core.run_subprocess_with_progress


# --------------------------------------------------------------------------- #
# Auth / adapter selection
# --------------------------------------------------------------------------- #


def _read_auth_token() -> str:
    """Lê o Bearer token do worker CLI.

    Caminhos em ordem (primeiro existente e não-vazio vence):
    1. ``/run/secrets/cli-worker/CLI_WORKER_BEARER_TOKEN`` (Secret montado).
    2. ``DEILE_CLI_WORKER_AUTH_TOKEN_FILE`` env (override pra dev).
    3. ``DEILE_CLI_WORKER_AUTH_TOKEN`` env (testes apenas).

    Raises:
        RuntimeError: nenhuma source disponível — server aborta no startup.
    """
    candidates = [
        Path("/run/secrets/cli-worker/CLI_WORKER_BEARER_TOKEN"),
        Path(os.environ.get("DEILE_CLI_WORKER_AUTH_TOKEN_FILE", "")),
    ]
    for p in candidates:
        if p and p.is_file():
            token = p.read_text(encoding="utf-8").strip()
            if token:
                return token
    env_val = os.environ.get("DEILE_CLI_WORKER_AUTH_TOKEN", "").strip()
    if env_val:
        return env_val
    raise RuntimeError(
        "cli-worker auth token not found: expected "
        "/run/secrets/cli-worker/CLI_WORKER_BEARER_TOKEN or "
        "DEILE_CLI_WORKER_AUTH_TOKEN env"
    )


def _selected_kind() -> str:
    """Kind do CLI deste pod, de ``DEILE_CLI_WORKER_KIND``."""
    return os.environ.get("DEILE_CLI_WORKER_KIND", "").strip()


def _resolve_adapter() -> CliAdapter:
    """Resolve o adapter ativo deste pod ou levanta ``KeyError``/``RuntimeError``."""
    kind = _selected_kind()
    if not kind:
        raise RuntimeError(
            "DEILE_CLI_WORKER_KIND não definido — o cli_worker_server precisa "
            "saber qual CLI servir (ex.: opencode, aider, goose)"
        )
    return get_adapter(kind)


def _worker_root() -> Path:
    """Diretório raiz dos workdirs deste worker.

    Default por kind (``/home/<kind>/work``); override por
    ``DEILE_CLI_WORKER_ROOT``. Cai em ``/tmp/cli-worker`` quando o kind é
    desconhecido (não deve acontecer em produção, mas evita crash em testes).
    """
    explicit = os.environ.get("DEILE_CLI_WORKER_ROOT", "").strip()
    if explicit:
        return Path(explicit)
    kind = _selected_kind() or "cli"
    return Path(f"/home/{kind}/work")


def _worker_home(adapter: CliAdapter) -> str:
    """HOME gravável deste worker (passado ao ``env_overlay`` do adapter)."""
    explicit = os.environ.get("DEILE_CLI_WORKER_HOME", "").strip()
    if explicit:
        return explicit
    return f"/home/{adapter.kind}"


# --------------------------------------------------------------------------- #
# Git post-run gate (exit-code não basta — plano §1.6)
# --------------------------------------------------------------------------- #


async def _git_head(workdir: Path) -> Optional[str]:
    """SHA do HEAD do repo em ``workdir/repo`` (ou ``workdir``), ou ``None``."""
    repo = workdir / "repo"
    cwd = repo if (repo / ".git").exists() else workdir
    if not (cwd / ".git").exists():
        return None
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD",
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace").strip() or None


async def _git_branch_pushed(workdir: Path, branch: Optional[str]) -> bool:
    """True se ``branch`` existe no remote ``origin`` (push confirmado).

    Sem branch → não dá pra confirmar push → False (gate falha, conservador).
    """
    if not branch:
        return False
    repo = workdir / "repo"
    cwd = repo if (repo / ".git").exists() else workdir
    if not (cwd / ".git").exists():
        return False
    proc = await asyncio.create_subprocess_exec(
        "git", "ls-remote", "--heads", "origin", branch,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return False
    return bool(out.decode("utf-8", "replace").strip())


async def _post_run_gate(
    adapter: CliAdapter,
    work_result: WorkResult,
    *,
    workdir: Path,
    base_sha: Optional[str],
    branch: Optional[str],
) -> WorkResult:
    """Combina o veredito do adapter com o gate de git (commit novo + push).

    Regra (plano §1.6/§4): ``ok = adapter.ok AND gate``, onde o gate depende do
    ``git_strategy`` — em ambos exige um commit novo desde ``base_sha`` E o
    branch pushado. Quando o adapter já reprovou, o gate não roda (preserva o
    ``error_code`` do adapter). Quando o adapter aprovou mas o gate falha,
    devolve ``ok=False`` com ``error_code=NO_PUSH`` e mantém o ``result_text``.

    Best-effort: se o repo não tem ``.git`` (impossível confirmar), o gate
    reprova com ``NO_PUSH`` — nunca declara sucesso sem evidência de push.
    """
    if not work_result.ok:
        return work_result

    head = await _git_head(workdir)
    has_new_commit = bool(head and base_sha and head != base_sha)
    # base_sha None (repo recém-clonado sem HEAD prévio capturado) → qualquer
    # HEAD presente conta como commit novo (o agente criou histórico).
    if has_new_commit is False and base_sha is None and head:
        has_new_commit = True

    pushed = await _git_branch_pushed(workdir, branch)

    if has_new_commit and pushed:
        return work_result

    reason = []
    if not has_new_commit:
        reason.append("nenhum commit novo desde o início do dispatch")
    if not pushed:
        reason.append(f"branch {branch!r} não confirmado no remote origin")
    return WorkResult(
        ok=False,
        result_text=work_result.result_text or "; ".join(reason),
        error_code="NO_PUSH",
        cost_usd=work_result.cost_usd,
    )


# --------------------------------------------------------------------------- #
# Models cache
# --------------------------------------------------------------------------- #


def _get_models(adapter: CliAdapter) -> List:
    """Lista de modelos do adapter, com cache TTL (list pode tocar a rede)."""
    now = time.monotonic()
    cached = _models_cache.get(adapter.kind)
    if cached is not None:
        fetched_at, models = cached
        if (now - fetched_at) < _MODELS_CACHE_TTL_S:
            return models
    try:
        models = list(adapter.list_models())
    except Exception as exc:  # noqa: BLE001 — list_models é best-effort
        logger.warning("list_models falhou para kind=%s: %s", adapter.kind, exc)
        models = []
    _models_cache[adapter.kind] = (now, models)
    return models


# --------------------------------------------------------------------------- #
# HTTP handlers
# --------------------------------------------------------------------------- #


async def health_handler(request: web.Request) -> web.Response:
    """``GET /v1/health`` — readiness: kind + auth_mode + ready.

    ``ready`` é False se faltam as ``auth_env_keys`` (modo env) ou a credencial
    OAuth (modo oauth_file) — assim o readinessProbe remove o pod do Service até
    estar de fato apto a dispatchar.
    """
    try:
        adapter = _resolve_adapter()
    except (KeyError, RuntimeError) as exc:
        return web.json_response(
            {"ok": False, "ready": False, "error": str(exc)}, status=500,
        )

    if adapter.auth_mode == "env":
        ready = all(os.environ.get(k, "").strip() for k in adapter.auth_env_keys)
    else:  # oauth_file
        cred = adapter.oauth.cred_path if adapter.oauth else ""
        ready = bool(cred and Path(os.path.expanduser(cred)).is_file())

    return web.json_response({
        "ok": True,
        "kind": adapter.kind,
        "auth_mode": adapter.auth_mode,
        "ready": ready,
    }, status=200 if ready else 503)


async def models_handler(request: web.Request) -> web.Response:
    """``GET /v1/models`` — modelos suportados pelo adapter deste worker."""
    try:
        adapter = _resolve_adapter()
    except (KeyError, RuntimeError) as exc:
        return web.json_response({"error": str(exc)}, status=500)

    models = _get_models(adapter)
    cached = _models_cache.get(adapter.kind)
    fetched_at = cached[0] if cached else time.monotonic()
    return web.json_response({
        "models": [m.as_dict() for m in models],
        "source": "catalog",
        "fetched_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - (time.monotonic() - fetched_at)),
        ),
        "kind": adapter.kind,
    })


async def progress_handler(request: web.Request) -> web.Response:
    """``GET /v1/progress/{task_id}`` — tail do stdout/stderr persistido no PVC.

    Lê os arquivos gravados por ``run_subprocess_with_progress`` em
    ``<root>/.progress/<task_id>.<stream>.log`` (mesmo formato do claude-worker).
    """
    task_id = request.match_info["task_id"]
    if not _core.validate_task_id_for_path(task_id):
        return web.json_response(
            {"error": "invalid task_id format (expected hex 16-char)"},
            status=400,
        )
    progress_dir = _worker_root() / ".progress"
    stdout_path = progress_dir / f"{task_id}.stdout.log"
    stderr_path = progress_dir / f"{task_id}.stderr.log"
    if not stdout_path.exists() and not stderr_path.exists():
        return web.json_response(
            {"error": f"task_id {task_id} not found"}, status=404,
        )

    def _read(p: Path) -> str:
        try:
            return p.read_text() if p.exists() else ""
        except OSError as exc:
            logger.warning("failed to read %s: %s", p, exc)
            return ""

    stdout = await asyncio.to_thread(_read, stdout_path)
    stderr = await asyncio.to_thread(_read, stderr_path)
    return web.json_response({
        "task_id": task_id,
        "stdout": stdout[-_STDOUT_TAIL:],
        "stderr": stderr[-_STDERR_TAIL:],
    })


async def dispatch_handler(request: web.Request) -> web.Response:
    """``POST /v1/dispatch`` — executa o CLI do adapter num workdir isolado.

    Fluxo:
    1. Valida o payload (``brief`` obrigatório) e resolve o adapter.
    2. Cria task_id + workdir + grava o brief em ``<workdir>/.brief.md``.
    3. Adquire o lease, inicia o heartbeat.
    4. Monta o argv via ``adapter.build_argv`` + env via ``adapter.env_overlay``.
    5. Roda via ``run_subprocess_with_progress`` (PID no lease, progress no PVC).
    6. ``adapter.parse_output`` + gate de git → ``WorkResult`` final.
    7. Libera o lease e devolve a resposta no contrato unificado.

    ``resume`` só é montado quando ``adapter.supports_resume`` E o payload trouxe
    ``resume_session_id`` + ``prev_task_id``; senão roda fresh (o brief lê
    ``.deile-progress.md`` para contexto natural).
    """
    try:
        adapter = _resolve_adapter()
    except (KeyError, RuntimeError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — corpo malformado
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    brief = payload.get("brief")
    if not brief or not isinstance(brief, str):
        return web.json_response(
            {"ok": False, "error": "missing or invalid 'brief'"}, status=400,
        )

    stage = payload.get("stage", "implement")
    branch = payload.get("branch")
    cli_model = payload.get("cli_model")
    reasoning = payload.get("preferred_reasoning")
    if not adapter.supports_reasoning:
        reasoning = None
    wait_for_result: bool = bool(payload.get("wait_for_result", True))
    _channel_id = str(payload.get("channel_id") or "").strip()

    dispatch_timeout_s: Optional[int] = None
    _raw_timeout = payload.get("timeout_s")
    if _raw_timeout is not None:
        try:
            _v = int(_raw_timeout)
            if _v > 0:
                dispatch_timeout_s = _v
        except (TypeError, ValueError):
            pass

    # Resume só quando o adapter suporta E o payload trouxe os dois campos.
    resume_ctx: Optional[ResumeCtx] = None
    resume_session_id = payload.get("resume_session_id")
    prev_task_id = payload.get("prev_task_id")
    if adapter.supports_resume and resume_session_id and prev_task_id:
        if not _core.validate_task_id_for_path(str(prev_task_id)):
            return web.json_response(
                {"ok": False, "error": f"invalid prev_task_id {prev_task_id!r}"},
                status=400,
            )
        resume_ctx = ResumeCtx(
            session_id=str(resume_session_id), prev_task_id=str(prev_task_id),
        )

    root = _worker_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return web.json_response(
            {"ok": False, "error": f"could not create work root: {exc}"},
            status=500,
        )

    # Resume reusa o workdir anterior; fresh cria um novo.
    if resume_ctx is not None:
        task_id = resume_ctx.prev_task_id
        workspace = root / task_id
        if not workspace.is_dir():
            # Workdir sumiu → degrada para fresh (brief lê .deile-progress.md).
            resume_ctx = None
            task_id = secrets.token_hex(_TASK_ID_BYTES)
            workspace = root / task_id
    else:
        task_id = secrets.token_hex(_TASK_ID_BYTES)
        workspace = root / task_id
    workspace.mkdir(parents=True, exist_ok=True)

    # Grava o brief num arquivo do workdir; o adapter o referencia via flag ou
    # lê o conteúdo conforme o CLI (opencode -f / aider --message-file / etc.).
    brief_path = workspace / ".brief.md"
    try:
        await asyncio.to_thread(brief_path.write_text, brief, "utf-8")
    except OSError as exc:
        return web.json_response(
            {"ok": False, "error": f"could not write brief: {exc}"}, status=500,
        )

    lease = await _acquire_lease(
        workspace, channel=_channel_id, session_id=task_id,
    )
    if lease is None:
        return web.json_response({
            "ok": False,
            "error_code": "TASK_ALREADY_RUNNING",
            "error": (
                f"outra réplica do {adapter.kind}-worker já executa "
                f"task_id={task_id}; pipeline deve retry no próximo tick"
            ),
            "task_id": task_id,
        }, status=409)

    timeout = dispatch_timeout_s if dispatch_timeout_s is not None else int(
        os.environ.get("DEILE_CLI_WORKER_TASK_TIMEOUT_S", "1800")
    )
    home = _worker_home(adapter)
    argv = adapter.build_argv(
        brief_path=str(brief_path),
        model=cli_model,
        reasoning=reasoning,
        workdir=str(workspace),
        resume=resume_ctx,
    )
    overlay = adapter.env_overlay(home=home)
    # Aplica o overlay do adapter no processo (HOME/XDG/config). As auth_env_keys
    # já vêm do Secret no Deployment; o overlay não as inclui (contrato base).
    for k, v in overlay.items():
        os.environ[k] = v

    logger.info(
        "dispatch kind=%s task_id=%s stage=%s model=%s resume=%s",
        adapter.kind, task_id, stage, cli_model, resume_ctx is not None,
    )

    base_sha = await _git_head(workspace)

    async def _run_and_finalize() -> dict:
        stop_hb = asyncio.Event()
        hb_task = asyncio.create_task(
            _heartbeat_loop(workspace / ".lease.json", stop_hb),
            name=f"lease-hb-{task_id}",
        )
        try:
            result = await run_subprocess_with_progress(
                argv, cwd=workspace, task_id=task_id, timeout=timeout,
                lease_path=workspace / ".lease.json", root=root,
            )
            work = adapter.parse_output(
                stdout=result.stdout, stderr=result.stderr, rc=result.returncode,
            )
            work = await _post_run_gate(
                adapter, work, workdir=workspace, base_sha=base_sha, branch=branch,
            )
            return _build_response(task_id, result, work)
        finally:
            stop_hb.set()
            try:
                await hb_task
            except Exception:  # noqa: BLE001
                pass
            await _release_lease(workspace / ".lease.json")

    if not wait_for_result:
        asyncio.create_task(_run_and_finalize(), name=f"dispatch-{task_id}")
        return web.json_response({"task_id": task_id, "status": "running"}, status=202)

    response = await _run_and_finalize()
    return web.json_response(response)


def _build_response(
    task_id: str, result, work: WorkResult,
) -> dict:
    """Monta a resposta JSON do contrato unificado a partir do resultado.

    Mesma forma do claude-worker: ``ok, stdout, stderr, task_id, returncode,
    duration_seconds, total_cost_usd, error_code?, error?``.
    """
    response = {
        "ok": work.ok,
        "stdout": result.stdout[-_STDOUT_TAIL:],
        "stderr": result.stderr[-_STDERR_TAIL:],
        "task_id": task_id,
        "session_id": None,
        "returncode": result.returncode,
        "duration_seconds": result.duration_seconds,
        "total_cost_usd": work.cost_usd,
        "is_error": not work.ok,
        "result": work.result_text,
    }
    if not work.ok:
        response["error_code"] = work.error_code or "WORKER_FAILED"
        response["error"] = _build_failure_reason(
            result.returncode, result.stderr, result.stdout, work.result_text,
        )[:500]
    return response


def _build_failure_reason(
    returncode: int, stderr: str, stdout: str, result_text: str,
    *, max_bytes: int = 1500,
) -> str:
    """Motivo de falha legível (mesma prioridade do claude-worker).

    Fonte, da mais para a menos confiável: ``result_text`` do adapter → tail do
    stderr → tail do stdout → motivo genérico do returncode (124 = timeout).
    """
    if result_text and result_text.strip():
        return result_text[:max_bytes]
    if stderr and stderr.strip():
        tail = stderr[-max_bytes:] if len(stderr) > max_bytes else stderr
        return f"rc={returncode} stderr: {tail.strip()}"
    if stdout and stdout.strip():
        tail = stdout[-max_bytes:] if len(stdout) > max_bytes else stdout
        return f"rc={returncode} stdout: {tail.strip()}"
    if returncode == 124:
        return "subprocess timed out (rc=124)"
    return f"subprocess exited with rc={returncode} (sem saída capturada)"


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

#: Bearer middleware com a whitelist de paths abertos deste server (readiness).
_bearer_auth_mw = _core.make_bearer_auth_mw(("/v1/health",))


def build_app(auth_token: Optional[str] = None) -> web.Application:
    """Monta a ``aiohttp.web.Application`` do CLI worker genérico.

    Bearer middleware ativo por default; ``auth_token`` opcional permite testes
    in-process. Startup hook varre workdirs stale do PVC (reusa o cleanup do
    core via ``startup_cleanup``-equivalente: aqui só registramos o root).
    """
    app = web.Application(
        middlewares=[_bearer_auth_mw],
        client_max_size=512 * 1024,
    )
    app["auth_token"] = auth_token or _read_auth_token()

    app.router.add_get("/v1/health", health_handler)
    app.router.add_get("/v1/models", models_handler)
    app.router.add_post("/v1/dispatch", dispatch_handler)
    app.router.add_get("/v1/progress/{task_id}", progress_handler)
    return app


def main(passthrough: Optional[List[str]] = None) -> int:
    """Entry point do ``wrapper.py`` no mode ``cli-worker``.

    Resolve o adapter por ``DEILE_CLI_WORKER_KIND`` (falha cedo se ausente/
    desconhecido), cria o work root e sobe o servidor aiohttp.
    """
    _log_level = os.environ.get("DEILE_CLI_WORKER_LOG_LEVEL", "INFO")
    os.environ.setdefault("DEILE_LOG_LEVEL", _log_level)
    try:
        from deile.log_mgmt import init_logging
        init_logging(pod_name=f"{_selected_kind() or 'cli'}-worker")
    except Exception:  # noqa: BLE001 — fallback intencional
        _handler = logging.StreamHandler(sys.stdout)
        _handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
        ))
        root_logger = logging.getLogger()
        root_logger.setLevel(_log_level)
        if not root_logger.handlers:
            root_logger.addHandler(_handler)
    logging.getLogger("deile").setLevel(_log_level)

    try:
        adapter = _resolve_adapter()
    except (KeyError, RuntimeError) as exc:
        logger.error("cannot start cli_worker_server: %s", exc)
        return 78

    host = os.environ.get("DEILE_CLI_WORKER_HOST", "0.0.0.0")
    port = int(os.environ.get("DEILE_CLI_WORKER_PORT", str(adapter.default_port)))
    root = _worker_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("could not create work root %s: %s", root, exc)
        return 78

    logger.info(
        "cli_worker_server kind=%s listening on %s:%d, work root=%s, "
        "known adapters=%s",
        adapter.kind, host, port, root, sorted(ADAPTERS),
    )
    app = build_app()
    web.run_app(app, host=host, port=port, print=lambda *_: None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
