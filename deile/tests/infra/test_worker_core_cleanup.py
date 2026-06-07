"""Cleanup genérico extraído para ``_worker_core`` (finding 5).

A Fase A1 do plano lista ``startup_cleanup`` como função a extrair para o core,
reusável por qualquer worker com PVC. Estes testes provam que a versão genérica
(``_worker_core.startup_cleanup``) remove leases stale e workdirs abandonados
por idade, e NUNCA toca workdir com lease vivo — o mesmo critério do claude.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _worker_core as core  # noqa: E402

_TASK = "a" * 16
_TASK2 = "b" * 16
_TASK3 = "c" * 16


def _mk_workdir(root: Path, task_id: str, *, lease_heartbeat=None, mtime=None) -> Path:
    wd = root / task_id
    wd.mkdir(parents=True)
    (wd / "data.txt").write_text("x")
    if lease_heartbeat is not None:
        (wd / ".lease.json").write_text(json.dumps({
            "pod": "dead-pod", "pid": 999999, "heartbeat_at": lease_heartbeat,
        }))
    if mtime is not None:
        import os
        os.utime(wd, (mtime, mtime))
    return wd


def test_missing_root_returns_error(tmp_path):
    res = core.startup_cleanup(tmp_path / "nope")
    assert res["workdirs_removed"] == 0
    assert res["errors"] == ["work root not found"]


def test_live_lease_workdir_is_never_removed(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    # Lease com heartbeat recente → vivo → nunca remove.
    _mk_workdir(root, _TASK, lease_heartbeat=time.time())
    res = core.startup_cleanup(root, retention_days=0)
    assert res["workdirs_removed"] == 0
    assert (root / _TASK).is_dir()


def test_stale_lease_removed_and_old_workdir_collected(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    old = time.time() - (10 * 86400)  # 10 dias atrás
    # Lease stale (heartbeat antigo, pid morto) + workdir velho.
    _mk_workdir(root, _TASK2, lease_heartbeat=old, mtime=old)
    res = core.startup_cleanup(root, retention_days=7, has_session=None)
    assert res["leases_removed"] == 1
    assert res["workdirs_removed"] == 1
    assert not (root / _TASK2).exists()


def test_recent_workdir_without_lease_kept_under_retention(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    # Sem lease, mtime recente → dentro da retenção → preservado.
    _mk_workdir(root, _TASK3, lease_heartbeat=None, mtime=time.time())
    res = core.startup_cleanup(root, retention_days=7, has_session=None)
    assert res["workdirs_removed"] == 0
    assert (root / _TASK3).is_dir()


def test_has_session_predicate_collects_workdir_without_session(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    _mk_workdir(root, _TASK, lease_heartbeat=None, mtime=time.time())
    # has_session sempre False → workdir sem sessão é elegível mesmo recente.
    res = core.startup_cleanup(
        root, retention_days=7, has_session=lambda _wd: False,
    )
    assert res["workdirs_removed"] == 1
    assert not (root / _TASK).exists()
