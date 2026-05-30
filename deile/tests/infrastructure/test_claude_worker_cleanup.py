"""Testes de ``startup_cleanup`` em ``claude_worker_server`` (issue #408).

Cobre:
- Lease stale (TTL expirado + PID morto) → apenas o arquivo de lease é removido.
- Lease vivo (heartbeat recente) → workdir intocado.
- Workdir sem session JSONL → removido inteiro.
- Workdir antigo (além do cutoff de retenção) → removido inteiro.
- Workdir recente com sessão → intocado.
- Work root ausente → retorna erro sem crash.
- Sumário de contagens correto.
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
    """Carrega claude_worker_server dinamicamente.

    Registra em sys.modules antes de exec_module — necessário para que os
    dataclasses do módulo resolvam ``__module__`` corretamente.
    """
    spec = importlib.util.spec_from_file_location(
        "cws_cleanup_test",
        str(_INFRA_K8S / "claude_worker_server.py"),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cws_cleanup_test"] = mod
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


def _write_lease(workdir: Path, heartbeat_at: float, pid: int = 999999999) -> None:
    (workdir / ".lease.json").write_text(
        json.dumps({"heartbeat_at": heartbeat_at, "pid": pid, "pod": "test-pod"}),
        encoding="utf-8",
    )


def _write_session(workdir: Path, home: Path | None = None) -> None:
    """Escreve um JSONL de sessão no local real onde claude armazena sessões.

    Claude armazena em ``HOME/.claude/projects/-home-claude-work-<task_id>/``,
    não no workdir em si.
    """
    if home is None:
        home = workdir.parent.parent  # tmp_path (acima de "work/")
    task_id = workdir.name
    workspace_hash = "-home-claude-work-" + task_id
    project_dir = home / ".claude" / "projects" / workspace_hash
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "session.jsonl").write_text(
        '{"type": "text", "text": "hello"}\n', encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# _lease_is_stale
# ---------------------------------------------------------------------------

def test_lease_is_stale_when_expired_and_pid_dead(cws, work_root):
    wd = _make_workdir(work_root)
    expired = time.time() - 3600  # 1h atrás
    _write_lease(wd, expired, pid=999999999)  # PID certamente morto
    assert cws._lease_is_stale(wd / ".lease.json") is True


def test_lease_is_not_stale_when_heartbeat_recent(cws, work_root):
    wd = _make_workdir(work_root)
    fresh = time.time() - 1  # 1s atrás
    _write_lease(wd, fresh)
    assert cws._lease_is_stale(wd / ".lease.json") is False


def test_lease_is_not_stale_when_file_missing(cws, work_root):
    wd = _make_workdir(work_root)
    # Arquivo não existe → conservador (assume não stale)
    assert cws._lease_is_stale(wd / ".lease.json") is False


def test_lease_is_not_stale_when_pid_still_alive(cws, work_root):
    wd = _make_workdir(work_root)
    expired = time.time() - 3600
    # PID do processo corrente está definitivamente vivo
    _write_lease(wd, expired, pid=__import__("os").getpid())
    assert cws._lease_is_stale(wd / ".lease.json") is False


# ---------------------------------------------------------------------------
# _workdir_has_session
# ---------------------------------------------------------------------------

def test_workdir_has_session_with_jsonl(cws, work_root, monkeypatch):
    wd = _make_workdir(work_root)
    monkeypatch.setenv("HOME", str(work_root.parent))
    _write_session(wd, home=work_root.parent)
    assert cws._workdir_has_session(wd) is True


def test_workdir_has_no_session_empty(cws, work_root, monkeypatch):
    wd = _make_workdir(work_root)
    monkeypatch.setenv("HOME", str(work_root.parent))
    assert cws._workdir_has_session(wd) is False


# ---------------------------------------------------------------------------
# startup_cleanup — cenários
# ---------------------------------------------------------------------------

def test_startup_cleanup_root_missing(cws, tmp_path):
    result = cws.startup_cleanup(root=tmp_path / "nonexistent")
    assert result["leases_removed"] == 0
    assert result["workdirs_removed"] == 0
    assert result["bytes_freed"] == 0
    assert result["errors"]  # deve ter pelo menos um erro


def test_startup_cleanup_removes_stale_lease_only(cws, work_root, monkeypatch):
    """Lease stale → remove o .lease.json mas MANTÉM o workdir (tem sessão)."""
    monkeypatch.setenv("HOME", str(work_root.parent))
    wd = _make_workdir(work_root)
    _write_session(wd, home=work_root.parent)
    expired = time.time() - 3600
    _write_lease(wd, expired, pid=999999999)
    # Garante que mtime é recente (não cai no critério de old workdir)
    import os
    os.utime(wd, (time.time(), time.time()))

    result = cws.startup_cleanup(root=work_root)

    assert result["leases_removed"] == 1
    assert result["workdirs_removed"] == 0
    assert not (wd / ".lease.json").exists()
    assert wd.exists()  # workdir mantido


def test_startup_cleanup_skips_workdir_with_live_lease(cws, work_root):
    """Lease vivo → pula o workdir completamente."""
    wd = _make_workdir(work_root)
    fresh = time.time() - 1
    _write_lease(wd, fresh)

    result = cws.startup_cleanup(root=work_root)

    assert result["leases_removed"] == 0
    assert result["workdirs_removed"] == 0
    assert wd.exists()


def test_startup_cleanup_removes_workdir_without_session(cws, work_root):
    """Workdir sem .lease.json e sem session JSONL → removido."""
    wd = _make_workdir(work_root)
    # Sem session, sem lease

    result = cws.startup_cleanup(root=work_root)

    assert result["workdirs_removed"] == 1
    assert result["bytes_freed"] >= 0
    assert not wd.exists()


def test_startup_cleanup_removes_old_workdir(cws, work_root, monkeypatch):
    """Workdir com sessão mas mtime muito antigo → removido."""
    monkeypatch.setenv("HOME", str(work_root.parent))
    wd = _make_workdir(work_root)
    _write_session(wd, home=work_root.parent)
    # Força mtime para 30 dias atrás
    old_mtime = time.time() - (30 * 86400)
    import os
    os.utime(wd, (old_mtime, old_mtime))

    result = cws.startup_cleanup(root=work_root)

    assert result["workdirs_removed"] == 1
    assert not wd.exists()


def test_startup_cleanup_keeps_recent_workdir_with_session(cws, work_root, monkeypatch):
    """Workdir recente com sessão → intocado."""
    monkeypatch.setenv("HOME", str(work_root.parent))
    wd = _make_workdir(work_root)
    _write_session(wd, home=work_root.parent)
    import os
    os.utime(wd, (time.time(), time.time()))

    result = cws.startup_cleanup(root=work_root)

    assert result["leases_removed"] == 0
    assert result["workdirs_removed"] == 0
    assert wd.exists()


def test_startup_cleanup_ignores_non_hex_dirs(cws, work_root):
    """Diretórios que não são task_id hex-16 são ignorados."""
    (work_root / "not-a-task-id").mkdir()
    (work_root / ".hidden").mkdir()
    (work_root / "toolong_aaaa_bbbb_cccc_ddd").mkdir()

    result = cws.startup_cleanup(root=work_root)

    assert result["workdirs_removed"] == 0
    assert (work_root / "not-a-task-id").exists()


def test_startup_cleanup_counts_multiple_workdirs(cws, work_root, monkeypatch):
    """Múltiplos workdirs: um a remover, um a manter."""
    monkeypatch.setenv("HOME", str(work_root.parent))

    # Workdir a manter: recente com sessão
    wd_keep = _make_workdir(work_root, "aabb00112233aabb")
    _write_session(wd_keep, home=work_root.parent)
    import os
    os.utime(wd_keep, (time.time(), time.time()))

    # Workdir a remover: sem sessão
    wd_remove = _make_workdir(work_root, "ccdd44556677ccdd")

    result = cws.startup_cleanup(root=work_root)

    assert result["workdirs_removed"] == 1
    assert wd_keep.exists()
    assert not wd_remove.exists()


def test_startup_cleanup_summary_fields_present(cws, work_root):
    """O resultado sempre tem os quatro campos esperados."""
    result = cws.startup_cleanup(root=work_root)
    assert "leases_removed" in result
    assert "workdirs_removed" in result
    assert "bytes_freed" in result
    assert "errors" in result
    assert isinstance(result["errors"], list)
