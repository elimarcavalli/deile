"""Testes de registro de presença de pod e recuperação proativa de lease (issue #495).

Cobre:
- _write_presence: cria/atualiza <root>/.pods/<pod>.presence de forma atômica.
- _get_alive_pods: retorna conjunto correto com base no PRESENCE_TTL_S.
- _lease_is_stale com alive_pods: recuperação imediata quando pod não está vivo.
- _workspace_is_stale com alive_pods: recuperação imediata sem aguardar TTL.
- startup_cleanup integrado: workdir com lease de pod morto é recuperado.
- _cleanup_stale_workspaces integrado: workdir com lease de pod morto é removido.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"


@pytest.fixture
def cws():
    k8s_dir = str(_INFRA_K8S)
    if k8s_dir not in sys.path:
        sys.path.insert(0, k8s_dir)
    spec = importlib.util.spec_from_file_location(
        "cws_presence_test",
        str(_INFRA_K8S / "claude_worker_server.py"),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cws_presence_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def work_root(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def _make_workdir(root: Path, name: str = "aabbccddeeff0011") -> Path:
    d = root / name
    d.mkdir()
    return d


def _write_lease(
    workdir: Path, heartbeat_at: float, pod: str = "test-pod", pid: int = 999999999
) -> None:
    (workdir / ".lease.json").write_text(
        json.dumps({"heartbeat_at": heartbeat_at, "pid": pid, "pod": pod}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# _write_presence
# ---------------------------------------------------------------------------


def test_write_presence_creates_file(cws, work_root, monkeypatch):
    """_write_presence cria <root>/.pods/<pod>.presence com JSON correto."""
    monkeypatch.setenv("HOSTNAME", "worker-abc123")
    cws._write_presence(work_root)

    pfile = work_root / ".pods" / "worker-abc123.presence"
    assert pfile.exists()
    data = json.loads(pfile.read_text())
    assert data["pod"] == "worker-abc123"
    assert abs(time.time() - data["written_at"]) < 5


def test_write_presence_updates_written_at(cws, work_root, monkeypatch):
    """_write_presence segunda chamada atualiza written_at."""
    monkeypatch.setenv("HOSTNAME", "worker-update")
    cws._write_presence(work_root)
    pfile = work_root / ".pods" / "worker-update.presence"
    first_ts = json.loads(pfile.read_text())["written_at"]

    # Pequena pausa para garantir ts diferente
    time.sleep(0.05)
    cws._write_presence(work_root)
    second_ts = json.loads(pfile.read_text())["written_at"]

    assert second_ts >= first_ts


# ---------------------------------------------------------------------------
# _get_alive_pods
# ---------------------------------------------------------------------------


def test_get_alive_pods_returns_fresh_pod(cws, work_root, monkeypatch):
    """Pod com written_at recente é incluído no conjunto de vivos."""
    monkeypatch.setenv("HOSTNAME", "pod-fresh")
    cws._write_presence(work_root)

    alive = cws._get_alive_pods(work_root)
    assert "pod-fresh" in alive


def test_get_alive_pods_excludes_expired(cws, work_root):
    """Pod com written_at > PRESENCE_TTL_S no passado não é considerado vivo."""
    pdir = work_root / ".pods"
    pdir.mkdir()
    old_ts = time.time() - (cws._PRESENCE_TTL_S + 10)
    (pdir / "dead-pod.presence").write_text(
        json.dumps({"pod": "dead-pod", "written_at": old_ts}),
        encoding="utf-8",
    )

    alive = cws._get_alive_pods(work_root)
    assert "dead-pod" not in alive


def test_get_alive_pods_returns_none_when_no_pods_dir(cws, work_root):
    """Sem diretório .pods, retorna None (presença não inicializada)."""
    alive = cws._get_alive_pods(work_root)
    assert alive is None


def test_get_alive_pods_ignores_corrupt_file(cws, work_root):
    """Arquivo .presence mal-formado é ignorado silenciosamente."""
    pdir = work_root / ".pods"
    pdir.mkdir()
    (pdir / "bad.presence").write_text("NOT JSON", encoding="utf-8")

    alive = cws._get_alive_pods(work_root)
    assert "bad" not in alive


# ---------------------------------------------------------------------------
# _lease_is_stale com alive_pods
# ---------------------------------------------------------------------------


def test_lease_is_stale_with_dead_pod_in_alive_pods(cws, work_root):
    """Lease de pod ausente de alive_pods é considerado stale imediatamente."""
    wd = _make_workdir(work_root)
    # Heartbeat bem recente — sem alive_pods seria considerado ativo.
    fresh = time.time() - 1
    _write_lease(wd, fresh, pod="dead-pod-x")

    # alive_pods não contém "dead-pod-x"
    assert cws._lease_is_stale(wd / ".lease.json", alive_pods={"other-pod"}) is True


def test_lease_is_not_stale_when_pod_is_alive(cws, work_root):
    """Lease de pod presente em alive_pods não é considerado stale por presença."""
    wd = _make_workdir(work_root)
    fresh = time.time() - 1
    _write_lease(wd, fresh, pod="live-pod")

    assert cws._lease_is_stale(wd / ".lease.json", alive_pods={"live-pod"}) is False


def test_lease_is_stale_fallback_without_alive_pods(cws, work_root):
    """Sem alive_pods, comportamento original por heartbeat TTL permanece."""
    wd = _make_workdir(work_root)
    expired = time.time() - 3600
    _write_lease(wd, expired, pid=999999999)

    assert cws._lease_is_stale(wd / ".lease.json", alive_pods=None) is True


# ---------------------------------------------------------------------------
# _workspace_is_stale com alive_pods
# ---------------------------------------------------------------------------


def test_workspace_is_stale_with_dead_pod(cws, work_root):
    """workspace_is_stale retorna True quando pod dono não está em alive_pods."""
    wd = _make_workdir(work_root)
    # Heartbeat fresquíssimo — sem alive_pods, seria ativo.
    _write_lease(wd, time.time(), pod="ghost-pod")

    assert (
        cws._workspace_is_stale(
            wd,
            threshold_s=1800,
            now=time.time(),
            alive_pods={"other-pod"},
        )
        is True
    )


def test_workspace_is_not_stale_when_pod_alive(cws, work_root):
    """workspace_is_stale retorna False quando pod dono está em alive_pods."""
    wd = _make_workdir(work_root)
    _write_lease(wd, time.time(), pod="alive-pod")

    assert (
        cws._workspace_is_stale(
            wd,
            threshold_s=1800,
            now=time.time(),
            alive_pods={"alive-pod"},
        )
        is False
    )


# ---------------------------------------------------------------------------
# startup_cleanup integrado
# ---------------------------------------------------------------------------


def test_startup_cleanup_recovers_dead_pod_lease(cws, work_root, monkeypatch):
    """startup_cleanup remove imediatamente lease de pod morto (sem esperar TTL)."""
    monkeypatch.setenv("HOME", str(work_root.parent))
    # Pod "this-pod" está vivo (presença fresca).
    pdir = work_root / ".pods"
    pdir.mkdir()
    (pdir / "this-pod.presence").write_text(
        json.dumps({"pod": "this-pod", "written_at": time.time()}),
        encoding="utf-8",
    )

    wd = _make_workdir(work_root)
    # Heartbeat recente, mas pod "dead-pod" não está em alive_pods.
    _write_lease(wd, time.time() - 1, pod="dead-pod")

    result = cws.startup_cleanup(root=work_root)

    assert result["leases_removed"] == 1
    assert not (wd / ".lease.json").exists()


def test_startup_cleanup_skips_live_pod_with_fresh_heartbeat(
    cws, work_root, monkeypatch
):
    """startup_cleanup não toca workdir cujo pod está na lista de vivos."""
    monkeypatch.setenv("HOME", str(work_root.parent))
    pdir = work_root / ".pods"
    pdir.mkdir()
    (pdir / "live-pod.presence").write_text(
        json.dumps({"pod": "live-pod", "written_at": time.time()}),
        encoding="utf-8",
    )

    wd = _make_workdir(work_root)
    _write_lease(wd, time.time() - 1, pod="live-pod")

    result = cws.startup_cleanup(root=work_root)

    assert result["leases_removed"] == 0
    assert (wd / ".lease.json").exists()


# ---------------------------------------------------------------------------
# _cleanup_stale_workspaces integrado
# ---------------------------------------------------------------------------


def test_cleanup_stale_workspaces_removes_dead_pod_workspace(
    cws, work_root, monkeypatch
):
    """_cleanup_stale_workspaces remove workspace de pod morto imediatamente."""
    pdir = work_root / ".pods"
    pdir.mkdir()
    (pdir / "survivor.presence").write_text(
        json.dumps({"pod": "survivor", "written_at": time.time()}),
        encoding="utf-8",
    )

    wd = _make_workdir(work_root)
    # Heartbeat recente — sem presença seria ativo; com presença é morto.
    _write_lease(wd, time.time(), pod="dead-worker")

    summary = cws._cleanup_stale_workspaces(work_root, threshold_s=1800)

    assert summary["removed"] == 1
    assert not wd.exists()
