"""Testes unit do enforcement da allowlist de repos (issue #639).

Cobre a FONTE ÚNICA de verificação por-request em ``_worker_core``:
``normalize_repo_slug`` / ``repo_slug_allowed`` / ``load_allowed_repo_patterns``
/ ``check_repo_allowed``. O enforcement de integração nos dois servidores vive em
``test_cli_worker_repo_allowlist.py`` e ``test_claude_worker_repo_allowlist.py``.

Semântica fail-closed: allowlist ausente/vazia/inválida → bloqueia tudo. É
consistente com o ``wrapper.py`` que faz ``sys.exit`` no startup (em produção o
worker nem sobe sem allowlist válida), então fail-closed aqui não quebra deploy
legítimo — apenas defende contra drift (ConfigMap removido em runtime) e testes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _worker_core as core  # noqa: E402

#: Allowlist canônica (cópia do ConfigMap 47-claude-worker-allowed-repos).
_CONFIGMAP = r"""# regex por linha — URLs completas
^https://github\.com/elimarcavalli/(deile|deilebot)(\.git)?$
^https://gitlab\.com/elimarcavalli/(deile|deilebot)(\.git)?$
^git@github\.com:elimarcavalli/(deile|deilebot)(\.git)?$
^git@gitlab\.com:elimarcavalli/(deile|deilebot)(\.git)?$
"""


@pytest.fixture
def allowlist(tmp_path, monkeypatch):
    """Escreve o ConfigMap canônico num tmp file e aponta a env var pra ele."""
    p = tmp_path / "allowed_repos.regex"
    p.write_text(_CONFIGMAP, encoding="utf-8")
    monkeypatch.setenv("DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(p))
    # Hosts default (evita herdar GHES CSV de outro teste/ambiente).
    monkeypatch.delenv("DEILE_GITHUB_HOST", raising=False)
    monkeypatch.delenv("DEILE_GITLAB_HOST", raising=False)
    return p


# --------------------------------------------------------------------------- #
# normalize_repo_slug
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("elimarcavalli/deile", "elimarcavalli/deile"),
    ("  elimarcavalli/deile  ", "elimarcavalli/deile"),
    ("elimarcavalli/deile.git", "elimarcavalli/deile"),
    ("group/sub/project", "group/sub/project"),
])
def test_normalize_accepts_well_formed_slugs(raw, expected):
    assert core.normalize_repo_slug(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "single",                       # falta segundo componente
    "owner/",                       # componente vazio
    "/repo",                        # / líder
    "owner//repo",                  # // interno
    "../../etc/passwd",             # path traversal
    "owner/repo/../leak",           # traversal por componente
    "owner/..",                     # componente ".."
    "owner/.",                      # componente "."
    "owner/repo@evil",              # auth/host smuggling
    "owner/repo:branch",            # ":" smuggling
    "https://github.com/o/r",       # URL inteira como slug
    "git@github.com:o/r",           # ssh inteiro como slug
    "owner\\repo",                  # backslash
    "owner /repo",                  # espaço interno
])
def test_normalize_rejects_malformed_or_unsafe(raw):
    assert core.normalize_repo_slug(raw) is None


# --------------------------------------------------------------------------- #
# load_allowed_repo_patterns (não-exiting, fail-closed)
# --------------------------------------------------------------------------- #

def test_load_returns_patterns_when_valid(allowlist):
    patterns, err = core.load_allowed_repo_patterns()
    assert err is None
    assert len(patterns) == 4


def test_load_fails_closed_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(tmp_path / "nope.regex"),
    )
    patterns, err = core.load_allowed_repo_patterns()
    assert patterns == []
    assert err is not None and "ausente" in err


def test_load_fails_closed_when_empty(monkeypatch, tmp_path):
    p = tmp_path / "empty.regex"
    p.write_text("# só comentário\n\n", encoding="utf-8")
    monkeypatch.setenv("DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(p))
    patterns, err = core.load_allowed_repo_patterns()
    assert patterns == []
    assert err is not None and "vazia" in err


def test_load_fails_closed_on_invalid_regex(monkeypatch, tmp_path):
    p = tmp_path / "bad.regex"
    p.write_text("^[unterminated\n", encoding="utf-8")
    monkeypatch.setenv("DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(p))
    patterns, err = core.load_allowed_repo_patterns()
    assert patterns == []
    assert err is not None and "inválida" in err


# --------------------------------------------------------------------------- #
# check_repo_allowed (end-to-end por-request)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("slug", [
    "elimarcavalli/deile",
    "elimarcavalli/deilebot",
    " elimarcavalli/deile ",
    "elimarcavalli/deile.git",
])
def test_check_allows_repos_in_allowlist(allowlist, slug):
    ok, reason, norm = core.check_repo_allowed(slug)
    assert ok is True, reason
    assert reason == ""
    assert norm == "elimarcavalli/deile" or norm == "elimarcavalli/deilebot"


@pytest.mark.parametrize("slug", [
    "elimarcavalli/leak-repo",      # repo não listado
    "attacker/deile",               # owner errado
    "elimarcavalli/Deile",          # case mismatch (allowlist é case-sensitive)
    "elimarcavalli/deile-secret",   # prefixo do nome permitido, mas não exato
])
def test_check_blocks_well_formed_repos_outside_allowlist(allowlist, slug):
    ok, reason, norm = core.check_repo_allowed(slug)
    assert ok is False
    assert "fora da allowlist" in reason
    assert norm is not None  # slug bem-formado, só não casa


@pytest.mark.parametrize("slug", [
    "../../etc/passwd",
    "evil.com/elimarcavalli/deile",
    "elimarcavalli/deile@evil.com",
    "https://github.com/elimarcavalli/deile",
])
def test_check_blocks_malformed_or_unsafe_slugs(allowlist, slug):
    ok, reason, _norm = core.check_repo_allowed(slug)
    assert ok is False


def test_check_fails_closed_without_allowlist(monkeypatch, tmp_path):
    """Sem ConfigMap (drift em runtime), NADA é permitido — fail-closed."""
    monkeypatch.setenv(
        "DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(tmp_path / "missing.regex"),
    )
    ok, reason, _norm = core.check_repo_allowed("elimarcavalli/deile")
    assert ok is False
    assert "indisponível" in reason


def test_check_honors_ghes_csv_host(monkeypatch, tmp_path):
    """``DEILE_GITHUB_HOST`` CSV (GHES multi-host) é resolvido na URL canônica."""
    p = tmp_path / "ghes.regex"
    p.write_text(r"^https://ghe\.corp\.com/team/app(\.git)?$" + "\n", "utf-8")
    monkeypatch.setenv("DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(p))
    monkeypatch.setenv("DEILE_GITHUB_HOST", "github.com,ghe.corp.com")
    ok, _reason, norm = core.check_repo_allowed("team/app")
    assert ok is True
    assert norm == "team/app"


def test_check_matches_gitlab_subgroup(monkeypatch, tmp_path):
    """Slug GitLab ``group/sub/project`` casa pattern de subgrupo."""
    p = tmp_path / "gl.regex"
    p.write_text(
        r"^https://gitlab\.com/acme/team/service(\.git)?$" + "\n", "utf-8",
    )
    monkeypatch.setenv("DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(p))
    ok, _reason, norm = core.check_repo_allowed("acme/team/service")
    assert ok is True
    assert norm == "acme/team/service"
