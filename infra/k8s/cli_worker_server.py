#!/usr/bin/env python3
"""cli_worker_server — servidor genérico de CLI worker da frota multi-worker.

Espelha o contrato HTTP do ``claude_worker_server.py`` mas é **agnóstico do CLI
concreto**: o comportamento divergente (opencode, codex, qwen, aider, goose…)
vive num *adapter* selecionado por ``DEILE_CLI_WORKER_KIND``.

Toda a maquinaria genérica é REUSADA de :mod:`_worker_core` (sem duplicação):
lease atômico multi-réplica, heartbeat, ``run_subprocess_with_progress``, Bearer
auth middleware. O claude mantém seu server dedicado (OAuth); este serve todos os
demais CLIs.

Diferenças deliberadas em relação ao claude-worker:

* **Modelo:** ``cli_model`` (string livre, model-id nativo do CLI), não
  ``preferred_model`` (``provider:model``).
* **Brief em arquivo:** gravado em ``<workdir>/.brief.md``; caminho passado ao
  ``build_argv`` (opencode ``-f``, aider ``--message-file``, etc.).
* **Gate pós-run:** exit-code não é confiável → ``WorkResult.ok`` do adapter é
  combinado com gate de git (commit novo + push).
* **``/v1/models``:** lista modelos que o adapter suporta (catálogo ou
  dinâmico), alimentando o picker do painel.

Sem OAuth, sem ``--max-budget-usd`` nativo (controle de custo = timeout + modelo barato).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

import _worker_core as _core
from aiohttp import web
from cli_adapters import ADAPTERS, get_adapter
from cli_adapters.base import CliAdapter, ResumeCtx, WorkResult

# Import best-effort: ausente → harvest no-op (fail-safe: nunca poda sem colher).
try:
    import fleet_progress_parse as _fpp  # noqa: E402
except Exception:  # noqa: BLE001
    _fpp = None

logger = logging.getLogger("deile.cli_worker")

#: Alinhado ao claude-worker (``secrets.token_hex(8)``).
_TASK_ID_BYTES = 8

#: Constantes de módulo para permitir monkeypatch nos testes.
_LEASE_TTL_S: int = int(os.environ.get("DEILE_CLI_LEASE_TTL_S", "30"))
_LEASE_HEARTBEAT_S: int = int(os.environ.get("DEILE_CLI_LEASE_HEARTBEAT_S", "5"))

#: list_models pode tocar a rede — cache com TTL.
_MODELS_CACHE_TTL_S: float = float(
    os.environ.get("DEILE_CLI_MODELS_CACHE_TTL_S", "600")
)

#: ``{kind: (fetched_at, [ModelInfo])}``
_models_cache: dict = {}

_STDOUT_TAIL = 50_000
_STDERR_TAIL = 10_000

#: Retenção de workdirs (dias); paridade com o claude.
_CLEANUP_RETENTION_DAYS: int = int(
    os.environ.get("DEILE_CLI_WORKER_CLEANUP_RETENTION_DAYS", "7")
)

_CLEANUP_INTERVAL_S: float = float(
    os.environ.get("DEILE_CLI_WORKER_CLEANUP_INTERVAL_S", "3600")
)

#: Retenção dos logs de progresso — gatilho da poda DEPOIS de o custo ser
#: colhido para o ledger durável. Logs volumosos; ledger minúsculo (~KB).
_PROGRESS_RETENTION_DAYS: int = int(
    os.environ.get("DEILE_CLI_WORKER_PROGRESS_RETENTION_DAYS", "30")
)

#: Grace TOCTOU: log modificado recentemente pode ter resume agendado.
_PROGRESS_GRACE_S: int = int(
    os.environ.get("DEILE_CLI_WORKER_PROGRESS_GRACE_S", "3600")
)


# --------------------------------------------------------------------------- #
# Lease wrappers — injetam as constantes monkeypatcháveis no core.
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


run_subprocess_with_progress = _core.run_subprocess_with_progress


# --------------------------------------------------------------------------- #
# Auth / adapter selection
# --------------------------------------------------------------------------- #


def _read_auth_token() -> str:
    """Lê o Bearer token do worker CLI.

    Ordem: Secret montado em ``/run/secrets/cli-worker/`` → env file →
    ``DEILE_CLI_WORKER_AUTH_TOKEN`` (testes). Levanta ``RuntimeError`` se
    nenhuma source disponível.
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
    return os.environ.get("DEILE_CLI_WORKER_KIND", "").strip()


def _resolve_adapter() -> CliAdapter:
    """Resolve o adapter ativo ou levanta ``KeyError``/``RuntimeError``."""
    kind = _selected_kind()
    if not kind:
        raise RuntimeError(
            "DEILE_CLI_WORKER_KIND não definido — o cli_worker_server precisa "
            "saber qual CLI servir (ex.: opencode, aider, goose)"
        )
    return get_adapter(kind)


