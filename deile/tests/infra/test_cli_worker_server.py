"""Testes do servidor genérico ``cli_worker_server`` (Fase 2 — framework).

Cobre o server agnóstico de CLI com um **adapter mock** registrado em runtime:
  - ``/v1/health`` reflete kind/auth_mode/ready (ready=False sem auth key).
  - ``/v1/models`` retorna o catálogo do adapter.
  - ``/v1/dispatch`` escreve o brief, roda o argv via core, aplica o gate de
    git (sem commit/push → NO_PUSH; com → ok).
  - ``/v1/progress`` devolve o tail persistido no PVC.
  - Bearer auth: paths protegidos exigem token; ``/v1/health`` é aberto.

O pacote ``cli_adapters`` e o módulo ``cli_worker_server`` vivem em
``infra/k8s/`` — path inserido manualmente (convenção dos testes de infra).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cli_adapters  # noqa: E402
import cli_worker_server as cws  # noqa: E402

_AUTH_HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def mock_adapter(tmp_path, monkeypatch):
    """Registra um adapter mock 'mock' e seleciona-o via env.

    O adapter escreve um arquivo no workdir e (opcionalmente) faz commit/push
    simulado controlado pelo monkeypatch dos helpers de git do server.
    """
    pkg_dir = Path(cli_adapters.__path__[0])
    mod_path = pkg_dir / "zzz_mock_worker.py"
    mod_path.write_text(textwrap.dedent('''\
        from cli_adapters.base import BaseCliAdapter, WorkResult, ModelInfo


        class MockAdapter(BaseCliAdapter):
            def build_argv(self, *, brief_path, model, reasoning, workdir, resume, task_id=""):
                # Comando trivial: cria um marcador no workdir e imprime OK.
                return ["sh", "-c",
                        f"echo dispatched model={model}; touch {workdir}/.ran"]

            def parse_output(self, *, stdout, stderr, rc):
                return WorkResult(ok=(rc == 0), result_text=stdout.strip()[:80])

            def env_overlay(self, *, home):
                # Declara um dir gravável sob o home — o server deve criá-lo
                # antes de rodar (regressão CODEX_HOME, fix #23).
                return {"MOCK_WRITABLE": f"{home}/mock-writable"}

            def list_models(self):
                return [
                    ModelInfo(id="openrouter/deepseek/deepseek-chat",
                              provider="openrouter", context=64000),
                ]


        ADAPTER = MockAdapter(
            kind="mock", default_port=8799, auth_env_keys=["MOCK_API_KEY"],
            writable_dirs=["HOME", "MOCK_WRITABLE"],
        )
    '''), encoding="utf-8")
    cli_adapters.reload_adapters()

    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "mock")
    monkeypatch.setenv("DEILE_CLI_WORKER_ROOT", str(tmp_path / "work"))
    # Disable real lease TTL waits: keep defaults (fast).
    try:
        yield
    finally:
        mod_path.unlink(missing_ok=True)
        sys.modules.pop("cli_adapters.zzz_mock_worker", None)
        cli_adapters.reload_adapters()
        cws._models_cache.clear()


async def test_health_not_ready_without_auth_key(mock_adapter, monkeypatch):
    monkeypatch.delenv("MOCK_API_KEY", raising=False)
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")
        assert resp.status == 503
        body = await resp.json()
        assert body["kind"] == "mock"
        assert body["auth_mode"] == "env"
        assert body["ready"] is False


async def test_health_ready_with_auth_key(mock_adapter, monkeypatch):
    monkeypatch.setenv("MOCK_API_KEY", "secret")
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")
        assert resp.status == 200
        assert (await resp.json())["ready"] is True


async def test_health_is_open_no_bearer_required(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")  # sem header
        assert resp.status in (200, 503)  # não 401


async def test_models_requires_bearer(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/models")  # sem header
        assert resp.status == 401


async def test_models_returns_adapter_catalog(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/models", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
        assert body["kind"] == "mock"
        ids = [m["id"] for m in body["models"]]
        assert "openrouter/deepseek/deepseek-chat" in ids


async def test_dispatch_missing_brief_returns_400(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch", json={"stage": "implement"}, headers=_AUTH_HEADERS,
        )
        assert resp.status == 400


async def test_dispatch_gate_fails_without_push(mock_adapter, monkeypatch):
    """Adapter retorna ok mas não há commit/push → gate reprova com NO_PUSH."""
    # Sem repo git no workdir → _git_head None, _git_branch_pushed False.
    monkeypatch.setattr(cws, "_git_head", _async_return(None))
    monkeypatch.setattr(cws, "_git_branch_pushed", _async_return(False))

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={"brief": "do the thing", "stage": "implement",
                  "branch": "auto/issue-1", "cli_model": "x"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is False
        assert body["error_code"] == "NO_PUSH"
        assert body["task_id"]


async def test_dispatch_success_with_commit_and_push(mock_adapter, monkeypatch):
    """Adapter ok + commit novo + push confirmado → ok=True."""
    seq = {"head": ["base-sha", "new-sha"]}

    async def _fake_head(_workdir):
        # 1ª chamada (base) = base-sha; 2ª (pós-run) = new-sha.
        return seq["head"].pop(0) if seq["head"] else "new-sha"

    async def _fake_pushed(_workdir, _branch):
        return True

    monkeypatch.setattr(cws, "_git_head", _fake_head)
    monkeypatch.setattr(cws, "_git_branch_pushed", _fake_pushed)

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={"brief": "do the thing", "stage": "implement",
                  "branch": "auto/issue-1", "cli_model": "x"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert body["returncode"] == 0
        assert "dispatched model=x" in body["result"]


async def test_dispatch_writes_brief_file(mock_adapter, monkeypatch, tmp_path):
    """O brief é gravado em <workdir>/.brief.md antes do build_argv."""
    monkeypatch.setattr(cws, "_git_head", _async_return("h"))
    monkeypatch.setattr(cws, "_git_branch_pushed", _async_return(True))

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={"brief": "BRIEF-CONTENT-MARKER", "branch": "b"},
            headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    task_id = body["task_id"]
    brief = tmp_path / "work" / task_id / ".brief.md"
    assert brief.is_file()
    assert brief.read_text() == "BRIEF-CONTENT-MARKER"


@pytest.fixture
def mock_adapter_auth_fail(tmp_path, monkeypatch):
    """Mock adapter cujo ``provision_auth`` reprova — testa o gate (Frente 4)."""
    pkg_dir = Path(cli_adapters.__path__[0])
    mod_path = pkg_dir / "zzz_mock_authfail.py"
    mod_path.write_text(textwrap.dedent('''\
        from cli_adapters.base import BaseCliAdapter, WorkResult, ModelInfo


        class MockAuthFailAdapter(BaseCliAdapter):
            def build_argv(self, *, brief_path, model, reasoning, workdir, resume, task_id=""):
                return ["sh", "-c", f"touch {workdir}/.ran"]

            def parse_output(self, *, stdout, stderr, rc):
                return WorkResult(ok=True, result_text="ran")

            def list_models(self):
                return [ModelInfo(id="x", provider="openai", auth="chatgpt")]

            def provision_auth(self, *, model, home, env):
                return False, "OAuth ausente — rode codex-login"


        ADAPTER = MockAuthFailAdapter(
            kind="mockauthfail", default_port=8798,
            auth_env_keys=["MOCK_API_KEY"], writable_dirs=["HOME"],
        )
    '''), encoding="utf-8")
    cli_adapters.reload_adapters()
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "mockauthfail")
    monkeypatch.setenv("DEILE_CLI_WORKER_ROOT", str(tmp_path / "work"))
    try:
        yield
    finally:
        mod_path.unlink(missing_ok=True)
        sys.modules.pop("cli_adapters.zzz_mock_authfail", None)
        cli_adapters.reload_adapters()
        cws._models_cache.clear()


async def test_dispatch_aborts_when_provision_auth_fails(mock_adapter_auth_fail):
    """provision_auth reprova → dispatch retorna WORKER_AUTH_EXPIRED, não roda."""
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={"brief": "x", "branch": "b", "cli_model": "x"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is False
        assert body["error_code"] == "WORKER_AUTH_EXPIRED"
        assert "OAuth" in body["error"]


async def test_dispatch_proceeds_when_provision_auth_ok(mock_adapter, monkeypatch):
    """Adapter sem provision_auth custom (no-op base) → dispatch segue + roda."""
    seq = {"head": ["base-sha", "new-sha"]}

    async def _fake_head(_workdir):
        return seq["head"].pop(0) if seq["head"] else "new-sha"

    monkeypatch.setattr(cws, "_git_head", _fake_head)
    monkeypatch.setattr(cws, "_git_branch_pushed", _async_return(True))
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={"brief": "x", "stage": "implement",
                  "branch": "auto/issue-1", "cli_model": "x"},
            headers=_AUTH_HEADERS,
        )
        body = await resp.json()
        assert body["ok"] is True


async def test_progress_404_for_unknown_task(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/progress/" + "a" * 16, headers=_AUTH_HEADERS,
        )
        assert resp.status == 404


async def test_progress_400_for_invalid_task_id(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/progress/../etc", headers=_AUTH_HEADERS,
        )
        assert resp.status in (400, 404)  # traversal barrado


# --------------------------------------------------------------------------- #
# /v1/dispatches/{task_id}/resume-info — LIVENESS (anti-double-dispatch)
# --------------------------------------------------------------------------- #


async def test_resume_info_400_invalid_task_id(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/dispatches/NOThex/resume-info", headers=_AUTH_HEADERS,
        )
        assert resp.status == 400


async def test_resume_info_404_when_no_workspace(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/dispatches/0123456789abcdef/resume-info", headers=_AUTH_HEADERS,
        )
        assert resp.status == 404


async def test_resume_info_alive_true_when_lease_fresh(mock_adapter):
    """Lease com heartbeat fresco → ``claude_alive=True`` → o pipeline NÃO
    re-despacha (impede o double-dispatch enquanto o subprocess roda)."""
    import json
    import os
    import time

    task_id = "0123456789abcdef"
    ws = cws._worker_root() / task_id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / ".lease.json").write_text(json.dumps({
        "pid": os.getpid(), "heartbeat_at": time.time(), "pod": "test",
    }))
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            f"/v1/dispatches/{task_id}/resume-info", headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["workdir_exists"] is True
        assert body["claude_alive"] is True
        assert body["session_id"] == ""  # cli workers não retomam sessão


async def test_resume_info_alive_false_when_no_lease(mock_adapter):
    """Workspace existe mas sem lease (task terminou) → ``claude_alive=False``
    → o pipeline cai em fresh (retry limitado pelo teto)."""
    task_id = "fedcba9876543210"
    ws = cws._worker_root() / task_id
    ws.mkdir(parents=True, exist_ok=True)
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            f"/v1/dispatches/{task_id}/resume-info", headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["claude_alive"] is False


async def test_resume_info_requires_bearer(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/dispatches/0123456789abcdef/resume-info")
        assert resp.status == 401


async def test_resume_info_returns_persisted_verdict_when_done(mock_adapter):
    """Task concluída → resume-info traz ``last_completed_at`` + ``last_result_full``
    (sem lease vivo). É o que o reconcile do pipeline lê para detectar DONE e
    parsear o veredito de crítica/refine — sem isto a issue ficaria RUNNING eterno."""
    from cli_adapters.base import WorkResult

    task_id = "abcabcabc1234567"
    ws = cws._worker_root() / task_id
    ws.mkdir(parents=True, exist_ok=True)  # workdir existe, sem lease (concluída)
    cws._save_task_result(
        task_id, WorkResult(ok=True, result_text="VEREDITO: CLARO\nescopo nítido"),
    )
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            f"/v1/dispatches/{task_id}/resume-info", headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["claude_alive"] is False          # lease ausente → não-vivo
        assert body["last_completed_at"] is not None    # → reconcile lê DONE
        assert body["last_is_error"] is False
        assert "CLARO" in body["last_result_full"]      # → parse_critique_verdict


async def test_dispatch_creates_adapter_writable_dirs(
    mock_adapter, monkeypatch, tmp_path,
):
    """Regressão #23 (CODEX_HOME): o server cria os ``writable_dirs`` do adapter
    (resolvidos do env_overlay) ANTES de rodar — senão o CLI aborta (ex.: codex
    "CODEX_HOME ... does not exist"). O dir é criado mesmo que o gate de git
    reprove depois."""
    import os
    monkeypatch.setenv("MOCK_API_KEY", "secret")
    # Home gravável (no pod é o volume /home/<kind>; no teste, um tmp).
    monkeypatch.setenv("DEILE_CLI_WORKER_HOME", str(tmp_path / "home"))
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch", headers=_AUTH_HEADERS,
            json={"brief": "x", "wait_for_result": True},
        )
        assert resp.status == 200  # 200 mesmo em NO_PUSH (contrato do worker)
    mw = os.environ.get("MOCK_WRITABLE", "")
    assert mw and Path(mw).is_dir(), f"writable dir não criado: {mw!r}"


def test_resolve_adapter_requires_kind(monkeypatch):
    monkeypatch.delenv("DEILE_CLI_WORKER_KIND", raising=False)
    with pytest.raises(RuntimeError):
        cws._resolve_adapter()


# --------------------------------------------------------------------------- #
# Resume nativo end-to-end (issue #445 — anti-sangria de custo)
# --------------------------------------------------------------------------- #


@pytest.fixture
def mock_resume_adapter(tmp_path, monkeypatch):
    """Adapter mock com ``supports_resume=True`` que registra o argv num arquivo.

    O ``build_argv`` grava o argv lógico em ``<workdir>/.argv.json`` (para o
    teste inspecionar resume flags) e cria um marcador. ``extract_session_id``
    devolve um id fixo ``ses_mock`` (simula a captura do id nativo do CLI).
    """
    pkg_dir = Path(cli_adapters.__path__[0])
    mod_path = pkg_dir / "zzz_mock_resume.py"
    mod_path.write_text(textwrap.dedent('''\
        from cli_adapters.base import BaseCliAdapter, WorkResult, ModelInfo


        class MockResumeAdapter(BaseCliAdapter):
            def build_argv(self, *, brief_path, model, reasoning, workdir,
                           resume, task_id=""):
                import json
                flag = []
                if resume is not None and resume.session_id:
                    flag = ["--session", resume.session_id]
                argv = ["sh", "-c", "touch " + workdir + "/.ran"]
                try:
                    with open(workdir + "/.argv.json", "w") as fh:
                        json.dump({"resume_flag": flag, "task_id": task_id,
                                   "session": resume.session_id if resume else ""}, fh)
                except OSError:
                    pass
                return argv

            def parse_output(self, *, stdout, stderr, rc):
                import _worker_core as _core
                code = _core.classify_provider_error(stdout + "\\n" + stderr)
                if code:
                    return WorkResult(ok=False, result_text=stdout.strip()[:200],
                                      error_code=code)
                return WorkResult(ok=(rc == 0), result_text=stdout.strip()[:200])

            def extract_session_id(self, *, stdout, stderr, task_id):
                return "ses_mock"

            def list_models(self):
                return [ModelInfo(id="m", provider="openrouter")]


        ADAPTER = MockResumeAdapter(
            kind="mockresume", default_port=8797, supports_resume=True,
            auth_env_keys=["MOCK_API_KEY"], writable_dirs=["HOME"],
        )
    '''), encoding="utf-8")
    cli_adapters.reload_adapters()
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "mockresume")
    monkeypatch.setenv("DEILE_CLI_WORKER_ROOT", str(tmp_path / "work"))
    try:
        yield tmp_path / "work"
    finally:
        mod_path.unlink(missing_ok=True)
        sys.modules.pop("cli_adapters.zzz_mock_resume", None)
        cli_adapters.reload_adapters()
        cws._models_cache.clear()


def _commit_pushed_git(monkeypatch):
    """Faz o gate de git passar: cada chamada a HEAD devolve um sha único (logo
    base_sha != head pós-run → commit novo), push confirmado, e ``_finalize_git``
    é um no-op (não toca git real)."""
    counter = {"n": 0}

    async def _fake_head(_workdir):
        counter["n"] += 1
        return f"sha-{counter['n']}"

    async def _passthrough_finalize(adapter, work, **_kw):
        return work

    monkeypatch.setattr(cws, "_git_head", _fake_head)
    monkeypatch.setattr(cws, "_git_branch_pushed", _async_return(True))
    monkeypatch.setattr(cws, "_finalize_git", _passthrough_finalize)


async def test_fresh_dispatch_persists_session_id(mock_resume_adapter, monkeypatch):
    """Fresh dispatch captura o session-id nativo e o devolve em resume-info."""
    _commit_pushed_git(monkeypatch)
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch", headers=_AUTH_HEADERS,
            json={"brief": "do it", "branch": "auto/issue-1", "cli_model": "m"},
        )
        body = await resp.json()
        assert body["ok"] is True
        assert body["session_id"] == "ses_mock"
        task_id = body["task_id"]

        ri = await client.get(
            f"/v1/dispatches/{task_id}/resume-info", headers=_AUTH_HEADERS,
        )
        ri_body = await ri.json()
        assert ri_body["session_id"] == "ses_mock"
        assert ri_body["attempt"] == 1
        assert ri_body["claude_alive"] is False


async def test_provider_error_is_not_clean_completion(
    mock_resume_adapter, monkeypatch,
):
    """402 mid-task -> ok=False + error_code, NUNCA last_is_error=False (bug #629)."""
    monkeypatch.setattr(cws, "_git_head", _async_return("h"))
    monkeypatch.setattr(cws, "_git_branch_pushed", _async_return(True))
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        adapter = cws._resolve_adapter()

        def _argv_402(**kw):
            wd = kw["workdir"]
            return ["sh", "-c",
                    f"echo 'Error: 402 Payment Required insufficient credit'; "
                    f"touch {wd}/.ran"]

        monkeypatch.setattr(adapter, "build_argv", _argv_402)
        resp = await client.post(
            "/v1/dispatch", headers=_AUTH_HEADERS,
            json={"brief": "x", "branch": "auto/issue-1", "cli_model": "m"},
        )
        body = await resp.json()
        assert body["ok"] is False
        assert body["error_code"] == "INSUFFICIENT_CREDIT"
        assert body["is_error"] is True

        ri = await client.get(
            f"/v1/dispatches/{body['task_id']}/resume-info", headers=_AUTH_HEADERS,
        )
        ri_body = await ri.json()
        assert ri_body["last_is_error"] is True
        assert ri_body["last_error_code"] == "INSUFFICIENT_CREDIT"


async def test_resume_reuses_workdir_and_passes_session(
    mock_resume_adapter, monkeypatch,
):
    """Re-dispatch com resume REUSA o workdir e passa --session no argv."""
    import json
    _commit_pushed_git(monkeypatch)
    root = mock_resume_adapter
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post(
            "/v1/dispatch", headers=_AUTH_HEADERS,
            json={"brief": "passo 1", "branch": "auto/issue-1", "cli_model": "m"},
        )
        b1 = await r1.json()
        task_id = b1["task_id"]
        workdir = root / task_id
        assert workdir.is_dir()
        (workdir / ".witness").write_text("eu-sobrevivi")

        r2 = await client.post(
            "/v1/dispatch", headers=_AUTH_HEADERS,
            json={"brief": "continue", "branch": "auto/issue-1", "cli_model": "m",
                  "resume_session_id": "ses_mock", "prev_task_id": task_id},
        )
        b2 = await r2.json()
        assert b2["task_id"] == task_id
        assert (workdir / ".witness").read_text() == "eu-sobrevivi"
        argv_meta = json.loads((workdir / ".argv.json").read_text())
        assert argv_meta["session"] == "ses_mock"
        assert argv_meta["resume_flag"] == ["--session", "ses_mock"]

        ri = await client.get(
            f"/v1/dispatches/{task_id}/resume-info", headers=_AUTH_HEADERS,
        )
        assert (await ri.json())["attempt"] == 2


async def test_resume_with_missing_workdir_degrades_to_fresh(
    mock_resume_adapter, monkeypatch, caplog,
):
    """prev_task_id cujo workdir sumiu -> degrada para fresh (novo task_id)
    + emite warning observável do re-gasto (FIX D)."""
    _commit_pushed_git(monkeypatch)
    app = cws.build_app(auth_token="test-token")
    with caplog.at_level("WARNING", logger="deile.cli_worker"):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/dispatch", headers=_AUTH_HEADERS,
                json={"brief": "x", "branch": "auto/issue-1", "cli_model": "m",
                      "resume_session_id": "ses_mock",
                      "prev_task_id": "0000000000000000"},
            )
            body = await resp.json()
            assert body["task_id"] != "0000000000000000"
            assert body["ok"] is True
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "degradando para FRESH" in m and "0000000000000000" in m
        for m in warnings
    ), f"warning de degrade resume→fresh ausente: {warnings}"



