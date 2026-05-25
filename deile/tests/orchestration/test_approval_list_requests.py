"""Regression test for ``ApprovalSystem.list_requests`` storage scan.

Caught during self-review of the bug-audit /simplify pass: the opus
agent dropped ``import json`` after migrating ``_save_request`` /
``_load_request`` to the shared ``aio_fileio`` helpers, but
``list_requests`` (further down the file) still referenced ``json.load``.
Calling ``list_requests`` against any directory that contained at least
one approval file raised ``NameError: name 'json' is not defined`` —
silently swallowed by the broad ``except Exception`` and reported as a
"Error reading approval requests" log line, but the storage scan
returned only pending requests (never the persisted ones).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.orchestration.approval_system import ApprovalSystem


@pytest.fixture()
def approvals_dir(tmp_path: Path) -> Path:
    d = tmp_path / "approvals"
    d.mkdir()
    return d


async def test_list_requests_reads_persisted_files(approvals_dir: Path) -> None:
    """A pre-existing approval JSON must be returned by list_requests."""
    payload = (
        '{"request_id":"r1","step_id":"s1","plan_id":"p1",'
        '"tool_name":"t","operation":"o","status":"approved",'
        '"risk_level":"low"}'
    )
    (approvals_dir / "r1.json").write_text(payload)

    system = ApprovalSystem(approvals_dir=approvals_dir)
    out = await system.list_requests()

    assert len(out) == 1
    assert out[0].get("request_id") == "r1"


async def test_list_requests_empty_dir_does_not_raise(approvals_dir: Path) -> None:
    system = ApprovalSystem(approvals_dir=approvals_dir)
    out = await system.list_requests()
    assert out == []
