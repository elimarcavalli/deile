"""Tests for the kubectl-based vigias (V2 pods, V6 jobs, V7 pipeline) of
``infra/k8s/monitor_vigias.py``.

A fake command runner returns canned ``CmdResult``s keyed by a substring of the
joined argv, so each vigia is exercised against realistic kubectl JSON without a
cluster. The contract under test is BEHAVIORAL: which anomalies get recorded,
which structured events are emitted, and which autonomous cures fire.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_INFRA = str(_REPO / "infra" / "k8s")
if _INFRA not in sys.path:
    sys.path.insert(0, _INFRA)


@pytest.fixture
def core():
    import monitor_core
    return monitor_core


@pytest.fixture
def vig():
    import monitor_vigias
    return monitor_vigias


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


class FakeRunner:
    """Maps a substring of the joined command to a (rc, out) pair."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, args, **kwargs):
        from monitor_core import CmdResult
        joined = " ".join(args)
        self.calls.append(joined)
        for needle, (rc, out) in self.table.items():
            if needle in joined:
                return CmdResult(rc, out, "")
        return CmdResult(0, "", "")


def _ctx(core, vig, runner, *, now=None, capture_notifies=None):
    now = now or _utc(2026, 6, 2, 11, 0, 0)
    state = core.default_state()
    flags = core.TickFlags()
    emitter = core.Emitter("/dev/null", flags, clock=lambda: now)

    class RecordingNotifier:
        def __init__(self):
            self.sent = []

        def notify(self, fingerprint, severity, title, body):
            self.sent.append((fingerprint, severity, title))
            if capture_notifies is not None:
                capture_notifies.append((fingerprint, severity, title))
            return True

    notifier = RecordingNotifier()
    ctx = vig.MonitorContext(
        run=runner, emitter=emitter, notifier=notifier, state=state,
        flags=flags, now=now, repo="elimarcavalli/deile", namespace="deile",
        kube_api="https://kubernetes.default.svc:443",
    )
    return ctx, notifier, state


# ---------------------------------------------------------------------------
# V7 — pipeline health
# ---------------------------------------------------------------------------

def _pods_json(items):
    return json.dumps({"items": items})


def test_v7_healthy_pipeline_emits_fix_no_notify(core, vig):
    pod = {
        "metadata": {"name": "deile-pipeline-abc"},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"restartCount": 0}],
        },
    }
    runner = FakeRunner({"-l app=deile-pipeline": (0, _pods_json([pod]))})
    ctx, notifier, state = _ctx(core, vig, runner)
    vig.vigia_pipeline_health(ctx)
    assert notifier.sent == []
    assert "pipeline_unhealthy_deile-pipeline-abc" not in state["known_anomalies"]


def test_v7_unhealthy_pipeline_notifies_p0(core, vig):
    pod = {
        "metadata": {"name": "deile-pipeline-xyz"},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "False"}],
            "containerStatuses": [{"restartCount": 0}],
        },
    }
    runner = FakeRunner({
        "-l app=deile-pipeline": (0, _pods_json([pod])),
        "logs": (0, "boom\ntraceback"),
    })
    ctx, notifier, state = _ctx(core, vig, runner)
    vig.vigia_pipeline_health(ctx)
    assert any(fp == "pipeline_unhealthy_deile-pipeline-xyz" and sev == "P0"
               for fp, sev, _ in notifier.sent)


# ---------------------------------------------------------------------------
# V2 — error pods + autonomous cleanup
# ---------------------------------------------------------------------------

def test_v2_deletes_abandoned_job_pod(core, vig):
    pod = {
        "metadata": {
            "name": "claude-credentials-renew-1-abc",
            "creationTimestamp": "2026-06-02T08:00:00Z",  # ~3h old
            "ownerReferences": [{"kind": "Job", "name": "claude-credentials-renew-1"}],
        },
        "status": {
            "phase": "Failed",
            "reason": "BackoffLimitExceeded",
            "containerStatuses": [],
        },
    }
    runner = FakeRunner({
        "get pods": (0, _pods_json([pod])),
        "delete pod": (0, "deleted"),
    })
    ctx, notifier, state = _ctx(core, vig, runner)
    vig.vigia_error_pods(ctx)
    assert any("delete pod" in c for c in runner.calls)
    # No notify for a silent cure
    assert notifier.sent == []


def test_v2_does_not_delete_young_job_pod(core, vig):
    pod = {
        "metadata": {
            "name": "claude-credentials-renew-2-def",
            "creationTimestamp": "2026-06-02T10:40:00Z",  # 20 min old (< 1h)
            "ownerReferences": [{"kind": "Job", "name": "claude-credentials-renew-2"}],
        },
        "status": {"phase": "Failed", "reason": "BackoffLimitExceeded", "containerStatuses": []},
    }
    runner = FakeRunner({"get pods": (0, _pods_json([pod]))})
    ctx, notifier, state = _ctx(core, vig, runner)
    vig.vigia_error_pods(ctx)
    assert not any("delete pod" in c for c in runner.calls)


def test_v2_never_deletes_deployment_owned_pod(core, vig):
    pod = {
        "metadata": {
            "name": "deile-worker-aaa",
            "creationTimestamp": "2026-06-01T00:00:00Z",
            "ownerReferences": [{"kind": "ReplicaSet", "name": "deile-worker-rs"}],
        },
        "status": {"phase": "Failed", "reason": "Evicted", "containerStatuses": []},
    }
    runner = FakeRunner({"get pods": (0, _pods_json([pod]))})
    ctx, notifier, state = _ctx(core, vig, runner)
    vig.vigia_error_pods(ctx)
    assert not any("delete pod" in c for c in runner.calls)


def test_v2_crashloop_worker_notifies(core, vig):
    pod = {
        "metadata": {"name": "claude-worker-bbb", "creationTimestamp": "2026-06-02T10:00:00Z",
                     "ownerReferences": [{"kind": "ReplicaSet", "name": "rs"}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {"restartCount": 7, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}
            ],
        },
    }
    runner = FakeRunner({"get pods": (0, _pods_json([pod])), "logs": (0, "stacktrace")})
    ctx, notifier, state = _ctx(core, vig, runner)
    vig.vigia_error_pods(ctx)
    assert any(fp.startswith("pod_crashloop_claude-worker-bbb") for fp, _, _ in notifier.sent)


# ---------------------------------------------------------------------------
# V6 — failed jobs
# ---------------------------------------------------------------------------

def _jobs_json(items):
    return json.dumps({"items": items})


def test_v6_credentials_renew_failure_is_p0(core, vig):
    job = {
        "metadata": {"name": "claude-credentials-renew-29673060",
                     "creationTimestamp": "2026-06-02T07:00:00Z"},
        "status": {"conditions": [{"type": "Failed", "status": "True"}]},
    }
    runner = FakeRunner({"get jobs": (0, _jobs_json([job]))})
    ctx, notifier, state = _ctx(core, vig, runner)
    vig.vigia_failed_jobs(ctx)
    assert any(sev == "P0" and "renew" in fp for fp, sev, _ in notifier.sent)


def test_v6_other_job_failure_is_p1_after_30min(core, vig):
    job = {
        "metadata": {"name": "some-batch-job", "creationTimestamp": "2026-06-02T10:00:00Z"},
        "status": {"conditions": [{"type": "Failed", "status": "True"}]},
    }
    runner = FakeRunner({"get jobs": (0, _jobs_json([job]))})
    ctx, notifier, state = _ctx(core, vig, runner)
    vig.vigia_failed_jobs(ctx)
    assert any(sev == "P1" for _, sev, _ in notifier.sent)
