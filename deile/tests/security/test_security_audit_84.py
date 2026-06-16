"""Security regression tests — issue #84.

Four invariants pinned by this file:

1. hashlib.md5() always carries usedforsecurity=False in non-cryptographic
   usages (content-hashing, cache-keying). Prevents misleading SAST reports.

2. os.system() is absent from ui/cli.py and emoji_support — replaced by
   subprocess.run with a list argument (no shell spawning).

3. The SQL queries in cost_tracker.py always bind user-controlled values via
   parameterised placeholders (?), never via f-string interpolation of raw
   values. where_sql is built from a hard-coded list of clause strings.

4. pyproject.toml declares cryptography>=44.0.0 to address CVE-2023-50782,
   CVE-2024-0727, and PYSEC-2024-225.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

DEILE_ROOT = Path(__file__).parent.parent.parent.parent  # repo root
DEILE_PKG = DEILE_ROOT / "deile"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _source(rel: str) -> str:
    return (DEILE_ROOT / rel).read_text(encoding="utf-8")


def _ast(rel: str) -> ast.Module:
    return ast.parse(_source(rel))


# ---------------------------------------------------------------------------
# 1. hashlib.md5 — usedforsecurity=False
# ---------------------------------------------------------------------------

MD5_FILES = [
    "deile/core/intent_analyzer.py",
    "deile/infrastructure/google_file_api.py",
    "deile/memory/working_memory.py",
    "deile/orchestration/artifact_manager.py",
    "deile/personas/base.py",
]


def _md5_calls_without_flag(tree: ast.Module) -> list[ast.Call]:
    """Return Call nodes that look like hashlib.md5(...) but lack usedforsecurity=False."""
    bad: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_md5 = (isinstance(func, ast.Attribute) and func.attr == "md5") or (
            isinstance(func, ast.Name) and func.id == "md5"
        )
        if not is_md5:
            continue
        has_flag = any(
            kw.arg == "usedforsecurity"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in node.keywords
        )
        if not has_flag:
            bad.append(node)
    return bad


@pytest.mark.security
@pytest.mark.parametrize("rel_path", MD5_FILES)
def test_md5_has_usedforsecurity_false(rel_path: str) -> None:
    """Every hashlib.md5() call must carry usedforsecurity=False."""
    tree = _ast(rel_path)
    bad = _md5_calls_without_flag(tree)
    lines = [n.lineno for n in bad]
    assert not bad, (
        f"{rel_path}: found hashlib.md5() without usedforsecurity=False "
        f"on lines {lines}. Add usedforsecurity=False (content-hashing only)."
    )


# ---------------------------------------------------------------------------
# 2. os.system absent from actions.py and ui/cli.py
# ---------------------------------------------------------------------------

OS_SYSTEM_FREE_FILES = [
    "deile/ui/cli.py",
    "deile/ui/emoji_support.py",
]


def _os_system_calls(tree: ast.Module) -> list[ast.Call]:
    """Return Call nodes for os.system(...)."""
    hits: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "system"
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        ):
            hits.append(node)
    return hits


@pytest.mark.security
@pytest.mark.parametrize("rel_path", OS_SYSTEM_FREE_FILES)
def test_no_os_system(rel_path: str) -> None:
    """os.system() must not appear in ui/cli.py or emoji_support (use subprocess.run)."""
    tree = _ast(rel_path)
    bad = _os_system_calls(tree)
    lines = [n.lineno for n in bad]
    assert not bad, (
        f"{rel_path}: os.system() found on lines {lines}. "
        "Use subprocess.run(['clear'], check=False) instead."
    )


# ---------------------------------------------------------------------------
# 3. cost_tracker.py — SQL parameterisation safety
# ---------------------------------------------------------------------------

# The SQL persistence layer was extracted from cost_tracker.py into
# cost_repository.py; both files are audited so the guarantee follows the code.
_COST_SQL_FILES = (
    "deile/infrastructure/monitoring/cost_tracker.py",
    "deile/infrastructure/monitoring/cost_repository.py",
)


@pytest.mark.security
@pytest.mark.parametrize("rel_path", _COST_SQL_FILES)
def test_cost_sql_uses_params(rel_path: str) -> None:
    """Every f-string execute call must also pass a `params` list."""
    source = _source(rel_path)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Look for conn.execute(f"...", params) patterns
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "execute"):
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        # If the first arg is an f-string (JoinedStr), ensure a second arg (params) exists
        if isinstance(first_arg, ast.JoinedStr):
            assert len(node.args) >= 2, (
                f"{rel_path} line {node.lineno}: "
                "f-string passed to .execute() without a params argument — "
                "this is a SQL injection risk."
            )


@pytest.mark.security
@pytest.mark.parametrize("rel_path", _COST_SQL_FILES)
def test_cost_where_clauses_hardcoded(rel_path: str) -> None:
    """where_clauses must only append string literals (no user-controlled SQL)."""
    source = _source(rel_path)

    # Find all where_clauses.append(...) calls in the source
    pattern = re.compile(r"where_clauses\.append\(([^)]+)\)")
    for m in pattern.finditer(source):
        arg = m.group(1).strip()
        # Argument must be a plain string literal (starts and ends with quote)
        is_string_literal = (arg.startswith('"') and arg.endswith('"')) or (
            arg.startswith("'") and arg.endswith("'")
        )
        assert is_string_literal, (
            f"{rel_path}: where_clauses.append() called with non-literal argument: {arg!r}. "
            "Only hardcoded SQL clause strings are allowed."
        )


# ---------------------------------------------------------------------------
# 4. pyproject.toml — cryptography minimum version
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_pyproject_pins_cryptography() -> None:
    """pyproject.toml must declare cryptography>=44.0.0 to address 2024 CVEs."""
    content = _source("pyproject.toml")
    # Accept any lower-bound >= 44 (e.g. >=44.0.0, >=44, >=44.0)
    match = re.search(r'"cryptography\s*>=\s*(\d+)', content)
    assert match, (
        "pyproject.toml: cryptography dependency not found or missing >= lower bound. "
        "Add 'cryptography>=44.0.0' to fix CVE-2023-50782, CVE-2024-0727, PYSEC-2024-225."
    )
    major = int(match.group(1))
    assert major >= 44, (
        f"pyproject.toml: cryptography>={major}.x.x is below the required >=44.0.0. "
        "Bump to >=44.0.0 to address the 2024 CVEs."
    )
