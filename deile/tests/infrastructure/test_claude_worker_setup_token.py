"""Testes para a migração de auth do claude-worker para setup-token (issue #603).

Cobre:
1. _assert_no_bare_in_argv: guard que impede --bare no argv do claude -p.
2. CLAUDE_CODE_OAUTH_TOKEN: verificação no startup do server.
3. setup_token_claude_worker: função de bootstrap com token de 1 ano.
4. _kubectl_apply_oauth_token_secret: aplicação do novo formato de Secret.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"


@pytest.fixture
def cws_mod():
    """Carrega claude_worker_server dinamicamente sem import pesado de aiohttp."""
    sys.path.insert(0, str(INFRA_K8S))
    try:
        # Stub mínimos para não precisar de aiohttp instalado.
        if "aiohttp" not in sys.modules:
            fake_web = MagicMock()
            sys.modules["aiohttp"] = MagicMock(web=fake_web)
            sys.modules["aiohttp.web"] = fake_web
        if "_worker_core" not in sys.modules:
            wc = MagicMock()
            wc.TASK_ID_RE = __import__("re").compile(r"^[0-9a-f]{16}$")
            wc.acquire_lease = MagicMock()
            wc.release_lease = MagicMock()
            wc.heartbeat_loop = MagicMock()
            wc.pid_alive = MagicMock(return_value=False)
            wc.dir_bytes = MagicMock(return_value=0)
            wc.validate_task_id_for_path = MagicMock(return_value=True)
            wc.update_lease_subprocess_pid = MagicMock()
            wc.lease_is_stale = MagicMock(return_value=True)
            wc.SubprocessResult = MagicMock
            wc.run_subprocess_with_progress = MagicMock()
            wc.RateLimiter = MagicMock()
            wc.make_bearer_auth_mw = MagicMock(return_value=MagicMock())
            sys.modules["_worker_core"] = wc
        if "dispatch_logger" not in sys.modules:
            dl = MagicMock()
            sys.modules["dispatch_logger"] = dl
        import claude_worker_server as m
        importlib.reload(m)
        yield m
    finally:
        sys.path.remove(str(INFRA_K8S))


@pytest.fixture
def claude_install_mod():
    """Carrega _claude_install dinamicamente."""
    sys.path.insert(0, str(INFRA_K8S))
    try:
        import _claude_install
        importlib.reload(_claude_install)
        yield _claude_install
    finally:
        if str(INFRA_K8S) in sys.path:
            sys.path.remove(str(INFRA_K8S))


# ---------------------------------------------------------------------------
# _assert_no_bare_in_argv
# ---------------------------------------------------------------------------


def test_assert_no_bare_raises_when_bare_present(cws_mod):
    """--bare no argv → RuntimeError (bare mode não lê CLAUDE_CODE_OAUTH_TOKEN)."""
    with pytest.raises(RuntimeError, match="--bare"):
        cws_mod._assert_no_bare_in_argv(["claude", "-p", "--bare", "prompt"])


def test_assert_no_bare_ok_without_bare(cws_mod):
    """argv sem --bare → sem exceção (caminho normal)."""
    cws_mod._assert_no_bare_in_argv([
        "claude", "-p",
        "--permission-mode", "bypassPermissions",
        "--output-format", "json",
        "do something",
    ])


def test_assert_no_bare_empty_argv(cws_mod):
    """argv vazio → sem exceção (defensivo)."""
    cws_mod._assert_no_bare_in_argv([])


def test_assert_no_bare_similar_flags_ok(cws_mod):
    """Flags que CONTÊM 'bare' mas não são '--bare' → não devem disparar."""
    cws_mod._assert_no_bare_in_argv(["claude", "-p", "--bare-metal-flag", "prompt"])


def test_canonical_dispatch_argv_has_no_bare(cws_mod):
    """O argv canônico do dispatch (espelho de ``dispatch_handler``) NÃO contém
    ``--bare`` e passa pelo guard sem erro.

    Bare mode não lê ``CLAUDE_CODE_OAUTH_TOKEN`` (doc Anthropic), então o
    ``claude -p`` do dispatch jamais pode receber ``--bare`` — caso contrário
    a auth por setup-token (issue #603) seria silenciosamente ignorada.
    """
    cmd = [
        "claude", "-p",
        "--permission-mode", "bypassPermissions",
        "--output-format", "json",
        "-r", "0123456789abcdef",
        "--model", "claude-opus-4-8",
        "--effort", "high",
        "--max-budget-usd", "8",
        "prompt do dispatch",
    ]
    assert "--bare" not in cmd
    cws_mod._assert_no_bare_in_argv(cmd)


def test_dispatch_handler_invokes_bare_guard(cws_mod):
    """O ``dispatch_handler`` chama ``_assert_no_bare_in_argv(cmd)`` após montar
    o argv — garante que o guard está REALMENTE wired no caminho de dispatch,
    não apenas testável em isolamento."""
    source = Path(cws_mod.__file__).read_text(encoding="utf-8")
    assert "_assert_no_bare_in_argv(cmd)" in source, (
        "o guard anti-`--bare` precisa ser invocado sobre o cmd montado no "
        "dispatch (issue #603)"
    )


# ---------------------------------------------------------------------------
# Verificação do CLAUDE_CODE_OAUTH_TOKEN no startup
# ---------------------------------------------------------------------------


def test_main_logs_warning_when_oauth_token_absent(cws_mod, monkeypatch, tmp_path):
    """Sem CLAUDE_CODE_OAUTH_TOKEN → warning logado no startup (não abort)."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_HOST", "127.0.0.1")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_PORT", "19999")
    # Bearer de teste (fallback env de _read_auth_token) — sem ele o startup
    # aborta na checagem do bearer ANTES de chegar no warning do OAuth.
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_AUTH_TOKEN", "test-bearer-token")

    warnings_logged = []

    real_warning = cws_mod.logger.warning

    def capture_warning(msg, *args, **kwargs):
        warnings_logged.append(msg % args if args else msg)
        real_warning(msg, *args, **kwargs)

    monkeypatch.setattr(cws_mod.logger, "warning", capture_warning)

    # Stub web.run_app para não iniciar servidor real.
    with patch.object(cws_mod.web, "run_app", side_effect=KeyboardInterrupt):
        try:
            cws_mod.main()
        except KeyboardInterrupt:
            pass

    assert any("CLAUDE_CODE_OAUTH_TOKEN" in w for w in warnings_logged), (
        f"Esperado warning sobre CLAUDE_CODE_OAUTH_TOKEN ausente; logado: {warnings_logged}"
    )


