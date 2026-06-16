"""AC9: MultiSourceActivityProvider._fetch() completes in <500ms.

Skipped unless DEILE_PERF_TEST=1 (or unset — skip is opt-out via =0).
Five sources each return 80 synthetic lines via a mocked subprocess.run;
total wall-clock time must be under 500ms.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel_data as pd  # noqa: E402

_SKIP = os.environ.get("DEILE_PERF_TEST", "1") == "0"


def _make_80_lines() -> str:
    # Unique task IDs per line to avoid triggering burst aggregation.
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return "".join(
        f"{ts} dispatch.started task=t{i} channel=pipeline-issue-{i}\n"
        for i in range(80)
    )


@pytest.mark.skipif(_SKIP, reason="DEILE_PERF_TEST=0")
def test_fetch_completes_under_500ms():
    """Five mocked sources each returning 80 lines; _fetch must finish <500ms."""
    p = pd.MultiSourceActivityProvider(ttl_s=60.0, namespace="test", enabled=True)
    p._kubectl = "/usr/bin/kubectl"

    def _fast_run(cmd, **kw):
        # Each mock subprocess returns quickly with 80 lines.
        return CompletedProcess(cmd, 0, _make_80_lines(), "")

    # Disable burst aggregation to test raw throughput / cap behavior.
    start = time.perf_counter()
    with patch.object(pd, "_BURST_THRESHOLD", 10_000):
        with patch("subprocess.run", side_effect=_fast_run):
            state = p._fetch()
    elapsed = time.perf_counter() - start

    assert (
        elapsed < 0.5
    ), f"_fetch() took {elapsed:.3f}s — expected <0.5s (ThreadPoolExecutor)"
    assert len(state.events) == pd._MULTI_BUFFER_CAP
