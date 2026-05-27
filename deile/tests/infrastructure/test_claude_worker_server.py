"""Integration tests para ``claude_worker_server`` (issue #309 fase 2 Task 12).

O ``claude_worker_server`` é o servidor HTTP do papel ``claude-worker`` no
cluster: recebe dispatches do ``deile-pipeline``, executa ``claude -p`` em
subprocess e devolve resultados. Esta task entrega apenas o esqueleto + o
endpoint ``/v1/health`` — ``/v1/dispatch`` e ``/v1/progress`` ficam como
``501 Not Implemented`` para serem preenchidos nas Tasks 13 e 14.

O módulo vive em ``infra/k8s/``, não no pacote Python. Carregamos via
``importlib.util`` (mesmo padrão de ``test_wrapper_claude_worker.py``) para
manter os testes isolados — sem mexer em ``sys.path`` global.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

#: Token usado pelos testes — mesmo valor passado para
#: ``build_app(auth_token="test-token")``. Reutilizado em todos os calls
#: HTTP autenticados.
_AUTH_HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def claude_worker_module():
    """Carrega ``infra/k8s/claude_worker_server.py`` dinamicamente.

    Cada teste recebe uma instância nova do módulo, evitando contaminação
    cross-teste (caches, handlers já registrados, etc.).
    """
    repo_root = Path(__file__).resolve().parents[3]
    server_path = repo_root / "infra" / "k8s" / "claude_worker_server.py"
    spec = importlib.util.spec_from_file_location(
        "claude_worker_server_under_test", str(server_path),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claude_worker_server_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_health_returns_200_when_binary_present(
    claude_worker_module, monkeypatch,
):
    """``/v1/health`` retorna 200 + caminho do binário quando ``claude``
    está no ``PATH``."""
    monkeypatch.setattr(
        claude_worker_module.shutil,
        "which",
        lambda b: "/usr/local/bin/claude" if b == "claude" else None,
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["claude_binary"] == "/usr/local/bin/claude"


async def test_health_returns_500_when_binary_missing(
    claude_worker_module, monkeypatch,
):
    """``/v1/health`` retorna 500 quando o binário ``claude`` não está no
    ``PATH`` — o pod é removido do Service pelo readinessProbe."""
    monkeypatch.setattr(claude_worker_module.shutil, "which", lambda b: None)

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")
        assert resp.status == 500
        body = await resp.json()
        # A mensagem precisa mencionar o binário/PATH para diagnóstico do
        # operador — não exigimos string exata para permitir refino futuro.
        assert "claude" in body["error"].lower()


# --------------------------------------------------------------------------- #
# Task 14: /v1/progress/{task_id}
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_progress_returns_404_for_unknown_task(claude_worker_module, monkeypatch, tmp_path):
    """task_id válido mas progress file não existe → 404."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        # 16-char hex valid format mas inexistente
        resp = await client.get("/v1/progress/0123456789abcdef",
                                headers=_AUTH_HEADERS)
        assert resp.status == 404


