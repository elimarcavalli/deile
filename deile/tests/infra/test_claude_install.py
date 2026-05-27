"""Unit tests para _claude_install.bootstrap_claude_worker (#309 fase 2)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

WORKER_DIR = Path(__file__).resolve().parents[3] / "infra" / "k8s"


@pytest.fixture
def claude_install_module():
    sys.path.insert(0, str(WORKER_DIR))
    try:
        import importlib

        import _claude_install

        importlib.reload(_claude_install)
        yield _claude_install
    finally:
        sys.path.remove(str(WORKER_DIR))


def test_bootstrap_returns_error_when_no_credentials_and_not_interactive(
    claude_install_module, tmp_path, monkeypatch,
):
    """No credentials at host + interactive=False -> fail fast.

    Patch dos detectores de credenciais (Keychain + file) para garantir que
    o teste é portável (não depende do estado real do Keychain do CI/host
    onde rodar). O fluxo testado é: sem creds detectadas E interactive=False
    → ClaudeLoginResult.ok=False com mensagem clara.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_install_module, "_check_claude_logged_in", lambda: None,
    )
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain", lambda: None,
    )

    result = claude_install_module.bootstrap_claude_worker(
        interactive=False, force_relogin=False, home=tmp_path,
    )
    assert result.ok is False
    # Aceita tanto PT-BR ("credenciais") quanto EN ("credentials") no error
    error_lower = (result.error or "").lower()
    assert ("credenciais" in error_lower
            or "credentials" in error_lower
            or "claude auth login" in error_lower)


def test_bootstrap_idempotent_when_credentials_present(
    claude_install_module, tmp_path, monkeypatch,
):
    """Credentials existing + cluster commands mockados -> ok=True idempotent.

    Patch dos detectores Keychain + auth_status pra não tocar no estado
    real do host (testes não devem depender da conta logada no claude do
    operador). ``_read_credentials_from_file`` ainda lê o file fixture.
    """
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text(
        json.dumps({"email": "user@test.com", "access_token": "fake_token"})
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_install_module, "_check_claude_logged_in", lambda: None,
    )
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain", lambda: None,
    )

    # Mock all kubectl subprocess calls to return 0
    with patch.object(claude_install_module, "_kubectl_apply_secret", return_value=True), \
         patch.object(claude_install_module, "_kubectl_apply_manifests", return_value=True), \
         patch.object(claude_install_module, "_kubectl_sync_bearer_token", return_value=True), \
         patch.object(claude_install_module, "_kubectl_wait_rollout", return_value=True):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=False, force_relogin=False, home=tmp_path,
        )

    assert result.ok is True
    assert result.account_email == "user@test.com"
    assert result.secret_applied is True
    assert result.deployment_applied is True
    assert result.rollout_ready is True


def test_bootstrap_force_relogin_runs_claude_logout_then_login(
    claude_install_module, tmp_path, monkeypatch,
):
    """force_relogin=True -> claude auth logout + claude auth login antes de continuar.

    Patch dos detectores antes do force-relogin (simula estado inicial sem
    creds detectadas) para o fluxo cair em ``_run_claude_login``, e mocka
    ``subprocess.run`` para capturar os args sem rodar realmente. Após login,
    re-injeta ``_read_credentials`` retornando creds válidas para o resto do
    fluxo (Secret/manifests/rollout) prosseguir.
    """
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    called_commands = []

    def fake_run(cmd, *args, **kwargs):
        called_commands.append(cmd)
        ret = MagicMock()
        ret.returncode = 0
        ret.stdout = ""  # _check_claude_logged_in pode tentar parse — vazio é ignorado
        return ret

    # Estado inicial: NÃO logado (força flow de login)
    auth_states = [None, {"loggedIn": True, "email": "u@x"}]  # antes / depois
    monkeypatch.setattr(
        claude_install_module, "_check_claude_logged_in",
        lambda: auth_states.pop(0) if auth_states else {"loggedIn": True, "email": "u@x"},
    )
    # Estado inicial: sem creds; depois do login, retorna creds.
    cred_states = [None, {"claudeAiOauth": {"access_token": "fake", "email": "u@x"}}]
    monkeypatch.setattr(
        claude_install_module, "_read_credentials",
        lambda home=None: cred_states.pop(0) if cred_states else {"claudeAiOauth": {"access_token": "fake", "email": "u@x"}},
    )

    with patch.object(claude_install_module.subprocess, "run", side_effect=fake_run), \
         patch.object(claude_install_module, "_kubectl_apply_secret", return_value=True), \
         patch.object(claude_install_module, "_kubectl_apply_manifests", return_value=True), \
         patch.object(claude_install_module, "_kubectl_sync_bearer_token", return_value=True), \
         patch.object(claude_install_module, "_kubectl_wait_rollout", return_value=True):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=True, force_relogin=True, home=tmp_path,
        )

    assert result.ok is True
    cmd_strs = [" ".join(c) if isinstance(c, list) else str(c) for c in called_commands]
    # Espera `claude auth logout` + `claude auth login` (não `claude login`).
    assert any("auth" in s and "logout" in s for s in cmd_strs), \
        f"expected `claude auth logout` in {cmd_strs}"
    assert any("auth" in s and "login" in s for s in cmd_strs), \
        f"expected `claude auth login` in {cmd_strs}"


