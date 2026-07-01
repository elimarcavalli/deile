"""MultiSourceActivityProvider._fetch() caps the merged event buffer.

Five mocked sources each return 80 synthetic lines (400 events total); after the
fetch, the merged state must be capped at _MULTI_BUFFER_CAP (200) events.
Deterministic — the cap is the real behavioral signal (the old wall-clock
assertion was meaningless against an instant mocked subprocess).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel_data as pd  # noqa: E402


def _make_80_lines() -> str:
    # Unique task IDs per line to avoid triggering burst aggregation.
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return "".join(
        f"{ts} dispatch.started task=t{i} channel=pipeline-issue-{i}\n"
        for i in range(80)
    )


def test_fetch_caps_event_buffer():
    """Five mocked sources x 80 lines (400 events) -> state capped at _MULTI_BUFFER_CAP."""
    p = pd.MultiSourceActivityProvider(ttl_s=60.0, namespace="test",
                                       enabled=True)
    p._kubectl = "/usr/bin/kubectl"

    def _fast_run(cmd, **kw):
        # Each mock subprocess returns quickly with 80 lines.
        return CompletedProcess(cmd, 0, _make_80_lines(), "")

    # Disable burst aggregation to exercise the raw cap behavior.
    with patch.object(pd, "_BURST_THRESHOLD", 10_000):
        with patch("subprocess.run", side_effect=_fast_run):
            state = p._fetch()

    assert len(state.events) == pd._MULTI_BUFFER_CAP
