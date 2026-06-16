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
    k8s_dir = str(repo_root / "infra" / "k8s")
    # Ensure sibling modules (e.g. dispatch_logger) are importable.
    if k8s_dir not in sys.path:
        sys.path.insert(0, k8s_dir)
    server_path = repo_root / "infra" / "k8s" / "claude_worker_server.py"
    spec = importlib.util.spec_from_file_location(
        "claude_worker_server_under_test",
        str(server_path),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claude_worker_server_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_health_returns_200_when_binary_present(
    claude_worker_module,
    monkeypatch,
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
    claude_worker_module,
    monkeypatch,
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
async def test_progress_returns_404_for_unknown_task(
    claude_worker_module, monkeypatch, tmp_path
):
    """task_id válido mas progress file não existe → 404."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        # 16-char hex valid format mas inexistente
        resp = await client.get("/v1/progress/0123456789abcdef", headers=_AUTH_HEADERS)
        assert resp.status == 404


@pytest.mark.asyncio
async def test_progress_returns_400_for_invalid_task_id(
    claude_worker_module, monkeypatch, tmp_path
):
    """task_id formato inválido (não-hex 16-char) → 400."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        # Not hex
        resp = await client.get("/v1/progress/garbage-no-hex", headers=_AUTH_HEADERS)
        assert resp.status == 400

        # Wrong length
        resp = await client.get("/v1/progress/abc123", headers=_AUTH_HEADERS)
        assert resp.status == 400


@pytest.mark.asyncio
async def test_progress_returns_tails(claude_worker_module, monkeypatch, tmp_path):
    """Quando progress files existem, devolve tail dos últimos N bytes."""
    progress_dir = tmp_path / ".progress"
    progress_dir.mkdir()
    (progress_dir / "abcdef0123456789.stdout.log").write_text(
        "line 1\nline 2\nline 3\n"
    )
    (progress_dir / "abcdef0123456789.stderr.log").write_text("err A\nerr B\n")

    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/progress/abcdef0123456789", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
        assert "stdout" in body
        assert "stderr" in body
        assert "line 3" in body["stdout"]
        assert "err B" in body["stderr"]
        assert body["task_id"] == "abcdef0123456789"


@pytest.mark.asyncio
async def test_progress_handles_only_stdout_present(
    claude_worker_module, monkeypatch, tmp_path
):
    """Se só stdout file existe (stderr vazio), ainda retorna 200."""
    progress_dir = tmp_path / ".progress"
    progress_dir.mkdir()
    (progress_dir / "fedcba9876543210.stdout.log").write_text("partial\n")
    # No stderr.log

    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/progress/fedcba9876543210", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
        assert body["stdout"] == "partial\n"
        assert body["stderr"] == ""


@pytest.mark.asyncio
async def test_progress_tail_caps_long_stdout(
    claude_worker_module, monkeypatch, tmp_path
):
    """Stdout muito longo é truncado pra tail 50KB."""
    progress_dir = tmp_path / ".progress"
    progress_dir.mkdir()
    # 60000 bytes — should be truncated to last 50000
    long_content = "A" * 60_000
    (progress_dir / "1111222233334444.stdout.log").write_text(long_content)

    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/progress/1111222233334444", headers=_AUTH_HEADERS)
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
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "test",
                "channel_id": "x",
                "preferred_model": "openai:gpt-4",
                "stage": "implement",
            },
        )
        assert resp.status == 400
        body = await resp.json()
        assert "anthropic" in body["error"].lower()


@pytest.mark.asyncio
async def test_dispatch_translates_model_slug(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Slug ``anthropic:claude-opus-4-8`` vira ``--model claude-opus-4-8`` na call.

    O prefixo ``anthropic:`` é convenção interna do DEILE; o CLI ``claude``
    espera só a parte após os dois pontos. Também garante que o invocador
    está passando ``-p`` (modo print) e ``--permission-mode bypassPermissions``.
    """
    captured = {}

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        captured["args"] = list(args)
        return claude_worker_module.SubprocessResult(
            returncode=0,
            stdout="ok\n",
            stderr="",
            duration_seconds=1.0,
        )

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "implement #1",
                "channel_id": "auto/issue-1",
                "preferred_model": "anthropic:claude-opus-4-8",
                "stage": "implement",
                "issue_number": 1,
                "branch": "auto/issue-1",
            },
        )
        assert resp.status == 200

    args = captured["args"]
    assert "claude" in args[0] or args[0] == "claude"
    assert "-p" in args
    assert "--model" in args
    model_idx = args.index("--model")
    assert args[model_idx + 1] == "claude-opus-4-8"
    assert "--permission-mode" in args
    perm_idx = args.index("--permission-mode")
    assert args[perm_idx + 1] == "bypassPermissions"


@pytest.mark.asyncio
async def test_dispatch_response_shape(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Response inclui ``ok``, ``stdout``, ``stderr``, ``task_id``,
    ``session_id``, ``attempt``, ``duration_seconds`` e ``returncode``
    — contrato consumido pelo ``deile-pipeline`` e pelo painel TUI."""
    import json as _json

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        # Com --output-format json, stdout é UM JSON object (resultado final).
        out = _json.dumps(
            {
                "is_error": False,
                "result": "ok",
                "session_id": "abc-123",
                "total_cost_usd": 0.05,
                "duration_ms": 42000,
                "num_turns": 3,
            }
        )
        return claude_worker_module.SubprocessResult(
            returncode=0,
            stdout=out,
            stderr="",
            duration_seconds=42.0,
        )

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "channel_id": "y",
                "preferred_model": "anthropic:claude-sonnet-4-6",
            },
        )
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
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Cada dispatch cria ``DEILE_CLAUDE_WORKER_ROOT/<task_id>/`` fresh.

    O ``claude`` é executado com ``cwd`` apontando para esse diretório, de
    modo que cada brief tem worktree isolado — sem leakage cross-task de
    arquivos/staged changes."""
    captured_cwd = []

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        captured_cwd.append(cwd)
        assert cwd.exists(), f"workspace {cwd} should exist before exec"
        return claude_worker_module.SubprocessResult(0, "", "", 0.1)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "channel_id": "y",
                "preferred_model": "anthropic:claude-haiku-4-5",
            },
        )
        assert resp.status == 200
        body = await resp.json()

        # ``cwd`` deve ser ``tmp_path/<task_id>``.
        assert captured_cwd[0].parent == tmp_path
        assert captured_cwd[0].name == body["task_id"]


@pytest.mark.asyncio
async def test_dispatch_passes_brief_with_preamble(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Brief recebido pelo dispatch vai como sufixo do preamble do stage.

    Verifica três coisas: o marker do brief vai pro prompt, o preamble por
    stage está renderizado (identidade do agente + contrato de output) e o
    ``$BRANCH`` é substituído no template antes do exec."""
    captured_args = []

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        captured_args.extend(args)
        return claude_worker_module.SubprocessResult(0, "", "", 0.1)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "MARKER_BRIEF_TEXT_42",
                "channel_id": "y",
                "preferred_model": "anthropic:claude-haiku-4-5",
                "stage": "implement",
                "branch": "auto/issue-42",
            },
        )

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


async def test_health_endpoint_does_not_require_auth(claude_worker_module, monkeypatch):
    """``/v1/health`` está whitelisted no middleware — readiness probe do
    Kubernetes não tem token."""
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        # SEM headers de auth.
        resp = await client.get("/v1/health")
        assert resp.status == 200


async def test_dispatch_rejects_request_without_bearer(
    claude_worker_module, monkeypatch
):
    """``/v1/dispatch`` sem ``Authorization`` header → 401 UNAUTHORIZED."""
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", json={"brief": "x"})
        assert resp.status == 401
        body = await resp.json()
        assert body["error"]["code"] == "UNAUTHORIZED"


async def test_dispatch_rejects_request_with_wrong_bearer(
    claude_worker_module, monkeypatch
):
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


async def test_progress_rejects_request_without_bearer(
    claude_worker_module, monkeypatch, tmp_path
):
    """``/v1/progress/{task_id}`` também exige Bearer (stdout/stderr podem
    conter secrets do brief)."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/progress/0123456789abcdef")
        assert resp.status == 401


async def test_build_app_raises_when_token_missing_and_not_provided(
    claude_worker_module,
    monkeypatch,
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


async def test_client_max_size_rejects_oversized_payload(
    claude_worker_module, monkeypatch
):
    """Body > 512 KiB → 413 Request Entity Too Large (anti-abuse PVC)."""
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    app = claude_worker_module.build_app(auth_token="test-token")
    huge_brief = "x" * (600 * 1024)  # 600 KiB > 512 KiB limit
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
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
    assert detect(
        "Failed to authenticate. API Error: 401 Invalid authentication credentials", ""
    )
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
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Integração: ``claude -p`` produz "Not logged in" → response inclui
    ``error_code=WORKER_AUTH_EXPIRED`` + ``ok=False`` + mensagem clara."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")

    # Mock o subprocess pra emular claude reportando token expirado.
    import types

    async def fake_run_subprocess(args, *, cwd, task_id, timeout, lease_path=None):
        return types.SimpleNamespace(
            returncode=1,
            stdout="Not logged in · Please run /login\n",
            stderr="",
            duration_seconds=0.5,
        )

    monkeypatch.setattr(
        claude_worker_module,
        "run_subprocess_with_progress",
        fake_run_subprocess,
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={"brief": "review PR #1", "stage": "pr_review"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is False
        assert body.get("error_code") == "WORKER_AUTH_EXPIRED"
        assert "claude-renew" in body.get("error", "")


async def test_dispatch_returns_ok_when_claude_succeeds_normally(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Sanity: dispatch normal NÃO seta ``error_code`` — só quando há
    detecção real de auth-expired."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")

    import json as _json
    import types

    async def fake_run_subprocess(args, *, cwd, task_id, timeout, lease_path=None):
        out = _json.dumps(
            {
                "is_error": False,
                "result": "Review completed STATUS: APPROVE",
                "session_id": "review-session",
                "total_cost_usd": 0.1,
                "duration_ms": 10000,
                "num_turns": 5,
            }
        )
        return types.SimpleNamespace(
            returncode=0,
            stdout=out,
            stderr="",
            duration_seconds=10.0,
        )

    monkeypatch.setattr(
        claude_worker_module,
        "run_subprocess_with_progress",
        fake_run_subprocess,
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
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
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Fresh dispatch grava ``~/.claude/tasks/<task_id>/session.json`` com
    session_id, workdir, stage, attempt=1, started_at, last_*."""
    import json as _json

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        out = _json.dumps(
            {
                "is_error": False,
                "result": "done",
                "session_id": "fake-sess",
                "total_cost_usd": 0.07,
                "duration_ms": 5000,
                "num_turns": 2,
            }
        )
        return claude_worker_module.SubprocessResult(0, out, "", 5.0)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "stage": "pr_review",
                "branch": "main",
                "preferred_model": "anthropic:claude-sonnet-4-6",
            },
        )
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
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Fresh dispatch passa ``--session-id <uuid>`` e ``--output-format json``
    pro claude CLI. Resume dispatch usa ``-r <session_id>`` em vez."""
    import json as _json

    captured = {}

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        captured["args"] = list(args)
        out = _json.dumps({"is_error": False, "result": "ok", "session_id": "x"})
        return claude_worker_module.SubprocessResult(0, out, "", 1.0)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "preferred_model": "anthropic:claude-haiku-4-5",
            },
        )
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
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Resume dispatch lê metadata do prev_task_id, reutiliza workdir,
    spawna com ``-r <session_id>`` em vez de ``--session-id``."""
    import json as _json

    captured = {}

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        captured["args"] = list(args)
        captured["cwd"] = cwd
        captured["task_id"] = task_id
        out = _json.dumps(
            {"is_error": False, "result": "resumed ok", "session_id": "the-session"}
        )
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
    (meta_dir / "session.json").write_text(
        _json.dumps(
            {
                "task_id": prev_task_id,
                "session_id": "the-session",
                "workdir": str(workdir),
                "stage": "pr_review",
                "branch": "auto/test",
                "attempt": 1,
                "started_at": 1000,
                "last_is_error": False,
            }
        )
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "preferred_model": "anthropic:claude-sonnet-4-6",
                "resume_session_id": "the-session",
                "prev_task_id": prev_task_id,
            },
        )
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
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """prev_task_id com formato inválido (não hex 16-char) → 400."""
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "resume_session_id": "session-id-here",
                "prev_task_id": "../etc/passwd",  # path traversal attempt
            },
        )
        assert resp.status == 400
        body = await resp.json()
        assert "prev_task_id" in body["error"].lower()