@pytest.mark.asyncio
async def test_progress_returns_400_for_invalid_task_id(claude_worker_module, monkeypatch, tmp_path):
    """task_id formato inválido (não-hex 16-char) → 400."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        # Not hex
        resp = await client.get("/v1/progress/garbage-no-hex",
                                headers=_AUTH_HEADERS)
        assert resp.status == 400

        # Wrong length
        resp = await client.get("/v1/progress/abc123",
                                headers=_AUTH_HEADERS)
        assert resp.status == 400


@pytest.mark.asyncio
async def test_progress_returns_tails(claude_worker_module, monkeypatch, tmp_path):
    """Quando progress files existem, devolve tail dos últimos N bytes."""
    progress_dir = tmp_path / ".progress"
    progress_dir.mkdir()
    (progress_dir / "abcdef0123456789.stdout.log").write_text("line 1\nline 2\nline 3\n")
    (progress_dir / "abcdef0123456789.stderr.log").write_text("err A\nerr B\n")

    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/progress/abcdef0123456789",
                                headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
        assert "stdout" in body
        assert "stderr" in body
        assert "line 3" in body["stdout"]
        assert "err B" in body["stderr"]
        assert body["task_id"] == "abcdef0123456789"


@pytest.mark.asyncio
async def test_progress_handles_only_stdout_present(claude_worker_module, monkeypatch, tmp_path):
    """Se só stdout file existe (stderr vazio), ainda retorna 200."""
    progress_dir = tmp_path / ".progress"
    progress_dir.mkdir()
    (progress_dir / "fedcba9876543210.stdout.log").write_text("partial\n")
    # No stderr.log

    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/progress/fedcba9876543210",
                                headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
        assert body["stdout"] == "partial\n"
        assert body["stderr"] == ""


@pytest.mark.asyncio
async def test_progress_tail_caps_long_stdout(claude_worker_module, monkeypatch, tmp_path):
    """Stdout muito longo é truncado pra tail 50KB."""
    progress_dir = tmp_path / ".progress"
    progress_dir.mkdir()
    # 60000 bytes — should be truncated to last 50000
    long_content = "A" * 60_000
    (progress_dir / "1111222233334444.stdout.log").write_text(long_content)

    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/progress/1111222233334444",
                                headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
        assert len(body["stdout"]) == 50_000


# --------------------------------------------------------------------------- #
# Task 13: /v1/dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_rejects_non_anthropic_model(claude_worker_module, monkeypatch):
    """``claude-worker`` só aceita ``preferred_model`` no namespace ``anthropic:*``.

    O CLI ``claude`` não roteia para outros provedores; o pipeline pode até
    enviar slugs de outros providers, mas eles devem ser barrados com 400 +
    mensagem clara para que o operador entenda o motivo do dispatch ter falhado.
    """
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "test",
            "channel_id": "x",
            "preferred_model": "openai:gpt-4",
            "stage": "implement",
        })
        assert resp.status == 400
        body = await resp.json()
        assert "anthropic" in body["error"].lower()


@pytest.mark.asyncio
async def test_dispatch_translates_model_slug(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Slug ``anthropic:claude-opus-4-7`` vira ``--model claude-opus-4-7`` na call.

    O prefixo ``anthropic:`` é convenção interna do DEILE; o CLI ``claude``
    espera só a parte após os dois pontos. Também garante que o invocador
    está passando ``-p`` (modo print) e ``--permission-mode bypassPermissions``.
    """
    captured = {}

    async def fake_run(args, *, cwd, task_id, timeout):
        captured["args"] = list(args)
        return claude_worker_module.SubprocessResult(
            returncode=0, stdout="ok\n", stderr="", duration_seconds=1.0,
        )

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "implement #1",
            "channel_id": "auto/issue-1",
            "preferred_model": "anthropic:claude-opus-4-7",
            "stage": "implement",
            "issue_number": 1,
            "branch": "auto/issue-1",
        })
        assert resp.status == 200

    args = captured["args"]
    assert "claude" in args[0] or args[0] == "claude"
    assert "-p" in args
    assert "--model" in args
    model_idx = args.index("--model")
    assert args[model_idx + 1] == "claude-opus-4-7"
    assert "--permission-mode" in args
    perm_idx = args.index("--permission-mode")
    assert args[perm_idx + 1] == "bypassPermissions"


