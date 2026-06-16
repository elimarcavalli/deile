"""AC1 — vocabulary completeness: exactly 15 log_* functions."""

from __future__ import annotations

import ast
from pathlib import Path

_MODULE = (
    Path(__file__).parent.parent.parent.parent
    / "orchestration"
    / "pipeline"
    / "pipeline_logger.py"
)

_EXPECTED = sorted(
    [
        "log_auth_backoff",
        "log_auth_fail",
        "log_auth_recover",
        "log_auth_skip",
        "log_batch_claim",
        "log_batch_release",
        "log_decomposition_fanout",
        "log_label_change",
        "log_reaper_block",
        "log_reaper_unblock",
        "log_refinement_critique",
        "log_refinement_refine",
        "log_routing_dropped",
        "log_routing_mention",
        "log_routing_pr_unified",
    ]
)


def test_exactly_15_log_functions():
    tree = ast.parse(_MODULE.read_text())
    names = sorted(
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("log_")
    )
    assert names == _EXPECTED, f"Got: {names}"