def _worker_root() -> Path:
    """Raiz dos workdirs: ``DEILE_CLI_WORKER_ROOT`` ou ``/home/<kind>/work``."""
    explicit = os.environ.get("DEILE_CLI_WORKER_ROOT", "").strip()
    if explicit:
        return Path(explicit)
    kind = _selected_kind() or "cli"
    return Path(f"/home/{kind}/work")


def _worker_home(adapter: CliAdapter) -> str:
    """HOME gravável: ``DEILE_CLI_WORKER_HOME`` ou ``/home/<kind>``."""
    explicit = os.environ.get("DEILE_CLI_WORKER_HOME", "").strip()
    if explicit:
        return explicit
    return f"/home/{adapter.kind}"


# --------------------------------------------------------------------------- #
# Git post-run gate (exit-code não basta — plano §1.6)
# --------------------------------------------------------------------------- #

# Wrappers de módulo para que os testes possam monkeypatchá-los.

async def _git_head(workdir: Path) -> Optional[str]:
    """SHA do HEAD ou ``None`` se sem repo."""
    return await _core.git_head(workdir)


async def _git_branch_pushed(workdir: Path, branch: Optional[str]) -> bool:
    """True se ``branch`` existe no remote ``origin``."""
    return await _core.git_branch_pushed(workdir, branch)


async def _post_run_gate(
    adapter: CliAdapter,
    work_result: WorkResult,
    *,
    workdir: Path,
    base_sha: Optional[str],
    branch: Optional[str],
) -> WorkResult:
    """Combina o veredito do adapter com gate de git (commit novo + push).

    Plano §1.6/§4: ``ok = adapter.ok AND gate``. Gate exige commit novo desde
    ``base_sha`` E branch pushado. Adapter já reprovado: gate não roda.
    Adapter aprovado + gate falha → ``ok=False, error_code=NO_PUSH``.
    Sem ``.git``: reprova com ``NO_PUSH`` — nunca declara sucesso sem push.
    """
    if not work_result.ok:
        return work_result

    head = await _git_head(workdir)
    has_new_commit = bool(head and base_sha and head != base_sha)
    # base_sha None = repo recém-clonado → qualquer HEAD vale como commit novo.
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
    # ``replace`` preserva tokens_by_model/model (issue #638): mesmo um dispatch
    # reprovado no gate de push CONSUMIU tokens — o custo precisa ser contabilizado.
    return replace(
        work_result,
        ok=False,
        result_text=work_result.result_text or "; ".join(reason),
        error_code="NO_PUSH",
    )


# --------------------------------------------------------------------------- #
# Models cache
# --------------------------------------------------------------------------- #


def _get_models(adapter: CliAdapter) -> List:
    """Lista de modelos do adapter com cache TTL."""
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

    ``ready=False`` quando as credenciais do adapter estão ausentes; o
    readinessProbe remove o pod do Service até estar apto a dispatchar.
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
    """``GET /v1/models`` — modelos suportados pelo adapter."""
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
    """``GET /v1/progress/{task_id}`` — tail do stdout/stderr em ``<root>/.progress/``."""
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


async def resume_info_handler(request: web.Request) -> web.Response:
    """``GET /v1/dispatches/{task_id}/resume-info`` — liveness da task.

    Consumido pelo pipeline para decidir *resume vs fresh vs skip*. Campo
    decisivo: ``claude_alive=True`` → pipeline não re-despacha (anti-double-
    dispatch para dispatches fire-and-forget ainda em execução).

    ``session_id`` (issue #445): id nativo do CLI capturado no dispatch
    anterior. Quando ``claude_alive=False`` e ``session_id`` não-vazio, o
    pipeline re-despacha com ``resume_session_id`` + mesmo workdir (anti-
    sangria). Vazio → fresh com retry.

    Respostas:
      - ``400`` task_id fora de ``[0-9a-f]{16}`` (path traversal guard).
      - ``404`` workspace inexistente → pipeline limpa ledger e segue fresh.
      - ``200`` ``{task_id, workdir, workdir_exists, session_id, claude_alive,
        last_result_summary, last_completed_at}``.
    """
    task_id = request.match_info["task_id"]
    if not _core.validate_task_id_for_path(task_id):
        return web.json_response(
            {"error": "invalid task_id format (expected hex 16-char)"},
            status=400,
        )
    workspace = _worker_root() / task_id
    if not await asyncio.to_thread(workspace.is_dir):
        return web.json_response(
            {"error": f"task_id {task_id} not found"}, status=404,
        )
    lease_path = workspace / ".lease.json"

    def _alive() -> bool:
        if not lease_path.exists():
            return False
        return not _core.lease_is_stale(lease_path, ttl_s=_LEASE_TTL_S)

    alive = await asyncio.to_thread(_alive)
    # Enquanto roda (fire-and-forget), meta ainda não existe → last_completed_at=None.
    meta = await asyncio.to_thread(_load_task_result, task_id) or {}
    return web.json_response({
        "task_id": task_id,
        "workdir": meta.get("workdir") or str(workspace),
        "workdir_exists": True,
        "session_id": meta.get("session_id", "") or "",
        "prev_task_id": meta.get("prev_task_id"),
        "attempt": meta.get("attempt", 1),
        "claude_alive": alive,
        "last_completed_at": meta.get("last_completed_at"),
        "last_is_error": meta.get("last_is_error"),
        "last_result_full": meta.get("last_result_full", ""),
        "last_result_summary": meta.get("last_result_summary", ""),
        "last_error_code": meta.get("last_error_code", ""),
        # Issue #638: bloco de uso para o reconcile gravar o custo central de
        # dispatches fire-and-forget (implement paralelo), cuja resposta do 202
        # foi descartada. ``cli_model`` viaja para o anti model=unknown na ponta.
        "usage": meta.get("usage") or {},
        "cli_model": meta.get("cli_model", "") or "",
    })