# --------------------------------------------------------------------------- #
# Modelo durável no meta (anti model=unknown — issue #445)
# --------------------------------------------------------------------------- #


def test_save_task_result_persists_cli_model(mock_adapter):
    """O ``cli_model`` recebido no dispatch é persistido no meta — fonte de verdade
    do modelo para a auditoria (vários CLIs não emitem o modelo no stdout)."""
    from cli_adapters.base import WorkResult

    task_id = "deadbeefdeadbeef"
    cws._save_task_result(
        task_id, WorkResult(ok=True, result_text="done"),
        cli_model="openrouter/deepseek/deepseek-v4-pro",
    )
    meta = cws._load_task_result(task_id)
    assert meta is not None
    assert meta["cli_model"] == "openrouter/deepseek/deepseek-v4-pro"


def test_save_task_result_cli_model_defaults_empty(mock_adapter):
    """Sem ``cli_model`` (CLI decide) → campo vazio, não quebra o meta."""
    from cli_adapters.base import WorkResult

    cws._save_task_result("0011223344556677", WorkResult(ok=True, result_text="x"))
    meta = cws._load_task_result("0011223344556677")
    assert meta["cli_model"] == ""


# --------------------------------------------------------------------------- #
# Ledger de custo durável da frota (issue #445)
# --------------------------------------------------------------------------- #