# ---------------------------------------------------------------------------
# _kubectl_apply_oauth_token_secret
# ---------------------------------------------------------------------------


def test_apply_oauth_token_secret_calls_kubectl_with_correct_key(claude_install_mod):
    """_kubectl_apply_oauth_token_secret usa key CLAUDE_CODE_OAUTH_TOKEN (não credentials.json)."""
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        ret = MagicMock()
        ret.returncode = 0
        ret.stdout = "fake yaml"
        ret.stderr = ""
        return ret

    with patch.object(claude_install_mod.subprocess, "run", side_effect=fake_run):
        result = claude_install_mod._kubectl_apply_oauth_token_secret(
            "sk-fake-token-xyz", namespace="deile-test"
        )

    assert result is True
    # O dry-run deve usar CLAUDE_CODE_OAUTH_TOKEN, não credentials.json.
    dry_run_cmd = calls[0]
    cmd_str = " ".join(dry_run_cmd)
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-fake-token-xyz" in cmd_str, (
        f"Esperado CLAUDE_CODE_OAUTH_TOKEN no dry-run; cmd: {cmd_str}"
    )
    assert "credentials.json" not in cmd_str, (
        "credentials.json NÃO deve aparecer no novo formato de Secret"
    )


def test_apply_oauth_token_secret_does_not_log_token_value(
    claude_install_mod, caplog
):
    """_kubectl_apply_oauth_token_secret NÃO loga o valor do token (princípio 08)."""
    secret_token = "sk-super-secret-never-log-me"

    def fake_run(cmd, *args, **kwargs):
        ret = MagicMock()
        ret.returncode = 0
        ret.stdout = "fake yaml"
        ret.stderr = ""
        return ret

    import logging

    with caplog.at_level(logging.DEBUG), \
         patch.object(claude_install_mod.subprocess, "run", side_effect=fake_run):
        claude_install_mod._kubectl_apply_oauth_token_secret(
            secret_token, namespace="deile"
        )

    for record in caplog.records:
        assert secret_token not in record.getMessage(), (
            f"Token secreto encontrado nos logs: {record.getMessage()!r}"
        )


