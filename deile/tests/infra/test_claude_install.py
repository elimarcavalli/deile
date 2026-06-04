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
# Testes da correção de race condition (issue #356)
# ---------------------------------------------------------------------------


def test_kubectl_apply_manifests_does_not_include_manifest_48(
    claude_install_module,
):
    """Manifest 48 (bearer Secret stub) não deve estar na lista de manifests.

    A Opção A do issue #356 elimina a race condition removendo o stub vazio
    da lista: o Secret é criado com token real por _kubectl_sync_bearer_token
    ANTES de _kubectl_apply_manifests ser chamado.
    """
    manifests_called = []

    def fake_run(cmd, *args, **kwargs):
        manifests_called.extend(cmd)
        ret = MagicMock()
        ret.returncode = 0
        ret.stdout = ""
        ret.stderr = ""
        return ret

    with patch.object(claude_install_module.subprocess, "run", side_effect=fake_run):
        claude_install_module._kubectl_apply_manifests(namespace="deile")

    cmd_str = " ".join(manifests_called)
    assert "48-claude-worker-bearer-secret" not in cmd_str, (
        "Manifest 48 não deve ser aplicado por _kubectl_apply_manifests — "
        "o Secret é criado por _kubectl_sync_bearer_token antes (issue #356)"
    )


def test_bootstrap_syncs_bearer_before_applying_manifests(
    claude_install_module, tmp_path, monkeypatch,
):
    """_kubectl_sync_bearer_token deve ser chamado ANTES de _kubectl_apply_manifests.

    Garante que a race condition do issue #356 está resolvida: o Secret
    claude-worker-bearer já contém o token real quando o Deployment 50 é aplicado.
    """
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text(
        json.dumps({"email": "u@x", "access_token": "t"})
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_install_module, "_check_claude_logged_in", lambda: None,
    )
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain", lambda: None,
    )

    call_order = []

    def fake_sync_bearer(*, namespace):
        call_order.append("sync_bearer")
        return True

    def fake_apply_manifests(*, namespace):
        call_order.append("apply_manifests")
        return True

    with patch.object(claude_install_module, "_kubectl_apply_secret", return_value=True), \
         patch.object(claude_install_module, "_kubectl_sync_bearer_token",
                      side_effect=fake_sync_bearer), \
         patch.object(claude_install_module, "_kubectl_apply_manifests",
                      side_effect=fake_apply_manifests), \
         patch.object(claude_install_module, "_kubectl_wait_rollout", return_value=True):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=False, force_relogin=False, home=tmp_path,
        )

    assert result.ok is True
    assert call_order.index("sync_bearer") < call_order.index("apply_manifests"), (
        f"sync_bearer deve preceder apply_manifests; ordem: {call_order}"
    )


def test_bootstrap_bearer_sync_failure_before_manifests_returns_error(
    claude_install_module, tmp_path, monkeypatch,
):
    """Se _kubectl_sync_bearer_token falhar, bootstrap deve retornar erro
    SEM ter chamado _kubectl_apply_manifests (evita aplicar Deployment
    com Secret vazio — issue #356)."""
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

    manifests_called = []

    with patch.object(claude_install_module, "_kubectl_apply_secret", return_value=True), \
         patch.object(claude_install_module, "_kubectl_sync_bearer_token", return_value=False), \
         patch.object(claude_install_module, "_kubectl_apply_manifests",
                      side_effect=lambda **kw: manifests_called.append(True) or True), \
         patch.object(claude_install_module, "_kubectl_wait_rollout", return_value=True):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=False, force_relogin=False, home=tmp_path,
        )

    assert result.ok is False
    assert manifests_called == [], (
        "_kubectl_apply_manifests não deve ser chamado se sync_bearer falhou "
        "(evita Deployment com Secret vazio — issue #356)"
    )


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


# ============================================================================
# renew_claude_worker — estratégia A da issue #309 fase 3 (resiliência auth)
# ============================================================================