def _write_progress_log(root, task_id, lines):
    pdir = root / ".progress"
    pdir.mkdir(parents=True, exist_ok=True)
    text = lines if isinstance(lines, str) else "\n".join(lines)
    p = pdir / f"{task_id}.stdout.log"
    p.write_text(text, encoding="utf-8")
    return p


def test_harvest_appends_cost_then_prunes_old_log(mock_adapter, monkeypatch):
    """O harvester colhe os tokens do .progress para o ledger ANTES de podar o log
    velho — espelha o claude-worker (#445)."""
    import json as _json
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "opencode")
    root = cws._worker_root()
    log = _write_progress_log(root, "aabbccdd00112233", [
        _json.dumps({"type": "step_finish", "modelID": "deepseek/deepseek-v4-pro",
                     "part": {"tokens": {"input": 1500, "output": 300,
                                         "cache": {"read": 1000, "write": 50}},
                              "cost": 0.012}}),
    ])
    # Loga como antigo (além da retenção e do grace).
    old = __import__("time").time() - 60 * 86400
    import os as _os
    _os.utime(log, (old, old))

    res = cws.harvest_progress_to_ledger(root, "opencode")
    assert res["sessions_harvested"] == 1
    assert res["logs_removed"] == 1
    assert not log.exists()  # log podado após colheita

    ledger = cws._cost_ledger_path()
    assert ledger.exists()
    recs = [_json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]
    assert len(recs) == 1
    r = recs[0]
    assert r["task_id"] == "aabbccdd00112233" and r["worker"] == "opencode"
    m = r["models"]["deepseek/deepseek-v4-pro"]
    assert m["in"] == 1500 and m["out"] == 300 and m["cr"] == 1000