# ---------------------------------------------------------------------------
# setup_token_claude_worker
# ---------------------------------------------------------------------------


def test_setup_token_fails_without_token_non_interactive(claude_install_mod, monkeypatch):
    """Sem token e interactive=False → ok=False com mensagem clara."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    result = claude_install_mod.setup_token_claude_worker(
        interactive=False,
        namespace="deile",
    )
    assert result.ok is False
    error = (result.error or "").lower()
    assert "token" in error or "setup-token" in error


def test_setup_token_uses_env_var(claude_install_mod, monkeypatch):
    """CLAUDE_CODE_OAUTH_TOKEN env var → usado como token (sem interação)."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token-from-env-xyz")

    captured = {}

    def fake_apply_token_secret(token, *, namespace):
        captured["token"] = token
        captured["ns"] = namespace
        return True

    with patch.object(claude_install_mod, "_kubectl_apply_oauth_token_secret",
                      side_effect=fake_apply_token_secret), \
         patch.object(claude_install_mod, "_kubectl_sync_bearer_token", return_value=True), \
         patch.object(claude_install_mod, "_kubectl_apply_manifests", return_value=True), \
         patch.object(claude_install_mod, "_kubectl_wait_rollout", return_value=True):
        result = claude_install_mod.setup_token_claude_worker(
            interactive=False, namespace="deile-test"
        )

    assert result.ok is True
    assert captured.get("token") == "token-from-env-xyz"
    assert captured.get("ns") == "deile-test"


def test_setup_token_explicit_token_takes_precedence(claude_install_mod, monkeypatch):
    """Token explícito passado como param vence a env var."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-token")

    captured = {}

    def fake_apply(token, *, namespace):
        captured["token"] = token
        return True

    with patch.object(claude_install_mod, "_kubectl_apply_oauth_token_secret",
                      side_effect=fake_apply), \
         patch.object(claude_install_mod, "_kubectl_sync_bearer_token", return_value=True), \
         patch.object(claude_install_mod, "_kubectl_apply_manifests", return_value=True), \
         patch.object(claude_install_mod, "_kubectl_wait_rollout", return_value=True):
        result = claude_install_mod.setup_token_claude_worker(
            token="explicit-param-token", interactive=False
        )

    assert result.ok is True
    assert captured["token"] == "explicit-param-token"


def test_setup_token_full_flow_success(claude_install_mod, monkeypatch):
    """Fluxo completo: apply Secret → sync bearer → apply manifests → rollout."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-annual-token")

    call_order = []

    with patch.object(claude_install_mod, "_kubectl_apply_oauth_token_secret",
                      side_effect=lambda *a, **kw: call_order.append("secret") or True), \
         patch.object(claude_install_mod, "_kubectl_sync_bearer_token",
                      side_effect=lambda **kw: call_order.append("bearer") or True), \
         patch.object(claude_install_mod, "_kubectl_apply_manifests",
                      side_effect=lambda **kw: call_order.append("manifests") or True), \
         patch.object(claude_install_mod, "_kubectl_wait_rollout",
                      side_effect=lambda **kw: call_order.append("rollout") or True):
        result = claude_install_mod.setup_token_claude_worker(
            interactive=False, namespace="deile"
        )

    assert result.ok is True
    assert result.secret_applied is True
    assert result.deployment_applied is True
    assert result.rollout_ready is True
    assert call_order == ["secret", "bearer", "manifests", "rollout"], (
        f"Ordem errada de operações: {call_order}"
    )


def test_setup_token_fails_on_secret_error(claude_install_mod, monkeypatch):
    """Falha ao aplicar Secret → ok=False, sem continuar."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-token")
    bearer_called = []

    with patch.object(claude_install_mod, "_kubectl_apply_oauth_token_secret",
                      return_value=False), \
         patch.object(claude_install_mod, "_kubectl_sync_bearer_token",
                      side_effect=lambda **kw: bearer_called.append(True) or True):
        result = claude_install_mod.setup_token_claude_worker(interactive=False)

    assert result.ok is False
    assert bearer_called == [], "bearer sync não deve ser chamado se Secret falhou"
