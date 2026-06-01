"""Testes do fix C3 (issue #477): sync I/O bloqueante no event loop.

Verifica que `_save_session_meta` e os `write_text` de progresso em
`claude_worker_server.py` são chamados via `asyncio.to_thread` — e não
diretamente no event loop.

Cobertura:
- Inspeção de código: `dispatch_handler` não tem mais chamadas diretas a
  `_save_session_meta(...)` — todas passaram por `asyncio.to_thread`.
- `run_subprocess_with_progress`: `write_text` vai via `asyncio.to_thread`.
- Smoke: `asyncio.to_thread` real com I/O de 50ms não bloqueia o loop.
- Regressão: `_save_session_meta` sync interna continua atômica.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import claude_worker_server as cws  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Inspeção de código-fonte: chamadas diretas a _save_session_meta removidas
# ---------------------------------------------------------------------------

def test_dispatch_handler_no_direct_save_session_meta_calls():
    """dispatch_handler não deve chamar _save_session_meta() diretamente."""
    src = inspect.getsource(cws.dispatch_handler)
    lines = src.splitlines()
    direct_calls = [
        line.strip() for line in lines
        if "_save_session_meta(task_id" in line
        and "asyncio.to_thread" not in line
        and not line.strip().startswith("#")
    ]
    assert not direct_calls, (
        f"Chamadas diretas (sem asyncio.to_thread) encontradas:\n"
        + "\n".join(direct_calls)
    )


def test_dispatch_handler_uses_to_thread_for_save_meta():
    """dispatch_handler deve chamar _save_session_meta via asyncio.to_thread."""
    src = inspect.getsource(cws.dispatch_handler)
    assert "asyncio.to_thread(_save_session_meta" in src, (
        "dispatch_handler deve usar asyncio.to_thread(_save_session_meta, ...) "
        "em vez de chamar _save_session_meta() diretamente"
    )


# ---------------------------------------------------------------------------
# run_subprocess_with_progress: write_text via asyncio.to_thread
# ---------------------------------------------------------------------------

def test_run_subprocess_source_uses_to_thread_for_write_text():
    """run_subprocess_with_progress deve usar asyncio.to_thread para write_text."""
    src = inspect.getsource(cws.run_subprocess_with_progress)
    assert "asyncio.to_thread" in src and "write_text" in src, (
        "run_subprocess_with_progress deve usar asyncio.to_thread + write_text"
    )
    lines = src.splitlines()
    direct_write = [
        l.strip() for l in lines
        if "write_text(" in l
        and "asyncio.to_thread" not in l
        and "tmp.write_text" not in l  # writes inside sync nested funcs são OK
        and not l.strip().startswith("#")
    ]
    assert not direct_write, (
        f"write_text chamado diretamente (sem to_thread) em run_subprocess_with_progress:\n"
        + "\n".join(direct_write)
    )


async def test_progress_write_text_uses_to_thread(tmp_path: Path):
    """stdout_path.write_text e stderr_path.write_text devem ir via to_thread."""
    to_thread_calls: list = []

    original_to_thread = asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        to_thread_calls.append(getattr(func, "__name__", repr(func)))
        return await original_to_thread(func, *args, **kwargs)

    mock_proc = MagicMock()
    mock_proc.pid = 42
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"stdout data", b"stderr data"))

    task_id = "test-task-001"
    env_patch = {"DEILE_CLAUDE_WORKER_ROOT": str(tmp_path)}

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
         patch("asyncio.to_thread", side_effect=spy_to_thread), \
         patch.dict(os.environ, env_patch):
        await cws.run_subprocess_with_progress(
            ["echo", "hello"],
            cwd=tmp_path,
            task_id=task_id,
            timeout=30,
            lease_path=None,
        )

    write_text_calls = [n for n in to_thread_calls if "write_text" in n]
    assert len(write_text_calls) >= 2, (
        f"write_text deve ser chamado 2× via asyncio.to_thread. "
        f"to_thread_calls: {to_thread_calls}"
    )


# ---------------------------------------------------------------------------
# Smoke: asyncio.to_thread não bloqueia o event loop
# ---------------------------------------------------------------------------

async def test_to_thread_does_not_block_event_loop():
    """I/O de 50ms em asyncio.to_thread não deve bloquear o event loop."""
    SLEEP_MS = 50
    MAX_DELAY_MS = 200  # folga generosa para CI lento

    async def blocker():
        await asyncio.to_thread(time.sleep, SLEEP_MS / 1000)

    async def probe():
        return time.monotonic()

    start = time.monotonic()
    probe_t, _ = await asyncio.gather(probe(), blocker())
    elapsed_probe_ms = (probe_t - start) * 1000

    assert elapsed_probe_ms < MAX_DELAY_MS, (
        f"probe demorou {elapsed_probe_ms:.1f}ms — event loop pode estar bloqueado"
    )


# ---------------------------------------------------------------------------
# Regressão: _save_session_meta sync interna ainda funciona e é atômica
# ---------------------------------------------------------------------------

def test_save_session_meta_atomic_write(tmp_path: Path):
    """_save_session_meta escreve atomicamente via tmp + os.replace."""
    meta_dir = tmp_path / "session-meta" / "task-abc"
    meta_dir.mkdir(parents=True)

    with patch.object(cws, "_session_meta_path",
                      return_value=meta_dir / "session.json"):
        cws._save_session_meta("task-abc", {"status": "running", "attempt": 1})

    out = meta_dir / "session.json"
    assert out.exists(), "session.json deve existir após _save_session_meta"
    data = json.loads(out.read_text())
    assert data["status"] == "running"
    assert data["attempt"] == 1
    tmp = meta_dir / "session.json.tmp"
    assert not tmp.exists(), "arquivo .tmp deve ser removido após os.replace"


def test_save_session_meta_ioerror_does_not_raise(tmp_path: Path):
    """Falha de I/O em _save_session_meta é best-effort — nunca lança."""
    with patch.object(cws, "_session_meta_path",
                      return_value=Path("/proc/fake/unwritable/session.json")):
        cws._save_session_meta("task-err", {"ok": False})  # não deve levantar
