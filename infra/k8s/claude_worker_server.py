#!/usr/bin/env python3
"""claude_worker_server — long-running ``claude-worker`` Pod (issue #309 fase 2).

Servidor HTTP aiohttp dentro do Pod ``claude-worker``. Recebe dispatches do
``deile-pipeline`` (Bearer auth, escopo do mesmo secret do ``deile-worker``)
e executa ``claude -p`` em subprocess sob ``/home/claude/work/<task_id>/``.
Diferenças de papel vs. o ``deile-worker``:

* O ``deile-worker`` roda o agente DEILE in-process e usa provedores LLM via
  ``*_API_KEY``. O ``claude-worker`` NÃO carrega API keys — o ``claude`` CLI
  usa autenticação por assinatura do Claude Code; ``ANTHROPIC_API_KEY`` é
  explicitamente removido pelo wrapper antes deste módulo subir.
* A allowlist regex de repositórios (``/etc/claude-worker/allowed_repos.regex``)
  é montada pelo wrapper e usada para barrar ``git push`` para destinos
  arbitrários (defense-in-depth contra prompt-injection no brief).

Endpoints:

* ``GET  /v1/health``              — readiness/liveness probe (Task 12)
* ``POST /v1/dispatch``            — receive brief + spawn ``claude -p`` (Task 13)
* ``GET  /v1/progress/{task_id}``  — mid-flight snapshot      (stub 501; Task 14)

Spec: ``docs/superpowers/specs/2026-05-26-claude-worker-design.md`` §4.4.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from aiohttp import web

logger = logging.getLogger("deile.claude_worker_server")


# --------------------------------------------------------------------------- #
# Subprocess execution
# --------------------------------------------------------------------------- #


@dataclass
class SubprocessResult:
    """Resultado de :func:`run_subprocess_with_progress`.

    Encapsula o que o handler precisa devolver na resposta JSON. ``stdout`` e
    ``stderr`` aqui são as strings completas (não truncadas); a truncagem por
    bytes vive no handler, próxima do contrato de resposta.
    """

    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


#: Preambles por stage. Cada um descreve identidade + contrato de output, com
#: placeholders ``$BRANCH``/``$TASK_ID`` substituídos por
#: :func:`_render_preamble` antes do exec.
PREAMBLE_TEMPLATES = {
    "implement": (
        "Você é Claude Code em modo autônomo (claude-worker pod, dispatch local).\n"
        "Worktree: já checked out em $PWD, branch $BRANCH.\n"
        "Tarefa: implemente o que está descrito após '---' abaixo.\n"
        "Quando terminar com sucesso, imprima 'STATUS: SUCCESS' como última linha.\n"
        "Em falha, 'STATUS: BLOCKED_<motivo>'.\n"
        "NÃO faça merge, NÃO use push --force, NÃO use --no-verify."
    ),
    "review": (
        "Você é Claude Code revisor (claude-worker pod). Worktree: $PWD, branch $BRANCH.\n"
        "Tarefa: revise a PR descrita após '---'. Comente achados via gh CLI.\n"
        "Imprima 'STATUS: SUCCESS' quando review estiver postado; "
        "'STATUS: BLOCKED_<motivo>' em falha."
    ),
    "classify": (
        "Você é Claude Code classificador (claude-worker pod). Tarefa: classifique "
        "a issue descrita após '---'. Imprima JSON com {category, severity, "
        "estimated_effort}. 'STATUS: SUCCESS' ao final."
    ),
    "refine": (
        "Você é Claude Code refinador (claude-worker pod). Tarefa: refine o body "
        "da issue descrita após '---' editando-a via gh CLI. 'STATUS: SUCCESS' ao final."
    ),
    "pr_review": (
        "Você é Claude Code revisor de PR (claude-worker pod). Worktree: $PWD, "
        "branch $BRANCH. Revise rigorosamente a PR descrita após '---', poste "
        "achados inline via gh api. Imprima 'STATUS: APPROVE' ou 'STATUS: REQUEST_CHANGES'."
    ),
    "follow_ups": (
        "Você é Claude Code follow-up handler (claude-worker pod). Worktree: $PWD. "
        "Trate os follow-ups descritos após '---'. 'STATUS: SUCCESS' ao final."
    ),
}


def _render_preamble(stage: str, branch: Optional[str], task_id: str) -> str:
    """Renderiza o preamble por ``stage`` substituindo placeholders.

    Stage desconhecido cai no template ``implement`` (default seguro: pede
    ``STATUS: SUCCESS`` e desencoraja operações destrutivas). ``$PWD`` fica
    vazio — o ``claude`` descobre via ``pwd`` na sessão; usamos a string só
    para sinalizar ao agente que ele já está no diretório certo.
    """
    template = PREAMBLE_TEMPLATES.get(stage, PREAMBLE_TEMPLATES["implement"])
    return (
        template
        .replace("$BRANCH", branch or "(no branch)")
        .replace("$PWD", "")
        .replace("$TASK_ID", task_id)
    )


async def run_subprocess_with_progress(
    args: list,
    *,
    cwd: Path,
    task_id: str,
    timeout: int,
) -> SubprocessResult:
    """Spawn de ``claude -p`` com persistência de stdout/stderr para o PVC.

    Os arquivos ``<task_id>.stdout.log``/``<task_id>.stderr.log`` ficam em
    ``DEILE_CLAUDE_WORKER_ROOT/.progress/`` e serão consumidos pelo
    ``/v1/progress/{task_id}`` (Task 14) para snapshot mid-flight no painel
    TUI. Em timeout, devolvemos ``returncode=124`` (convenção do ``coreutils
    timeout``) com mensagem em ``stderr``.
    """
    start = time.monotonic()

    # Persistir progress files em DEILE_CLAUDE_WORKER_ROOT/.progress/.
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    progress_dir = root / ".progress"
    progress_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = progress_dir / f"{task_id}.stdout.log"
    stderr_path = progress_dir / f"{task_id}.stderr.log"

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        duration = time.monotonic() - start
        return SubprocessResult(
            returncode=124, stdout="",
            stderr=f"claude -p timed out after {timeout}s",
            duration_seconds=duration,
        )

    duration = time.monotonic() - start
    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")

    # Persiste para o ``/v1/progress`` (Task 14) — best-effort; falha em
    # escrita NÃO derruba o dispatch (o cliente já recebeu o resultado).
    try:
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
    except OSError as exc:
        logger.warning(
            "failed to persist progress logs for task_id=%s: %s", task_id, exc,
        )

    return SubprocessResult(
        returncode=proc.returncode or 0,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
    )


#: Slugs internos do DEILE têm forma ``provider:model``. O ``claude-worker``
#: só aceita ``anthropic:*`` — outros providers são rejeitados em 400.
_ANTHROPIC_SLUG_RE = re.compile(r"^anthropic:(.+)$")


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


async def health_handler(request: web.Request) -> web.Response:
    """Readiness/liveness — verifica que o ``claude`` está acessível no ``PATH``.

    O ``readinessProbe`` do Kubernetes consome este endpoint: 200 mantém o Pod
    no Service (aceitando dispatches); 500 removes do Service. Como rodamos
    com uma única réplica em V1, o sinal serve principalmente ao operador
    (Pod ``NotReady`` aparece em ``kubectl get pods``).
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        return web.json_response(
            {"status": "error", "error": "claude binary not found in PATH"},
            status=500,
        )
    return web.json_response({"status": "ok", "claude_binary": claude_bin})


