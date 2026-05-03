"""Regression guard for issue #45.

Recursively scans every ``test_*.py`` under ``deile/tests/might/`` and flags
any top-level ``test_*`` function whose positional arguments are not standard
pytest fixtures. Such functions cause pytest to fail at collection with
``fixture '<name>' not found`` (the original #45 symptom), regardless of
which file they live in.

Generalised over the original single-file guard so that any future runner
script added under ``might/`` is covered automatically. Top-level only —
methods of ``Test*`` classes are intentionally excluded (pytest's own class
collection handles them).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Built-in pytest fixtures + the conventional ``self`` for class methods.
# A ``test_*`` function whose positional args fall outside this set will
# fail collection with ``fixture not found`` unless a matching fixture is
# defined in a conftest somewhere on the path. Runner scripts in ``might/``
# never define such fixtures, so we treat any unknown name as a leak.
_STANDARD_PYTEST_FIXTURES = frozenset(
    {
        "self",
        "cls",
        "monkeypatch",
        "tmp_path",
        "tmp_path_factory",
        "tmpdir",
        "tmpdir_factory",
        "capsys",
        "capsysbinary",
        "capfd",
        "capfdbinary",
        "caplog",
        "request",
        "pytestconfig",
        "record_property",
        "record_xml_attribute",
        "record_testsuite_property",
        "recwarn",
        "cache",
        "doctest_namespace",
        "testdir",
        "pytester",
    }
)


def _find_leaks_in_file(py_file: Path) -> list[str]:
    """Return human-readable descriptions of leaking test_* functions."""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        # If a file in might/ doesn't even parse, that's a separate problem;
        # don't mask it as a fixture-leak issue.
        return []

    leaks: list[str] = []
    for node in tree.body:  # top-level only — skip class methods on purpose
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        positional_args = [a.arg for a in node.args.args]
        unknown = [a for a in positional_args if a not in _STANDARD_PYTEST_FIXTURES]
        if unknown:
            leaks.append(f"{node.name}(unresolved fixture-shaped args: {unknown})")
    return leaks


@pytest.mark.unit
def test_might_runner_scripts_have_no_unresolved_fixture_test_funcs():
    might_dir = Path(__file__).parent
    self_path = Path(__file__).resolve()

    findings: dict[str, list[str]] = {}
    for py_file in sorted(might_dir.rglob("test_*.py")):
        if py_file.resolve() == self_path:
            continue
        leaks = _find_leaks_in_file(py_file)
        if leaks:
            rel = py_file.relative_to(might_dir.parents[1])  # relative to deile/
            findings[str(rel)] = leaks

    assert not findings, (
        "Found top-level def test_* functions under deile/tests/might/ whose "
        "positional arguments look like pytest fixtures but are not standard "
        "ones. pytest will collect them and fail at setup with 'fixture not "
        "found' (original #45 symptom). Either rename the function (test_* -> "
        "run_*) or add the fixture to a conftest. Findings: " + repr(findings)
    )
