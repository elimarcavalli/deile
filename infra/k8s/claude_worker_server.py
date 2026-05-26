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

Endpoints (Task 12 = skeleton + ``/v1/health``):

* ``GET  /v1/health``              — readiness/liveness probe (implementado)
* ``POST /v1/dispatch``            — receive brief + spawn ``claude -p``  (stub 501; Task 13)
* ``GET  /v1/progress/{task_id}``  — mid-flight snapshot                  (stub 501; Task 14)

Spec: ``docs/superpowers/specs/2026-05-26-claude-worker-design.md`` §4.4.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from aiohttp import web

logger = logging.getLogger("deile.claude_worker_server")


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
    """``POST /v1/dispatch`` — STUB (Task 13 implementa).

    Será responsável por: validar ``DispatchPayload`` (modelo em
    ``anthropic:*``), criar worktree sob ``/home/claude/work/<task_id>/``,
    executar ``claude -p`` (com timeout e tail de stdout/stderr no PVC) e
    devolver ``{task_id, stdout, stderr, duration_s, exit_code}``.
    """
    return web.json_response(
        {"status": "not_implemented_yet", "task": 13}, status=501,
    )


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