def test_bootstrap_failure_in_secret_apply_returns_error(
    claude_install_module, tmp_path, monkeypatch,
):
    """Secret apply falha -> ClaudeLoginResult.ok=False com error explicado."""
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text(json.dumps({"email": "u@x"}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_install_module, "_check_claude_logged_in", lambda: None,
    )
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain", lambda: None,
    )

    with patch.object(claude_install_module, "_kubectl_apply_secret", return_value=False):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=False, force_relogin=False, home=tmp_path,
        )

    assert result.ok is False
    assert "secret" in (result.error or "").lower()


def test_bootstrap_failure_in_rollout_returns_error(
    claude_install_module, tmp_path, monkeypatch,
):
    """Rollout timeout -> ok=False, manifests aplicados mas rollout pendente."""
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text(json.dumps({"email": "u@x"}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_install_module, "_check_claude_logged_in", lambda: None,
    )
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain", lambda: None,
    )

    with patch.object(claude_install_module, "_kubectl_apply_secret", return_value=True), \
         patch.object(claude_install_module, "_kubectl_apply_manifests", return_value=True), \
         patch.object(claude_install_module, "_kubectl_sync_bearer_token", return_value=True), \
         patch.object(claude_install_module, "_kubectl_wait_rollout", return_value=False):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=False, force_relogin=False, home=tmp_path,
        )

    assert result.ok is False
    assert result.deployment_applied is True  # passou aqui
    assert result.rollout_ready is False
    assert "rollout" in (result.error or "").lower()


def test_kubectl_sync_bearer_token_warns_when_worker_bearer_missing(
    claude_install_module, monkeypatch,
):
    """worker-bearer ausente -> WARN + return True (não-fatal; rollout falha
    depois com mensagem clara)."""
    def fake_kubectl_get_missing(cmd, *args, **kwargs):
        ret = MagicMock()
        ret.returncode = 1
        ret.stdout = ""
        ret.stderr = "Error from server (NotFound): secrets 'worker-bearer'"
        return ret

    with patch.object(claude_install_module.subprocess, "run",
                      side_effect=fake_kubectl_get_missing):
        result = claude_install_module._kubectl_sync_bearer_token(
            namespace="deile",
        )
    assert result is True  # não-fatal


def test_kubectl_sync_bearer_token_succeeds_when_worker_bearer_present(
    claude_install_module, monkeypatch,
):
    """worker-bearer presente -> kubectl get + base64 decode + apply do
    claude-worker-bearer com mesmo token."""
    import base64
    token_plain = "abc123token"
    token_b64 = base64.b64encode(token_plain.encode()).decode()

    call_log = []

    def fake_run(cmd, *args, **kwargs):
        call_log.append(cmd)
        ret = MagicMock()
        ret.returncode = 0
        if "get" in cmd and "secret" in cmd and "worker-bearer" in cmd:
            ret.stdout = token_b64
        else:
            ret.stdout = "fake yaml manifest"
        ret.stderr = ""
        return ret

    with patch.object(claude_install_module.subprocess, "run",
                      side_effect=fake_run):
        result = claude_install_module._kubectl_sync_bearer_token(
            namespace="deile",
        )
    assert result is True
    # 3 chamadas: get worker-bearer + create dry-run claude-worker-bearer + apply
    assert len(call_log) == 3
    # Token plain entra no --from-literal da segunda chamada (dry-run).
    dry_run_cmd = call_log[1]
    assert any(token_plain in arg for arg in dry_run_cmd), \
        f"token plain não encontrado em {dry_run_cmd}"


# ---------------------------------------------------------------------------
# Testes de CLAUDE_OAUTH_ACCESS_TOKEN env var (#309 fase 3)
# ---------------------------------------------------------------------------


def test_read_credentials_from_env_returns_none_when_unset(
    claude_install_module, monkeypatch,
):
    """Env var ausente (ou não definida) → retorna None."""
    monkeypatch.delenv("CLAUDE_OAUTH_ACCESS_TOKEN", raising=False)
    result = claude_install_module._read_credentials_from_env()
    assert result is None


def test_read_credentials_from_env_returns_dict_when_set(
    claude_install_module, monkeypatch,
):
    """Env var setada → retorna dict com formato Keychain canônico."""
    monkeypatch.setenv("CLAUDE_OAUTH_ACCESS_TOKEN", "my-oauth-token-abc123")
    result = claude_install_module._read_credentials_from_env()
    assert result is not None
    assert result == {"claudeAiOauth": {"accessToken": "my-oauth-token-abc123"}}