@pytest.mark.asyncio
async def test_dispatch_response_shape(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Response inclui ``ok``, ``stdout``, ``stderr``, ``task_id``,
    ``session_id``, ``attempt``, ``duration_seconds`` e ``returncode``
    — contrato consumido pelo ``deile-pipeline`` e pelo painel TUI."""
    import json as _json
    async def fake_run(args, *, cwd, task_id, timeout):
        # Com --output-format json, stdout é UM JSON object (resultado final).
        out = _json.dumps({
            "is_error": False, "result": "ok", "session_id": "abc-123",
            "total_cost_usd": 0.05, "duration_ms": 42000, "num_turns": 3,
        })
        return claude_worker_module.SubprocessResult(
            returncode=0, stdout=out, stderr="", duration_seconds=42.0,
        )

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "x", "channel_id": "y",
            "preferred_model": "anthropic:claude-sonnet-4-6",
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert "stdout" in body
        assert "stderr" in body
        assert "task_id" in body
        # ``secrets.token_hex(8)`` produz 16 chars hex.
        assert len(body["task_id"]) == 16
        assert body["duration_seconds"] == 42.0
        assert body["returncode"] == 0
        assert body["session_id"]  # UUID4 gerado
        assert body["attempt"] == 1  # fresh dispatch sempre attempt=1
        assert body["total_cost_usd"] == 0.05
        assert body["num_turns"] == 3


@pytest.mark.asyncio
async def test_dispatch_creates_workspace_dir(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Cada dispatch cria ``DEILE_CLAUDE_WORKER_ROOT/<task_id>/`` fresh.

    O ``claude`` é executado com ``cwd`` apontando para esse diretório, de
    modo que cada brief tem worktree isolado — sem leakage cross-task de
    arquivos/staged changes."""
    captured_cwd = []

    async def fake_run(args, *, cwd, task_id, timeout):
        captured_cwd.append(cwd)
        assert cwd.exists(), f"workspace {cwd} should exist before exec"
        return claude_worker_module.SubprocessResult(0, "", "", 0.1)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "x", "channel_id": "y",
            "preferred_model": "anthropic:claude-haiku-4-5",
        })
        assert resp.status == 200
        body = await resp.json()

        # ``cwd`` deve ser ``tmp_path/<task_id>``.
        assert captured_cwd[0].parent == tmp_path
        assert captured_cwd[0].name == body["task_id"]


@pytest.mark.asyncio
async def test_dispatch_passes_brief_with_preamble(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Brief recebido pelo dispatch vai como sufixo do preamble do stage.

    Verifica três coisas: o marker do brief vai pro prompt, o preamble por
    stage está renderizado (identidade do agente + contrato de output) e o
    ``$BRANCH`` é substituído no template antes do exec."""
    captured_args = []

    async def fake_run(args, *, cwd, task_id, timeout):
        captured_args.extend(args)
        return claude_worker_module.SubprocessResult(0, "", "", 0.1)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "MARKER_BRIEF_TEXT_42",
            "channel_id": "y",
            "preferred_model": "anthropic:claude-haiku-4-5",
            "stage": "implement",
            "branch": "auto/issue-42",
        })

    # O último argumento do CLI ``claude`` é o ``full_prompt`` (preamble + brief).
    full_prompt = captured_args[-1]
    assert "MARKER_BRIEF_TEXT_42" in full_prompt
    # Identidade do agente vinda do preamble.
    assert "Claude Code" in full_prompt or "claude-worker" in full_prompt
    # Substituição de ``$BRANCH`` no template.
    assert "auto/issue-42" in full_prompt
    # Contrato de output presente no preamble.
    assert "STATUS: SUCCESS" in full_prompt


# ============================================================================
# Bug #3 hotfix: Bearer middleware (issue #309 fase 2 hardening)
# Defense-in-depth: NetworkPolicy bloqueia ingress externo, mas auth no
# app-layer impede que pod comprometido dentro do allowlist envie dispatch.
# ============================================================================


async def test_health_endpoint_does_not_require_auth(claude_worker_module,
                                                     monkeypatch):
    """``/v1/health`` está whitelisted no middleware — readiness probe do
    Kubernetes não tem token."""
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        # SEM headers de auth.
        resp = await client.get("/v1/health")
        assert resp.status == 200


async def test_dispatch_rejects_request_without_bearer(claude_worker_module,
                                                       monkeypatch):
    """``/v1/dispatch`` sem ``Authorization`` header → 401 UNAUTHORIZED."""
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", json={"brief": "x"})
        assert resp.status == 401
        body = await resp.json()
        assert body["error"]["code"] == "UNAUTHORIZED"


async def test_dispatch_rejects_request_with_wrong_bearer(claude_worker_module,
                                                          monkeypatch):
    """Token incorreto → 401 (constant-time compare via ``hmac.compare_digest``)."""
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    app = claude_worker_module.build_app(auth_token="real-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers={"Authorization": "Bearer wrong-token"},
            json={"brief": "x"},
        )
        assert resp.status == 401


async def test_progress_rejects_request_without_bearer(claude_worker_module,
                                                       monkeypatch, tmp_path):
    """``/v1/progress/{task_id}`` também exige Bearer (stdout/stderr podem
    conter secrets do brief)."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/progress/0123456789abcdef")
        assert resp.status == 401


