"""Tests for ``infra/k8s/pipeline_status_server.py`` (issue #347).

Mirrors the loading pattern used by
``deile/tests/infrastructure/test_claude_worker_server.py``: dynamically
load the server module via ``importlib.util`` so the ``infra/k8s/`` scripts
(which are NOT a Python package) can be exercised in-process without
modifying ``sys.path`` globally.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

_AUTH_HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def status_module():
    repo_root = Path(__file__).resolve().parents[4]
    server_path = repo_root / "infra" / "k8s" / "pipeline_status_server.py"
    spec = importlib.util.spec_from_file_location(
        "pipeline_status_server_under_test", str(server_path),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pipeline_status_server_under_test"] = mod
    spec.loader.exec_module(mod)
    # Reset the singleton so tests do not bleed state into each other.
    mod.reset_global_state()
    yield mod
    mod.reset_global_state()


async def test_health_returns_200_without_auth(status_module):
    """``/v1/health`` is whitelisted from auth (K8s probe lane)."""
    app = status_module.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"


async def test_status_returns_tick_metrics(status_module):
    """``GET /v1/pipeline-status`` reflects ``ticks_total`` / timestamps."""
    state = status_module.PipelineStatusState()
    state.record_tick(now=1716830000.0, next_tick_at=1716830060.0)
    state.record_tick(now=1716830060.0, next_tick_at=1716830120.0)
    state.record_error()
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/pipeline-status", headers=_AUTH_HEADERS)
        body = await resp.json()
    assert resp.status == 200
    assert body["ticks_total"] == 2
    assert body["errors_total"] == 1
    assert body["last_tick_at"] == 1716830060.0
    assert body["next_tick_at"] == 1716830120.0


async def test_status_includes_pod_visibility(status_module):
    """``pods_seen`` is surfaced verbatim — used by the cluster status screen."""
    state = status_module.PipelineStatusState()
    state.set_pods_seen({
        "deile-worker": {"ready_replicas": 2, "phase": "Running"},
        "claude-worker": {"ready_replicas": 1, "phase": "Running"},
    })
    state.set_schedule_summary({"poll_interval_seconds": 60})
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/pipeline-status", headers=_AUTH_HEADERS)
        body = await resp.json()
    assert body["pods_seen"]["deile-worker"]["ready_replicas"] == 2
    assert body["schedule_summary"]["poll_interval_seconds"] == 60


async def test_backlog_lists_eligible_items(status_module):
    """``/backlog`` returns whatever the monitor published, in order."""
    state = status_module.PipelineStatusState()
    state.set_backlog([
        {"kind": "issue", "number": 12, "title": "feat: x",
         "labels": ["~workflow:nova"], "age_seconds": 300, "why_eligible": "new"},
        {"kind": "pr", "number": 99, "title": "review me",
         "labels": ["~review:pendente"], "age_seconds": 60, "why_eligible": "review"},
    ])
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/pipeline-status/backlog", headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    assert resp.status == 200
    assert len(body["backlog"]) == 2
    assert body["backlog"][0]["number"] == 12


async def test_backlog_explains_why_eligible(status_module):
    """Every backlog row carries ``why_eligible`` (motivation, not just label)."""
    state = status_module.PipelineStatusState()
    state.set_backlog([
        {"kind": "issue", "number": 7,
         "why_eligible": "labeled ~workflow:nova in last 5m"},
    ])
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/pipeline-status/backlog", headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    assert "why_eligible" in body["backlog"][0]
    assert body["backlog"][0]["why_eligible"].startswith("labeled")


async def test_recent_returns_chronological_events(status_module):
    """``/recent`` is sorted newest-first."""
    state = status_module.PipelineStatusState()
    state.record_event(event_type="merged", summary="PR #346 merged",
                       ts=1716830000.0)
    state.record_event(event_type="started", summary="PR #346 review started",
                       ts=1716829000.0)
    state.record_event(event_type="merged", summary="PR #343 merged",
                       ts=1716828000.0)
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/pipeline-status/recent", headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    events = body["events"]
    assert [e["ts"] for e in events] == [1716830000.0, 1716829000.0, 1716828000.0]


async def test_recent_truncates_old_events(status_module):
    """Capacity cap drops oldest events to keep memory bounded."""
    state = status_module.PipelineStatusState(recent_capacity=3)
    for i in range(10):
        state.record_event(event_type="tick", summary=f"event-{i}", ts=float(i))
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/pipeline-status/recent", headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    # Only the latest 3 survive — sorted newest-first.
    assert [e["summary"] for e in body["events"]] == ["event-9", "event-8", "event-7"]


async def test_recent_respects_limit_query(status_module):
    """``?limit=N`` caps the response."""
    state = status_module.PipelineStatusState()
    for i in range(8):
        state.record_event(event_type="tick", summary=f"e{i}", ts=float(i))
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/pipeline-status/recent?limit=2", headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    assert len(body["events"]) == 2


async def test_ledger_returns_snapshot(status_module):
    """``/ledger`` mirrors the structure the monitor published."""
    state = status_module.PipelineStatusState()
    state.set_ledger_snapshot({
        "issue:345": {"stage": "implement", "task_id": "abc", "attempt": 2},
        "pr:346": {"stage": "pr_review", "task_id": "xyz", "attempt": 1},
    })
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/pipeline-status/ledger", headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    assert resp.status == 200
    assert body["ledger"]["issue:345"]["task_id"] == "abc"
    assert body["ledger"]["pr:346"]["attempt"] == 1


async def test_reaper_preview_lists_planned_actions(status_module):
    """``/reaper-preview`` returns the next-tick planned reaper work."""
    state = status_module.PipelineStatusState()
    state.set_reaper_preview([
        {"kind": "issue", "number": 999, "age": 7200,
         "action_planned": "clear stale ~by:default lock"},
    ])
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/pipeline-status/reaper-preview", headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    assert len(body["actions"]) == 1
    assert body["actions"][0]["action_planned"].startswith("clear")


async def test_force_tick_triggers_immediate_run(status_module):
    """A registered callback fires; response says ``triggered=True``."""
    state = status_module.PipelineStatusState()
    fired = []
    state.set_force_tick_callback(lambda: fired.append(True))
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/pipeline/force-tick", headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    assert resp.status == 200
    assert body["triggered"] is True
    assert fired == [True]


async def test_force_tick_returns_409_without_callback(status_module):
    """Without a callback, force-tick is a no-op and reports the reason."""
    state = status_module.PipelineStatusState()
    # no callback registered
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/pipeline/force-tick", headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    assert resp.status == 409
    assert body["triggered"] is False


async def test_endpoints_require_bearer(status_module):
    """All status endpoints — except /v1/health — require Bearer auth."""
    state = status_module.PipelineStatusState()
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        for path in (
            "/v1/pipeline-status",
            "/v1/pipeline-status/backlog",
            "/v1/pipeline-status/recent",
            "/v1/pipeline-status/ledger",
            "/v1/pipeline-status/reaper-preview",
        ):
            resp = await client.get(path)
            assert resp.status == 401, f"{path} did not require bearer"
        resp = await client.post("/v1/pipeline/force-tick")
        assert resp.status == 401


async def test_build_app_raises_without_token_source(status_module, monkeypatch):
    """When no token file/env is set and no override is passed, error early."""
    monkeypatch.delenv("DEILE_PIPELINE_STATUS_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("DEILE_PIPELINE_STATUS_AUTH_TOKEN_FILE", raising=False)
    with pytest.raises(RuntimeError):
        status_module.build_app()


async def test_force_tick_swallows_callback_exception(status_module):
    """A throwing callback does NOT 500 — it reports triggered=False/409.

    The server is a passive observer; a buggy callback must never take it down.
    """
    state = status_module.PipelineStatusState()

    def boom():
        raise RuntimeError("monitor is wedged")

    state.set_force_tick_callback(boom)
    app = status_module.build_app(auth_token="test-token", state=state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/pipeline/force-tick", headers=_AUTH_HEADERS,
        )
    assert resp.status == 409


def test_record_tick_accepts_monitor_publisher_kwargs(status_module):
    """``record_tick`` must accept the kwargs ``monitor._publish_status_state``
    actually sends (``duration_seconds``, ``ticks_total``, ``errors_total``)
    — without this the publish call raises TypeError and the endpoint
    forever reports zeros (regression of PR #352 first-fix attempt).
    """
    state = status_module.PipelineStatusState()
    # The exact call shape used by ``monitor._publish_status_state``.
    state.record_tick(duration_seconds=0.42, ticks_total=7, errors_total=2)
    snap = state.snapshot_status()
    assert snap["ticks_total"] == 7
    assert snap["errors_total"] == 2
    assert snap["last_tick_duration_seconds"] == 0.42
    assert snap["last_tick_at"] is not None