def test_harvest_dedup_by_task_id_idempotent(mock_adapter, monkeypatch):
    """Rodar o harvest duas vezes não duplica o registro do mesmo task_id."""
    import json as _json
    import os as _os
    import time as _time
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "opencode")
    root = cws._worker_root()
    log = _write_progress_log(root, "1122334455667788", [
        _json.dumps({"type": "step_finish", "modelID": "qwen3-coder-plus",
                     "part": {"tokens": {"input": 100, "output": 20}, "cost": 0.0}}),
    ])
    old = _time.time() - 60 * 86400
    _os.utime(log, (old, old))
    cws.harvest_progress_to_ledger(root, "opencode")
    # Recria o log (mesmo task_id) e roda de novo — não deve re-anexar.
    log2 = _write_progress_log(root, "1122334455667788", [
        _json.dumps({"type": "step_finish", "modelID": "qwen3-coder-plus",
                     "part": {"tokens": {"input": 100, "output": 20}, "cost": 0.0}}),
    ])
    _os.utime(log2, (old, old))
    res2 = cws.harvest_progress_to_ledger(root, "opencode")
    assert res2["sessions_harvested"] == 0  # já no ledger
    ledger = cws._cost_ledger_path()
    recs = [ln for ln in ledger.read_text().splitlines() if ln.strip()]
    assert len(recs) == 1  # sem duplicata