def test_renew_fails_when_credentials_absent(claude_install_module, tmp_path,
                                              monkeypatch):
    """Sem credentials (Keychain + file + env) -> ok=False com mensagem
    apontando pra `claude auth login` (não tenta browser autônomo)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_OAUTH_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain", lambda: None,
    )

    result = claude_install_module.renew_claude_worker(home=tmp_path)
    assert result.ok is False
    assert "credenciais" in (result.error or "").lower()
    assert "claude auth login" in (result.error or "")


def test_renew_success_path_calls_secret_then_restart(
    claude_install_module, tmp_path, monkeypatch,
):
    """Credentials presentes + kubectl ok → re-apply Secret + rollout
    restart + wait. NÃO chama _kubectl_apply_manifests (lightweight)."""
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "fake", "email": "u@x"}}'
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_OAUTH_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain", lambda: None,
    )

    call_log = []

    def fake_apply_secret(creds, *, namespace):
        call_log.append(("apply_secret", namespace))
        return True

    def fake_run(cmd, *args, **kwargs):
        call_log.append(("kubectl", " ".join(cmd[:5])))
        ret = MagicMock()
        ret.returncode = 0
        ret.stdout = ""
        ret.stderr = ""
        return ret

    def fake_wait_rollout(*, namespace, timeout_s=180):
        call_log.append(("wait_rollout", namespace, timeout_s))
        return True

    with patch.object(claude_install_module, "_kubectl_apply_secret",
                      side_effect=fake_apply_secret), \
         patch.object(claude_install_module.subprocess, "run",
                      side_effect=fake_run), \
         patch.object(claude_install_module, "_kubectl_wait_rollout",
                      side_effect=fake_wait_rollout):
        result = claude_install_module.renew_claude_worker(
            namespace="my-ns", home=tmp_path,
        )

    assert result.ok is True
    assert result.account_email == "u@x"
    assert result.secret_applied is True
    assert result.rollout_ready is True
    # Ordem esperada: apply_secret → rollout restart → wait_rollout
    kinds = [c[0] for c in call_log]
    assert kinds == ["apply_secret", "kubectl", "wait_rollout"]
    # rollout restart no claude-worker do namespace correto
    assert "rollout restart deployment/claude-worker" in call_log[1][1]
    assert call_log[2] == ("wait_rollout", "my-ns", 180)


def test_renew_propagates_apply_secret_failure(claude_install_module,
                                                 tmp_path, monkeypatch):
    """kubectl apply secret falha -> ok=False, sem rollout (early return)."""
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "x"}}'
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_OAUTH_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain", lambda: None,
    )

    rollout_called = []

    def fake_run_rollout(cmd, *args, **kwargs):
        rollout_called.append(cmd)
        ret = MagicMock()
        ret.returncode = 0
        ret.stdout = ""
        ret.stderr = ""
        return ret

    with patch.object(claude_install_module, "_kubectl_apply_secret",
                      return_value=False), \
         patch.object(claude_install_module.subprocess, "run",
                      side_effect=fake_run_rollout):
        result = claude_install_module.renew_claude_worker(home=tmp_path)

    assert result.ok is False
    assert "secret" in (result.error or "").lower()
    # Não deve ter rolled out se Secret falhou.
    assert rollout_called == []


def test_renew_uses_env_var_credentials_when_present(claude_install_module,
                                                       tmp_path, monkeypatch):
    """CLAUDE_OAUTH_ACCESS_TOKEN seta -> usa diretamente (sem Keychain/file)."""
    monkeypatch.setenv("CLAUDE_OAUTH_ACCESS_TOKEN", "sk-from-env-12345")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        claude_install_module, "_read_credentials_from_keychain", lambda: None,
    )

    captured = {}

    def fake_apply_secret(creds, *, namespace):
        captured["creds"] = creds
        return True

    def fake_run(cmd, *args, **kwargs):
        ret = MagicMock()
        ret.returncode = 0
        ret.stdout = ""
        ret.stderr = ""
        return ret

    with patch.object(claude_install_module, "_kubectl_apply_secret",
                      side_effect=fake_apply_secret), \
         patch.object(claude_install_module.subprocess, "run",
                      side_effect=fake_run), \
         patch.object(claude_install_module, "_kubectl_wait_rollout",
                      return_value=True):
        result = claude_install_module.renew_claude_worker(home=tmp_path)

    assert result.ok is True
    # Token da env var foi propagado ao Secret.
    assert captured["creds"]["claudeAiOauth"]["accessToken"] == "sk-from-env-12345"


# ---------------------------------------------------------------------------
# bootstrap_claude_worker_in_pod — issue #335
# ---------------------------------------------------------------------------


def test_bootstrap_in_pod_applies_placeholder_credentials(claude_install_module):
    """``bootstrap_claude_worker_in_pod`` aplica Secret placeholder + manifests."""
    captured = {}

    def fake_apply_secret(creds, *, namespace):
        captured["creds"] = creds
        captured["ns"] = namespace
        return True

    with patch.object(claude_install_module, "_kubectl_apply_secret",
                      side_effect=fake_apply_secret), \
         patch.object(claude_install_module, "_kubectl_sync_bearer_token",
                      return_value=True), \
         patch.object(claude_install_module, "_kubectl_apply_manifests",
                      return_value=True):
        result = claude_install_module.bootstrap_claude_worker_in_pod(
            namespace="deile-test",
        )

    assert result.ok is True
    assert result.secret_applied is True
    assert result.deployment_applied is True
    assert result.rollout_ready is False
    token = captured["creds"]["claudeAiOauth"]["accessToken"]
    assert token, "placeholder token deve ser não-vazio"
    assert captured["ns"] == "deile-test"


def test_bootstrap_in_pod_fails_on_secret_error(claude_install_module):
    """Falha no apply do Secret placeholder propagada como ok=False."""
    with patch.object(claude_install_module, "_kubectl_apply_secret",
                      return_value=False):
        result = claude_install_module.bootstrap_claude_worker_in_pod()

    assert result.ok is False
    assert result.error is not None


def test_bootstrap_in_pod_fails_on_bearer_sync_error(claude_install_module):
    """Falha no sync do bearer retorna secret_applied=True, ok=False."""
    with patch.object(claude_install_module, "_kubectl_apply_secret",
                      return_value=True), \
         patch.object(claude_install_module, "_kubectl_sync_bearer_token",
                      return_value=False):
        result = claude_install_module.bootstrap_claude_worker_in_pod()

    assert result.ok is False
    assert result.secret_applied is True


def test_bootstrap_in_pod_fails_on_manifest_error(claude_install_module):
    """Falha nos manifests propagada com deployment_applied=False."""
    with patch.object(claude_install_module, "_kubectl_apply_secret",
                      return_value=True), \
         patch.object(claude_install_module, "_kubectl_sync_bearer_token",
                      return_value=True), \
         patch.object(claude_install_module, "_kubectl_apply_manifests",
                      return_value=False):
        result = claude_install_module.bootstrap_claude_worker_in_pod()

    assert result.ok is False
    assert result.secret_applied is True
    assert result.deployment_applied is False