async def test_build_app_raises_when_token_missing_and_not_provided(
    claude_worker_module, monkeypatch,
):
    """Sem ``auth_token`` explícito + sem Secret file + sem env var → raise
    no build_app (server abort no startup pra forçar fix).

    Hermético: stuba ``_read_auth_token`` para forçar o RuntimeError
    independente do filesystem do host. Sem isso, falha quando rodado
    dentro de um pod onde o Secret está realmente mountado em
    ``/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN``."""
    monkeypatch.delenv("DEILE_CLAUDE_WORKER_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("DEILE_CLAUDE_WORKER_AUTH_TOKEN_FILE", raising=False)

    def _no_token():
        raise RuntimeError(
            "claude-worker auth token not found: expected "
            "/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN "
            "or DEILE_CLAUDE_WORKER_AUTH_TOKEN env"
        )

    monkeypatch.setattr(claude_worker_module, "_read_auth_token", _no_token)
    with pytest.raises(RuntimeError, match="auth token not found"):
        claude_worker_module.build_app()


async def test_client_max_size_rejects_oversized_payload(claude_worker_module,
                                                         monkeypatch):
    """Body > 512 KiB → 413 Request Entity Too Large (anti-abuse PVC)."""
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    app = claude_worker_module.build_app(auth_token="test-token")
    huge_brief = "x" * (600 * 1024)  # 600 KiB > 512 KiB limit
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch", headers=_AUTH_HEADERS,
            json={"brief": huge_brief},
        )
        # aiohttp 3.x devolve 400 com body "Content-Length X exceeds maximum
        # payload size Y" (não 413 RFC-canônico). Aceita ambos.
        assert resp.status in (400, 413), f"esperado 400 ou 413, got {resp.status}"


# ============================================================================
# WORKER_AUTH_EXPIRED — estratégia C da issue #309 fase 3 (resiliência auth)
# ============================================================================


def test_detect_auth_expired_recognizes_claude_signatures(claude_worker_module):
    """``_detect_auth_expired`` detecta os 6 padrões do claude CLI quando
    token OAuth expirou/foi revogado."""
    detect = claude_worker_module._detect_auth_expired
    # Padrões reais do claude CLI no Linux quando ANTHROPIC_AUTH_TOKEN está
    # ausente, expirado ou inválido.
    assert detect("Not logged in · Please run /login", "")
    assert detect("Failed to authenticate. API Error: 401 Invalid authentication credentials", "")
    assert detect("Please run `claude auth login`", "")
    assert detect("ERROR: 401 unauthorized", "")
    # Case-insensitive: claude pode variar capitalização.
    assert detect("NOT LOGGED IN", "")
    # Stderr também é considerado (não só stdout).
    assert detect("", "401 invalid authentication credentials")


def test_detect_auth_expired_ignores_other_failures(claude_worker_module):
    """Falsos positivos zero: erros genéricos (timeout, fs, network) NÃO
    são auth-expired. Operador não deve confundir.

    Conservador (preferimos false negative a false positive)."""
    detect = claude_worker_module._detect_auth_expired
    assert not detect("Timed out after 1800s", "")
    assert not detect("Permission denied: /home/claude/work", "")
    assert not detect("git clone failed: repository not found", "")
    # 401 SEM o contexto auth (HTTP de outra source) não dispara.
    assert not detect("Some unrelated 401", "")
    # Empty inputs.
    assert not detect("", "")


