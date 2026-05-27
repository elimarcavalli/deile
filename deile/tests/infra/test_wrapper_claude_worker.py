"""Unit tests para ``wrapper.py`` no mode ``claude-worker`` (issue #309 fase 2).

O ``claude-worker`` é um novo papel do entrypoint que precisa carregar uma
allowlist regex de repositórios (montada como ConfigMap) **antes** de
chamar o ``claude_worker_server``. Sem allowlist, o pod NÃO pode subir —
defense-in-depth contra prompt-injection que tentasse ``git push`` para
um repositório arbitrário.

Os testes cobrem três cenários:

1. ``_load_allowed_repo_patterns`` carrega corretamente regexes válidas,
   ignorando comentários (``#``) e linhas em branco.
2. Falha hard (``SystemExit``) quando o arquivo de config está ausente.
3. Falha hard (``SystemExit``) quando o arquivo só tem comentários/linhas
   em branco (allowlist vazia).
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
