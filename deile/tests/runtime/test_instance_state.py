"""Testes do :mod:`deile.runtime.instance_state` (issue #303 — Fase 1).

Cobre schema, escrita atômica, validação de enums, lifecycle do heartbeat,
acumulação de stats, idempotência de ``close()``, registro de ``atexit``,
``pid_alive`` (POSIX + PermissionError), formato de ``instance_id`` e
override por ``DEILE_RUNTIME_DIR``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from pathlib import Path

import pytest

from deile.runtime.instance_state import (DETAIL_MAX_LEN, SCHEMA_VERSION,
                                          VALID_ACTION_KINDS, VALID_ROLES,
                                          InstanceState, get_instance_state,
                                          pid_alive, reset_instance_state)

# ── helpers ──────────────────────────────────────────────────────────────


def _read(path: Path) -> dict:
    """Lê o state file e devolve o dict (testes só inspecionam JSON íntegro)."""
    return json.loads(path.read_text(encoding="utf-8"))


# ── schema ───────────────────────────────────────────────────────────────


def test_schema_has_all_required_fields():
    state = InstanceState(role="cli")
    try:
        data = _read(state.path)
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["instance_id"] == state.instance_id
        assert data["pid"] == os.getpid()
        assert data["role"] == "cli"
        assert isinstance(data["started_at"], str) and data["started_at"].endswith("+00:00")
        assert isinstance(data["last_heartbeat_at"], str) and data["last_heartbeat_at"].endswith("+00:00")
        assert data["current_action"] is None
        stats = data["stats"]
        assert stats == {
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "turns": 0,
            "tool_calls": 0,
            "errors": 0,
        }
    finally:
        state.close()


def test_role_validation_rejects_unknown():
    with pytest.raises(ValueError, match="role inválido"):
        InstanceState(role="rogue")  # noqa: F841


@pytest.mark.parametrize("role", sorted(VALID_ROLES))
def test_role_validation_accepts_each_valid(role):
    state = InstanceState(role=role)
    try:
        assert state.role == role
        assert _read(state.path)["role"] == role
    finally:
        state.close()


def test_instance_id_format_matches_regex():
    state = InstanceState(role="pipeline")
    try:
        assert re.fullmatch(r"^pipeline-[0-9a-f]{8}$", state.instance_id)
    finally:
        state.close()


# ── escrita atômica ──────────────────────────────────────────────────────


def test_atomic_write_never_leaves_partial_json(tmp_path):
    """Confirma que cada flush deixa JSON íntegro (sem leitura parcial).

    A heurística é simples: faz vários updates seguidos e, em cada parada,
    confirma que o arquivo se lê como JSON válido (não levanta
    ``JSONDecodeError``). O tmp é gravado num path .tmp distinto e o
    ``os.replace`` é a única operação visível para leitores.
    """
    state = InstanceState(role="cli", runtime_dir=tmp_path / "run")
    try:
        for i in range(50):
            state.update_action("tool_execution", detail=f"iter-{i}")
            data = _read(state.path)
            assert data["current_action"]["detail"] == f"iter-{i}"
            # tmp file não pode permanecer entre flushes
            assert not state.path.with_suffix(".tmp").exists()
    finally:
        state.close()


def test_runtime_dir_override_via_env(monkeypatch, tmp_path):
    custom = tmp_path / "custom-run"
    monkeypatch.setenv("DEILE_RUNTIME_DIR", str(custom))
    state = InstanceState(role="worker")
    try:
        assert state.runtime_dir == custom.resolve()
        assert state.path.parent == custom.resolve()
        assert state.path.is_file()
    finally:
        state.close()


def test_runtime_dir_explicit_override_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DEILE_RUNTIME_DIR", str(tmp_path / "from-env"))
    explicit = tmp_path / "from-arg"
    state = InstanceState(role="bot", runtime_dir=explicit)
    try:
        assert state.runtime_dir == explicit.resolve()
    finally:
        state.close()


# ── update_action ────────────────────────────────────────────────────────


def test_update_action_rejects_unknown_kind():
    state = InstanceState(role="cli")
    try:
        with pytest.raises(ValueError, match="kind inválido"):
            state.update_action("frobnicate")
    finally:
        state.close()


@pytest.mark.parametrize("kind", sorted(VALID_ACTION_KINDS))
def test_update_action_accepts_each_valid_kind(kind):
    state = InstanceState(role="cli")
    try:
        state.update_action(kind, detail=f"k={kind}")
        data = _read(state.path)
        assert data["current_action"]["kind"] == kind
    finally:
        state.close()


def test_update_action_truncates_detail_to_80_chars():
    state = InstanceState(role="cli")
    try:
        too_long = "x" * 500
        state.update_action("tool_execution", detail=too_long)
        data = _read(state.path)
        assert len(data["current_action"]["detail"]) == DETAIL_MAX_LEN
        assert data["current_action"]["detail"] == "x" * DETAIL_MAX_LEN
    finally:
        state.close()


def test_update_action_includes_optional_session_and_model():
    state = InstanceState(role="cli")
    try:
        state.update_action(
            "llm_call",
            detail="generate_stream",
            session_id="sess-9f2",
            model="deepseek:v4-pro",
        )
        action = _read(state.path)["current_action"]
        assert action["session_id"] == "sess-9f2"
        assert action["model"] == "deepseek:v4-pro"
    finally:
        state.close()


def test_clear_action_sets_null():
    state = InstanceState(role="cli")
    try:
        state.update_action("tool_execution", detail="busy")
        assert _read(state.path)["current_action"] is not None
        state.clear_action()
        assert _read(state.path)["current_action"] is None
    finally:
        state.close()


# ── update_stats ─────────────────────────────────────────────────────────


def test_update_stats_accumulates_does_not_replace():
    state = InstanceState(role="cli")
    try:
        state.update_stats(tokens_in=100, tokens_out=20, cost_usd=0.01, tool_calls=1)
        state.update_stats(tokens_in=50, tokens_out=10, cost_usd=0.005, tool_calls=1)
        state.update_stats(turns=1, errors=1)
        stats = _read(state.path)["stats"]
        assert stats["tokens_in"] == 150
        assert stats["tokens_out"] == 30
        assert stats["cost_usd"] == pytest.approx(0.015)
        assert stats["tool_calls"] == 2
        assert stats["turns"] == 1
        assert stats["errors"] == 1
    finally:
        state.close()


def test_update_stats_is_threadsafe_under_concurrency():
    """Mutar contadores de N threads em paralelo não deve perder updates."""
    state = InstanceState(role="cli")
    try:
        N = 50
        per_thread_calls = 20

        def worker():
            for _ in range(per_thread_calls):
                state.update_stats(tool_calls=1)

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert _read(state.path)["stats"]["tool_calls"] == N * per_thread_calls
    finally:
        state.close()


# ── heartbeat ────────────────────────────────────────────────────────────


async def test_heartbeat_loop_updates_last_heartbeat_at():
    state = InstanceState(role="cli")
    try:
        before = _read(state.path)["last_heartbeat_at"]
        task = asyncio.create_task(state.heartbeat_loop(interval_s=0.05))
        # Garante pelo menos 2 ticks observáveis (≥100ms total).
        await asyncio.sleep(0.25)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        after = _read(state.path)["last_heartbeat_at"]
        assert after != before  # heartbeat realmente avançou
    finally:
        state.close()


async def test_heartbeat_loop_reraises_cancelled_error():
    """Princípio 6 — ``asyncio.CancelledError`` nunca é silenciado."""
    state = InstanceState(role="cli")
    try:
        task = asyncio.create_task(state.heartbeat_loop(interval_s=0.05))
        await asyncio.sleep(0.01)  # deixa entrar no loop
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        state.close()


async def test_heartbeat_loop_rejects_zero_interval():
    state = InstanceState(role="cli")
    try:
        with pytest.raises(ValueError, match="interval_s deve ser > 0"):
            await state.heartbeat_loop(interval_s=0)
    finally:
        state.close()


async def test_heartbeat_loop_exits_quickly_after_close():
    """Fechar o state file durante o heartbeat deve encerrar a task limpa."""
    state = InstanceState(role="cli")
    task = asyncio.create_task(state.heartbeat_loop(interval_s=0.05))
    await asyncio.sleep(0.01)
    state.close()
    # Sem cancel: a flag _closed faz o loop sair naturalmente após o próximo sleep.
    await asyncio.wait_for(task, timeout=1.0)


# ── close / lifecycle ────────────────────────────────────────────────────


def test_close_removes_state_file():
    state = InstanceState(role="cli")
    path = state.path
    assert path.is_file()
    state.close()
    assert not path.exists()


def test_close_is_idempotent():
    state = InstanceState(role="cli")
    state.close()
    state.close()  # não deve levantar
    state.close()


def test_updates_after_close_are_noop():
    """``close`` torna o estado terminal — updates posteriores não levantam."""
    state = InstanceState(role="cli")
    state.close()
    # Nenhuma destas chamadas deve recriar o arquivo nem levantar.
    state.update_action("tool_execution", detail="post-close")
    state.update_stats(tool_calls=1)
    state.clear_action()
    assert not state.path.exists()


def test_atexit_register_called_on_init(monkeypatch):
    """Sanity: ``__init__`` registra ``self.close`` em ``atexit``."""
    import atexit as atexit_mod

    captured: list = []

    def fake_register(fn, *args, **kwargs):
        captured.append(fn)
        return fn

    monkeypatch.setattr(atexit_mod, "register", fake_register)
    state = InstanceState(role="cli")
    try:
        assert state.close in captured
    finally:
        state.close()


# ── snapshot ─────────────────────────────────────────────────────────────


def test_snapshot_returns_deep_copy():
    state = InstanceState(role="cli")
    try:
        state.update_stats(tokens_in=42)
        snap1 = state.snapshot()
        snap1["stats"]["tokens_in"] = 9999  # mutar cópia
        snap2 = state.snapshot()
        assert snap2["stats"]["tokens_in"] == 42  # original intacto
    finally:
        state.close()


# ── pid_alive ────────────────────────────────────────────────────────────


def test_pid_alive_true_for_current_process():
    assert pid_alive(os.getpid()) is True


def test_pid_alive_false_for_nonexistent_pid():
    # 999999 é improvável de existir em ambientes de teste; se existir,
    # o teste é ainda válido (sinal 0 sucederia → True) mas raramente o caso.
    # Para garantir, escolhemos um valor astronômico que excede maxpid em
    # qualquer Linux/macOS razoável.
    assert pid_alive(2_147_483_646) is False


def test_pid_alive_zero_or_negative_returns_false():
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_pid_alive_permission_error_treated_as_alive(monkeypatch):
    """``EPERM`` significa "processo existe mas é de outro user" — está vivo."""
    def fake_kill(pid, sig):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "kill", fake_kill)
    assert pid_alive(12345) is True


def test_pid_alive_other_oserror_treated_as_alive(monkeypatch):
    """OSError genérico (Windows quirks) → false positive de vivo, não GC."""
    def fake_kill(pid, sig):
        raise OSError("weird")

    monkeypatch.setattr(os, "kill", fake_kill)
    assert pid_alive(12345) is True


# ── singleton ────────────────────────────────────────────────────────────


def test_singleton_is_same_instance_across_calls():
    s1 = get_instance_state(role="cli")
    s2 = get_instance_state(role="cli")
    assert s1 is s2


def test_singleton_ignores_role_on_subsequent_calls():
    """O primeiro caller fixa a identidade; depois ``role`` é ignorado."""
    s1 = get_instance_state(role="cli")
    s2 = get_instance_state(role="pipeline")  # ignorado
    assert s1 is s2
    assert s1.role == "cli"


def test_reset_instance_state_closes_and_clears():
    s1 = get_instance_state(role="cli")
    path = s1.path
    assert path.is_file()
    reset_instance_state()
    assert not path.exists()
    s2 = get_instance_state(role="pipeline")
    assert s2 is not s1
    assert s2.role == "pipeline"
