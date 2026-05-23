"""Testes do comando /todo (issue #287).

Cobre:
  - Git repo com 2 TODOs em 2 arquivos → comando lista os 2
  - Repo sem marcadores → mensagem amigável
  - Case-insensitive: FIXME, fixme, FixMe todos reconhecidos
  - Marcadores HACK e XXX
  - Arquivo sem comentários → ignorado
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from deile.commands.base import CommandContext
from deile.commands.builtin.todo_command import (
    _is_comment_line,
    TodoCommand,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render(content) -> str:
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=200)
    console.print(content)
    return buf.getvalue()


def _ctx(args: str = "") -> CommandContext:
    return CommandContext(user_input="/todo", args=args)


def _cmd() -> TodoCommand:
    return TodoCommand()


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test User",
             "GIT_AUTHOR_EMAIL": "test@example.com",
             "GIT_COMMITTER_NAME": "Test User",
             "GIT_COMMITTER_EMAIL": "test@example.com"},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo_with_todos(tmp_path: Path) -> Path:
    """Cria um repo git temporário com arquivos contendo TODOs."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    _run_git(repo, "init")

    # Arquivo 1: dois TODOs
    (repo / "main.py").write_text(
        '# TODO: add error handling\n'
        'def main():\n'
        '    print("hello")\n'
        '    # FIXME: this is slow\n'
        '    pass\n'
    )
    _run_git(repo, "add", "main.py")
    _run_git(repo, "commit", "-m", "initial commit with todos")

    # Arquivo 2: um HACK
    (repo / "utils.py").write_text(
        '"""Utils module."""\n'
        '# HACK: workaround for bug #42\n'
        'def util():\n'
        '    return True\n'
    )
    _run_git(repo, "add", "utils.py")
    _run_git(repo, "commit", "-m", "add utils with hack")

    return repo


@pytest.fixture
def git_repo_no_markers(tmp_path: Path) -> Path:
    """Repo sem marcadores."""
    repo = tmp_path / "clean_repo"
    repo.mkdir()
    _run_git(repo, "init")

    (repo / "clean.py").write_text(
        '"""Clean module with no TODOs."""\n'
        'def func():\n'
        '    # just a regular comment\n'
        '    pass\n'
    )
    _run_git(repo, "add", "clean.py")
    _run_git(repo, "commit", "-m", "clean commit")
    return repo


@pytest.fixture
def git_repo_case_sensitivity(tmp_path: Path) -> Path:
    """Repo com marcadores em diferentes capitalizações."""
    repo = tmp_path / "case_repo"
    repo.mkdir()
    _run_git(repo, "init")

    (repo / "mixed.py").write_text(
        '# TODO: normal\n'
        '# todo: lowercase\n'
        '# Todo: title case\n'
        '# FIXME: normal\n'
        '# fixme: lowercase\n'
        '# FixMe: mixed\n'
        '# HACK: normal\n'
        '# XXX: normal\n'
        'print("test")\n'
    )
    _run_git(repo, "add", "mixed.py")
    _run_git(repo, "commit", "-m", "mixed case todos")
    return repo


# ---------------------------------------------------------------------------
# Testes unitários — _is_comment_line
# ---------------------------------------------------------------------------


class TestIsCommentLine:
    def test_python_comment(self):
        assert _is_comment_line("# TODO: fix this")

    def test_shell_comment(self):
        assert _is_comment_line("# FIXME: broken")

    def test_js_comment(self):
        assert _is_comment_line("// HACK: workaround")

    def test_block_comment(self):
        assert _is_comment_line("/* XXX: danger */")

    def test_non_comment(self):
        assert not _is_comment_line("print('TODO')")

    def test_indented_comment(self):
        assert _is_comment_line("    # FIXME: bug")

    def test_empty(self):
        assert not _is_comment_line("")


# ---------------------------------------------------------------------------
# Testes de integração
# ---------------------------------------------------------------------------


class TestTodoWithMarkers:
    async def test_lists_all_markers(self, git_repo_with_todos: Path):
        """Repo com 2 arquivos e 3 marcadores → lista todos."""
        with _cd(git_repo_with_todos):
            result = await _cmd().execute(_ctx())
        assert result.success
        assert result.content_type == "rich"
        rendered = _render(result.content)
        assert "TODO" in rendered
        assert "FIXME" in rendered
        assert "HACK" in rendered
        assert "main.py" in rendered
        assert "utils.py" in rendered
        assert result.metadata["marker_count"] == 3

    async def test_marker_count_in_metadata(self, git_repo_with_todos: Path):
        with _cd(git_repo_with_todos):
            result = await _cmd().execute(_ctx())
        assert result.metadata["marker_count"] == 3