@pytest.mark.asyncio
async def test_dispatch_resume_404_when_meta_missing(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """prev_task_id formato OK mas sem session.json no PVC (pod recreated)
    → 404 com error_code RESUME_META_MISSING."""
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "resume_session_id": "sid",
                "prev_task_id": "0123456789abcdef",  # válido formato, não existe
            },
        )
        assert resp.status == 404
        body = await resp.json()
        assert body["error_code"] == "RESUME_META_MISSING"


@pytest.mark.asyncio
async def test_dispatch_resume_410_when_workdir_lost(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """prev_task_id OK + meta OK MAS workdir não existe → 410 Gone."""
    import json as _json

    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    prev_task_id = "abcdef0123456789"
    meta_dir = tmp_path / ".claude" / "tasks" / prev_task_id
    meta_dir.mkdir(parents=True)
    (meta_dir / "session.json").write_text(
        _json.dumps(
            {
                "task_id": prev_task_id,
                "session_id": "sid",
                "workdir": str(tmp_path / "nonexistent"),
                "attempt": 1,
                "started_at": 1000,
            }
        )
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "resume_session_id": "sid",
                "prev_task_id": prev_task_id,
            },
        )
        assert resp.status == 410
        body = await resp.json()
        assert body["error_code"] == "RESUME_WORKDIR_LOST"


@pytest.mark.asyncio
async def test_dispatch_resume_409_when_session_mismatch(
    claude_worker_module,
    monkeypatch,
    tmp_path,
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
    (meta_dir / "session.json").write_text(
        _json.dumps(
            {
                "task_id": prev_task_id,
                "session_id": "real-session-X",
                "workdir": str(workdir),
                "attempt": 1,
                "started_at": 1000,
            }
        )
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "resume_session_id": "different-session-Y",
                "prev_task_id": prev_task_id,
            },
        )
        assert resp.status == 409
        body = await resp.json()
        assert body["error_code"] == "RESUME_SESSION_MISMATCH"