async def test_dispatch_returns_auth_expired_when_claude_reports_not_logged_in(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Integração: ``claude -p`` produz "Not logged in" → response inclui
    ``error_code=WORKER_AUTH_EXPIRED`` + ``ok=False`` + mensagem clara."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")

    # Mock o subprocess pra emular claude reportando token expirado.
    from infra.k8s import claude_worker_server as mod
    import types

    async def fake_run_subprocess(args, *, cwd, task_id, timeout):
        return types.SimpleNamespace(
            returncode=1,
            stdout="Not logged in · Please run /login\n",
            stderr="",
            duration_seconds=0.5,
        )

    monkeypatch.setattr(
        claude_worker_module, "run_subprocess_with_progress", fake_run_subprocess,
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch", headers=_AUTH_HEADERS,
            json={"brief": "review PR #1", "stage": "pr_review"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is False
        assert body.get("error_code") == "WORKER_AUTH_EXPIRED"
        assert "claude-renew" in body.get("error", "")


async def test_dispatch_returns_ok_when_claude_succeeds_normally(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Sanity: dispatch normal NÃO seta ``error_code`` — só quando há
    detecção real de auth-expired."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")

    import json as _json
    import types

    async def fake_run_subprocess(args, *, cwd, task_id, timeout):
        out = _json.dumps({
            "is_error": False,
            "result": "Review completed STATUS: APPROVE",
            "session_id": "review-session", "total_cost_usd": 0.1,
            "duration_ms": 10000, "num_turns": 5,
        })
        return types.SimpleNamespace(
            returncode=0,
            stdout=out,
            stderr="",
            duration_seconds=10.0,
        )

    monkeypatch.setattr(
        claude_worker_module, "run_subprocess_with_progress", fake_run_subprocess,
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch", headers=_AUTH_HEADERS,
            json={"brief": "review PR #1", "stage": "pr_review"},
        )
        body = await resp.json()
        assert body["ok"] is True
        assert "error_code" not in body


# --------------------------------------------------------------------------- #
# Issue #309 fase 3.5: resume support + session metadata + JSON output parsing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_persists_session_metadata(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Fresh dispatch grava ``~/.claude/tasks/<task_id>/session.json`` com
    session_id, workdir, stage, attempt=1, started_at, last_*."""
    import json as _json

    async def fake_run(args, *, cwd, task_id, timeout):
        out = _json.dumps({
            "is_error": False, "result": "done", "session_id": "fake-sess",
            "total_cost_usd": 0.07, "duration_ms": 5000, "num_turns": 2,
        })
        return claude_worker_module.SubprocessResult(0, out, "", 5.0)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "x", "stage": "pr_review", "branch": "main",
            "preferred_model": "anthropic:claude-sonnet-4-6",
        })
        body = await resp.json()

    meta_path = tmp_path / ".claude" / "tasks" / body["task_id"] / "session.json"
    assert meta_path.exists()
    meta = _json.loads(meta_path.read_text())
    assert meta["task_id"] == body["task_id"]
    assert meta["session_id"] == body["session_id"]
    assert meta["stage"] == "pr_review"
    assert meta["branch"] == "main"
    assert meta["attempt"] == 1
    assert meta["prev_task_id"] is None
    assert meta["last_is_error"] is False
    assert meta["last_returncode"] == 0
    assert meta["last_total_cost_usd"] == 0.07


@pytest.mark.asyncio
async def test_dispatch_passes_session_id_flag_to_claude(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Fresh dispatch passa ``--session-id <uuid>`` e ``--output-format json``
    pro claude CLI. Resume dispatch usa ``-r <session_id>`` em vez."""
    import json as _json
    captured = {}

    async def fake_run(args, *, cwd, task_id, timeout):
        captured["args"] = list(args)
        out = _json.dumps({"is_error": False, "result": "ok", "session_id": "x"})
        return claude_worker_module.SubprocessResult(0, out, "", 1.0)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "x", "preferred_model": "anthropic:claude-haiku-4-5",
        })
        body = await resp.json()

    args = captured["args"]
    assert "--session-id" in args
    sid_idx = args.index("--session-id")
    # session_id passado deve bater com o que voltou no response.
    assert args[sid_idx + 1] == body["session_id"]
    assert "--output-format" in args
    fmt_idx = args.index("--output-format")
    assert args[fmt_idx + 1] == "json"
    # fresh dispatch NÃO usa -r.
    assert "-r" not in args