def test_harvest_preserves_recent_log_within_grace(mock_adapter, monkeypatch):
    """Log recém-modificado (dentro do grace TOCTOU) NÃO é colhido nem podado."""
    import json as _json
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "opencode")
    root = cws._worker_root()
    log = _write_progress_log(root, "99aabbccddeeff00", [
        _json.dumps({"type": "step_finish", "modelID": "x/y",
                     "part": {"tokens": {"input": 10, "output": 5}, "cost": 0.0}}),
    ])
    res = cws.harvest_progress_to_ledger(root, "opencode")  # mtime = agora
    assert res["sessions_harvested"] == 0 and res["logs_removed"] == 0
    assert log.exists()  # preservado (resume agendado pode precisar)


def test_harvest_uses_meta_model_for_unknown(mock_adapter, monkeypatch):
    """Quando o stdout não emite o modelo (goose), o harvester usa o ``cli_model``
    do meta — não grava ``unknown`` no ledger."""
    import json as _json
    import os as _os
    import time as _time
    from cli_adapters.base import WorkResult
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "goose")
    root = cws._worker_root()
    task_id = "f0f0f0f0f0f0f0f0"
    # goose só emite total_tokens, sem modelo no metadata.
    log = _write_progress_log(root, task_id, _json.dumps({
        "messages": [], "metadata": {"total_tokens": 4000, "status": "completed"}}))
    old = _time.time() - 60 * 86400
    _os.utime(log, (old, old))
    # Meta com o cli_model (gravado no dispatch).
    cws._save_task_result(task_id, WorkResult(ok=True, result_text="ok"),
                          cli_model="deepseek/deepseek-v4-flash")
    cws.harvest_progress_to_ledger(root, "goose")
    ledger = cws._cost_ledger_path()
    rec = _json.loads(ledger.read_text().splitlines()[0])
    assert "unknown" not in rec["models"]
    assert "deepseek/deepseek-v4-flash" in rec["models"]


