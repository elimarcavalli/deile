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
    ``duration_seconds`` e ``returncode`` — contrato consumido pelo
    ``deile-pipeline`` e pelo painel TUI."""
    async def fake_run(args, *, cwd, task_id, timeout):
        return claude_worker_module.SubprocessResult(
            returncode=0, stdout="success\n", stderr="", duration_seconds=42.0,
        )

    monkeypatch.setattr(claude_worker_module, "run_subprocess_with_progress", fake_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))

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
    no build_app (server abort no startup pra forçar fix)."""
    monkeypatch.delenv("DEILE_CLAUDE_WORKER_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("DEILE_CLAUDE_WORKER_AUTH_TOKEN_FILE", raising=False)
    # _read_auth_token vai falhar (nenhum file existe nem env vars setadas).
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

    import types

    async def fake_run_subprocess(args, *, cwd, task_id, timeout):
        return types.SimpleNamespace(
            returncode=0,
            stdout="Review completed\nSTATUS: APPROVE\n",
            stderr="",
            duration_seconds=10.0,
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
        body = await resp.json()
        assert body["ok"] is True
        assert "error_code" not in body
