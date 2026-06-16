"""Testes do :mod:`deile.runtime.registry` (issue #303 — Fase 3).

Cobre o lifecycle de registro/derregistro, idempotência por ``instance_id``,
GC de órfãos (PID morto e state_file ausente), tolerância a JSON corrompido
e schema_version desconhecido, file lock cross-thread, atomicidade
write-tmp + replace.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from deile.runtime.registry import (
    DEFAULT_REGISTRY_FILENAME,
    REGISTRY_SCHEMA_VERSION,
    Registry,
    RegistryEntry,
)


def _entry(
    instance_id: str, *, pid: int = None, role: str = "cli", state_file: str = ""
) -> RegistryEntry:
    """Helper conciso para fabricar entries de teste."""
    return RegistryEntry(
        instance_id=instance_id,
        pid=pid if pid is not None else os.getpid(),
        role=role,
        started_at="2026-05-25T00:00:00+00:00",
        endpoint=f"unix:///tmp/{instance_id}.sock",
        state_file=state_file,
    )


# ── registro / derregistro ────────────────────────────────────────────────


def test_register_creates_registry_file(short_runtime_dir):
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    assert not reg.path.exists()
    reg.register(_entry("cli-aaaa1111"))
    assert reg.path.exists()
    payload = json.loads(reg.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == REGISTRY_SCHEMA_VERSION
    assert len(payload["instances"]) == 1
    assert payload["instances"][0]["instance_id"] == "cli-aaaa1111"


def test_register_is_idempotent_by_instance_id(short_runtime_dir):
    """Re-registrar o mesmo ``instance_id`` sobrescreve sem duplicar."""
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    reg.register(_entry("cli-dup", role="cli"))
    reg.register(_entry("cli-dup", role="worker"))  # mesma id, role nova
    entries = reg.list(gc=False)
    assert len(entries) == 1
    assert entries[0].role == "worker"


def test_register_appends_multiple_instances(short_runtime_dir):
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    reg.register(_entry("cli-1111"))
    reg.register(_entry("cli-2222"))
    reg.register(_entry("worker-3333", role="worker"))
    entries = reg.list(gc=False)
    ids = sorted(e.instance_id for e in entries)
    assert ids == ["cli-1111", "cli-2222", "worker-3333"]


def test_deregister_removes_entry(short_runtime_dir):
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    reg.register(_entry("cli-a"))
    reg.register(_entry("cli-b"))
    reg.deregister("cli-a")
    entries = reg.list(gc=False)
    assert len(entries) == 1
    assert entries[0].instance_id == "cli-b"


def test_deregister_unknown_id_is_noop(short_runtime_dir):
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    reg.register(_entry("cli-keep"))
    reg.deregister("cli-doesnt-exist")  # não levanta
    entries = reg.list(gc=False)
    assert len(entries) == 1


def test_deregister_when_file_missing_is_noop(short_runtime_dir):
    """Deregister num registry inexistente não cria arquivo."""
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    assert not reg.path.exists()
    reg.deregister("nothing-to-remove")
    assert not reg.path.exists()


# ── list / GC ─────────────────────────────────────────────────────────────


def test_list_returns_empty_when_file_missing(short_runtime_dir):
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    assert reg.list() == []


def test_list_gc_removes_dead_pid(short_runtime_dir):
    """Entry com PID astronomicamente alto (inexistente) é GC."""
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    reg.register(_entry("alive", pid=os.getpid()))
    reg.register(_entry("dead", pid=2_147_483_646))  # certamente inexistente
    entries = reg.list(gc=True)
    ids = [e.instance_id for e in entries]
    assert ids == ["alive"]
    # O arquivo foi reescrito sem a entry morta.
    payload = json.loads(reg.path.read_text(encoding="utf-8"))
    assert [e["instance_id"] for e in payload["instances"]] == ["alive"]


def test_list_gc_removes_when_state_file_missing(short_runtime_dir):
    """state_file ausente → proxy de "morreu sem cleanup" → GC."""
    fake_state = short_runtime_dir / "ghost.json"
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    reg.register(_entry("ghost", pid=os.getpid(), state_file=str(fake_state)))
    assert not fake_state.exists()
    entries = reg.list(gc=True)
    assert entries == []


def test_list_keeps_entry_with_existing_state_file(short_runtime_dir):
    state_file = short_runtime_dir / "live.json"
    state_file.write_text("{}", encoding="utf-8")
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    reg.register(_entry("live", pid=os.getpid(), state_file=str(state_file)))
    entries = reg.list(gc=True)
    assert len(entries) == 1
    assert entries[0].instance_id == "live"


def test_list_gc_false_skips_cleanup(short_runtime_dir):
    """``gc=False`` devolve a lista crua, mesmo com órfãos."""
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    reg.register(_entry("alive", pid=os.getpid()))
    reg.register(_entry("dead", pid=2_147_483_646))
    entries = reg.list(gc=False)
    ids = sorted(e.instance_id for e in entries)
    assert ids == ["alive", "dead"]


# ── tolerância a payload corrompido ──────────────────────────────────────


def test_list_handles_corrupt_json(short_runtime_dir):
    """JSON inválido vira lista vazia (log warning, não levanta)."""
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    reg.path.write_text("{ not valid json", encoding="utf-8")
    assert reg.list() == []


def test_list_handles_unknown_schema_version(short_runtime_dir, caplog):
    """schema_version diferente do esperado é ignorado (forward-compat)."""
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    bad_payload = {
        "schema_version": 99,
        "instances": [{"instance_id": "x", "pid": 1}],
    }
    reg.path.write_text(json.dumps(bad_payload), encoding="utf-8")
    assert reg.list() == []


def test_list_skips_malformed_entries(short_runtime_dir):
    """Entries com tipos errados são puladas, demais sobrevivem."""
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    payload = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "instances": [
            {
                "instance_id": "good",
                "pid": os.getpid(),
                "role": "cli",
                "started_at": "",
                "endpoint": "",
                "state_file": "",
            },
            {"instance_id": "", "pid": 1},  # id vazio — skip
            {"pid": 0},  # sem id, pid inválido
            "not a dict",  # tipo errado
            {"instance_id": "bad-pid", "pid": "nope"},  # pid não-int
        ],
    }
    reg.path.write_text(json.dumps(payload), encoding="utf-8")
    entries = reg.list(gc=False)
    assert [e.instance_id for e in entries] == ["good"]


# ── concorrência via file-lock ───────────────────────────────────────────


def test_register_threadsafe_no_lost_updates(short_runtime_dir):
    """N threads registrando entries distintas — nenhuma deve ser perdida.

    O ``flock(LOCK_EX)`` serializa o read-modify-write — sem ele, threads
    simultâneas leem o mesmo registry (vazio) e sobrescrevem-se mutuamente.
    """
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    N = 40
    barrier = threading.Barrier(N)

    def worker(i: int) -> None:
        barrier.wait()  # libera todas as threads simultaneamente
        reg.register(_entry(f"cli-{i:04x}", pid=os.getpid()))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = reg.list(gc=False)
    assert len({e.instance_id for e in entries}) == N


# ── outros ───────────────────────────────────────────────────────────────


def test_registry_entry_from_dict_rejects_invalid():
    assert RegistryEntry.from_dict({}) is None
    assert RegistryEntry.from_dict({"instance_id": "", "pid": 1}) is None
    assert RegistryEntry.from_dict({"instance_id": "x"}) is None  # sem pid


def test_register_rejects_non_entry(short_runtime_dir):
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    with pytest.raises(TypeError):
        reg.register({"instance_id": "x", "pid": 1})  # type: ignore[arg-type]


def test_default_path_uses_env_runtime_dir(short_runtime_dir, monkeypatch):
    """Sem ``registry_path`` explícito, resolve via ``DEILE_RUNTIME_DIR``."""
    monkeypatch.setenv("DEILE_RUNTIME_DIR", str(short_runtime_dir))
    reg = Registry()
    assert reg.path == short_runtime_dir.resolve() / DEFAULT_REGISTRY_FILENAME


def test_register_uses_atomic_write_tmp_then_replace(short_runtime_dir):
    """O tmp file não pode permanecer entre updates (atomic replace)."""
    reg = Registry(registry_path=short_runtime_dir / "registry.json")
    for i in range(10):
        reg.register(_entry(f"cli-{i:04x}"))
        tmp = reg.path.with_suffix(reg.path.suffix + ".tmp")
        # Pode ou não existir momentos antes (race window), mas após o
        # replace o tmp some.
        assert not tmp.exists() or tmp.stat().st_size > 0
    # Estado final: 10 entries, sem tmp pendurado.
    assert not reg.path.with_suffix(reg.path.suffix + ".tmp").exists()
    assert len(reg.list(gc=False)) == 10
