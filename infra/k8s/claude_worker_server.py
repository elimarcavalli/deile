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
* ``GET  /v1/progress/{task_id}``  — mid-flight snapshot via PVC tail (Task 14)

Spec: ``docs/superpowers/specs/2026-05-26-claude-worker-design.md`` §4.4.
"""

from __future__ import annotations

import asyncio
import hmac
import json
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
# OAuth token extraction — claude CLI no Linux NÃO lê
# ``~/.claude/credentials.json`` automaticamente (esse caminho é uma
# convenção macOS — no Linux ele só lê variáveis de ambiente). Extraímos
# o ``accessToken`` no startup e o exportamos como ``ANTHROPIC_AUTH_TOKEN``
# antes de spawnar o subprocess do claude.
# --------------------------------------------------------------------------- #


def _load_oauth_token_into_env() -> bool:
    """Lê ``credentials.json`` (mountado pelo initContainer) e exporta
    ``ANTHROPIC_AUTH_TOKEN`` na env do processo.

    Returns ``True`` se token foi carregado; ``False`` caso contrário (file
    ausente, JSON malformado, sem ``claudeAiOauth.accessToken``). O server
    continua subindo em qualquer caso — a falha real aparece quando o
    ``claude -p`` rodar e reportar ``Not logged in``.
    """
    home = Path(os.environ.get("HOME", "/home/claude"))
    creds_path = home / ".claude" / "credentials.json"
    if not creds_path.exists():
        logger.warning("credentials.json não encontrado em %s", creds_path)
        return False
    try:
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("não foi possível parsear %s: %s", creds_path, exc)
        return False
    # macOS Keychain JSON: {"claudeAiOauth": {"accessToken": "..."}}
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    token = (oauth or {}).get("accessToken") if isinstance(oauth, dict) else None
    if not token:
        # Fallback: tenta "accessToken" no root level (formatos diferentes).
        token = creds.get("accessToken") if isinstance(creds, dict) else None
    if not token:
        logger.warning(
            "credentials.json não contém claudeAiOauth.accessToken nem "
            "accessToken root-level — claude CLI vai reportar 'Not logged in'",
        )
        return False
    os.environ["ANTHROPIC_AUTH_TOKEN"] = token
    logger.info("ANTHROPIC_AUTH_TOKEN carregado de %s (len=%d)",
                creds_path, len(token))
    return True


# --------------------------------------------------------------------------- #
# Bearer auth (defense-in-depth — NetworkPolicy bloqueia ingress fora do
# deile-pipeline, mas auth no app-layer impede que pod comprometido dentro
# do allowlist envie dispatch malicioso).
# --------------------------------------------------------------------------- #


def _read_auth_token() -> str:
    """Lê o Bearer token do Secret K8s ``claude-worker-bearer``.

    Caminhos em ordem (primeiro existente vence):
    1. ``/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN`` (Secret
       montado como file pelo manifest 50).
    2. ``DEILE_CLAUDE_WORKER_AUTH_TOKEN_FILE`` env var (override pra dev).
    3. ``DEILE_CLAUDE_WORKER_AUTH_TOKEN`` env var (testes apenas — nunca
       loga o valor).

    Raises:
        RuntimeError: nenhuma source disponível (Secret não populado +
            env vars vazias) — server abort no startup pra forçar fix.
    """
    candidates = [
        Path("/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN"),
        Path(os.environ.get("DEILE_CLAUDE_WORKER_AUTH_TOKEN_FILE", "")),
    ]
    for p in candidates:
        if p and p.is_file():
            token = p.read_text(encoding="utf-8").strip()
            if token:
                return token
    env_val = os.environ.get("DEILE_CLAUDE_WORKER_AUTH_TOKEN", "").strip()
    if env_val:
        return env_val
    raise RuntimeError(
        "claude-worker auth token not found: expected "
        "/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN "
        "(populated by deploy.py k8s claude-login) or "
        "DEILE_CLAUDE_WORKER_AUTH_TOKEN env"
    )


@web.middleware
async def _bearer_auth_mw(request: web.Request, handler):
    """Bearer auth middleware (paridade com ``worker_server._bearer_auth_mw``).

    Whitelist ``/v1/health`` (readiness probe sem token). Demais paths
    exigem ``Authorization: Bearer <token>`` comparado em constant-time
    (``hmac.compare_digest``) para evitar timing-attack na descoberta.
    """
    if request.path == "/v1/health":
        return await handler(request)
    expected = request.app["auth_token"]
    got = request.headers.get("Authorization", "")
    if not got.startswith("Bearer ") or not hmac.compare_digest(
            got[len("Bearer "):], expected):
        return web.json_response(
            {"error": {"code": "UNAUTHORIZED", "message": "bad bearer"}},
            status=401,
        )
    return await handler(request)


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
        "branch $BRANCH. Revise rigorosamente a PR descrita após '---'.\n"
        "\n"
        "REGRA OBRIGATÓRIA (não negociável): a EXECUÇÃO INTEIRA é considerada "
        "FALHA se você terminar sem ter postado pelo menos um destes:\n"
        "  - `gh pr review <pr_number> --comment --body \"<resumo>\"` (top-level), OU\n"
        "  - `gh api repos/<owner>/<repo>/pulls/<pr>/comments -f body=...` (inline), OU\n"
        "  - `gh issue comment <pr_number> --body \"<resumo>\"` (fallback simples)\n"
        "\n"
        "Não basta analisar e imprimir STATUS — o operador precisa VER a review "
        "no GitHub. Faça primeiro o `gh pr review` (ou `gh issue comment`), CONFIRME "
        "que postou (saída do comando contém URL), e SÓ ENTÃO imprima 'STATUS: APPROVE' "
        "ou 'STATUS: REQUEST_CHANGES'. Em bloqueio real: imprima "
        "'STATUS: BLOCKED_<motivo>' DEPOIS de também postar um `gh issue comment` "
        "explicando o que faltou."
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


#: ``secrets.token_hex(8)`` em :func:`dispatch_handler` gera exatamente 16
#: chars hex; qualquer outra forma é rejeitada para não permitir path traversal
#: pela URL nem leitura de arquivos arbitrários no PVC.
_TASK_ID_RE = re.compile(r"[0-9a-f]{16}")


async def progress_handler(request: web.Request) -> web.Response:
    """``GET /v1/progress/{task_id}`` — snapshot do task em execução ou completo.

    Lê os arquivos persistidos por :func:`run_subprocess_with_progress` no PVC
    (``DEILE_CLAUDE_WORKER_ROOT/.progress/<task_id>.<stream>.log``) e devolve
    tail (stdout 50 KiB, stderr 10 KiB). Usado pelo painel TUI / subagent
    orchestration para acompanhar mid-flight sem aguardar a resposta do
    ``/v1/dispatch``.

    Returns:
        - ``200`` com ``{task_id, stdout, stderr}`` se algum dos arquivos existe.
        - ``404`` se ``task_id`` tem formato válido mas nenhum dos arquivos
          de progress está presente (task ainda não rodou, foi GCed, etc.).
        - ``400`` se ``task_id`` não bate ``[0-9a-f]{16}`` — defende contra
          path traversal pela URL e contra IDs vazados de outros sistemas.

    Erros de I/O ao ler os arquivos viram ``logger.warning`` + string vazia
    (best-effort): o que o cliente vê é o que conseguimos ler.
    """
    task_id = request.match_info["task_id"]

    # Sanity: task_id deve ser hex 16-char (gerado por secrets.token_hex(8)).
    if not _TASK_ID_RE.fullmatch(task_id):
        return web.json_response(
            {"error": "invalid task_id format (expected hex 16-char)"},
            status=400,
        )

    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    progress_dir = root / ".progress"
    stdout_path = progress_dir / f"{task_id}.stdout.log"
    stderr_path = progress_dir / f"{task_id}.stderr.log"

    if not stdout_path.exists() and not stderr_path.exists():
        return web.json_response(
            {"error": f"task_id {task_id} not found"},
            status=404,
        )

    try:
        stdout = stdout_path.read_text() if stdout_path.exists() else ""
    except OSError as exc:
        logger.warning("failed to read %s: %s", stdout_path, exc)
        stdout = ""

    try:
        stderr = stderr_path.read_text() if stderr_path.exists() else ""
    except OSError as exc:
        logger.warning("failed to read %s: %s", stderr_path, exc)
        stderr = ""

    return web.json_response({
        "task_id": task_id,
        "stdout": stdout[-50_000:],
        "stderr": stderr[-10_000:],
    })


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def build_app(auth_token: Optional[str] = None) -> web.Application:
    """Monta a ``aiohttp.web.Application`` com as três rotas do contrato.

    Bearer middleware ativo por default (paridade com
    ``worker_server.build_app``). O ``auth_token`` opcional permite testes
    in-process passarem o token sem precisar mockar
    :func:`_read_auth_token`. Em produção (chamado pelo :func:`main`), o
    token vem de ``/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN``.

    ``client_max_size=512 KiB`` limita o body do ``/v1/dispatch`` — briefs
    de pipeline normalmente cabem em <50 KiB; o teto generoso (10x) ainda
    barra payloads anômalos que poderiam encher o PVC.
    """
    app = web.Application(
        middlewares=[_bearer_auth_mw],
        client_max_size=512 * 1024,
    )
    app["auth_token"] = auth_token or _read_auth_token()
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

    # Carrega o OAuth token do ``credentials.json`` (montado pelo
    # initContainer via Secret claude-credentials) e exporta como
    # ``ANTHROPIC_AUTH_TOKEN``. SEM isso o claude CLI roda como
    # "Not logged in" porque no Linux ele NÃO lê
    # ``~/.claude/credentials.json`` automaticamente (esse path é
    # convenção macOS — Linux só lê env vars).
    _load_oauth_token_into_env()

    logger.info(
        "claude_worker_server listening on %s:%d, work root=%s", host, port, root,
    )
    app = build_app()
    web.run_app(app, host=host, port=port, print=lambda *_: None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