@pytest.mark.asyncio
async def test_dispatch_resume_uses_minus_r_flag(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Resume dispatch lê metadata do prev_task_id, reutiliza workdir,
    spawna com ``-r <session_id>`` em vez de ``--session-id``."""
    import json as _json
    captured = {}

    async def fake_run(args, *, cwd, task_id, timeout):
        captured["args"] = list(args)
        captured["cwd"] = cwd
        captured["task_id"] = task_id
        out = _json.dumps({"is_error": False, "result": "resumed ok",
                           "session_id": "the-session"})
        return claude_worker_module.SubprocessResult(0, out, "", 2.0)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    # Setup: cria metadata + workdir de um dispatch fictício prévio.
    prev_task_id = "abcdef0123456789"
    workdir = tmp_path / "work" / prev_task_id
    workdir.mkdir(parents=True)
    meta_dir = tmp_path / ".claude" / "tasks" / prev_task_id
    meta_dir.mkdir(parents=True)
    (meta_dir / "session.json").write_text(_json.dumps({
        "task_id": prev_task_id, "session_id": "the-session",
        "workdir": str(workdir), "stage": "pr_review", "branch": "auto/test",
        "attempt": 1, "started_at": 1000, "last_is_error": False,
    }))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "x", "preferred_model": "anthropic:claude-sonnet-4-6",
            "resume_session_id": "the-session",
            "prev_task_id": prev_task_id,
        })
        assert resp.status == 200
        body = await resp.json()

    args = captured["args"]
    # Resume usa -r não --session-id.
    assert "-r" in args
    r_idx = args.index("-r")
    assert args[r_idx + 1] == "the-session"
    assert "--session-id" not in args
    # Task_id é o mesmo do prev (reutiliza pra acumular tentativas).
    assert body["task_id"] == prev_task_id
    assert body["session_id"] == "the-session"
    assert body["attempt"] == 2  # incrementado
    # cwd é o workdir original.
    assert captured["cwd"] == workdir


@pytest.mark.asyncio
async def test_dispatch_resume_rejects_invalid_prev_task_id(
    claude_worker_module, monkeypatch, tmp_path,
):
    """prev_task_id com formato inválido (não hex 16-char) → 400."""
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "x",
            "resume_session_id": "session-id-here",
            "prev_task_id": "../etc/passwd",  # path traversal attempt
        })
        assert resp.status == 400
        body = await resp.json()
        assert "prev_task_id" in body["error"].lower()


@pytest.mark.asyncio
async def test_dispatch_resume_404_when_meta_missing(
    claude_worker_module, monkeypatch, tmp_path,
):
    """prev_task_id formato OK mas sem session.json no PVC (pod recreated)
    → 404 com error_code RESUME_META_MISSING."""
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "x",
            "resume_session_id": "sid",
            "prev_task_id": "0123456789abcdef",  # válido formato, não existe
        })
        assert resp.status == 404
        body = await resp.json()
        assert body["error_code"] == "RESUME_META_MISSING"


@pytest.mark.asyncio
async def test_dispatch_resume_410_when_workdir_lost(
    claude_worker_module, monkeypatch, tmp_path,
):
    """prev_task_id OK + meta OK MAS workdir não existe → 410 Gone."""
    import json as _json
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    prev_task_id = "abcdef0123456789"
    meta_dir = tmp_path / ".claude" / "tasks" / prev_task_id
    meta_dir.mkdir(parents=True)
    (meta_dir / "session.json").write_text(_json.dumps({
        "task_id": prev_task_id, "session_id": "sid",
        "workdir": str(tmp_path / "nonexistent"),
        "attempt": 1, "started_at": 1000,
    }))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "x",
            "resume_session_id": "sid",
            "prev_task_id": prev_task_id,
        })
        assert resp.status == 410
        body = await resp.json()
        assert body["error_code"] == "RESUME_WORKDIR_LOST"


@pytest.mark.asyncio
async def test_dispatch_resume_409_when_session_mismatch(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Meta diz session=X, payload pede resume_session=Y → 409 Conflict
    (corrupção do mini-ledger ou IDs trocados)."""
    import json as _json
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    prev_task_id = "abcdef0123456789"
    workdir = tmp_path / "work" / prev_task_id
    workdir.mkdir(parents=True)
    meta_dir = tmp_path / ".claude" / "tasks" / prev_task_id
    meta_dir.mkdir(parents=True)
    (meta_dir / "session.json").write_text(_json.dumps({
        "task_id": prev_task_id, "session_id": "real-session-X",
        "workdir": str(workdir), "attempt": 1, "started_at": 1000,
    }))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "x",
            "resume_session_id": "different-session-Y",
            "prev_task_id": prev_task_id,
        })
        assert resp.status == 409
        body = await resp.json()
        assert body["error_code"] == "RESUME_SESSION_MISMATCH"


