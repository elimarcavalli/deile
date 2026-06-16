"""Tests for _build_file_context token-overflow fix.

Verifies that the file context injected into the system prompt:
  - Never exceeds _FILE_CONTEXT_MAX_CHARS characters.
  - Properly prunes ignored directories (does not descend into them).
  - Excludes binary/compiled file extensions.
  - Logs a warning when truncation occurs.
  - Returns empty string for non-existent working directories.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.core.context_manager import ContextManager

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_tree(tmp_path: Path, spec: dict) -> None:
    """Recursively create files and directories from a spec dict.

    Keys ending with '/' are directories; their values are nested specs.
    Other keys are files; their values are file contents (str).
    """
    for name, value in spec.items():
        if name.endswith("/"):
            subdir = tmp_path / name.rstrip("/")
            subdir.mkdir(parents=True, exist_ok=True)
            _make_tree(subdir, value)
        else:
            (tmp_path / name).write_text(value, encoding="utf-8")


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def cm() -> ContextManager:
    return ContextManager()


@pytest.fixture()
def simple_project(tmp_path: Path) -> Path:
    """A small clean project tree."""
    _make_tree(
        tmp_path,
        {
            "README.md": "# project",
            "pyproject.toml": "[project]",
            "src/": {
                "main.py": "print('hello')",
                "utils.py": "pass",
            },
        },
    )
    return tmp_path


@pytest.fixture()
def project_with_ignored_dirs(tmp_path: Path) -> Path:
    """Tree with directories that must be pruned."""
    _make_tree(
        tmp_path,
        {
            "main.py": "x = 1",
            "__pycache__/": {
                "main.cpython-311.pyc": "binary content",
            },
            ".git/": {
                "HEAD": "ref: refs/heads/main",
                "config": "[core]",
            },
            "venv/": {
                "pyvenv.cfg": "include-system-site-packages = false",
                "lib/": {
                    "site-packages/": {
                        "requests/": {
                            "__init__.py": "# requests",
                        }
                    }
                },
            },
            "deile_bot/": {
                "bot.py": "# legacy clone name (transitional)",
                "requirements.txt": "discord.py",
            },
            "deilebot/": {
                "daemon.py": "# canonical clone name",
                "pyproject.toml": '[project]\nname = "deilebot"',
            },
            "node_modules/": {
                "lodash/": {
                    "index.js": "// lodash",
                }
            },
            "work_items/": {
                "PLAN.md": "# planning",
            },
        },
    )
    return tmp_path


@pytest.fixture()
def project_with_binary_files(tmp_path: Path) -> Path:
    """Tree with binary extensions that must be excluded."""
    _make_tree(
        tmp_path,
        {
            "main.py": "pass",
            "compiled.pyc": "\x00\x00",  # compiled Python
            "lib.so": "\x7fELF",  # shared object
            "image.png": "\x89PNG\r\n",  # PNG header
            "archive.zip": "PK",  # ZIP
            "data.sqlite3": "SQLite format",  # DB
        },
    )
    return tmp_path


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_returns_empty_for_nonexistent_dir(cm: ContextManager) -> None:
    result = await cm._build_file_context(
        None, working_directory="/nonexistent/path/xyz"
    )
    assert result == ""


@pytest.mark.unit
async def test_basic_project_lists_files(
    cm: ContextManager, simple_project: Path
) -> None:
    result = await cm._build_file_context(None, working_directory=str(simple_project))
    assert result != ""
    assert "README.md" in result
    assert "pyproject.toml" in result
    assert "main.py" in result


@pytest.mark.unit
async def test_ignored_dirs_are_pruned(
    cm: ContextManager, project_with_ignored_dirs: Path
) -> None:
    result = await cm._build_file_context(
        None, working_directory=str(project_with_ignored_dirs)
    )

    # These belong to pruned directories — must NOT appear.
    assert "main.cpython-311.pyc" not in result, "__pycache__ was not pruned"
    assert "HEAD" not in result, ".git was not pruned"
    assert "pyvenv.cfg" not in result, "venv was not pruned"
    assert "bot.py" not in result, "deile_bot was not pruned"
    assert "daemon.py" not in result, "deilebot was not pruned"
    assert "lodash" not in result, "node_modules was not pruned"
    assert "PLAN.md" not in result, "work_items was not pruned"

    # The top-level file should still be visible.
    assert "main.py" in result


@pytest.mark.unit
async def test_binary_extensions_excluded(
    cm: ContextManager, project_with_binary_files: Path
) -> None:
    result = await cm._build_file_context(
        None, working_directory=str(project_with_binary_files)
    )

    assert "compiled.pyc" not in result, ".pyc files must be excluded"
    assert "lib.so" not in result, ".so files must be excluded"
    assert "image.png" not in result, ".png files must be excluded"
    assert "archive.zip" not in result, ".zip files must be excluded"
    assert "data.sqlite3" not in result, ".sqlite3 files must be excluded"

    # The Python source must still appear.
    assert "main.py" in result


@pytest.mark.unit
async def test_output_does_not_exceed_char_limit(
    cm: ContextManager, tmp_path: Path
) -> None:
    """Generate many files and confirm output stays within _FILE_CONTEXT_MAX_CHARS."""
    for i in range(500):
        (tmp_path / f"module_{i:04d}.py").write_text("pass", encoding="utf-8")

    result = await cm._build_file_context(None, working_directory=str(tmp_path))
    assert (
        len(result) <= cm._FILE_CONTEXT_MAX_CHARS
    ), f"Output length {len(result)} exceeds limit {cm._FILE_CONTEXT_MAX_CHARS}"


@pytest.mark.unit
async def test_truncation_warning_logged(cm: ContextManager, tmp_path: Path) -> None:
    """A WARNING must be emitted when the file list is truncated.

    Uses unittest.mock.patch rather than caplog so that the test is robust
    to the logging.disable() calls made by other tests in the suite.
    """
    from unittest.mock import patch

    for i in range(500):
        (tmp_path / f"module_{i:04d}.py").write_text("pass", encoding="utf-8")

    with patch("deile.core.context_manager.logger") as mock_logger:
        await cm._build_file_context(None, working_directory=str(tmp_path))

    # Collect all warning call args
    warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
    assert any(
        "_build_file_context" in call and "truncated" in call for call in warning_calls
    ), f"Expected truncation WARNING. Got warning calls: {warning_calls}"


@pytest.mark.unit
async def test_no_warning_for_small_project(
    cm: ContextManager, simple_project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No warning should appear for a project that fits within the limit."""
    import logging

    with caplog.at_level(logging.WARNING, logger="deile.core.context_manager"):
        await cm._build_file_context(None, working_directory=str(simple_project))

    truncation_records = [
        r
        for r in caplog.records
        if "_build_file_context" in r.message and "truncated" in r.message
    ]
    assert not truncation_records, "Unexpected truncation WARNING for a small project"


@pytest.mark.unit
async def test_session_working_directory_used(
    cm: ContextManager, simple_project: Path
) -> None:
    """When a session object is provided, its working_directory takes precedence."""

    class _FakeSession:
        working_directory = simple_project

    result = await cm._build_file_context(_FakeSession())
    assert "README.md" in result


@pytest.mark.unit
async def test_estimated_tokens_below_provider_limit(
    cm: ContextManager, tmp_path: Path
) -> None:
    """Rough token estimate of the output must be far below any provider's limit."""
    for i in range(500):
        (tmp_path / f"module_{i:04d}.py").write_text("pass", encoding="utf-8")

    result = await cm._build_file_context(None, working_directory=str(tmp_path))
    estimated_tokens = len(result) // 4  # 1 token ≈ 4 chars
    # The fix keeps it under 2 000 tokens; sanity-check against 10 000 to be loose.
    assert (
        estimated_tokens < 10_000
    ), f"File context uses ~{estimated_tokens} tokens — dangerously close to provider limits"