def test_harvest_failsafe_aborts_without_parser(mock_adapter, monkeypatch):
    """Sem o parser (fleet_progress_parse ausente da imagem) o harvest NÃO poda —
    fail-safe cardinal: nunca deletar custo não colhido (#445)."""
    import json as _json
    import os as _os
    import time as _time
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "opencode")
    monkeypatch.setattr(cws, "_fpp", None)
    root = cws._worker_root()
    log = _write_progress_log(root, "cafecafecafecafe", [
        _json.dumps({"type": "step_finish", "modelID": "x/y",
                     "part": {"tokens": {"input": 10, "output": 5}, "cost": 0.0}}),
    ])
    old = _time.time() - 60 * 86400
    _os.utime(log, (old, old))
    res = cws.harvest_progress_to_ledger(root, "opencode")
    assert res["logs_removed"] == 0
    assert log.exists()  # preservado
    assert any("indisponível" in e for e in res["errors"])


def test_run_cleanup_invokes_harvest(mock_adapter, monkeypatch):
    """``run_cleanup`` encadeia o harvest do ledger após o cleanup de workdirs."""
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "opencode")
    res = cws.run_cleanup()
    assert "cost_ledger" in res  # harvest sempre roda (best-effort)


# --------------------------------------------------------------------------- #
# Bloco de uso estruturado para o store central (issue #638)
# --------------------------------------------------------------------------- #