async def dispatch_handler(request: web.Request) -> web.Response:
    """``POST /v1/dispatch`` — executa o CLI do adapter num workdir isolado.

    Resume só quando ``adapter.supports_resume`` E o payload trouxe
    ``resume_session_id`` + ``prev_task_id``; senão roda fresh.
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

    # repo/branch base viajam no bloco ``resume`` (mesma origem que o claude-worker).
    _resume_block = payload.get("resume") or {}
    repo_slug = str(_resume_block.get("repo") or "").strip()
    base_branch = str(_resume_block.get("main_branch") or "").strip()

    # Enforcement da allowlist (issue #639) — ANTES de qualquer clone. Só vale
    # quando há slug (sem slug o CLI roda no workspace cru, não clona nada).
    # Fail-closed: slug fora da allowlist (ou allowlist indisponível) → 403.
    if repo_slug:
        _allowed, _reason, _norm = _core.check_repo_allowed(repo_slug)
        if not _allowed:
            logger.warning(
                "dispatch BLOQUEADO — repo fora da allowlist (issue #639): %s",
                _reason,
            )
            return web.json_response({
                "ok": False,
                "error_code": "REPO_NOT_ALLOWED",
                "error": _reason,
            }, status=403)

    dispatch_timeout_s: Optional[int] = None
    _raw_timeout = payload.get("timeout_s")
    if _raw_timeout is not None:
        try:
            _v = int(_raw_timeout)
            if _v > 0:
                dispatch_timeout_s = _v
        except (TypeError, ValueError):
            pass

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

    if resume_ctx is not None:
        task_id = resume_ctx.prev_task_id
        workspace = root / task_id
        if not workspace.is_dir():
            # Workdir sumiu → degrada para fresh (brief lê .deile-progress.md).
            logger.warning(
                "resume solicitado (prev_task_id=%s) mas workdir sumiu — "
                "degradando para FRESH (re-gasto de tokens)", task_id,
            )
            resume_ctx = None
            task_id = secrets.token_hex(_TASK_ID_BYTES)
            workspace = root / task_id
    else:
        task_id = secrets.token_hex(_TASK_ID_BYTES)
        workspace = root / task_id
    workspace.mkdir(parents=True, exist_ok=True)

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

    # Plano §1.5: clone + checkout ANTES do CLI. Sem slug o CLI roda no workspace
    # cru e o gate reprova se nada for pushado.
    repo_ok = True
    repo_detail = "sem repo slug — CLI roda no workspace cru"
    if repo_slug:
        repo_ok, repo_detail = await _ensure_repo(
            workspace, repo=repo_slug, branch=branch, base_branch=base_branch,
        )
        if not repo_ok:
            await _release_lease(workspace / ".lease.json")
            return web.json_response({
                "ok": False,
                "error_code": "REPO_SETUP_FAILED",
                "error": repo_detail[:500],
                "task_id": task_id,
            }, status=200)
    repo_workdir = _core.repo_dir_for(workspace)
    run_cwd = repo_workdir if (repo_workdir / ".git").exists() else workspace

    timeout = dispatch_timeout_s if dispatch_timeout_s is not None else int(
        os.environ.get("DEILE_CLI_WORKER_TASK_TIMEOUT_S", "1800")
    )
    home = _worker_home(adapter)
    argv = adapter.build_argv(
        brief_path=str(brief_path),
        model=cli_model,
        reasoning=reasoning,
        workdir=str(run_cwd),
        resume=resume_ctx,
        task_id=task_id,
    )
    overlay = adapter.env_overlay(home=home)
    for k, v in overlay.items():
        os.environ[k] = v
    # Cria dirs graváveis declarados pelo adapter: com ``readOnlyRootFilesystem``
    # o subdir pode não existir e o CLI aborta (ex.: codex "CODEX_HOME does not
    # exist"). Best-effort: falha de mkdir não derruba o dispatch.
    for _dir_var in getattr(adapter, "writable_dirs", None) or []:
        _dir_path = os.environ.get(_dir_var, "").strip()
        if _dir_path:
            try:
                Path(_dir_path).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("não criei writable dir %s=%s: %s",
                               _dir_var, _dir_path, exc)

    # Provisiona a credencial por modelo antes do CLI (codex dual-mode: gpt-5*-codex
    # exige OAuth, mini aceita API key). Falha aborta com erro tipado.
    provision = getattr(adapter, "provision_auth", None)
    if callable(provision):
        try:
            auth_ok, auth_detail = provision(
                model=cli_model, home=home, env=dict(os.environ),
            )
        except Exception as exc:  # noqa: BLE001 — provision nunca crasha o handler
            auth_ok, auth_detail = False, f"provision_auth exceção: {exc}"
        if not auth_ok:
            await _release_lease(workspace / ".lease.json")
            return web.json_response({
                "ok": False,
                "error_code": "WORKER_AUTH_EXPIRED",
                "error": (auth_detail or "provisionamento de auth falhou")[:500],
                "task_id": task_id,
            }, status=200)
        if auth_detail:
            logger.info("provision_auth kind=%s model=%s → %s",
                        adapter.kind, cli_model, auth_detail)

    logger.info(
        "dispatch kind=%s task_id=%s stage=%s model=%s resume=%s repo=%s (%s)",
        adapter.kind, task_id, stage, cli_model, resume_ctx is not None,
        repo_slug or "-", repo_detail,
    )

    base_sha = await _git_head(workspace)

    # Lido antes da closure para que o meta reflita a cadeia mesmo sem session-id.
    _prev_meta = await asyncio.to_thread(_load_task_result, task_id) or {}
    if resume_ctx is not None:
        attempt = int(_prev_meta.get("attempt", 1)) + 1
        meta_prev_task_id = resume_ctx.prev_task_id
    else:
        attempt = 1
        meta_prev_task_id = str(prev_task_id or "") or None

    async def _run_and_finalize() -> dict:
        stop_hb = asyncio.Event()
        hb_task = asyncio.create_task(
            _heartbeat_loop(workspace / ".lease.json", stop_hb),
            name=f"lease-hb-{task_id}",
        )
        try:
            result = await run_subprocess_with_progress(
                argv, cwd=run_cwd, task_id=task_id, timeout=timeout,
                lease_path=workspace / ".lease.json", root=root,
            )
            work = adapter.parse_output(
                stdout=result.stdout, stderr=result.stderr, rc=result.returncode,
            )
            # Observabilidade de custo central (issue #638): enriquece o veredito
            # do adapter com tokens-por-modelo a partir do parser ÚNICO
            # (fleet_progress_parse), ANTES da finalização de git (que reconstrói
            # o WorkResult). Best-effort: extração falha → bloco vazio, dispatch segue.
            _tokens_by_model, _usage_model = build_usage_block(
                kind=adapter.kind, stdout=result.stdout,
                task_id=task_id, cli_model=cli_model,
            )
            if _tokens_by_model or _usage_model:
                work = replace(
                    work,
                    tokens_by_model=_tokens_by_model,
                    model=_usage_model,
                )
            # session_id nativo (issue #445): opencode/qwen no JSON; codex no
            # thread.started; goose/aider retornam o task_id como sentinela.
            try:
                session_id = adapter.extract_session_id(
                    stdout=result.stdout, stderr=result.stderr, task_id=task_id,
                )
            except Exception as exc:  # noqa: BLE001 — extração nunca derruba o dispatch
                logger.warning("extract_session_id falhou task_id=%s: %s", task_id, exc)
                session_id = ""
            # Git pós-run (plano §1.5/§4): só quando adapter aprovou.
            work = await _finalize_git(
                adapter, work, workspace=workspace, branch=branch,
                base_sha=base_sha, task_id=task_id,
            )
            work = await _post_run_gate(
                adapter, work, workdir=workspace, base_sha=base_sha, branch=branch,
            )
            # Persiste ANTES de retornar: no caminho fire-and-forget (202) a
            # resposta é descartada; sem isto o resume-info ficaria sem
            # session_id/last_completed_at e o reconcile trataria como RUNNING.
            await asyncio.to_thread(
                _save_task_result, task_id, work,
                session_id=session_id, workspace=workspace,
                prev_task_id=meta_prev_task_id, attempt=attempt,
                cli_model=cli_model,
            )
            return _build_response(
                task_id, result, work, session_id=session_id,
                worker_kind=adapter.kind,
            )
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


async def _ensure_repo(
    workspace: Path, *, repo: str, branch: Optional[str], base_branch: str,
) -> tuple:
    """Wrapper monkeypatchável do ciclo de repo do core."""
    return await _core.ensure_repo_and_branch(
        workspace, repo=repo, branch=branch, base_branch=base_branch,
    )


async def _finalize_git(
    adapter: CliAdapter,
    work: WorkResult,
    *,
    workspace: Path,
    branch: Optional[str],
    base_sha: Optional[str],
    task_id: str,
) -> WorkResult:
    """Commit (fallback) + push pós-run, por ``adapter.git_strategy`` (plano §1.5/§4).

    ``brief_driven``: se não houve commit novo, faz fallback commit e marca
    ``WRAPPER_COMMITTED``. ``cli_autocommit`` (aider): só pusha.
    Só atua quando ``work.ok``. Best-effort: falha de push vira ``error_code``.
    """
    if not work.ok or not branch:
        return work

    error_code = work.error_code
    if adapter.git_strategy == "brief_driven":
        head = await _git_head(workspace)
        has_new_commit = bool(head and (base_sha is None or head != base_sha))
        if not has_new_commit:
            committed = await _core.git_fallback_commit(
                workspace, branch,
                message=(
                    f"chore(cli-worker): fallback commit do {adapter.kind}-worker "
                    f"(task {task_id})"
                ),
            )
            if committed:
                error_code = "WRAPPER_COMMITTED"
                logger.info(
                    "fallback commit aplicado (brief_driven sem commit do agente)"
                    " task_id=%s branch=%s", task_id, branch,
                )

    pushed, push_detail = await _core.git_push(workspace, branch)
    if not pushed:
        logger.warning("push pós-run falhou task_id=%s: %s", task_id, push_detail)

    if error_code != work.error_code:
        # ``replace`` preserva tokens_by_model/model (issue #638).
        return replace(work, error_code=error_code)
    return work


#: Subdir onde o veredito de cada task é persistido (lido pelo ``resume-info``).
_RESULT_SUBDIR = ".sessions"


def _result_meta_path(task_id: str) -> Path:
    return _worker_root() / _RESULT_SUBDIR / f"{task_id}.json"



def _save_task_result(
    task_id: str,
    work: WorkResult,
    *,
    session_id: str = "",
    workspace: Optional[Path] = None,
    prev_task_id: Optional[str] = None,
    attempt: int = 1,
    cli_model: Optional[str] = None,
) -> None:
    """Persiste veredito + metadata da task (write-tmp + replace, issue #445).

    Três propósitos:
    1. **fire-and-forget (202):** sem isto o resume-info fica sem
       ``last_completed_at`` e o reconcile trata como RUNNING para sempre.
    2. **Resume nativo (anti-sangria):** persiste ``session_id``, ``workdir``,
       ``prev_task_id``, ``attempt`` para o pipeline re-despachar com
       ``resume_session_id`` + mesmo workdir.
    3. **Modelo durável (anti model=unknown):** vários CLIs (goose, aider, codex)
       não emitem o modelo no stdout — só no argv. Auditoria lê este meta.
    4. **Custo central de fire-and-forget (issue #638):** persiste o bloco
       ``usage`` (tokens-por-modelo) para que o reconcile do pipeline o leia via
       resume-info e grave no UsageRepository central (o 202 descarta a resposta
       do dispatch, então sem isto o implement paralelo não teria custo central).

    Best-effort: falha de I/O vira warning.
    """
    full = work.result_text or ""
    meta = {
        "task_id": task_id,
        "session_id": session_id or "",
        "workdir": str(workspace) if workspace is not None else "",
        "prev_task_id": prev_task_id,
        "attempt": int(attempt),
        "cli_model": cli_model or "",
        "last_completed_at": int(time.time()),
        "last_is_error": not work.ok,
        "last_result_full": full,
        "last_result_summary": full[:300],
        "last_error_code": work.error_code or "",
        "last_total_cost_usd": work.cost_usd,
        "usage": {
            "model": work.model,
            "tokens_by_model": work.tokens_by_model or {},
        },
    }
    path = _result_meta_path(task_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("failed to persist task result %s: %s", task_id, exc)


def _load_task_result(task_id: str) -> Optional[dict]:
    """Carrega o meta de resultado da task; ``None`` se ausente/ilegível."""
    path = _result_meta_path(task_id)
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("failed to read task result %s: %s", task_id, exc)
        return None


def _build_response(
    task_id: str, result, work: WorkResult, *, session_id: str = "",
    worker_kind: str = "",
) -> dict:
    """Monta a resposta JSON do contrato unificado (paridade com claude-worker).

    ``session_id`` = id nativo do CLI (issue #445); ``None`` quando o adapter
    não suporta resume. ``worker_kind`` (issue #638) viaja no bloco ``usage``
    para o pipeline atribuir o registro de custo central ao worker correto.
    """
    response = {
        "ok": work.ok,
        "stdout": result.stdout[-_STDOUT_TAIL:],
        "stderr": result.stderr[-_STDERR_TAIL:],
        "task_id": task_id,
        "session_id": session_id or None,
        "returncode": result.returncode,
        "duration_seconds": result.duration_seconds,
        "total_cost_usd": work.cost_usd,
        "is_error": not work.ok,
        "result": work.result_text,
        # Observabilidade de custo central (issue #638): bloco estruturado de uso
        # que o pipeline persiste no UsageRepository (1 registro por modelo).
        # ``worker`` permite atribuir o registro mesmo quando o endpoint não
        # revela o kind; ``model`` evita ``unknown`` (cai no cli_model).
        "usage": {
            "worker": worker_kind,
            "model": work.model,
            "tokens_by_model": work.tokens_by_model or {},
        },
    }
    if not work.ok:
        response["error_code"] = work.error_code or "WORKER_FAILED"
        response["error"] = _build_failure_reason(
            result.returncode, result.stderr, result.stdout, work.result_text,
        )[:500]
    elif work.error_code:
        # Sucesso degradado (ex.: WRAPPER_COMMITTED) — propaga sem marcar is_error.
        response["error_code"] = work.error_code
    return response


def _build_failure_reason(
    returncode: int, stderr: str, stdout: str, result_text: str,
    *, max_bytes: int = 1500,
) -> str:
    """Motivo de falha: result_text → stderr → stdout → rc genérico (124=timeout)."""
    if result_text and result_text.strip():
        return result_text[:max_bytes]
    if stderr and stderr.strip():
        tail = stderr[-max_bytes:]
        return f"rc={returncode} stderr: {tail.strip()}"
    if stdout and stdout.strip():
        tail = stdout[-max_bytes:]
        return f"rc={returncode} stdout: {tail.strip()}"
    if returncode == 124:
        return "subprocess timed out (rc=124)"
    return f"subprocess exited with rc={returncode} (sem saída capturada)"


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

_bearer_auth_mw = _core.make_bearer_auth_mw(("/v1/health",))


# --------------------------------------------------------------------------- #
# Ledger de custo durável da frota CLI (issue #445)
# --------------------------------------------------------------------------- #


def _cost_ledger_path() -> Path:
    """Caminho do ledger durável (sobrevive à poda dos logs)."""
    env = os.environ.get("DEILE_CLI_WORKER_COST_LEDGER_PATH", "").strip()
    if env:
        return Path(env)
    return _worker_root() / ".cost-ledger.jsonl"


def _harvested_task_ids(ledger_path: Path) -> set:
    """Conjunto de ``task_id`` já no ledger (dedup).

    Shim sobre :func:`_worker_core.ledger_harvested_ids` — preservado para que
    testes que monkeypatchem este nome continuem funcionando.
    """
    return _core.ledger_harvested_ids(ledger_path, key="task_id")


def _append_ledger(ledger_path: Path, record: dict) -> int:
    """Anexa um registro ao ledger. Retorna bytes escritos.

    Shim sobre :func:`_worker_core.ledger_append_record` — preservado para que
    testes que monkeypatchem este nome continuem funcionando.
    """
    return _core.ledger_append_record(
        ledger_path, record, ensure_ascii=False,
    )


def _meta_model_for(task_id: str) -> Optional[str]:
    """``cli_model`` do meta (fonte de verdade do modelo para auditoria)."""
    meta = _load_task_result(task_id) or {}
    m = meta.get("cli_model")
    return m.strip() if isinstance(m, str) and m.strip() else None


def _predominant_model(tokens_by_model: dict, cli_model: Optional[str]) -> Optional[str]:
    """Modelo predominante de um bloco de uso (maior total de tokens).

    Anti ``unknown`` (issue #638): se o único modelo é ``unknown`` (CLIs que não
    emitem o modelo no stdout — goose/aider/codex), cai no ``cli_model`` do
    payload. Empate é resolvido determinístico (maior soma, depois nome).
    """
    real = {
        m: sum(int(v or 0) for v in tk.values())
        for m, tk in tokens_by_model.items()
        if m and m != "unknown"
    }
    if real:
        return max(sorted(real), key=real.get)
    return (cli_model or "").strip() or (
        next(iter(tokens_by_model), None) if tokens_by_model else None
    )


def build_usage_block(
    *, kind: str, stdout: str, task_id: str, cli_model: Optional[str],
) -> tuple:
    """Extrai ``(tokens_by_model, model)`` da saída nativa do CLI (issue #638).

    Fonte ÚNICA de parsing: ``fleet_progress_parse`` (o MESMO parser do
    harvester do ledger e da auditoria), via :data:`_fpp`. Remapeia ``unknown``
    → ``cli_model`` do payload (anti model=unknown), espelhando exatamente o
    ``harvest_progress_to_ledger``. Retorna ``({}, None)`` quando o kind não usa
    ``.progress`` (claude/deile contabilizam por outras vias) ou o parser está
    indisponível — best-effort, nunca derruba o dispatch.

    O custo NÃO é calculado aqui: viaja como tokens-por-modelo e o pipeline
    recompõe o custo via ``jsonl_cost.fleet_cost_of_model`` (fonte única de
    preço) — paridade com o ledger durável.
    """
    if _fpp is None:
        return {}, (cli_model or "").strip() or None
    try:
        parsed = _fpp.parse_progress_text(kind, stdout, task_id)
    except Exception as exc:  # noqa: BLE001 — extração de uso é best-effort
        logger.warning("build_usage_block falhou kind=%s task_id=%s: %s",
                       kind, task_id, exc)
        return {}, (cli_model or "").strip() or None
    if parsed is None:
        return {}, (cli_model or "").strip() or None
    models = dict(parsed.get("models") or {})
    cm = (cli_model or "").strip()
    if cm and "unknown" in models:
        unk = models.pop("unknown")
        dst = models.setdefault(cm, {"in": 0, "out": 0, "cc": 0, "cr": 0})
        for k in ("in", "out", "cc", "cr"):
            dst[k] = int(dst.get(k, 0)) + int(unk.get(k, 0) or 0)
    # Normaliza para o contrato do WorkResult: chaves cache_read/cache_write.
    tokens_by_model = {
        m: {
            "in": int(tk.get("in", 0) or 0),
            "out": int(tk.get("out", 0) or 0),
            "cache_read": int(tk.get("cr", 0) or 0),
            "cache_write": int(tk.get("cc", 0) or 0),
        }
        for m, tk in models.items()
    }
    return tokens_by_model, _predominant_model(models, cli_model)


def harvest_progress_to_ledger(
    root: Path,
    kind: str,
    *,
    ledger_path: Optional[Path] = None,
    retention_days: Optional[int] = None,
    grace_s: Optional[int] = None,
    now: Optional[float] = None,
    dry_run: bool = False,
) -> dict:
    """Colhe custo dos logs de progresso para o ledger durável e poda.

    Para cada ``<root>/.progress/<task>.stdout.log`` além da retenção (fora do
    grace TOCTOU):
    1. Parseia via ``fleet_progress_parse``; remapeia ``unknown`` → ``cli_model``
       do meta (anti model=unknown).
    2. Anexa ``{task_id, worker, models, native_cost, …}`` ao ledger (dedup por
       task_id, idempotente).
    3. Remove ``.stdout.log`` + ``.stderr.log`` SOMENTE após contabilizar.

    Fail-safe cardinal: sem o parser disponível, NÃO poda (preserva custo).
    ``dry_run`` só reporta candidatos sem podar.
    """
    if ledger_path is None:
        ledger_path = _cost_ledger_path()
    if retention_days is None:
        retention_days = _PROGRESS_RETENTION_DAYS
    if grace_s is None:
        grace_s = _PROGRESS_GRACE_S
    if now is None:
        now = time.time()

    progress_dir = root / ".progress"
    result = {
        "sessions_harvested": 0,
        "logs_removed": 0,
        "ledger_bytes_written": 0,
        "bytes_freed": 0,
        "errors": [],
    }
    if not progress_dir.is_dir():
        return result

    cutoff = now - max(max(0, retention_days) * 86400, grace_s)
    try:
        logs = sorted(progress_dir.glob("*.stdout.log"))
    except OSError as exc:
        result["errors"].append(f"glob {progress_dir}: {exc}")
        return result

    candidates = []
    for log in logs:
        try:
            if log.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        candidates.append(log)

    if dry_run or not candidates:
        result["candidates"] = [str(p) for p in candidates]
        return result

    if _fpp is None:
        result["errors"].append(
            "fleet_progress_parse indisponível — poda abortada (fail-safe)")
        logger.error(
            "cost-ledger harvest ABORTADO: fleet_progress_parse indisponível; "
            "%d logs preservados (sem poda) para não perder custo", len(candidates),
        )
        return result

    harvested = _harvested_task_ids(ledger_path)
    for log in candidates:
        task_id = log.name[: -len(".stdout.log")]
        try:
            text = log.read_text(errors="replace")
        except OSError as exc:
            result["errors"].append(f"read {log}: {exc}")
            continue
        try:
            parsed = _fpp.parse_progress_text(kind, text, task_id)
        except Exception as exc:  # noqa: BLE001 — parse falhou: preserva o log
            result["errors"].append(f"parse {log}: {exc}")
            continue
        if parsed is None:
            # Kind não usa .progress (ex.: claude/deile) — não é nosso.
            continue
        models = dict(parsed.get("models") or {})
        # Remap unknown → cli_model do meta (anti model=unknown).
        meta_model = _meta_model_for(task_id)
        if meta_model and "unknown" in models:
            unk = models.pop("unknown")
            dst = models.setdefault(meta_model, {"in": 0, "out": 0, "cc": 0, "cr": 0})
            for k in ("in", "out", "cc", "cr"):
                dst[k] = int(dst.get(k, 0)) + int(unk.get(k, 0) or 0)

        has_tokens = any(sum(int(x or 0) for x in v.values()) > 0 for v in models.values())
        if task_id not in harvested and has_tokens:
            try:
                source_mtime = log.stat().st_mtime
            except OSError:
                source_mtime = now
            try:
                written = _append_ledger(ledger_path, {
                    "v": 1,
                    "task_id": task_id,
                    "worker": kind,
                    "models": models,
                    "native_cost": parsed.get("native_cost"),
                    "harvested_at": now,
                    "source_mtime": source_mtime,
                })
            except OSError as exc:
                result["errors"].append(f"ledger write {log}: {exc}")
                continue  # preserva o log: custo não contabilizado ainda
            result["ledger_bytes_written"] += written
            result["sessions_harvested"] += 1
            harvested.add(task_id)

        freed = 0
        for sibling in (log, log.with_name(task_id + ".stderr.log")):
            try:
                if sibling.is_file():
                    freed += sibling.stat().st_size
                    sibling.unlink()
            except OSError as exc:
                result["errors"].append(f"unlink {sibling}: {exc}")
        if not log.exists():
            result["logs_removed"] += 1
            result["bytes_freed"] += freed

    logger.info(
        "cost-ledger harvest (%s): sessions=%d logs_removed=%d freed=%d bytes "
        "ledger=+%d bytes errors=%d",
        kind, result["sessions_harvested"], result["logs_removed"],
        result["bytes_freed"], result["ledger_bytes_written"], len(result["errors"]),
    )
    return result


def run_cleanup() -> dict:
    """Ciclo de cleanup síncrono: workdirs stale + harvest do ledger (issue #445).

    ``has_session=None`` porque workers CLI são fresh-only (sem JSONL a
    preservar) — critério = lease morto + idade. Harvest é best-effort.
    """
    root = _worker_root()
    res = _core.startup_cleanup(
        root, retention_days=_CLEANUP_RETENTION_DAYS, has_session=None,
    )
    try:
        harvest = harvest_progress_to_ledger(root, _selected_kind())
        res["cost_ledger"] = harvest
    except Exception as exc:  # noqa: BLE001 — harvest nunca derruba o cleanup
        logger.warning("harvest do ledger falhou: %s", exc)
        res["cost_ledger"] = {"errors": [str(exc)]}
    return res


async def _cleanup_loop(stop_event: asyncio.Event) -> None:
    """Roda ``run_cleanup`` periodicamente; encerra quando ``stop_event`` é setado."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=_CLEANUP_INTERVAL_S,
            )
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            await asyncio.to_thread(run_cleanup)
        except Exception as exc:  # noqa: BLE001 — cleanup nunca derruba o server
            logger.warning("cleanup periódico falhou: %s", exc)


def build_app(auth_token: Optional[str] = None) -> web.Application:
    """Monta a ``aiohttp.web.Application`` do CLI worker.

    ``auth_token`` opcional para testes in-process. Startup = cleanup de workdirs
    stale (issue #445) + task periódica a cada ``_CLEANUP_INTERVAL_S``.
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
    app.router.add_get(
        "/v1/dispatches/{task_id}/resume-info", resume_info_handler,
    )

    async def _on_startup(_app: web.Application) -> None:
        try:
            res = await asyncio.to_thread(run_cleanup)
            logger.info("startup cleanup: %s", res)
        except Exception as exc:  # noqa: BLE001 — boot nunca falha por cleanup
            logger.warning("startup cleanup falhou: %s", exc)
        stop = asyncio.Event()
        _app["_cleanup_stop"] = stop
        _app["_cleanup_task"] = asyncio.create_task(
            _cleanup_loop(stop), name="cli-worker-cleanup",
        )

    async def _on_cleanup(_app: web.Application) -> None:
        stop = _app.get("_cleanup_stop")
        if stop is not None:
            stop.set()
        task = _app.get("_cleanup_task")
        if task is not None:
            try:
                await task
            except Exception:  # noqa: BLE001
                pass

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main(passthrough: Optional[List[str]] = None) -> int:
    """Entry point no mode ``cli-worker``: resolve adapter, cria work root, sobe aiohttp."""
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