@pytest.mark.asyncio
async def test_dispatch_detects_auth_expired_via_json_output(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Bug Opus: claude rc=0 + JSON output ``is_error=true`` + result
    'Not logged in' → ok=False, error_code=WORKER_AUTH_EXPIRED.
    """
    import json as _json
    async def fake_run(args, *, cwd, task_id, timeout):
        out = _json.dumps({
            "is_error": True,
            "result": "Not logged in · Please run /login",
            "session_id": "x", "total_cost_usd": 0, "duration_ms": 50,
            "num_turns": 1,
        })
        return claude_worker_module.SubprocessResult(0, out, "", 0.05)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", headers=_AUTH_HEADERS, json={
            "brief": "x", "preferred_model": "anthropic:claude-haiku-4-5",
        })
        body = await resp.json()

    assert body["ok"] is False
    assert body["error_code"] == "WORKER_AUTH_EXPIRED"
    assert body["returncode"] == 0  # claude saiu OK, mas funcionalmente falhou


# --------------------------------------------------------------------------- #
# /v1/dispatches/{task_id}/resume-info
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resume_info_returns_404_for_unknown_task(
    claude_worker_module, monkeypatch, tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/dispatches/0123456789abcdef/resume-info",
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 404


@pytest.mark.asyncio
async def test_resume_info_returns_400_for_invalid_task_id(
    claude_worker_module, monkeypatch, tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/dispatches/garbage/resume-info",
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_resume_info_returns_full_meta(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Endpoint retorna todos campos pro pipeline decidir resume vs fresh."""
    import json as _json
    monkeypatch.setenv("HOME", str(tmp_path))

    task_id = "abcdef0123456789"
    workdir = tmp_path / "work" / task_id
    workdir.mkdir(parents=True)
    meta_dir = tmp_path / ".claude" / "tasks" / task_id
    meta_dir.mkdir(parents=True)
    (meta_dir / "session.json").write_text(_json.dumps({
        "task_id": task_id, "session_id": "sess-uuid",
        "workdir": str(workdir), "stage": "pr_review",
        "branch": "auto/issue-99", "model": "claude-sonnet-4-6",
        "started_at": 1716000000, "last_completed_at": 1716000420,
        "last_is_error": False, "last_result_summary": "Review postada e aprovada.",
        "last_returncode": 0, "last_duration_seconds": 420.5,
        "last_total_cost_usd": 0.137, "attempt": 2,
        "prev_task_id": "fedcba9876543210",
    }))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            f"/v1/dispatches/{task_id}/resume-info",
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()

    assert body["task_id"] == task_id
    assert body["session_id"] == "sess-uuid"
    assert body["workdir"] == str(workdir)
    assert body["workdir_exists"] is True
    assert body["stage"] == "pr_review"
    assert body["branch"] == "auto/issue-99"
    assert body["model"] == "claude-sonnet-4-6"
    assert body["last_is_error"] is False
    assert body["last_result_summary"] == "Review postada e aprovada."
    assert body["last_returncode"] == 0
    assert body["last_duration_seconds"] == 420.5
    assert body["last_total_cost_usd"] == 0.137
    assert body["attempt"] == 2
    assert body["prev_task_id"] == "fedcba9876543210"
    assert "claude_alive" in body  # heuristic — pode ser True ou False


@pytest.mark.asyncio
async def test_resume_info_detects_workdir_lost(
    claude_worker_module, monkeypatch, tmp_path,
):
    """Se workdir foi GC'd / pod recriado, ``workdir_exists=False``."""
    import json as _json
    monkeypatch.setenv("HOME", str(tmp_path))

    task_id = "1111aaaa2222bbbb"
    meta_dir = tmp_path / ".claude" / "tasks" / task_id
    meta_dir.mkdir(parents=True)
    (meta_dir / "session.json").write_text(_json.dumps({
        "task_id": task_id, "session_id": "s",
        "workdir": "/nonexistent/path",  # workdir não existe
        "attempt": 1, "started_at": 100,
    }))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            f"/v1/dispatches/{task_id}/resume-info",
            headers=_AUTH_HEADERS,
        )
        body = await resp.json()

    assert body["workdir_exists"] is False


