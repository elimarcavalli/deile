"""
Smoke tests verifying that the 3 autouse fixtures in deile/tests/conftest.py
guarantee ordering-determinism for the 9 known ordering-dependent tests
(issues #432/#471, delivered in PR #434).

Target node IDs (verbatim):
    deile/tests/orchestration/pipeline/test_runner_token_warning.py::test_warns_when_no_tokens
    deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_idle_tick_logs_summary_with_zeros
    deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_reflects_classify_delta
    deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_reflects_review_delta
    deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_reflects_implement_delta
    deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_reflects_dispatched_delta
    deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_backlog_unavailable_on_forge_error
    deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_includes_backlog_counts
    deile/tests/orchestration/test_subagent_orchestrator.py::test_renderer_task_awaited_before_stdout_restore
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_REPO_ROOT = Path(__file__).parent.parent.parent

_TARGET_TESTS = [
    "deile/tests/orchestration/pipeline/test_runner_token_warning.py::test_warns_when_no_tokens",
    "deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_idle_tick_logs_summary_with_zeros",
    "deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_reflects_classify_delta",
    "deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_reflects_review_delta",
    "deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_reflects_implement_delta",
    "deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_reflects_dispatched_delta",
    "deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_backlog_unavailable_on_forge_error",
    "deile/tests/orchestration/pipeline/test_monitor.py::TestTickSummary::test_tick_summary_includes_backlog_counts",
    "deile/tests/orchestration/test_subagent_orchestrator.py::test_renderer_task_awaited_before_stdout_restore",
]


def test_ordering_smoke_reversed(pytester):
    """9 ordering-dependent tests run last→first with random ordering disabled."""
    reversed_tests = [str(_REPO_ROOT / t) for t in reversed(_TARGET_TESTS)]
    result = pytester.runpytest(
        *reversed_tests,
        "-p", "no:randomly",
        "-p", "no:cov",
        f"--rootdir={_REPO_ROOT}",
        "-q",
        "--timeout=120",
    )
    assert result.ret == 0


def test_ordering_smoke_random_seed_42(pytester):
    """9 ordering-dependent tests run with fixed random seed 42."""
    pytest.importorskip("pytest_randomly")
    target_tests = [str(_REPO_ROOT / t) for t in _TARGET_TESTS]
    result = pytester.runpytest(
        *target_tests,
        "--randomly-seed=42",
        "-p", "no:cov",
        f"--rootdir={_REPO_ROOT}",
        "-q",
        "--timeout=120",
    )
    assert result.ret == 0