def test_build_usage_block_opencode_shape(mock_adapter, monkeypatch):
    """``build_usage_block`` extrai tokens-por-modelo do shape nativo via o parser
    ÚNICO (fleet_progress_parse) e normaliza p/ cache_read/cache_write (#638)."""
    import json as _json
    stdout = "\n".join([
        _json.dumps({"type": "step_start", "modelID": "deepseek/deepseek-v4-pro"}),
        _json.dumps({"type": "step_finish", "part": {
            "cost": 0.012,
            "tokens": {"input": 1500, "output": 300,
                       "cache": {"read": 21415, "write": 100}}}}),
    ])
    tbm, model = cws.build_usage_block(
        kind="opencode", stdout=stdout, task_id="t1",
        cli_model="deepseek/deepseek-v4-pro",
    )
    assert model == "deepseek/deepseek-v4-pro"
    assert tbm == {"deepseek/deepseek-v4-pro": {
        "in": 1500, "out": 300, "cache_read": 21415, "cache_write": 100}}


def test_build_usage_block_remaps_unknown_to_cli_model(mock_adapter):
    """goose só emite total_tokens (sem modelo) → ``unknown`` é remapeado para o
    ``cli_model`` do payload (anti model=unknown)."""
    import json as _json
    stdout = _json.dumps({"messages": [],
                          "metadata": {"total_tokens": 8000, "status": "completed"}})
    tbm, model = cws.build_usage_block(
        kind="goose", stdout=stdout, task_id="t2",
        cli_model="deepseek/deepseek-v4-flash",
    )
    assert "unknown" not in tbm
    assert "deepseek/deepseek-v4-flash" in tbm
    assert model == "deepseek/deepseek-v4-flash"


def test_build_usage_block_noop_for_non_progress_kind(mock_adapter):
    """Kinds sem parser de .progress (claude/deile/mock) → bloco vazio + cli_model."""
    tbm, model = cws.build_usage_block(
        kind="claude", stdout="irrelevante", task_id="t3", cli_model="claude:sonnet",
    )
    assert tbm == {} and model == "claude:sonnet"