class TestTodoEmptyRepo:
    async def test_no_markers_friendly_message(self, git_repo_no_markers: Path):
        """Repo sem marcadores → mensagem amigável."""
        with _cd(git_repo_no_markers):
            result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        assert "Nenhum" in rendered
        assert "🎉" in rendered
        assert result.metadata["marker_count"] == 0

    async def test_no_markers_content_type(self, git_repo_no_markers: Path):
        with _cd(git_repo_no_markers):
            result = await _cmd().execute(_ctx())
        assert result.content_type == "rich"


class TestCaseInsensitive:
    async def test_all_variants_recognized(self, git_repo_case_sensitivity: Path):
        """Todas as capitalizações são reconhecidas."""
        with _cd(git_repo_case_sensitivity):
            result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        # Deve encontrar todos os 8 marcadores
        assert result.metadata["marker_count"] == 8
        for marker in ["TODO", "FIXME", "HACK", "XXX"]:
            assert marker in rendered

    async def test_lowercase_todo_recognized(self, git_repo_case_sensitivity: Path):
        with _cd(git_repo_case_sensitivity):
            result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        # "todo" minúsculo deve ser normalizado para "TODO" na coluna Marcador
        assert "TODO" in rendered


class TestTodoOutsideGit:
    async def test_command_error_when_not_in_git(self, tmp_path: Path):
        """Fora de repo git → CommandError."""
        with _cd(tmp_path):
            with pytest.raises(Exception):
                await _cmd().execute(_ctx())


class TestContentType:
    async def test_content_type_is_rich(self, git_repo_with_todos: Path):
        with _cd(git_repo_with_todos):
            result = await _cmd().execute(_ctx())
        assert result.content_type == "rich"

    async def test_content_not_string(self, git_repo_with_todos: Path):
        with _cd(git_repo_with_todos):
            result = await _cmd().execute(_ctx())
        assert not isinstance(result.content, str)


class TestTableColumns:
    async def test_table_has_correct_columns(self, git_repo_with_todos: Path):
        with _cd(git_repo_with_todos):
            result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        assert "Arquivo" in rendered
        assert "Linha" in rendered
        assert "Autor" in rendered
        assert "Idade" in rendered
        assert "Marcador" in rendered


class TestIntegrationWithGit:
    async def test_git_ls_files_respects_gitignore(self, tmp_path: Path):
        """Arquivos em .gitignore não são escaneados."""
        repo = tmp_path / "ignored_repo"
        repo.mkdir()
        _run_git(repo, "init")

        (repo / ".gitignore").write_text("ignored/\n")
        (repo / "tracked.py").write_text("# TODO: visible\n")
        (repo / "ignored").mkdir()
        (repo / "ignored" / "hidden.py").write_text("# TODO: should not appear\n")

        _run_git(repo, "add", ".gitignore", "tracked.py")
        _run_git(repo, "commit", "-m", "with gitignore")

        with _cd(repo):
            result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        assert "tracked.py" in rendered
        assert "hidden.py" not in rendered
        assert result.metadata["marker_count"] == 1


class TestHACKandXXX:
    async def test_hack_marker(self, git_repo_with_todos: Path):
        with _cd(git_repo_with_todos):
            result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        assert "HACK" in rendered

    async def test_xxx_marker(self, tmp_path: Path):
        """Testa o marcador XXX especificamente."""
        repo = tmp_path / "xxx_repo"
        repo.mkdir()
        _run_git(repo, "init")
        (repo / "code.py").write_text("# XXX: dangerous code path\nprint('hi')\n")
        _run_git(repo, "add", "code.py")
        _run_git(repo, "commit", "-m", "xxx test")

        with _cd(repo):
            result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        assert "XXX" in rendered


# ---------------------------------------------------------------------------
# Helper: change directory as context manager
# ---------------------------------------------------------------------------


class _cd:
    """Context manager para mudar temporariamente de diretório."""

    def __init__(self, path: Path):
        self.path = path
        self._prev = None

    def __enter__(self):
        self._prev = os.getcwd()
        os.chdir(str(self.path))
        return self

    def __exit__(self, *args):
        os.chdir(self._prev)