async def dispatch_handler(request: web.Request) -> web.Response:
    """``POST /v1/dispatch`` — executa ``claude -p`` em worktree isolado.

    Fluxo:

    1. Parse + validação do payload (``brief`` obrigatório; ``preferred_model``,
       se presente, deve ser ``anthropic:*``).
    2. Geração de ``task_id`` (``secrets.token_hex(8)``) e criação do
       diretório de trabalho ``DEILE_CLAUDE_WORKER_ROOT/<task_id>/``.
    3. Render do preamble do ``stage`` (default ``implement``) + concatenação
       com o ``brief`` via separador ``---``.
    4. ``claude -p --permission-mode bypassPermissions [--model <slug>]
       <full_prompt>`` executado em ``cwd=workspace``.
    5. Persistência best-effort de ``stdout``/``stderr`` no PVC para
       consumo de ``/v1/progress/{task_id}`` (Task 14).
    6. Resposta JSON com ``{ok, stdout(tail 50K), stderr(tail 10K), task_id,
       duration_seconds, returncode}``.

    Truncagem de tails: a resposta JSON limita ``stdout`` a 50 KiB e
    ``stderr`` a 10 KiB para não inflar o body — os logs completos ficam no
    PVC e podem ser inspecionados via ``/v1/progress`` ou ``kubectl exec``.
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "error": "invalid JSON"}, status=400,
        )

    brief = payload.get("brief")
    if not brief or not isinstance(brief, str):
        return web.json_response(
            {"ok": False, "error": "missing or invalid 'brief'"}, status=400,
        )

    stage = payload.get("stage", "implement")
    branch = payload.get("branch")
    model_slug = payload.get("preferred_model")

    # claude-worker SÓ aceita anthropic:* — outros providers viraram 400.
    claude_model: Optional[str] = None
    if model_slug:
        match = _ANTHROPIC_SLUG_RE.match(model_slug)
        if not match:
            return web.json_response({
                "ok": False,
                "error": (
                    f"claude-worker requires 'anthropic:*' model, "
                    f"got {model_slug!r}"
                ),
            }, status=400)
        claude_model = match.group(1)

    # Workspace fresh por dispatch — sem leakage cross-task.
    task_id = secrets.token_hex(8)
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    workspace = root / task_id
    workspace.mkdir(parents=True, exist_ok=True)

    # Preamble do stage + brief, separados pelo delimitador convencionado.
    preamble = _render_preamble(stage, branch, task_id)
    full_prompt = preamble + "\n\n---\n\n" + brief

    claude_bin = shutil.which("claude") or "claude"
    cmd = [claude_bin, "-p", "--permission-mode", "bypassPermissions"]
    if claude_model:
        cmd.extend(["--model", claude_model])
    cmd.append(full_prompt)

    logger.info(
        "dispatch task_id=%s stage=%s model=%s branch=%s",
        task_id, stage, claude_model, branch,
    )

    timeout = int(os.environ.get("DEILE_CLAUDE_WORKER_TASK_TIMEOUT_S", "1800"))

    try:
        result = await run_subprocess_with_progress(
            cmd, cwd=workspace, task_id=task_id, timeout=timeout,
        )
    except Exception as exc:
        logger.exception("dispatch failed task_id=%s", task_id)
        return web.json_response({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "task_id": task_id,
        }, status=500)

    return web.json_response({
        "ok": result.returncode == 0,
        "stdout": result.stdout[-50_000:],
        "stderr": result.stderr[-10_000:],
        "task_id": task_id,
        "duration_seconds": result.duration_seconds,
        "returncode": result.returncode,
    })


async def progress_handler(request: web.Request) -> web.Response:
    """``GET /v1/progress/{task_id}`` — STUB (Task 14 implementa).

    Será responsável por devolver o tail dos arquivos de stdout/stderr que
    o ``run_subprocess_with_progress`` persistir no PVC, permitindo ao
    painel TUI fazer polling mid-flight (paridade com o
    ``/v1/progress/{task_id}`` do ``deile-worker``).
    """
    return web.json_response(
        {"status": "not_implemented_yet", "task": 14}, status=501,
    )


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def build_app() -> web.Application:
    """Monta a ``aiohttp.web.Application`` com as três rotas do contrato.

    Espelha o padrão de ``worker_server.build_app`` (deile-worker) — registry
    centralizado no construtor + handlers como ``async def`` de módulo. A
    autenticação Bearer será adicionada na Task 13 (junto com o dispatch
    real), seguindo o mesmo modelo de middleware.
    """
    app = web.Application()
    app.router.add_get("/v1/health", health_handler)
    app.router.add_post("/v1/dispatch", dispatch_handler)
    app.router.add_get("/v1/progress/{task_id}", progress_handler)
    return app


def main(passthrough: Optional[List[str]] = None) -> int:
    """Entry point chamado pelo ``wrapper.py`` no mode ``claude-worker``.

    ``passthrough`` existe apenas para casar a assinatura usada pelo
    ``wrapper.py`` (``server_main(passthrough)``); por enquanto não temos
    flags de CLI próprias. Tasks futuras podem consumi-lo com ``argparse``.
    """
    del passthrough  # reservado para uso futuro (ver docstring).

    logging.basicConfig(
        level=os.environ.get("DEILE_CLAUDE_WORKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("DEILE_CLAUDE_WORKER_HOST", "0.0.0.0")
    port = int(os.environ.get("DEILE_CLAUDE_WORKER_PORT", "8767"))
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("could not create work root %s: %s", root, exc)
        return 78

    logger.info(
        "claude_worker_server listening on %s:%d, work root=%s", host, port, root,
    )
    app = build_app()
    web.run_app(app, host=host, port=port, print=lambda *_: None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
