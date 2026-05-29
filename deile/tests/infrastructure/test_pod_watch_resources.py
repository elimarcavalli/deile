"""Tests para PodWatch RESOURCES header — issue #394.

Cobre:
- PodMetricsProvider: parse de `kubectl top pod`, degradação sem metrics-server
- PodsProvider: extração de OOM history e resource limits
- EndpointProbeProvider: mapeamento de pods para services
- PodWatchView: renderização das linhas RESOURCES e ENDPOINT
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixture: carrega _panel_data dinamicamente (mesmo padrão do
# test_claude_worker_server.py).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def panel_data_mod():
    repo_root = Path(__file__).resolve().parents[3]
    mod_path = repo_root / "infra" / "k8s" / "_panel_data.py"
    spec = importlib.util.spec_from_file_location("_panel_data_under_test", str(mod_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_panel_data_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def panel_mod(panel_data_mod):
    """Carrega _panel.py com _panel_data já no sys.modules."""
    repo_root = Path(__file__).resolve().parents[3]
    infra_k8s = str(repo_root / "infra" / "k8s")
    # _panel.py importa `_panel_data` e `_panel_demo` por nome curto —
    # precisamos que infra/k8s esteja no sys.path para que os módulos
    # sejam encontrados no import normal.
    if infra_k8s not in sys.path:
        sys.path.insert(0, infra_k8s)
    # Inject our dynamically loaded panel_data_mod for the duration of
    # exec_module, then restore the previous value so other test modules
    # that use 'from _panel_data import ...' inline don't get confused.
    prev_panel_data = sys.modules.get("_panel_data")
    sys.modules["_panel_data"] = panel_data_mod
    mod_path = repo_root / "infra" / "k8s" / "_panel.py"
    spec = importlib.util.spec_from_file_location("_panel_under_test", str(mod_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_panel_under_test"] = mod
    spec.loader.exec_module(mod)
    # Restore so other tests' `from _panel_data import X` get the real module.
    if prev_panel_data is None:
        sys.modules.pop("_panel_data", None)
    else:
        sys.modules["_panel_data"] = prev_panel_data
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pod_item(name="deile-worker-abc", app="deile-worker",
              phase="Running", node="node1",
              container_statuses=None, containers=None):
    cs = container_statuses or [{"ready": True, "restartCount": 0}]
    c = containers or [{}]
    return {
        "metadata": {
            "name": name,
            "labels": {"app": app},
        },
        "spec": {
            "nodeName": node,
            "containers": c,
        },
        "status": {
            "phase": phase,
            "startTime": "2026-01-01T00:00:00Z",
            "containerStatuses": cs,
        },
    }


def _get_pods_json(*items):
    return json.dumps({"items": list(items)})


def _get_endpoints_json(*items):
    return json.dumps({"items": list(items)})


def _kubectl_top_output(*rows):
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# PodMetricsProvider
# ---------------------------------------------------------------------------

def test_pod_metrics_provider_parses_kubectl_top_output(panel_data_mod):
    pmp = panel_data_mod.PodMetricsProvider(enabled=True)
    pmp._kubectl = "/usr/bin/kubectl"

    top_text = _kubectl_top_output(
        "deile-worker-abc   230m   412Mi",
        "deile-pipeline-xyz  50m   128Mi",
    )

    with patch.object(panel_data_mod, "_capture_text", return_value=top_text):
        result = pmp._fetch()

    assert "deile-worker-abc" in result
    cpu_mc, mem_b = result["deile-worker-abc"]
    assert cpu_mc == 230
    assert mem_b == 412 * 1024 ** 2

    assert "deile-pipeline-xyz" in result
    cpu_mc2, mem_b2 = result["deile-pipeline-xyz"]
    assert cpu_mc2 == 50
    assert mem_b2 == 128 * 1024 ** 2


def test_pod_metrics_provider_degrades_gracefully_when_metrics_server_absent(panel_data_mod):
    """Quando `kubectl top` falha (exit≠0 → _capture_text devolve None),
    a exceção é capturada pelo Cache e o provider retorna {}."""
    pmp = panel_data_mod.PodMetricsProvider(enabled=True)
    pmp._kubectl = "/usr/bin/kubectl"

    with patch.object(panel_data_mod, "_capture_text", return_value=None):
        with pytest.raises(RuntimeError, match="kubectl top pod falhou"):
            pmp._fetch()

    # Via cache (fallback): não lança
    result = pmp.get()   # cold-start: chama _fetch → captura → retorna {}
    assert result == {}


# ---------------------------------------------------------------------------
# PodsProvider — OOM history
# ---------------------------------------------------------------------------

def test_pods_provider_extracts_oom_killed_from_last_state(panel_data_mod):
    oom_cs = [
        {
            "ready": True,
            "restartCount": 3,
            "lastState": {
                "terminated": {
                    "reason": "OOMKilled",
                    "finishedAt": "2026-05-01T10:00:00Z",
                },
            },
            "state": {"running": {}},
        }
    ]
    item = _pod_item(container_statuses=oom_cs)
    pods_json = _get_pods_json(item)

    provider = panel_data_mod.PodsProvider(enabled=True)
    provider._kubectl = "/usr/bin/kubectl"

    with patch.object(panel_data_mod, "_capture_json",
                      return_value=json.loads(pods_json)):
        pods = provider._fetch()

    assert len(pods) == 1
    pod = pods[0]
    assert pod.oom_killed_count == 1
    assert pod.last_oom_at is not None
    assert pod.last_oom_at.year == 2026


def test_pods_provider_no_oom_when_clean(panel_data_mod):
    item = _pod_item(container_statuses=[{"ready": True, "restartCount": 0}])
    provider = panel_data_mod.PodsProvider(enabled=True)
    provider._kubectl = "/usr/bin/kubectl"

    with patch.object(panel_data_mod, "_capture_json",
                      return_value={"items": [item]}):
        pods = provider._fetch()

    assert pods[0].oom_killed_count == 0
    assert pods[0].last_oom_at is None


# ---------------------------------------------------------------------------
# PodsProvider — resource limits
# ---------------------------------------------------------------------------

def test_pods_provider_extracts_resource_limits(panel_data_mod):
    containers = [
        {
            "name": "app",
            "resources": {
                "limits": {"cpu": "2", "memory": "6Gi"},
            },
        }
    ]
    item = _pod_item(containers=containers)
    provider = panel_data_mod.PodsProvider(enabled=True)
    provider._kubectl = "/usr/bin/kubectl"

    with patch.object(panel_data_mod, "_capture_json",
                      return_value={"items": [item]}):
        pods = provider._fetch()

    pod = pods[0]
    assert pod.cpu_limit_millicores == 2000
    assert pod.mem_limit_bytes == 6 * 1024 ** 3


def test_pods_provider_no_limits_when_unset(panel_data_mod):
    item = _pod_item(containers=[{"name": "app", "resources": {}}])
    provider = panel_data_mod.PodsProvider(enabled=True)
    provider._kubectl = "/usr/bin/kubectl"

    with patch.object(panel_data_mod, "_capture_json",
                      return_value={"items": [item]}):
        pods = provider._fetch()

    pod = pods[0]
    assert pod.cpu_limit_millicores is None
    assert pod.mem_limit_bytes is None


# ---------------------------------------------------------------------------
# EndpointProbeProvider
# ---------------------------------------------------------------------------

def test_endpoint_probe_provider_maps_pods_to_services(panel_data_mod):
    ep_item = {
        "metadata": {"name": "deile-worker"},
        "subsets": [
            {
                "addresses": [
                    {
                        "ip": "10.0.0.1",
                        "targetRef": {"kind": "Pod", "name": "deile-worker-abc"},
                    },
                    {
                        "ip": "10.0.0.2",
                        "targetRef": {"kind": "Pod", "name": "deile-worker-xyz"},
                    },
                ],
            }
        ],
    }
    ep_json = {"items": [ep_item]}

    provider = panel_data_mod.EndpointProbeProvider(enabled=True)
    provider._kubectl = "/usr/bin/kubectl"

    with patch.object(panel_data_mod, "_capture_json", return_value=ep_json):
        result = provider._fetch()

    assert "deile-worker" in result.ready
    assert "deile-worker-abc" in result.ready["deile-worker"]
    assert "deile-worker-xyz" in result.ready["deile-worker"]


def test_endpoint_probe_provider_empty_when_no_subsets(panel_data_mod):
    ep_item = {"metadata": {"name": "deile-worker"}, "subsets": []}
    provider = panel_data_mod.EndpointProbeProvider(enabled=True)
    provider._kubectl = "/usr/bin/kubectl"

    with patch.object(panel_data_mod, "_capture_json",
                      return_value={"items": [ep_item]}):
        result = provider._fetch()

    assert result.ready.get("deile-worker", set()) == set()


# ---------------------------------------------------------------------------
# PodWatchView — RESOURCES line rendering
# ---------------------------------------------------------------------------

def _make_pod(panel_data_mod, name="deile-worker-abc", role="worker",
              cpu_mc=230, mem_b=412*1024**2,
              cpu_lim=2000, mem_lim=6*1024**3,
              oom_count=0, last_oom_at=None):
    pod = panel_data_mod.PodInfo(
        name=name,
        role=role,
        status="Running",
        ready=True,
        restarts=0,
        age_s=3600.0,
        started_at=None,
        node="node1",
        oom_killed_count=oom_count,
        last_oom_at=last_oom_at,
        cpu_limit_millicores=cpu_lim,
        mem_limit_bytes=mem_lim,
        cpu_millicores=cpu_mc,
        mem_bytes=mem_b,
    )
    return pod


def _make_panel_data_mock(panel_data_mod, pod, metrics_map=None, endpoints_map=None):
    """Constrói um PanelData mock com pods, pod_metrics e endpoints."""
    mock_data = MagicMock()
    mock_data.pods.get.return_value = [pod]
    mock_data.workers.get.return_value = {}

    metrics = metrics_map if metrics_map is not None else {
        pod.name: (pod.cpu_millicores, pod.mem_bytes)
    }
    mock_data.pod_metrics.get.return_value = metrics

    if endpoints_map is not None:
        ep = panel_data_mod.EndpointInfo(ready=endpoints_map, not_ready={})
    else:
        ep = panel_data_mod.EndpointInfo(
            ready={"deile-worker": {pod.name}}, not_ready={}
        )
    mock_data.endpoints.get.return_value = ep
    return mock_data


def test_pod_watch_view_renders_resources_line(panel_mod, panel_data_mod):
    pod = _make_pod(panel_data_mod)
    mock_data = _make_panel_data_mock(panel_data_mod, pod)

    view = panel_mod.PodWatchView(data=mock_data)
    view.pod_name = pod.name
    view.pod_role = pod.role

    renderable = view._header_body()
    from rich.console import Console
    console = Console(width=200)
    with console.capture() as cap:
        console.print(renderable)
    text = cap.get()

    assert "RESOURCES" in text
    assert "mem" in text
    assert "cpu" in text
    assert "no OOM history" in text


def test_pod_watch_view_renders_oom_history_in_red(panel_mod, panel_data_mod):
    last_oom = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    pod = _make_pod(panel_data_mod, oom_count=3, last_oom_at=last_oom)
    mock_data = _make_panel_data_mock(panel_data_mod, pod)

    view = panel_mod.PodWatchView(data=mock_data)
    view.pod_name = pod.name
    view.pod_role = pod.role

    renderable = view._header_body()
    from rich.console import Console
    console = Console(width=200)
    with console.capture() as cap:
        console.print(renderable)
    text = cap.get()

    assert "OOM" in text
    assert "×3" in text


def test_pod_watch_view_renders_endpoint_not_ready(panel_mod, panel_data_mod):
    pod = _make_pod(panel_data_mod, role="worker")
    # pod NOT in endpoint ready set
    mock_data = _make_panel_data_mock(
        panel_data_mod, pod,
        endpoints_map={"deile-worker": set()},
    )

    view = panel_mod.PodWatchView(data=mock_data)
    view.pod_name = pod.name
    view.pod_role = pod.role

    renderable = view._header_body()
    from rich.console import Console
    console = Console(width=200)
    with console.capture() as cap:
        console.print(renderable)
    text = cap.get()

    assert "ENDPOINT" in text
    assert "NOT in Service" in text


def test_pod_watch_view_omits_endpoint_line_when_pod_in_endpoints(panel_mod, panel_data_mod):
    pod = _make_pod(panel_data_mod, role="worker")
    mock_data = _make_panel_data_mock(
        panel_data_mod, pod,
        endpoints_map={"deile-worker": {pod.name}},
    )

    view = panel_mod.PodWatchView(data=mock_data)
    view.pod_name = pod.name
    view.pod_role = pod.role

    renderable = view._header_body()
    from rich.console import Console
    console = Console(width=200)
    with console.capture() as cap:
        console.print(renderable)
    text = cap.get()

    assert "ENDPOINT" not in text


def test_pod_watch_view_omits_endpoint_line_for_pipeline(panel_mod, panel_data_mod):
    pod = _make_pod(panel_data_mod, role="pipeline", name="deile-pipeline-abc")
    mock_data = _make_panel_data_mock(panel_data_mod, pod, endpoints_map={})

    view = panel_mod.PodWatchView(data=mock_data)
    view.pod_name = pod.name
    view.pod_role = pod.role

    renderable = view._header_body()
    from rich.console import Console
    console = Console(width=200)
    with console.capture() as cap:
        console.print(renderable)
    text = cap.get()

    assert "ENDPOINT" not in text


def test_pod_watch_view_shows_dim_metrics_when_kubectl_top_unavailable(panel_mod, panel_data_mod):
    """Quando metrics-server ausente (metrics_map vazio), mostra '?' sem crashar."""
    pod = _make_pod(panel_data_mod, cpu_mc=None, mem_b=None)
    # metrics_map empty → no data for this pod
    mock_data = _make_panel_data_mock(panel_data_mod, pod, metrics_map={})

    view = panel_mod.PodWatchView(data=mock_data)
    view.pod_name = pod.name
    view.pod_role = pod.role

    renderable = view._header_body()
    from rich.console import Console
    console = Console(width=200)
    with console.capture() as cap:
        console.print(renderable)
    text = cap.get()

    assert "RESOURCES" in text
    assert "?" in text


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------

def test_parse_cpu(panel_data_mod):
    assert panel_data_mod._parse_cpu("230m") == 230
    assert panel_data_mod._parse_cpu("2") == 2000
    assert panel_data_mod._parse_cpu("0.5") == 500
    assert panel_data_mod._parse_cpu("") is None
    assert panel_data_mod._parse_cpu("badval") is None


def test_parse_mem(panel_data_mod):
    assert panel_data_mod._parse_mem("412Mi") == 412 * 1024 ** 2
    assert panel_data_mod._parse_mem("6Gi") == 6 * 1024 ** 3
    assert panel_data_mod._parse_mem("1024Ki") == 1024 * 1024
    assert panel_data_mod._parse_mem("1024") == 1024
    assert panel_data_mod._parse_mem("") is None


def test_pct(panel_data_mod):
    assert panel_data_mod._pct(50, 100) == pytest.approx(50.0)
    assert panel_data_mod._pct(None, 100) is None
    assert panel_data_mod._pct(50, 0) is None
    assert panel_data_mod._pct(50, None) is None
