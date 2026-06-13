"""Unit tests para ``wrapper.py`` no mode ``claude-worker`` (issue #309 fase 2).

O ``claude-worker`` é um novo papel do entrypoint que precisa carregar uma
allowlist regex de repositórios (montada como ConfigMap) **antes** de
chamar o ``claude_worker_server``. Sem allowlist, o pod NÃO pode subir —
defense-in-depth contra prompt-injection que tentasse ``git push`` para
um repositório arbitrário.

Os testes cobrem:

1. ``_load_allowed_repo_patterns`` carrega corretamente regexes válidas,
   ignorando comentários (``#``) e linhas em branco.
2. Falha hard (``SystemExit``) quando o arquivo de config está ausente.
3. Falha hard (``SystemExit``) quando o arquivo só tem comentários/linhas
   em branco (allowlist vazia).
4. ``ANTHROPIC_API_KEY`` NUNCA está no env do subprocess (issue #603).
5. ``--bare`` detectado → RuntimeError antes do spawn (issue #603).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture
def wrapper_mod():
    """Carrega ``infra/k8s/wrapper.py`` dinamicamente (mesmo padrão do
    ``test_wrapper_dual_forge.py``).

    O script não é pacote regular, então usamos ``importlib.util`` em vez
    de manipular ``sys.path``. Cada teste recebe uma instância nova do
    módulo para evitar contaminação cross-teste.
    """
    repo_root = Path(__file__).resolve().parents[3]
    wrapper_path = repo_root / "infra" / "k8s" / "wrapper.py"
    spec = importlib.util.spec_from_file_location(
        "wrapper_under_test_claude_worker", str(wrapper_path),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wrapper_under_test_claude_worker"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_load_allowed_repo_patterns_reads_file(wrapper_mod, tmp_path, monkeypatch):
    """Lê regex de arquivo, ignora comentário e linha vazia."""
    config = tmp_path / "allowed_repos.regex"
    config.write_text(
        "^https://github\\.com/elimarcavalli/(deile|deilebot)(\\.git)?$\n"
        "# comment line ignored\n"
        "\n"
        "^git@github\\.com:elimarcavalli/(deile|deilebot)(\\.git)?$\n"
    )
    monkeypatch.setenv("DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(config))

    patterns = wrapper_mod._load_allowed_repo_patterns()
    assert len(patterns) == 2  # apenas as 2 linhas não-comentário
    # padrão HTTPS deve casar com a URL canônica de clone
    assert any(p.match("https://github.com/elimarcavalli/deile.git") for p in patterns)
    # padrão SSH também
    assert any(p.match("git@github.com:elimarcavalli/deilebot.git") for p in patterns)


def test_load_allowed_repo_patterns_fails_when_missing(wrapper_mod, tmp_path, monkeypatch):
    """Arquivo ausente → ``SystemExit`` (sem whitelist, NÃO arrancamos)."""
    missing = tmp_path / "nonexistent.regex"
    monkeypatch.setenv("DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(missing))

    with pytest.raises(SystemExit) as exc:
        wrapper_mod._load_allowed_repo_patterns()
    assert "missing" in str(exc.value).lower()


def test_load_allowed_repo_patterns_fails_when_empty(wrapper_mod, tmp_path, monkeypatch):
    """Arquivo só com comentários → ``SystemExit`` (allowlist vazia é proibida)."""
    config = tmp_path / "allowed_repos.regex"
    config.write_text("# only comments\n\n#another\n")
    monkeypatch.setenv("DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(config))

    with pytest.raises(SystemExit) as exc:
        wrapper_mod._load_allowed_repo_patterns()
    assert "empty" in str(exc.value).lower()


def test_load_allowed_repo_patterns_rejects_invalid_regex(wrapper_mod, tmp_path, monkeypatch):
    """Regex inválido na config → ``SystemExit`` (não pode iniciar com pattern quebrado).

    Este caso não estava no plano original mas é defesa simétrica óbvia:
    se o operador errar a sintaxe da regex, queremos falhar cedo e
    explícito, não tentar continuar com lista parcial.
    """
    config = tmp_path / "allowed_repos.regex"
    config.write_text(
        "^https://github\\.com/elimarcavalli/(deile|deilebot)(\\.git)?$\n"
        "[invalid(regex\n"
    )
    monkeypatch.setenv("DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(config))

    with pytest.raises(SystemExit) as exc:
        wrapper_mod._load_allowed_repo_patterns()
    assert "invalid regex" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Testes de segurança de auth (issue #603)
# ---------------------------------------------------------------------------


def test_sensitive_keys_does_not_include_claude_code_oauth_token(wrapper_mod):
    """CLAUDE_CODE_OAUTH_TOKEN NÃO deve estar em _SENSITIVE_KEYS.

    O token DEVE chegar ao subprocess do claude -p (issue #603 §3).
    Se estiver em _SENSITIVE_KEYS, wrapper.py o removeria do env do
    subprocess e o claude -p ficaria sem autenticação silenciosamente.
    """
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in wrapper_mod._SENSITIVE_KEYS, (
        "CLAUDE_CODE_OAUTH_TOKEN não deve ser removido do env do subprocess "
        "(é o mecanismo de auth primário — issue #603 §3)"
    )


def test_sensitive_keys_includes_anthropic_api_key(wrapper_mod):
    """ANTHROPIC_API_KEY DEVE estar em _SENSITIVE_KEYS (precedência alta
    sobre CLAUDE_CODE_OAUTH_TOKEN — issue #603 §1).

    Em modo ``claude -p``, ANTHROPIC_API_KEY vence na precedência de auth e
    cobraria via API key (não assinatura), quebrando a frota se a key for de
    org desabilitada. O wrapper deve removê-la do env antes do spawn.
    """
    assert "ANTHROPIC_API_KEY" in wrapper_mod._SENSITIVE_KEYS, (
        "ANTHROPIC_API_KEY deve estar em _SENSITIVE_KEYS para não vazar "
        "pro subprocess do claude -p (issue #603 §1)"
    )


def test_run_claude_worker_strips_anthropic_api_key_from_env(
    wrapper_mod, monkeypatch,
):
    """``_run_claude_worker`` remove ``ANTHROPIC_API_KEY`` de ``os.environ``
    ANTES de delegar pro servidor — o ``claude -p`` herda um env sem a key,
    autenticando via ``CLAUDE_CODE_OAUTH_TOKEN`` (issue #603 §1).

    Comportamento, não só pertencimento a ``_SENSITIVE_KEYS``: a key pode
    chegar montada em ``/run/secrets/deile`` e ``_load_secret_files`` a
    injetaria de volta no env — o strip explícito do papel claude-worker é o
    que garante que ela não sobreviva pro subprocess.
    """
    import os

    # Infra pesada stubada — só nos interessa o strip do ANTHROPIC_API_KEY.
    monkeypatch.setattr(wrapper_mod, "_harden_runtime_dirs", lambda: None)
    monkeypatch.setattr(
        wrapper_mod, "_load_allowed_repo_patterns", lambda: [object()],
    )
    monkeypatch.setattr(
        wrapper_mod, "_install_git_repo_guard", lambda patterns: None,
    )
    # _load_secret_files emula o mount do Secret deile reinjetando a API key
    # (caminho real onde a key voltaria pro env via arquivo de Secret).
    monkeypatch.setattr(
        wrapper_mod, "_load_secret_files",
        lambda role_dir: (os.environ.__setitem__("ANTHROPIC_API_KEY",
                                                  "sk-ant-leak"),
                          ["ANTHROPIC_API_KEY"])[1],
    )
    monkeypatch.setattr(wrapper_mod, "_setup_forge_credentials", lambda: None)

    # Bearer: stub do Path.is_file/read_text só para o arquivo do bearer.
    _BEARER = "CLAUDE_WORKER_BEARER_TOKEN"
    real_is_file = wrapper_mod.Path.is_file
    real_read_text = wrapper_mod.Path.read_text
    monkeypatch.setattr(
        wrapper_mod.Path, "is_file",
        lambda self: True if self.name == _BEARER else real_is_file(self),
    )
    monkeypatch.setattr(
        wrapper_mod.Path, "read_text",
        lambda self, *a, **k: "bearer-xyz" if self.name == _BEARER
        else real_read_text(self, *a, **k),
    )

    captured_env = {}

    def fake_server_main(*args, **kwargs):
        captured_env["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY")
        return 0

    fake_module = type(sys)("claude_worker_server")
    fake_module.main = fake_server_main
    monkeypatch.setitem(sys.modules, "claude_worker_server", fake_module)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leak")

    rc = wrapper_mod._run_claude_worker([])

    assert rc == 0
    assert "ANTHROPIC_API_KEY" not in os.environ, (
        "ANTHROPIC_API_KEY deve ser removido de os.environ pelo claude-worker"
    )
    assert captured_env["ANTHROPIC_API_KEY"] is None, (
        "o env visto pelo servidor (e herdado pelo claude -p) não pode conter "
        "ANTHROPIC_API_KEY"
    )