async def test_dispatch_usage_block_present(mock_resume_adapter, monkeypatch):
    """A resposta do /v1/dispatch + resume-info carregam o bloco ``usage`` (#638)."""
    import json as _json
    _commit_pushed_git(monkeypatch)
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "opencode")
    cli_adapters.reload_adapters()
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        adapter = cws._resolve_adapter()

        def _argv(**kw):
            wd = kw["workdir"]
            modeline = _json.dumps({"type": "step_start",
                                    "modelID": "openrouter/deepseek/deepseek-v4-pro"})
            event = _json.dumps({"type": "step_finish", "part": {
                "tokens": {"input": 1200, "output": 250,
                           "cache": {"read": 0, "write": 0}}}})
            return ["sh", "-c",
                    f"printf '%s\\n%s\\n' '{modeline}' '{event}'; touch {wd}/.ran"]

        monkeypatch.setattr(adapter, "build_argv", _argv)
        resp = await client.post(
            "/v1/dispatch", headers=_AUTH_HEADERS,
            json={"brief": "x", "branch": "auto/issue-1",
                  "cli_model": "openrouter/deepseek/deepseek-v4-pro"},
        )
        body = await resp.json()
        usage = body["usage"]
        assert usage["worker"] == "opencode"
        assert usage["model"] == "openrouter/deepseek/deepseek-v4-pro"
        tbm = usage["tokens_by_model"]["openrouter/deepseek/deepseek-v4-pro"]
        assert tbm["in"] == 1200 and tbm["out"] == 250

        # resume-info também surface o bloco usage (para o read-back fire-and-forget).
        ri = await client.get(
            f"/v1/dispatches/{body['task_id']}/resume-info", headers=_AUTH_HEADERS,
        )
        ri_body = await ri.json()
        assert ri_body["usage"]["model"] == "openrouter/deepseek/deepseek-v4-pro"
        assert ri_body["cli_model"] == "openrouter/deepseek/deepseek-v4-pro"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _async_return(value):
    """Factory de coroutine que ignora args e retorna *value*."""
    async def _coro(*_a, **_kw):
        return value
    return _coro


# ---------------------------------------------------------------------------
# Fix #3 — log NÃO deletado quando has_tokens=False e not in harvested (bug #779)
# ---------------------------------------------------------------------------

def test_harvest_preserves_log_when_no_tokens(mock_adapter, monkeypatch, tmp_path):
    """AC-3a: log permanece quando has_tokens=False e task_id nunca colhido."""
    import json as _json
    import os as _os
    import time as _time
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "opencode")
    root = cws._worker_root()

    # Log sem tokens (tudo zerado)
    task_id = "deadbeef00000001"
    log = _write_progress_log(root, task_id, [
        _json.dumps({"type": "step_finish", "modelID": "openrouter/qwen/qwen3-coder",
                     "part": {"tokens": {"input": 0, "output": 0}, "cost": 0.0}}),
    ])
    old = _time.time() - 60 * 86400
    _os.utime(log, (old, old))

    res = cws.harvest_progress_to_ledger(root, "opencode")

    # AC-3a: log deve permanecer
    assert log.exists(), "log deve ser preservado quando has_tokens=False e não colhido"
    # AC-3a: ledger não deve ter entrada
    ledger = cws._cost_ledger_path()
    if ledger.exists():
        recs = [_json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]
        assert all(r["task_id"] != task_id for r in recs), (
            "ledger não deve ter entrada para task sem tokens"
        )


def test_harvest_removes_log_when_already_harvested(mock_adapter, monkeypatch):
    """AC-3b: log é removido quando task_id já está no harvested (ciclo anterior)."""
    import json as _json
    import os as _os
    import time as _time
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "opencode")
    root = cws._worker_root()

    task_id = "deadbeef00000002"

    # Primeiro ciclo: colhe com tokens reais
    log1 = _write_progress_log(root, task_id, [
        _json.dumps({"type": "step_finish", "modelID": "deepseek/deepseek-v4-pro",
                     "part": {"tokens": {"input": 100, "output": 10}, "cost": 0.001}}),
    ])
    old = _time.time() - 60 * 86400
    _os.utime(log1, (old, old))
    cws.harvest_progress_to_ledger(root, "opencode")
    assert not log1.exists(), "log deve ter sido removido no primeiro ciclo"

    # Segundo ciclo: mesmo task_id sem tokens — mas já está no harvested via ledger
    log2 = _write_progress_log(root, task_id, [
        _json.dumps({"type": "step_finish", "modelID": "deepseek/deepseek-v4-pro",
                     "part": {"tokens": {"input": 0, "output": 0}, "cost": 0.0}}),
    ])
    _os.utime(log2, (old, old))
    res2 = cws.harvest_progress_to_ledger(root, "opencode")

    # AC-3b: como o task_id já estava no harvested (ledger), o log deve ser removido
    assert not log2.exists(), "log deve ser removido quando task_id já contabilizado"