@pytest.mark.asyncio
async def test_dispatch_detects_auth_expired_via_json_output(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Bug Opus: claude rc=0 + JSON output ``is_error=true`` + result
    'Not logged in' → ok=False, error_code=WORKER_AUTH_EXPIRED.
    """
    import json as _json

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        out = _json.dumps(
            {
                "is_error": True,
                "result": "Not logged in · Please run /login",
                "session_id": "x",
                "total_cost_usd": 0,
                "duration_ms": 50,
                "num_turns": 1,
            }
        )
        return claude_worker_module.SubprocessResult(0, out, "", 0.05)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "preferred_model": "anthropic:claude-haiku-4-5",
            },
        )
        body = await resp.json()

    assert body["ok"] is False
    assert body["error_code"] == "WORKER_AUTH_EXPIRED"
    assert body["returncode"] == 0  # claude saiu OK, mas funcionalmente falhou


# --------------------------------------------------------------------------- #
# /v1/dispatches/{task_id}/resume-info
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resume_info_returns_404_for_unknown_task(
    claude_worker_module,
    monkeypatch,
    tmp_path,
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
    claude_worker_module,
    monkeypatch,
    tmp_path,
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
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Endpoint retorna todos campos pro pipeline decidir resume vs fresh."""
    import json as _json

    monkeypatch.setenv("HOME", str(tmp_path))

    task_id = "abcdef0123456789"
    workdir = tmp_path / "work" / task_id
    workdir.mkdir(parents=True)
    meta_dir = tmp_path / ".claude" / "tasks" / task_id
    meta_dir.mkdir(parents=True)
    (meta_dir / "session.json").write_text(
        _json.dumps(
            {
                "task_id": task_id,
                "session_id": "sess-uuid",
                "workdir": str(workdir),
                "stage": "pr_review",
                "branch": "auto/issue-99",
                "model": "claude-sonnet-4-6",
                "started_at": 1716000000,
                "last_completed_at": 1716000420,
                "last_is_error": False,
                "last_result_summary": "Review postada e aprovada.",
                "last_returncode": 0,
                "last_duration_seconds": 420.5,
                "last_total_cost_usd": 0.137,
                "attempt": 2,
                "prev_task_id": "fedcba9876543210",
            }
        )
    )

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
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Se workdir foi GC'd / pod recriado, ``workdir_exists=False``."""
    import json as _json

    monkeypatch.setenv("HOME", str(tmp_path))

    task_id = "1111aaaa2222bbbb"
    meta_dir = tmp_path / ".claude" / "tasks" / task_id
    meta_dir.mkdir(parents=True)
    (meta_dir / "session.json").write_text(
        _json.dumps(
            {
                "task_id": task_id,
                "session_id": "s",
                "workdir": "/nonexistent/path",  # workdir não existe
                "attempt": 1,
                "started_at": 100,
            }
        )
    )

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
    result = claude_worker_module._parse_claude_json_output(
        "just some text\nnot json\n"
    )
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


def test_is_claude_process_alive_when_proc_missing(
    claude_worker_module, monkeypatch, tmp_path
):
    """Se /proc não está acessível (ex: macOS local), retorna False
    best-effort sem crashar."""
    nonexistent = tmp_path / "no-such-dir"
    monkeypatch.setattr(claude_worker_module, "_PROC_ROOT", str(nonexistent))
    assert claude_worker_module._is_claude_process_alive("session-id") is False


def test_is_claude_process_alive_finds_match(
    claude_worker_module, monkeypatch, tmp_path
):
    """Detecta session-id na cmdline de algum /proc/<pid>/."""
    proc_root = tmp_path / "fake_proc"
    proc_root.mkdir()
    pid1 = proc_root / "12345"
    pid1.mkdir()
    (pid1 / "cmdline").write_bytes(
        b"claude\x00-p\x00--session-id\x00the-target-session\x00"
    )
    pid2 = proc_root / "67890"
    pid2.mkdir()
    (pid2 / "cmdline").write_bytes(b"bash\x00-c\x00echo\x00hi\x00")
    (proc_root / "self").mkdir()  # ignored (não-numeric)

    monkeypatch.setattr(claude_worker_module, "_PROC_ROOT", str(proc_root))
    assert claude_worker_module._is_claude_process_alive("the-target-session") is True
    assert claude_worker_module._is_claude_process_alive("not-found-session") is False


# Regression — triple-dispatch bug 2026-05-27:
# Com 3 réplicas de claude-worker + Service round-robin, ``_is_claude_process_alive``
# scaneava só o /proc local do pod que recebia a request. Quando claude rodava
# em outra réplica, retornava False enganosamente → pipeline disparava RESUME
# pensando que estava morto → triple-dispatch de Opus 4.8 paralelos.
# Fix: fallback pra mtime do JSONL na PVC compartilhada.
def test_is_claude_alive_falls_back_to_jsonl_mtime_when_proc_does_not_see_it(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Multi-replica safe: claude vivo em OUTRA réplica = JSONL recém-modificado."""
    # /proc local vazio (não enxerga claude que está em outro pod).
    empty_proc = tmp_path / "fake_proc_empty"
    empty_proc.mkdir()
    monkeypatch.setattr(claude_worker_module, "_PROC_ROOT", str(empty_proc))

    # PVC compartilhada: JSONL existe e foi modificado AGORA (claude appendou
    # via outra réplica).
    home = tmp_path / "home"
    project_dir = home / ".claude" / "projects" / "-home-claude-work-abc123"
    project_dir.mkdir(parents=True)
    sid = "multi-pod-session-xyz"
    jsonl = project_dir / f"{sid}.jsonl"
    jsonl.write_text('{"message":{"model":"claude-sonnet-4-6"}}\n')
    monkeypatch.setenv("HOME", str(home))

    # Resultado: True via fallback JSONL (mesmo com /proc vazio).
    assert claude_worker_module._is_claude_process_alive(sid) is True


def test_is_claude_alive_returns_false_when_jsonl_stale(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """JSONL antigo (> threshold) = sessão morta, fallback retorna False."""
    import os
    import time

    empty_proc = tmp_path / "fake_proc_empty"
    empty_proc.mkdir()
    monkeypatch.setattr(claude_worker_module, "_PROC_ROOT", str(empty_proc))

    home = tmp_path / "home"
    project_dir = home / ".claude" / "projects" / "-home-claude-work-abc"
    project_dir.mkdir(parents=True)
    sid = "stale-session"
    jsonl = project_dir / f"{sid}.jsonl"
    jsonl.write_text("{}\n")
    # Backdate mtime para 5 minutos atrás (bem além do threshold de 60s).
    stale_time = time.time() - 300
    os.utime(jsonl, (stale_time, stale_time))
    monkeypatch.setenv("HOME", str(home))

    assert claude_worker_module._is_session_jsonl_recently_active(sid) is False
    assert claude_worker_module._is_claude_process_alive(sid) is False


def test_is_session_jsonl_recently_active_empty_session_returns_false(
    claude_worker_module,
):
    """Guard explícito: session_id vazio nunca casa."""
    assert claude_worker_module._is_session_jsonl_recently_active("") is False


# --------------------------------------------------------------------------- #
# Observability endpoints (issue #347)
# --------------------------------------------------------------------------- #


import json as _json  # noqa: E402  (used by the section below)


def _write_session_meta(home: Path, task_id: str, meta: dict) -> Path:
    """Helper: write ``~/.claude/tasks/<task_id>/session.json`` for tests."""
    target = home / ".claude" / "tasks" / task_id / "session.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_json.dumps(meta))
    return target


async def test_sessions_list_returns_summary(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """``GET /v1/sessions`` returns one row per valid session.json file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_session_meta(
        tmp_path,
        "0123456789abcdef",
        {
            "task_id": "0123456789abcdef",
            "session_id": "sess-1",
            "stage": "implement",
            "branch": "auto/issue-1",
            "model": "claude-sonnet-4-6",
            "started_at": 1716830000,
            "last_completed_at": 1716830420,
            "last_is_error": False,
            "last_returncode": 0,
            "last_total_cost_usd": 0.13,
            "attempt": 1,
            "workdir": str(tmp_path / "work" / "0123456789abcdef"),
        },
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/sessions", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()

    assert isinstance(body["sessions"], list)
    assert len(body["sessions"]) == 1
    row = body["sessions"][0]
    assert row["task_id"] == "0123456789abcdef"
    assert row["session_id"] == "sess-1"
    assert row["stage"] == "implement"
    assert row["last_total_cost_usd"] == 0.13
    # workdir does not exist => workdir_exists false.
    assert row["workdir_exists"] is False


async def test_sessions_list_filters_orphan_dirs(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Directories without a valid session.json (or with invalid task_id
    names) must be skipped silently — no 500, no garbage rows."""
    monkeypatch.setenv("HOME", str(tmp_path))
    base = tmp_path / ".claude" / "tasks"
    base.mkdir(parents=True)
    # valid session
    _write_session_meta(
        tmp_path,
        "abcdef0123456789",
        {
            "task_id": "abcdef0123456789",
            "session_id": "sx",
            "attempt": 1,
        },
    )
    # orphan dir without session.json (valid hex name)
    (base / "fedcba9876543210").mkdir()
    # invalid name (not hex 16)
    (base / "not-a-task-id").mkdir()
    (base / "not-a-task-id" / "session.json").write_text("{}")

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/sessions", headers=_AUTH_HEADERS)
        body = await resp.json()

    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["task_id"] == "abcdef0123456789"


async def test_session_command_redacts_secrets(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """``GET /v1/sessions/{id}/command`` redacts secrets in env AND in cmd argv.

    Issue #709: cmd[-1] == full_prompt, so secrets in the prompt must not leak
    via the ``cmd`` field in the default (non-admin) response path.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_42")
    monkeypatch.setenv("BENIGN_FLAG", "true")
    # A well-formed GitHub PAT (ghp_ + 36 alphanumeric chars) that SecretsScanner
    # recognises at ≥0.95 confidence and will redact.
    secret_token = "ghp_" + "A" * 36
    full_prompt = f"implement the feature using token {secret_token}"
    _write_session_meta(
        tmp_path,
        "deadbeefcafebabe",
        {
            "task_id": "deadbeefcafebabe",
            "session_id": "sx",
            "command": ["claude", "-p", "--session-id", "sx", full_prompt],
            "full_prompt": full_prompt,
            "subprocess_pid": 1234,
        },
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/sessions/deadbeefcafebabe/command",
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()

    # Benign argv elements are preserved.
    assert body["cmd"][0] == "claude"
    assert body["cmd"][1] == "-p"
    # Secret token must NOT appear verbatim in cmd[-1] (issue #709).
    assert secret_token not in body["cmd"][-1]
    # full_prompt field must also be redacted on the default path.
    assert secret_token not in body["full_prompt"]
    # Environment secrets are redacted.
    env = body["env_redacted"]
    assert env["ANTHROPIC_API_KEY"] == "***"
    assert env["GITHUB_TOKEN"] == "***"
    assert env["BENIGN_FLAG"] == "true"


async def test_session_command_raw_exposes_unredacted_cmd(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """``?raw=true`` with admin bearer returns unredacted cmd and full_prompt.

    The raw path is gated by the admin bearer token (via
    ``DEILE_CLAUDE_WORKER_ADMIN_AUTH_TOKEN``) plus ``X-Deile-Actor`` header.
    In this test the admin token equals the worker bearer so both the
    middleware check and the admin gate are satisfied with the same credential.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Set the admin token equal to the worker bearer used for this test app so
    # the middleware (which checks Authorization against the worker bearer)
    # passes, and the admin gate (which checks Authorization against the admin
    # token) also passes.
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ADMIN_AUTH_TOKEN", "test-token")
    secret_token = "ghp_" + "B" * 36
    full_prompt = f"implement the feature using token {secret_token}"
    _write_session_meta(
        tmp_path,
        "cafebabe01234567",
        {
            "task_id": "cafebabe01234567",
            "session_id": "sy",
            "command": ["claude", "-p", "--session-id", "sy", full_prompt],
            "full_prompt": full_prompt,
            "subprocess_pid": 5678,
        },
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/sessions/cafebabe01234567/command?raw=true",
            headers={
                **_AUTH_HEADERS,
                "X-Deile-Actor": "operator@example.com",
            },
        )
        assert resp.status == 200
        body = await resp.json()

    # Raw path: the secret token must appear verbatim in both cmd and full_prompt.
    assert secret_token in body["cmd"][-1]
    assert secret_token in body["full_prompt"]


async def test_session_command_returns_404_for_unknown_task(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Valid task_id format but no session metadata → 404."""
    monkeypatch.setenv("HOME", str(tmp_path))
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/sessions/0000000000000000/command",
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 404


async def test_session_command_returns_400_for_invalid_task_id(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Bad task_id format must 400 (path traversal guard)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/sessions/..%2Fetc%2Fpasswd/command",
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 400


async def test_session_chat_returns_parsed_turns(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """``GET /v1/sessions/{id}/chat`` returns parsed turns from the JSONL."""
    monkeypatch.setenv("HOME", str(tmp_path))
    workdir = tmp_path / "work" / "1111111122222222"
    workdir.mkdir(parents=True)
    _write_session_meta(
        tmp_path,
        "1111111122222222",
        {
            "task_id": "1111111122222222",
            "session_id": "sid-chat",
            "workdir": str(workdir),
        },
    )
    # Build the JSONL where the worker expects it.
    workspace_hash = "-" + str(workdir).lstrip("/").replace("/", "-")
    jsonl_dir = tmp_path / ".claude" / "projects" / workspace_hash
    jsonl_dir.mkdir(parents=True)
    (jsonl_dir / "sid-chat.jsonl").write_text(
        '{"type":"user","content":"hi"}\n' '{"type":"assistant","content":"hello"}\n',
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/sessions/1111111122222222/chat",
            headers=_AUTH_HEADERS,
        )
        body = await resp.json()

    assert resp.status == 200
    assert body["session_id"] == "sid-chat"
    assert len(body["turns"]) == 2
    assert body["turns"][0]["role"] == "user"
    assert body["turns"][1]["role"] == "assistant"


async def test_session_chat_supports_tail(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """``?tail=N`` caps the response (latest N turns)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    workdir = tmp_path / "work" / "2222222233333333"
    workdir.mkdir(parents=True)
    _write_session_meta(
        tmp_path,
        "2222222233333333",
        {
            "task_id": "2222222233333333",
            "session_id": "sid-tail",
            "workdir": str(workdir),
        },
    )
    workspace_hash = "-" + str(workdir).lstrip("/").replace("/", "-")
    jsonl_dir = tmp_path / ".claude" / "projects" / workspace_hash
    jsonl_dir.mkdir(parents=True)
    rows = [f'{{"type":"user","content":"m{i}"}}' for i in range(10)]
    (jsonl_dir / "sid-tail.jsonl").write_text("\n".join(rows) + "\n")

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/sessions/2222222233333333/chat?tail=3",
            headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    assert len(body["turns"]) == 3
    assert body["turns"][0]["content"] == "m7"
    assert body["turns"][-1]["content"] == "m9"


async def test_session_chat_handles_missing_jsonl(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """When the JSONL is absent, response carries ``missing=True`` (200)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    workdir = tmp_path / "work" / "3333333344444444"
    workdir.mkdir(parents=True)
    _write_session_meta(
        tmp_path,
        "3333333344444444",
        {
            "task_id": "3333333344444444",
            "session_id": "missing-sid",
            "workdir": str(workdir),
        },
    )
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/sessions/3333333344444444/chat",
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()
    assert body["missing"] is True
    assert body["turns"] == []


async def test_session_chat_handles_malformed_jsonl(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """A malformed line is reported in ``skipped_malformed_lines``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    workdir = tmp_path / "work" / "4444444455555555"
    workdir.mkdir(parents=True)
    _write_session_meta(
        tmp_path,
        "4444444455555555",
        {
            "task_id": "4444444455555555",
            "session_id": "sid-bad",
            "workdir": str(workdir),
        },
    )
    workspace_hash = "-" + str(workdir).lstrip("/").replace("/", "-")
    jsonl_dir = tmp_path / ".claude" / "projects" / workspace_hash
    jsonl_dir.mkdir(parents=True)
    (jsonl_dir / "sid-bad.jsonl").write_text(
        '{"type":"user","content":"ok"}\n' "garbage-not-json\n",
    )
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/sessions/4444444455555555/chat",
            headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    assert len(body["turns"]) == 1
    assert body["skipped_malformed_lines"] == 1


async def test_session_stdout_returns_tails(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """``GET /v1/sessions/{id}/stdout`` returns capped tails for both streams."""
    root = tmp_path / "work"
    progress = root / ".progress"
    progress.mkdir(parents=True)
    (progress / "5555555566666666.stdout.log").write_text("hello-stdout")
    (progress / "5555555566666666.stderr.log").write_text("hello-stderr")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(root))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/sessions/5555555566666666/stdout",
            headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    assert resp.status == 200
    assert body["stdout"] == "hello-stdout"
    assert body["stderr"] == "hello-stderr"


async def test_kill_requires_confirm_token(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """``POST /v1/sessions/{id}/kill`` without the right confirm → 400."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_session_meta(
        tmp_path,
        "6666666677777777",
        {
            "task_id": "6666666677777777",
            "session_id": "sid-alive",
        },
    )
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/sessions/6666666677777777/kill",
            headers=_AUTH_HEADERS,
            json={"confirm": "wrong"},
        )
        assert resp.status == 400
        body = await resp.json()
    assert "yes-task-66666666" in body["expected"]


async def test_kill_returns_409_when_no_live_process(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Without a live claude subprocess we cannot kill — 409 (not 500)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_worker_module,
        "_find_claude_pid",
        lambda _sid: None,
    )
    _write_session_meta(
        tmp_path,
        "7777777788888888",
        {
            "task_id": "7777777788888888",
            "session_id": "sid-dead",
        },
    )
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/sessions/7777777788888888/kill",
            headers=_AUTH_HEADERS,
            json={"confirm": "yes-task-77777777"},
        )
        assert resp.status == 409


async def test_kill_returns_200_when_pid_found(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """When a PID is discoverable, kill issues SIGKILL and reports the PID."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_worker_module,
        "_find_claude_pid",
        lambda _sid: 4321,
    )
    sent = {}

    def fake_kill(pid, sig):
        sent["pid"] = pid
        sent["sig"] = sig

    monkeypatch.setattr(claude_worker_module.os, "kill", fake_kill)
    _write_session_meta(
        tmp_path,
        "8888888899999999",
        {
            "task_id": "8888888899999999",
            "session_id": "sid-target",
        },
    )
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/sessions/8888888899999999/kill",
            headers=_AUTH_HEADERS,
            json={"confirm": "yes-task-88888888"},
        )
        body = await resp.json()
    assert resp.status == 200
    assert body["killed"] is True
    assert body["pid"] == 4321
    assert sent == {"pid": 4321, "sig": 9}


async def test_kill_returns_404_for_unknown_task(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Task without session.json → 404."""
    monkeypatch.setenv("HOME", str(tmp_path))
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/sessions/9999999900000000/kill",
            headers=_AUTH_HEADERS,
            json={"confirm": "yes-task-99999999"},
        )
        assert resp.status == 404


async def test_cleanup_removes_workdir_and_jsonl(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """``DELETE /v1/sessions/{id}/cleanup`` removes workdir + jsonl + session."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_worker_module,
        "_is_claude_process_alive",
        lambda _sid: False,
    )
    workdir = tmp_path / "work" / "aaaa0000aaaa0000"
    workdir.mkdir(parents=True)
    (workdir / "trash.txt").write_text("x")
    workspace_hash = "-" + str(workdir).lstrip("/").replace("/", "-")
    jsonl_dir = tmp_path / ".claude" / "projects" / workspace_hash
    jsonl_dir.mkdir(parents=True)
    (jsonl_dir / "sid-clean.jsonl").write_text("...")
    _write_session_meta(
        tmp_path,
        "aaaa0000aaaa0000",
        {
            "task_id": "aaaa0000aaaa0000",
            "session_id": "sid-clean",
            "workdir": str(workdir),
        },
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.delete(
            "/v1/sessions/aaaa0000aaaa0000/cleanup",
            headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    assert resp.status == 200
    assert body["removed"]["workdir"] is True
    assert body["removed"]["jsonl"] is True
    assert not workdir.exists()


async def test_cleanup_returns_409_when_task_is_alive(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Cleanup MUST refuse to drop a live task — operator must kill first."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_worker_module,
        "_is_claude_process_alive",
        lambda _sid: True,
    )
    _write_session_meta(
        tmp_path,
        "bbbbcccc1111dddd",
        {
            "task_id": "bbbbcccc1111dddd",
            "session_id": "alive",
        },
    )
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.delete(
            "/v1/sessions/bbbbcccc1111dddd/cleanup",
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 409


async def test_observability_endpoints_require_bearer(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """All new endpoints honor the Bearer auth middleware."""
    monkeypatch.setenv("HOME", str(tmp_path))
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        for path in (
            "/v1/sessions",
            "/v1/sessions/0000000000000000/command",
            "/v1/sessions/0000000000000000/chat",
            "/v1/sessions/0000000000000000/stdout",
        ):
            resp = await client.get(path)
            assert resp.status == 401, f"{path} did not require bearer"


# ============================================================================
# Housekeeping / cleanup (issue #408)
# ============================================================================


def test_cleanup_scan_empty_root(claude_worker_module, tmp_path):
    """_cleanup_scan on empty/missing directory returns empty lists."""
    root = tmp_path / "work"
    result = claude_worker_module._cleanup_scan(root, retention_days=7)
    assert result["dead_leases"] == []
    assert result["old_workdirs"] == []
    assert result["empty_workdirs"] == []
    assert result["total_candidate_bytes"] == 0


def test_cleanup_scan_skips_active_lease(claude_worker_module, tmp_path, monkeypatch):
    """Active lease (fresh heartbeat) protects a workdir from cleanup."""
    root = tmp_path / "work"
    workdir = root / "aabb0011aabb0011"
    workdir.mkdir(parents=True)
    import time

    lease = {
        "pod": "p1",
        "pid": 99999,
        "started_at": time.time(),
        "heartbeat_at": time.time(),  # fresh
    }
    (workdir / ".lease.json").write_text(__import__("json").dumps(lease))
    result = claude_worker_module._cleanup_scan(root, retention_days=7)
    assert str(workdir) in result["active_workdirs"]
    assert str(workdir) not in result["old_workdirs"]
    assert str(workdir) not in result["empty_workdirs"]


def test_cleanup_scan_detects_dead_lease(claude_worker_module, tmp_path):
    """Expired lease + dead PID marked as dead_leases candidate."""
    root = tmp_path / "work"
    workdir = root / "ccdd0022ccdd0022"
    workdir.mkdir(parents=True)
    import json
    import time

    lease = {
        "pod": "p1",
        "pid": 999999999,  # non-existent PID
        "started_at": time.time() - 3600,
        "heartbeat_at": time.time() - 3600,  # very old
    }
    (workdir / ".lease.json").write_text(json.dumps(lease))
    result = claude_worker_module._cleanup_scan(root, retention_days=7)
    lease_paths = [str(workdir / ".lease.json")]
    assert any(lp in result["dead_leases"] for lp in lease_paths)


def test_cleanup_scan_detects_empty_workdir(
    claude_worker_module, tmp_path, monkeypatch
):
    """Workdir without session JSONL (no claude run) detected as empty."""
    root = tmp_path / "work"
    workdir = root / "eeff0033eeff0033"
    workdir.mkdir(parents=True)
    # No JSONL in projects dir → empty workdir
    monkeypatch.setenv("HOME", str(tmp_path))
    result = claude_worker_module._cleanup_scan(root, retention_days=365)
    assert str(workdir) in result["empty_workdirs"]


def test_do_cleanup_dry_run_deletes_nothing(
    claude_worker_module, tmp_path, monkeypatch
):
    """dry_run=True returns scan but does NOT delete anything."""
    root = tmp_path / "work"
    workdir = root / "ff110044ff110044"
    workdir.mkdir(parents=True)
    (workdir / "somefile.txt").write_text("important")
    monkeypatch.setenv("HOME", str(tmp_path))
    result = claude_worker_module._do_cleanup(root, retention_days=365, dry_run=True)
    assert result["dry_run"] is True
    assert result["removed_leases"] == []
    assert result["removed_workdirs"] == []
    assert workdir.exists()  # not deleted


async def test_cleanup_preview_endpoint(claude_worker_module, monkeypatch, tmp_path):
    """GET /v1/cleanup returns preview without deleting."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    (tmp_path / "work").mkdir()
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/cleanup", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
    assert body["dry_run"] is True
    assert "dead_leases" in body
    assert "old_workdirs" in body
    assert "empty_workdirs" in body


async def test_cleanup_execute_endpoint(claude_worker_module, monkeypatch, tmp_path):
    """POST /v1/cleanup executes cleanup and returns result."""
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(root))
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/cleanup", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
    assert body["dry_run"] is False
    assert "removed_leases" in body
    assert "removed_workdirs" in body
    assert "freed_bytes" in body


async def test_cleanup_endpoints_require_bearer(
    claude_worker_module, monkeypatch, tmp_path
):
    """Cleanup endpoints respect bearer auth middleware."""
    monkeypatch.setenv("HOME", str(tmp_path))
    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        for method, path in (
            ("GET", "/v1/cleanup"),
            ("POST", "/v1/cleanup"),
        ):
            resp = await getattr(client, method.lower())(path)
            assert resp.status == 401, f"{method} {path} should require bearer"


# --------------------------------------------------------------------------- #
# Issue #395 — /v1/pod-status
# --------------------------------------------------------------------------- #


async def test_pod_status_endpoint_returns_lease_when_active(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Active lease.json with recent heartbeat → lease dict is returned."""
    import time as _time

    # Create a fake workspace with a live lease.
    task_id = "aabbccdd11223344"
    workspace = tmp_path / task_id
    workspace.mkdir()
    now = _time.time()
    lease_data = {
        "pod": "claude-worker-0",
        "pid": 42,
        "task_id": task_id,
        "extra_secret": "should-not-appear",
        "started_at": now - 10,
        "heartbeat_at": now - 2,
    }
    (workspace / ".lease.json").write_text(
        __import__("json").dumps(lease_data), encoding="utf-8"
    )

    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("DEILE_CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(claude_worker_module, "_count_claude_processes", lambda: 1)

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/pod-status", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
    assert body["lease"] is not None
    assert body["lease"]["task_id"] == task_id
    assert body["lease"]["pid"] == 42
    assert "heartbeat_at" in body["lease"]
    assert body["claude_processes"] == 1
    assert "disk" in body
    assert "ts" in body


async def test_pod_status_endpoint_returns_null_lease_when_idle(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """No lease.json in workspace → lease is null (pod is idle)."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("DEILE_CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(claude_worker_module, "_count_claude_processes", lambda: 0)

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/pod-status", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
    assert body["lease"] is None
    assert body["claude_processes"] == 0


async def test_pod_status_endpoint_returns_null_lease_when_heartbeat_stale(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Lease with expired heartbeat (> TTL) → treated as dead, returns null."""
    import time as _time

    task_id = "deadbeef00112233"
    workspace = tmp_path / task_id
    workspace.mkdir()
    ttl = claude_worker_module._LEASE_TTL_S
    stale_data = {
        "pod": "old-pod",
        "pid": 99,
        "heartbeat_at": _time.time() - ttl - 60,  # well past TTL
    }
    (workspace / ".lease.json").write_text(
        __import__("json").dumps(stale_data), encoding="utf-8"
    )

    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("DEILE_CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(claude_worker_module, "_count_claude_processes", lambda: 0)

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/pod-status", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
    assert body["lease"] is None, "stale lease should be treated as idle"


async def test_pod_status_endpoint_redacts_lease_payload(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Only task_id, heartbeat_at, pid are exposed — no extra fields leak."""
    import time as _time

    task_id = "1122334455667788"
    workspace = tmp_path / task_id
    workspace.mkdir()
    now = _time.time()
    lease_data = {
        "pod": "claude-worker-0",
        "pid": 77,
        "started_at": now - 5,
        "heartbeat_at": now - 1,
        "secret_prompt": "DO NOT LEAK",
        "credentials": "also-secret",
    }
    (workspace / ".lease.json").write_text(
        __import__("json").dumps(lease_data), encoding="utf-8"
    )

    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("DEILE_CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(claude_worker_module, "_count_claude_processes", lambda: 1)

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/pod-status", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
    lease = body["lease"]
    assert lease is not None
    allowed_keys = {"task_id", "heartbeat_at", "pid", "claude_pid", "claude_running"}
    assert (
        set(lease.keys()) <= allowed_keys
    ), f"Lease exposed extra fields: {set(lease.keys()) - allowed_keys}"
    assert "secret_prompt" not in lease
    assert "credentials" not in lease
    assert "pod" not in lease
    assert "started_at" not in lease


async def test_pod_status_endpoint_requires_bearer(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """/v1/pod-status requires Bearer auth — no anonymous access."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("DEILE_CLAUDE_HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/pod-status")  # no auth header
        assert resp.status == 401


async def test_pod_status_endpoint_returns_disk_usage_via_shutil(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """disk field is populated from shutil.disk_usage — no subprocess."""
    import collections

    DiskUsage = collections.namedtuple("DiskUsage", ["total", "used", "free"])
    fake_du = DiskUsage(total=10 * 1024**3, used=3 * 1024**3, free=7 * 1024**3)

    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("DEILE_CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(claude_worker_module.shutil, "disk_usage", lambda path: fake_du)
    monkeypatch.setattr(claude_worker_module, "_count_claude_processes", lambda: 0)

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/pod-status", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()

    disk = body["disk"]
    assert disk is not None
    assert disk["used_bytes"] == 3 * 1024**3
    assert disk["total_bytes"] == 10 * 1024**3
    assert "mount" in disk


async def test_pod_status_endpoint_counts_claude_processes_via_psutil(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """claude_processes reflects the value returned by _count_claude_processes."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("DEILE_CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(claude_worker_module, "_count_claude_processes", lambda: 3)

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/pod-status", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()

    assert body["claude_processes"] == 3


def test_anthropic_quota_capture_middleware_stores_latest_header(
    claude_worker_module,
):
    """_try_capture_quota_from_output stores rate-limit token count from dispatch output."""
    assert claude_worker_module._get_quota_snapshot() is None

    claude_worker_module._try_capture_quota_from_output(
        stdout="",
        stderr="anthropic-ratelimit-tokens-remaining: 87654",
    )

    snap = claude_worker_module._get_quota_snapshot()
    assert snap is not None
    assert snap.tokens_remaining == 87654
    assert snap.captured_at > 0

    # Second call with x-ratelimit variant overwrites with newer value.
    claude_worker_module._try_capture_quota_from_output(
        stdout="x-ratelimit-remaining-tokens: 50000",
        stderr="",
    )
    snap2 = claude_worker_module._get_quota_snapshot()
    assert snap2.tokens_remaining == 50000


# ---------------------------------------------------------------------------
# In-pod OAuth broker endpoints (issue #335)
# ---------------------------------------------------------------------------


async def test_auth_start_requires_no_bearer_token(claude_worker_module, monkeypatch):
    """/v1/auth/start e /v1/auth/status são unauthenticated."""

    def fake_popen(cmd, *args, **kwargs):
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr(claude_worker_module.subprocess, "Popen", fake_popen)
    claude_worker_module._oauth_broker.reset()

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        # Sem Bearer header — deve passar (unauthenticated por design)
        resp_start = await client.get("/v1/auth/start")
        assert resp_start.status == 200

        resp_status = await client.get("/v1/auth/status")
        assert resp_status.status == 200

        # Outros endpoints ainda exigem Bearer
        resp_dispatch = await client.post("/v1/dispatch", json={})
        assert resp_dispatch.status == 401


async def test_auth_status_returns_idle_initially(claude_worker_module):
    """/v1/auth/status retorna idle quando nenhum fluxo foi iniciado."""
    claude_worker_module._oauth_broker.reset()

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/auth/status")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "idle"
        assert body["oauth_url"] is None


async def test_auth_start_returns_error_when_claude_not_found(
    claude_worker_module,
    monkeypatch,
):
    """/v1/auth/start retorna error se claude binary não está no PATH."""
    import subprocess as _sp  # noqa: PLC0415

    def fake_popen(cmd, *args, **kwargs):
        if cmd and cmd[0] == "claude":
            raise FileNotFoundError("claude not found")
        return _sp.Popen(cmd, *args, **kwargs)

    monkeypatch.setattr(claude_worker_module.subprocess, "Popen", fake_popen)
    claude_worker_module._oauth_broker.reset()

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/auth/start")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "error"
        assert body.get("error") is not None


async def test_auth_start_captures_oauth_url_from_claude_output(
    claude_worker_module,
    monkeypatch,
):
    """/v1/auth/start captura URL OAuth impressa pelo claude auth login."""
    import io  # noqa: PLC0415
    import subprocess as _sp  # noqa: PLC0415

    fake_url = (
        "https://claude.ai/auth/login?state=abc&code_challenge=xyz"
        "&redirect_uri=http://localhost:54321/callback"
    )

    class FakeProc:
        returncode = 0
        stdout = io.StringIO(f"Opening browser...\nIf not opened, visit:\n{fake_url}\n")

        def wait(self):
            pass

    def fake_popen(cmd, *args, **kwargs):
        if cmd and cmd[0] == "claude":
            return FakeProc()
        return _sp.Popen(cmd, *args, **kwargs)

    monkeypatch.setattr(claude_worker_module.subprocess, "Popen", fake_popen)
    claude_worker_module._oauth_broker.reset()

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/auth/start")
        assert resp.status == 200
        body = await resp.json()

    assert body.get("oauth_url") == fake_url, f"URL não capturada; body={body}"
    assert body.get("callback_port") == 54321, f"Porta não detectada; body={body}"
    assert body.get("status") in ("pending", "complete")


async def test_auth_start_captures_callback_port_from_percent_encoded_url(
    claude_worker_module,
    monkeypatch,
):
    """/v1/auth/start extrai callback_port mesmo com redirect_uri percent-encoded."""
    import io  # noqa: PLC0415
    import subprocess as _sp  # noqa: PLC0415

    # redirect_uri é percent-encoded como o claude CLI produz em produção
    fake_url = (
        "https://claude.ai/auth/login?state=abc&code_challenge=xyz"
        "&redirect_uri=http%3A%2F%2Flocalhost%3A54321%2Fcallback"
    )

    class FakeProc:
        returncode = 0
        stdout = io.StringIO(f"Opening browser...\nIf not opened, visit:\n{fake_url}\n")

        def wait(self):
            pass

    def fake_popen(cmd, *args, **kwargs):
        if cmd and cmd[0] == "claude":
            return FakeProc()
        return _sp.Popen(cmd, *args, **kwargs)

    monkeypatch.setattr(claude_worker_module.subprocess, "Popen", fake_popen)
    claude_worker_module._oauth_broker.reset()

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/auth/start")
        assert resp.status == 200
        body = await resp.json()

    assert body.get("oauth_url") == fake_url, f"URL não capturada; body={body}"
    assert (
        body.get("callback_port") == 54321
    ), f"Porta não detectada com redirect_uri percent-encoded; body={body}"


def test_oauth_broker_state_reset(claude_worker_module):
    """_OAuthBrokerState.reset() limpa todos os campos."""
    state = claude_worker_module._OAuthBrokerState()
    state.status = "complete"
    state.oauth_url = "https://example.com"
    state.email = "test@example.com"
    state.error = "some error"
    state.started_at = 9999.0

    state.reset()

    assert state.status == "idle"
    assert state.oauth_url is None
    assert state.email is None
    assert state.error is None
    assert state.started_at == 0.0


@pytest.mark.asyncio
async def test_dispatch_409_lease_conflict_emits_dispatch_failed(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """AC #435 §2 — 409 lease conflict é caminho terminal; precisa emitir
    ``dispatch.failed`` para o painel limpar ``current_task`` (senão fica
    preso quando duas réplicas brigam pelo mesmo workspace).
    """
    import logging

    # Lease acquisition always fails (simulates other replica holding it).
    # ``**kwargs`` aceita os novos channel=/session_id= do dedup por channel.
    async def fake_acquire(workspace, **kwargs):
        return None

    monkeypatch.setattr(claude_worker_module, "_acquire_lease", fake_acquire)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    # Capture deile.dispatch records.
    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    lg = logging.getLogger("deile.dispatch")
    h = _H()
    lg.addHandler(h)
    old_level = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        app = claude_worker_module.build_app(auth_token="test-token")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/dispatch",
                headers=_AUTH_HEADERS,
                json={
                    "brief": "test",
                    "channel_id": "auto/issue-1",
                    "preferred_model": "anthropic:claude-sonnet-4-6",
                    "stage": "implement",
                    "issue_number": 1,
                    "branch": "auto/issue-1",
                },
            )
            assert resp.status == 409
            body = await resp.json()
            assert body.get("error_code") == "TASK_ALREADY_RUNNING"
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)

    msgs = [r.getMessage() for r in records]
    failed = [m for m in msgs if m.startswith("dispatch.failed ")]
    assert len(failed) == 1, f"expected exactly one dispatch.failed, got {msgs}"
    assert "error_code=TASK_ALREADY_RUNNING" in failed[0]
    assert "reason=lease_conflict" in failed[0]


# --- Channel-based fresh-dispatch dedup (anti dup-dispatch #433/#446) --------


def test_find_live_task_for_channel(claude_worker_module, monkeypatch, tmp_path):
    """Dedup por channel: encontra o task_id de um workdir cujo lease casa o
    channel E cujo claude está vivo; ignora channel divergente, claude morto,
    lease ausente/corrompido e channel vazio."""
    import json

    cws = claude_worker_module
    # A: channel alvo + claude vivo
    (tmp_path / "aaaa").mkdir()
    (tmp_path / "aaaa" / ".lease.json").write_text(
        json.dumps({"channel": "pipeline-issue-446", "session_id": "sid-a"})
    )
    # B: channel divergente (claude vivo, mas channel != alvo)
    (tmp_path / "bbbb").mkdir()
    (tmp_path / "bbbb" / ".lease.json").write_text(
        json.dumps({"channel": "pipeline-issue-999", "session_id": "sid-b"})
    )
    # C: channel alvo MAS claude morto → não conta
    (tmp_path / "cccc").mkdir()
    (tmp_path / "cccc" / ".lease.json").write_text(
        json.dumps({"channel": "pipeline-issue-446", "session_id": "sid-dead"})
    )
    # D: workdir sem lease → ignorado
    (tmp_path / "dddd").mkdir()

    monkeypatch.setattr(
        cws,
        "_is_claude_process_alive",
        lambda sid: sid in {"sid-a", "sid-b"},
    )
    # acha A (channel casa + vivo); ignora C (mesmo channel, claude morto)
    assert cws._find_live_task_for_channel(tmp_path, "pipeline-issue-446") == "aaaa"
    # channel sem nenhum vivo casando → None (B existe mas é outro channel)
    assert cws._find_live_task_for_channel(tmp_path, "pipeline-issue-000") is None
    # channel vazio → None (nunca dedupa sem channel)
    assert cws._find_live_task_for_channel(tmp_path, "") is None


def test_find_live_task_for_channel_dead_only_returns_none(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Channel casa mas o único claude está morto → None (permite fresh)."""
    import json

    cws = claude_worker_module
    (tmp_path / "x1").mkdir()
    (tmp_path / "x1" / ".lease.json").write_text(
        json.dumps({"channel": "pipeline-issue-433", "session_id": "dead"})
    )
    monkeypatch.setattr(cws, "_is_claude_process_alive", lambda sid: False)
    assert cws._find_live_task_for_channel(tmp_path, "pipeline-issue-433") is None


# --------------------------------------------------------------------------- #
# F1a — captura de stderr/motivo na resposta de falha (issue #445)
# --------------------------------------------------------------------------- #


# --- Testes unitários de _build_failure_reason ---


def test_build_failure_reason_prefere_claude_result_text(claude_worker_module):
    """Quando ``claude_result_text`` não está vazio, é retornado como motivo
    (fonte mais confiável: JSON estruturado do claude CLI)."""
    fn = claude_worker_module._build_failure_reason
    reason = fn(
        returncode=1,
        stderr="Erro de disco",
        stdout="saída qualquer",
        claude_result_text="Task failed: no tests pass",
    )
    assert reason == "Task failed: no tests pass"
    # stderr e stdout não aparecem quando há claude_result_text.
    assert "disco" not in reason


def test_build_failure_reason_usa_stderr_quando_result_vazio(claude_worker_module):
    """Sem ``claude_result_text``, a cauda do stderr é priorizada."""
    fn = claude_worker_module._build_failure_reason
    reason = fn(
        returncode=2,
        stderr="fatal: repository not found\npermission denied",
        stdout="",
        claude_result_text="",
    )
    assert "rc=2" in reason
    assert "permission denied" in reason
    # Prefixo deve mencionar stderr.
    assert "stderr" in reason


def test_build_failure_reason_fallback_stdout_quando_sem_stderr(claude_worker_module):
    """Quando stderr está vazio, usa cauda do stdout como fallback."""
    fn = claude_worker_module._build_failure_reason
    reason = fn(
        returncode=1,
        stderr="",
        stdout="Algo importante aconteceu no stdout\nfim",
        claude_result_text="",
    )
    assert "rc=1" in reason
    assert "stdout" in reason
    assert "fim" in reason


def test_build_failure_reason_timeout_sem_output(claude_worker_module):
    """rc=124 sem nenhuma saída produz motivo legível mencionando timeout."""
    fn = claude_worker_module._build_failure_reason
    reason = fn(returncode=124, stderr="", stdout="", claude_result_text="")
    assert "124" in reason or "timeout" in reason.lower()


def test_build_failure_reason_rc_generico_sem_output(claude_worker_module):
    """rc diferente de 0 e 124 sem saída produz motivo mencionando o rc."""
    fn = claude_worker_module._build_failure_reason
    reason = fn(returncode=137, stderr="", stdout="", claude_result_text="")
    assert "137" in reason


def test_build_failure_reason_trunca_stderr_longo(claude_worker_module):
    """stderr muito longo é truncado na cauda (últimos max_bytes chars)."""
    fn = claude_worker_module._build_failure_reason
    stderr_long = "A" * 5000
    reason = fn(
        returncode=1,
        stderr=stderr_long,
        stdout="",
        claude_result_text="",
        max_bytes=2000,
    )
    # O motivo não deve ultrapassar max_bytes + overhead do prefixo.
    assert len(reason) <= 2000 + len("rc=1 stderr: ")
    # A cauda deve conter os últimos caracteres.
    assert reason.endswith("A" * 10)


def test_build_failure_reason_trunca_stdout_longo(claude_worker_module):
    """stdout muito longo (com stderr vazio) é truncado na cauda."""
    fn = claude_worker_module._build_failure_reason
    stdout_long = "B" * 5000
    reason = fn(
        returncode=1,
        stderr="",
        stdout=stdout_long,
        claude_result_text="",
        max_bytes=2000,
    )
    assert len(reason) <= 2000 + len("rc=1 stdout: ")
    assert reason.endswith("B" * 10)


# --- Testes de integração via dispatch_handler ---


@pytest.mark.asyncio
async def test_dispatch_error_contém_stderr_quando_subprocess_falha(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """F1a: quando subprocess falha (rc != 0) com stderr não-vazio, a resposta
    JSON deve incluir ``error`` com a cauda do stderr — e NÃO deve ser
    ``"unknown"`` nem vazio.

    Simula o cenário real observado: review de PR com 557s que terminou com
    ``dispatch.failed reason=unknown turns=0`` porque o motivo não era capturado.
    """
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")

    stderr_msg = (
        "Error: API timeout after 557s\n"
        "fatal: could not resolve host: api.anthropic.com\n"
        "claude -p exited with code 1"
    )

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        # Subprocess falha com rc=1 e stderr não-vazio; stdout sem JSON válido.
        return claude_worker_module.SubprocessResult(
            returncode=1,
            stdout="",
            stderr=stderr_msg,
            duration_seconds=557.0,
        )

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={"brief": "review PR #42", "stage": "pr_review"},
        )
        body = await resp.json()

    assert body["ok"] is False
    error = body.get("error", "")
    # O motivo não deve ser "unknown" nem vazio.
    assert error, "response deve ter campo 'error' preenchido"
    assert error.lower() != "unknown", f"error não deve ser 'unknown', got: {error!r}"
    # Deve conter informação do stderr real.
    assert (
        "api.anthropic.com" in error or "timeout" in error or "rc=1" in error
    ), f"error deve referenciar o stderr, got: {error!r}"


@pytest.mark.asyncio
async def test_dispatch_error_contém_stderr_truncado_quando_gigante(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """F1a: stderr gigante (> 2000 bytes) resulta num campo ``error`` truncado.

    Verifica duas propriedades:
    1. O campo ``error`` é preenchido (não vazio/unknown) mesmo com stderr enorme.
    2. O tamanho do campo ``error`` é limitado (não vaza o stderr inteiro).

    Nota: a cauda de 2000 bytes capturada por ``_build_failure_reason`` e
    depois truncada em 500 pelo handler significa que o ``error`` final contém
    apenas o início da cauda do stderr. Testes unitários de ``_build_failure_reason``
    (``test_build_failure_reason_trunca_stderr_longo``) verificam o comportamento
    completo de truncagem — aqui validamos apenas que a integração não quebra.
    """
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")

    # stderr de 10 KB — grande o suficiente para forçar truncagem em ambas as camadas.
    stderr_gigante = "Z" * 10_000

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        return claude_worker_module.SubprocessResult(
            returncode=2,
            stdout="",
            stderr=stderr_gigante,
            duration_seconds=10.0,
        )

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={"brief": "implement #1", "stage": "implement"},
        )
        body = await resp.json()

    assert body["ok"] is False
    error = body.get("error", "")
    # O campo error deve ser preenchido (não vazio/unknown).
    assert error, "campo 'error' deve ser preenchido quando subprocess falha"
    assert error.lower() != "unknown", f"error não deve ser 'unknown', got: {error!r}"
    # Deve mencionar que é stderr (prefixo da _build_failure_reason).
    assert (
        "stderr" in error or "rc=2" in error
    ), f"error deve indicar a origem do erro, got: {error!r}"
    # O campo error deve ser limitado pelo handler (≤500 chars do motivo + prefixo).
    assert len(error) <= 550, f"error deve ser truncado, got len={len(error)}"


@pytest.mark.asyncio
async def test_dispatch_timeout_error_contém_motivo_legível(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """F1a: timeout (rc=124) sem saída resulta em error descritivo mencionando
    timeout — nunca ``'unknown'``."""
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        # Simula o que run_subprocess_with_progress devolve em timeout real.
        return claude_worker_module.SubprocessResult(
            returncode=124,
            stdout="",
            stderr=f"claude -p timed out after {timeout}s",
            duration_seconds=float(timeout),
        )

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={"brief": "implement #99", "stage": "implement", "timeout_s": 30},
        )
        body = await resp.json()

    assert body["ok"] is False
    error = body.get("error", "")
    assert error, "deve ter campo 'error'"
    assert error.lower() != "unknown"
    # Deve mencionar timeout ou rc=124.
    assert (
        "timeout" in error.lower() or "124" in error
    ), f"error de timeout deve mencionar timeout ou rc=124, got: {error!r}"


@pytest.mark.asyncio
async def test_dispatch_ok_nao_preenche_error(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Sanidade: dispatch bem-sucedido NÃO deve ter campo ``error`` na resposta
    (não-regressão — a nova lógica de falha só dispara quando ok=False)."""
    import json as _json

    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        out = _json.dumps(
            {
                "is_error": False,
                "result": "STATUS: SUCCESS",
                "session_id": "s1",
                "total_cost_usd": 0.02,
                "duration_ms": 5000,
                "num_turns": 3,
            }
        )
        return claude_worker_module.SubprocessResult(
            returncode=0,
            stdout=out,
            stderr="",
            duration_seconds=5.0,
        )

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={"brief": "implement #7", "stage": "implement"},
        )
        body = await resp.json()

    assert body["ok"] is True
    assert (
        "error" not in body
    ), f"dispatch bem-sucedido não deve ter 'error', got: {body.get('error')}"


# =============================================================================
# T1 — last_result_full: veredito completo sem truncagem a 300 chars
# =============================================================================


@pytest.mark.asyncio
async def test_resume_info_expõe_last_result_full_completo(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """T1: resume_info_handler deve devolver ``last_result_full`` completo —
    sem truncar a 300 chars como o ``last_result_summary`` faz.

    Prova com veredito > 300 chars para garantir que a string não é cortada.
    """
    import json as _json

    monkeypatch.setenv("HOME", str(tmp_path))

    task_id = "aabbccdd11223344"
    workdir = tmp_path / "work" / task_id
    workdir.mkdir(parents=True)
    meta_dir = tmp_path / ".claude" / "tasks" / task_id
    meta_dir.mkdir(parents=True)

    # Veredito com 500 chars — bem acima do limite de 300 do summary.
    veredito_longo = "CLARO: " + "X" * 493  # total 500 chars
    assert len(veredito_longo) == 500

    (meta_dir / "session.json").write_text(
        _json.dumps(
            {
                "task_id": task_id,
                "session_id": "sess-full",
                "workdir": str(workdir),
                "stage": "pr_review",
                "branch": "auto/issue-1",
                "started_at": 1000,
                "last_completed_at": 1420,
                "last_is_error": False,
                "last_result_summary": veredito_longo[:300],  # como grava o handler
                "last_result_full": veredito_longo,  # novo campo T1
                "last_returncode": 0,
                "attempt": 1,
            }
        )
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            f"/v1/dispatches/{task_id}/resume-info",
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()

    # last_result_summary DEVE ser truncado a 300.
    assert body["last_result_summary"] == veredito_longo[:300]
    # last_result_full NÃO deve ser truncado — 500 chars completos.
    assert "last_result_full" in body, "campo last_result_full deve estar presente"
    assert body["last_result_full"] == veredito_longo, (
        f"last_result_full foi truncado: len={len(body['last_result_full'])} "
        f"esperado=500"
    )


@pytest.mark.asyncio
async def test_dispatch_persiste_last_result_full_no_session_json(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """T1: após um dispatch bloqueante (wait_for_result=True), o session.json
    deve conter ``last_result_full`` com o texto completo do resultado do claude.
    """
    import json as _json

    veredito = "CLARO: tudo certo " + "Y" * 400  # total > 300 chars
    assert len(veredito) > 300

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        out = _json.dumps(
            {
                "is_error": False,
                "result": veredito,
                "session_id": "s-full",
                "total_cost_usd": 0.01,
                "duration_ms": 1000,
                "num_turns": 1,
            }
        )
        return claude_worker_module.SubprocessResult(0, out, "", 1.0)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "implement #1",
                "stage": "implement",
                "preferred_model": "anthropic:claude-sonnet-4-6",
            },
        )
        body = await resp.json()

    assert resp.status == 200
    assert body["ok"] is True

    # Lê o session.json gravado no disco.
    meta_path = tmp_path / ".claude" / "tasks" / body["task_id"] / "session.json"
    assert meta_path.exists()
    meta = _json.loads(meta_path.read_text())

    # last_result_summary = truncado a 300.
    assert meta["last_result_summary"] == veredito[:300]
    # last_result_full = texto completo (não truncado a 300).
    assert (
        meta["last_result_full"] == veredito
    ), f"last_result_full foi truncado: len={len(meta['last_result_full'])}"


@pytest.mark.asyncio
async def test_resume_info_retorna_string_vazia_quando_last_result_full_ausente(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """T1 retrocompat: metadados antigos (sem last_result_full) → campo
    ausente retorna string vazia (não KeyError nem None).
    """
    import json as _json

    monkeypatch.setenv("HOME", str(tmp_path))

    task_id = "0011223344556677"
    meta_dir = tmp_path / ".claude" / "tasks" / task_id
    meta_dir.mkdir(parents=True)
    # session.json sem o campo last_result_full (metadado antigo).
    (meta_dir / "session.json").write_text(
        _json.dumps(
            {
                "task_id": task_id,
                "session_id": "s-old",
                "workdir": "/tmp/nonexistent",
                "attempt": 1,
                "started_at": 100,
                "last_result_summary": "ok",
                # last_result_full ausente intencionalmente.
            }
        )
    )

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            f"/v1/dispatches/{task_id}/resume-info",
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()

    assert "last_result_full" in body
    assert body["last_result_full"] == ""


# =============================================================================
# T2 — dispatch nowait: 202 + task_id + background subprocess
# =============================================================================


@pytest.mark.asyncio
async def test_dispatch_nowait_retorna_202_com_task_id(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """T2: POST dispatch com ``wait_for_result=False`` retorna 202 imediatamente
    com ``{"task_id": ..., "status": "running"}`` sem esperar o subprocess.
    """
    import asyncio as _asyncio
    import json as _json

    # Evento para sincronizar: testa que 202 chegou ANTES do subprocess "terminar".
    subprocess_started = _asyncio.Event()
    subprocess_may_finish = _asyncio.Event()

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        subprocess_started.set()
        await subprocess_may_finish.wait()
        out = _json.dumps(
            {
                "is_error": False,
                "result": "done",
                "session_id": "s",
                "total_cost_usd": 0.01,
                "duration_ms": 10,
                "num_turns": 1,
            }
        )
        return claude_worker_module.SubprocessResult(0, out, "", 0.01)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "implement #2",
                "stage": "implement",
                "preferred_model": "anthropic:claude-sonnet-4-6",
                "wait_for_result": False,
            },
        )
        body = await resp.json()

    # 202 deve chegar ANTES do subprocess terminar.
    assert resp.status == 202, f"esperado 202, got {resp.status}: {body}"
    assert "task_id" in body
    assert len(body["task_id"]) == 16  # hex 16-char
    assert body["status"] == "running"

    # Libera o subprocess para que background task termine limpo.
    subprocess_may_finish.set()
    await _asyncio.sleep(0.05)  # aguarda cleanup da background task


@pytest.mark.asyncio
async def test_dispatch_nowait_grava_session_json_antes_do_subprocess(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """T2: session.json com task_id deve existir IMEDIATAMENTE após o 202,
    antes mesmo de o subprocess terminar — permite que resume-info funcione
    para tasks em andamento.
    """
    import asyncio as _asyncio
    import json as _json

    subprocess_may_finish = _asyncio.Event()

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        await subprocess_may_finish.wait()
        out = _json.dumps(
            {
                "is_error": False,
                "result": "done",
                "session_id": "s",
                "total_cost_usd": 0.0,
                "duration_ms": 1,
                "num_turns": 1,
            }
        )
        return claude_worker_module.SubprocessResult(0, out, "", 0.01)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "preferred_model": "anthropic:claude-haiku-4-5",
                "wait_for_result": False,
            },
        )
        body = await resp.json()

    assert resp.status == 202
    task_id = body["task_id"]

    # session.json deve existir imediatamente (gravado ANTES do spawn).
    meta_path = tmp_path / ".claude" / "tasks" / task_id / "session.json"
    assert (
        meta_path.exists()
    ), f"session.json deve existir imediatamente após 202, não encontrado: {meta_path}"
    meta = _json.loads(meta_path.read_text())
    assert meta["task_id"] == task_id
    assert "session_id" in meta

    subprocess_may_finish.set()
    await _asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_dispatch_nowait_grava_last_completed_ao_terminar(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """T2: após o subprocess terminar em background, session.json deve ter
    ``last_completed_at`` e ``last_result_full`` preenchidos.
    """
    import asyncio as _asyncio
    import json as _json

    resultado = "STATUS: SUCCESS - tudo implementado corretamente " + "Z" * 300
    subprocess_done = _asyncio.Event()

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        out = _json.dumps(
            {
                "is_error": False,
                "result": resultado,
                "session_id": "s",
                "total_cost_usd": 0.05,
                "duration_ms": 500,
                "num_turns": 3,
            }
        )
        result = claude_worker_module.SubprocessResult(0, out, "", 0.5)
        subprocess_done.set()
        return result

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "implement #3",
                "stage": "implement",
                "preferred_model": "anthropic:claude-sonnet-4-6",
                "wait_for_result": False,
            },
        )
        body = await resp.json()

    assert resp.status == 202
    task_id = body["task_id"]

    # Aguarda o subprocess terminar (com timeout de segurança).
    try:
        await _asyncio.wait_for(subprocess_done.wait(), timeout=5.0)
    except _asyncio.TimeoutError:
        pytest.fail("subprocess não terminou dentro do prazo esperado")

    # Aguarda a background task gravar os metadados finais.
    await _asyncio.sleep(0.1)

    meta_path = tmp_path / ".claude" / "tasks" / task_id / "session.json"
    meta = _json.loads(meta_path.read_text())

    assert (
        meta["last_completed_at"] is not None
    ), "last_completed_at deve ser gravado após conclusão do subprocess"
    assert meta["last_is_error"] is False
    assert (
        meta["last_result_full"] == resultado
    ), f"last_result_full incompleto: len={len(meta['last_result_full'])}"
    assert meta["last_result_summary"] == resultado[:300]


@pytest.mark.asyncio
async def test_dispatch_nowait_limpa_lease_ao_terminar(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """T2: após o subprocess terminar em background, o lease deve ser removido
    — sem lease órfão deixado no filesystem.
    """
    import asyncio as _asyncio
    import json as _json

    subprocess_done = _asyncio.Event()

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        out = _json.dumps(
            {
                "is_error": False,
                "result": "ok",
                "session_id": "s",
                "total_cost_usd": 0.0,
                "duration_ms": 1,
                "num_turns": 1,
            }
        )
        result = claude_worker_module.SubprocessResult(0, out, "", 0.01)
        subprocess_done.set()
        return result

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "check lease",
                "preferred_model": "anthropic:claude-haiku-4-5",
                "wait_for_result": False,
            },
        )
        body = await resp.json()

    assert resp.status == 202
    task_id = body["task_id"]
    workspace = tmp_path / "work" / task_id
    lease_path = workspace / ".lease.json"

    # Aguarda o subprocess terminar.
    try:
        await _asyncio.wait_for(subprocess_done.wait(), timeout=5.0)
    except _asyncio.TimeoutError:
        pytest.fail("subprocess não terminou dentro do prazo")

    # Aguarda o cleanup após o subprocess.
    await _asyncio.sleep(0.1)

    assert (
        not lease_path.exists()
    ), f"lease.json deve ser removido após conclusão do nowait; ainda existe: {lease_path}"


@pytest.mark.asyncio
async def test_dispatch_wait_true_mantem_comportamento_bloqueante(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """T2 retrocompat: ``wait_for_result=True`` (explícito) retorna 200 com
    resultado completo — comportamento original preservado.
    """
    import json as _json

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        out = _json.dumps(
            {
                "is_error": False,
                "result": "done",
                "session_id": "s",
                "total_cost_usd": 0.02,
                "duration_ms": 200,
                "num_turns": 2,
            }
        )
        return claude_worker_module.SubprocessResult(0, out, "", 0.2)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "implement #5",
                "preferred_model": "anthropic:claude-sonnet-4-6",
                "wait_for_result": True,  # explícito
            },
        )
        body = await resp.json()

    assert (
        resp.status == 200
    ), f"wait_for_result=True deve retornar 200, got {resp.status}"
    assert body["ok"] is True
    assert "task_id" in body
    assert body["returncode"] == 0


@pytest.mark.asyncio
async def test_dispatch_sem_wait_for_result_mantem_comportamento_bloqueante(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """T2 retrocompat: ausência de ``wait_for_result`` no payload (campo
    omitido) retorna 200 bloqueante — default compatível com clientes legados.
    """
    import json as _json

    async def fake_run(args, *, cwd, task_id, timeout, lease_path=None):
        out = _json.dumps(
            {
                "is_error": False,
                "result": "done",
                "session_id": "s",
                "total_cost_usd": 0.01,
                "duration_ms": 100,
                "num_turns": 1,
            }
        )
        return claude_worker_module.SubprocessResult(0, out, "", 0.1)

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = claude_worker_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "implement #6",
                "preferred_model": "anthropic:claude-sonnet-4-6",
                # wait_for_result ausente propositalmente.
            },
        )
        body = await resp.json()

    assert resp.status == 200, f"default deve ser 200 bloqueante, got {resp.status}"
    assert body["ok"] is True


# --- _estimate_context_tokens: pico de contexto, não soma (issue #445 FU) -----
def test_estimate_context_tokens_uses_peak_not_sum(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """Mede o CONTEXTO OCUPADO (pico de um round), não a soma dos rounds.

    Regressão direta do bug que promovia toda review longa a fresh: somar
    ``cache_read_input_tokens`` em 65 rounds gerava 11,5M tokens quando o
    contexto real era ~70K. Aqui 5 rounds relendo 50K de cache cada têm soma
    ~255K, mas o contexto real ocupado (pico de 1 round) é só 51K.
    """
    import json as _json

    mod = claude_worker_module
    jsonl = tmp_path / "sess.jsonl"
    lines = []
    for i in range(5):
        lines.append(
            _json.dumps(
                {
                    "type": "assistant",
                    "requestId": f"r{i}",
                    "message": {
                        "id": f"m{i}",
                        "role": "assistant",
                        "usage": {
                            "input_tokens": 1000,
                            "cache_read_input_tokens": 50000,
                            "cache_creation_input_tokens": 0,
                            "output_tokens": 800,
                        },
                    },
                }
            )
        )
    jsonl.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(mod, "_resolve_jsonl_path", lambda s, w: jsonl)

    got = mod._estimate_context_tokens("sess", tmp_path)
    assert got == 51000  # pico (1000 + 50000) de um único round, não 5x


def test_estimate_context_tokens_zero_when_missing(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """JSONL ausente → 0 (fallback conservador, não promove por falha de medir)."""
    mod = claude_worker_module
    monkeypatch.setattr(mod, "_resolve_jsonl_path", lambda s, w: None)
    assert mod._estimate_context_tokens("sess", tmp_path) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Issue #515 — versioned agent personalization: skill/CLAUDE.md injection
# ─────────────────────────────────────────────────────────────────────────────


_AGENTS_DIR_CW = (
    Path(__file__).resolve().parents[3] / "infra" / "k8s" / "agents" / "claude-worker"
)


def test_versioned_skill_file_exists_and_readable() -> None:
    """Issue #515 AC#1 — skill brainstorm deve existir na fonte versionada."""
    skill_path = _AGENTS_DIR_CW / "skills" / "brainstorm" / "SKILL.md"
    assert skill_path.is_file(), (
        f"Skill versionada ausente: {skill_path}. "
        "A fonte em infra/k8s/agents/ é a origem canônica injetada pelo initContainer "
        "(issue #515 AC#1)."
    )
    content = skill_path.read_text(encoding="utf-8")
    assert (
        len(content) > 100
    ), f"Skill {skill_path} parece vazia ou truncada ({len(content)} chars)."


def test_versioned_claude_md_exists_and_readable() -> None:
    """Issue #515 AC#2 — CLAUDE.md versionado deve existir na fonte."""
    claude_md = _AGENTS_DIR_CW / "CLAUDE.md"
    assert (
        claude_md.is_file()
    ), f"CLAUDE.md versionado ausente: {claude_md} (issue #515 AC#2)."
    content = claude_md.read_text(encoding="utf-8")
    assert len(content) > 50, f"CLAUDE.md parece vazio ({len(content)} chars)."


def test_versioned_command_plan_exists() -> None:
    """Issue #515 — command plan.md deve existir na fonte versionada."""
    cmd_path = _AGENTS_DIR_CW / "commands" / "plan.md"
    assert cmd_path.is_file(), f"Command plan.md ausente: {cmd_path} (issue #515)."


def test_initcontainer_script_uses_chmod_0644() -> None:
    """Issue #515 AC#1 — o script do initContainer inject-agents usa chmod 0644.

    Verifica o YAML do manifest diretamente (sem precisar de k8s em execução).
    O mode 0644 garante que o owner 10001:10001 (uid deile) pode ler/escrever
    mas outros não podem — exigência explícita do AC#1.
    """
    import yaml

    manifest_path = (
        Path(__file__).resolve().parents[3]
        / "infra"
        / "k8s"
        / "manifests"
        / "50-claude-worker-deployment.yaml"
    )
    assert manifest_path.is_file(), f"Manifest ausente: {manifest_path}"
    docs = list(yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")))
    deployment = next((d for d in docs if d and d.get("kind") == "Deployment"), None)
    assert deployment is not None, "Deployment não encontrado no manifest 50."
    init_containers = (deployment.get("spec", {}) or {}).get("template", {}).get(
        "spec", {}
    ).get("initContainers") or []
    inject = next(
        (c for c in init_containers if c.get("name") == "inject-agents"), None
    )
    assert inject is not None, "initContainer 'inject-agents' não encontrado."
    args = inject.get("args") or []
    script = " ".join(str(a) for a in args)
    assert "chmod 0644" in script, (
        "initContainer 'inject-agents' não chama 'chmod 0644'. "
        "AC#1 exige mode 0644 nos arquivos injetados (issue #515)."
    )


def test_initcontainer_script_fails_fast_on_missing_source() -> None:
    """Issue #515 AC#5 — o script do initContainer usa 'set -eu' (fail-fast).

    Se a fonte estiver ausente, o initContainer deve falhar imediatamente
    (não subir o pod meio-configurado silenciosamente).
    """
    import yaml

    manifest_path = (
        Path(__file__).resolve().parents[3]
        / "infra"
        / "k8s"
        / "manifests"
        / "50-claude-worker-deployment.yaml"
    )
    docs = list(yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")))
    deployment = next((d for d in docs if d and d.get("kind") == "Deployment"), None)
    assert deployment is not None
    init_containers = (deployment.get("spec", {}) or {}).get("template", {}).get(
        "spec", {}
    ).get("initContainers") or []
    inject = next(
        (c for c in init_containers if c.get("name") == "inject-agents"), None
    )
    assert inject is not None
    args = inject.get("args") or []
    script = " ".join(str(a) for a in args)
    assert "set -eu" in script, (
        "initContainer 'inject-agents' não usa 'set -eu'. "
        "AC#5 exige fail-fast quando a fonte estiver ausente (issue #515)."
    )