# --------------------------------------------------------------------------- #
# _parse_claude_json_output edge cases
# --------------------------------------------------------------------------- #


def test_parse_claude_json_output_handles_empty_stdout(claude_worker_module):
    """Stdout vazio (claude foi killed antes do print JSON final) → defaults
    seguros com is_error=True."""
    result = claude_worker_module._parse_claude_json_output("")
    assert result["is_error"] is True
    assert result["result"] == ""
    assert result["session_id"] == ""


def test_parse_claude_json_output_handles_garbage(claude_worker_module):
    """Stdout não-JSON (corruption / crash) → defaults seguros."""
    result = claude_worker_module._parse_claude_json_output("just some text\nnot json\n")
    assert result["is_error"] is True


def test_parse_claude_json_output_extracts_from_last_line(claude_worker_module):
    """Stdout com logs antes do JSON final — pega a última linha JSON válida."""
    stdout = (
        "loading...\n"
        "starting session...\n"
        '{"type":"result","is_error":false,"result":"done",'
        '"session_id":"abc","total_cost_usd":0.1,"duration_ms":100,"num_turns":2}\n'
    )
    result = claude_worker_module._parse_claude_json_output(stdout)
    assert result["is_error"] is False
    assert result["result"] == "done"
    assert result["session_id"] == "abc"
    assert result["total_cost_usd"] == 0.1


def test_parse_claude_json_output_extracts_full_json(claude_worker_module):
    """Caminho comum: stdout é apenas o JSON object."""
    stdout = (
        '{"is_error":false,"result":"hello",'
        '"session_id":"xyz","total_cost_usd":0.5,"duration_ms":1000,"num_turns":3}'
    )
    result = claude_worker_module._parse_claude_json_output(stdout)
    assert result["is_error"] is False
    assert result["result"] == "hello"
    assert result["num_turns"] == 3


# --------------------------------------------------------------------------- #
# _is_claude_process_alive
# --------------------------------------------------------------------------- #


def test_is_claude_process_alive_returns_false_for_empty_session(claude_worker_module):
    """Empty session_id sempre é False — não vazar match acidental."""
    assert claude_worker_module._is_claude_process_alive("") is False


def test_is_claude_process_alive_when_pgrep_missing(claude_worker_module, monkeypatch):
    """Se pgrep não está no PATH, retorna False (best-effort, não crasha)."""
    import subprocess as _sub
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("pgrep not found")
    monkeypatch.setattr(_sub, "run", fake_run)
    assert claude_worker_module._is_claude_process_alive("session-id") is False