def test_read_credentials_from_env_strips_whitespace(
    claude_install_module, monkeypatch,
):
    """Env var com espaços/newline ao redor → token stripado antes de retornar."""
    monkeypatch.setenv("CLAUDE_OAUTH_ACCESS_TOKEN", "  token-with-spaces  \n")
    result = claude_install_module._read_credentials_from_env()
    assert result is not None
    assert result["claudeAiOauth"]["accessToken"] == "token-with-spaces"


def test_read_credentials_from_env_returns_none_when_empty(
    claude_install_module, monkeypatch,
):
    """Env var setada mas vazia → retorna None (equivalente a não setada)."""
    monkeypatch.setenv("CLAUDE_OAUTH_ACCESS_TOKEN", "   ")
    result = claude_install_module._read_credentials_from_env()
    assert result is None


def test_bootstrap_uses_env_var_first_when_set(
    claude_install_module, tmp_path, monkeypatch,
):
    """CLAUDE_OAUTH_ACCESS_TOKEN setada → usada como credencial, sem tocar Keychain/file.

    Valida que a cadeia de precedência respeita a env var como primeira fonte.
    Keychain e file ficam "indisponíveis" propositalmente — se o código tocar
    em qualquer um deles, o teste falha.
    """
    monkeypatch.setenv("CLAUDE_OAUTH_ACCESS_TOKEN", "env-token-xyz")
    monkeypatch.setenv("HOME", str(tmp_path))

    keychain_called = []

    def _keychain_should_not_be_called():
        keychain_called.append(True)
        return None  # mesmo que retorne None, registra a chamada indevida

    monkeypatch.setattr(
        claude_install_module, "_check_claude_logged_in", lambda: None,
    )
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain",
        _keychain_should_not_be_called,
    )
    # Não criamos ~/.claude/credentials.json — file também não deve ser lido.

    captured_creds = {}

    def fake_apply_secret(creds, *, namespace):
        captured_creds.update(creds)
        return True

    with patch.object(claude_install_module, "_kubectl_apply_secret", side_effect=fake_apply_secret), \
         patch.object(claude_install_module, "_kubectl_apply_manifests", return_value=True), \
         patch.object(claude_install_module, "_kubectl_sync_bearer_token", return_value=True), \
         patch.object(claude_install_module, "_kubectl_wait_rollout", return_value=True):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=False, force_relogin=False, home=tmp_path,
        )

    assert result.ok is True
    # Env var foi usada: Secret deve conter o token da env var.
    oauth = captured_creds.get("claudeAiOauth", {})
    assert oauth.get("accessToken") == "env-token-xyz", \
        f"expected env-token-xyz, got {captured_creds!r}"
    # Keychain não deveria ter sido chamado.
    assert keychain_called == [], "Keychain foi chamado mesmo com env var setada"


def test_bootstrap_falls_back_to_keychain_when_env_unset(
    claude_install_module, tmp_path, monkeypatch,
):
    """Env var ausente → cai para Keychain (segunda prioridade)."""
    monkeypatch.delenv("CLAUDE_OAUTH_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_install_module, "_check_claude_logged_in", lambda: None,
    )

    fake_keychain_creds = {"claudeAiOauth": {"accessToken": "keychain-token"}}
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain",
        lambda: fake_keychain_creds,
    )
    # Sem credentials.json — se o código cair no file path, terá None do file.

    captured_creds = {}

    def fake_apply_secret(creds, *, namespace):
        captured_creds.update(creds)
        return True

    with patch.object(claude_install_module, "_kubectl_apply_secret", side_effect=fake_apply_secret), \
         patch.object(claude_install_module, "_kubectl_apply_manifests", return_value=True), \
         patch.object(claude_install_module, "_kubectl_sync_bearer_token", return_value=True), \
         patch.object(claude_install_module, "_kubectl_wait_rollout", return_value=True):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=False, force_relogin=False, home=tmp_path,
        )

    assert result.ok is True
    assert captured_creds.get("claudeAiOauth", {}).get("accessToken") == "keychain-token"


def test_bootstrap_falls_back_to_file_when_env_and_keychain_unset(
    claude_install_module, tmp_path, monkeypatch,
):
    """Env var ausente + Keychain ausente → cai para ~/.claude/credentials.json."""
    monkeypatch.delenv("CLAUDE_OAUTH_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_install_module, "_check_claude_logged_in", lambda: None,
    )
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain", lambda: None,
    )

    # Cria credentials.json no path esperado.
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    file_creds = {"email": "file@test.com", "access_token": "file-token"}
    (claude_dir / "credentials.json").write_text(json.dumps(file_creds))

    captured_creds = {}

    def fake_apply_secret(creds, *, namespace):
        captured_creds.update(creds)
        return True

    with patch.object(claude_install_module, "_kubectl_apply_secret", side_effect=fake_apply_secret), \
         patch.object(claude_install_module, "_kubectl_apply_manifests", return_value=True), \
         patch.object(claude_install_module, "_kubectl_sync_bearer_token", return_value=True), \
         patch.object(claude_install_module, "_kubectl_wait_rollout", return_value=True):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=False, force_relogin=False, home=tmp_path,
        )

    assert result.ok is True
    assert captured_creds.get("email") == "file@test.com"
