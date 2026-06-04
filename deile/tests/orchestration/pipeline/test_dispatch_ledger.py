"""Tests para :mod:`deile.orchestration.pipeline.dispatch_ledger` (issue #309
fase 3.5). Cobertura: persistência atomic, leitura/escrita, clear, corrupção,
versão, env override, key helpers, list_all, cache invalidation."""
from __future__ import annotations

import json
import time


from deile.orchestration.pipeline.dispatch_ledger import (
    LEDGER_SCHEMA_VERSION, DispatchLedger, _default_ledger_path)


def test_key_helpers():
    assert DispatchLedger.key_for_pr(344) == "pr:344"
    assert DispatchLedger.key_for_issue(99) == "issue:99"


def test_record_then_get_roundtrip(tmp_path):
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record(
        "pr:1", task_id="t1", session_id="s1",
        stage="pr_review", branch="auto/issue-1", worker_kind="claude",
    )
    r = ledger.get("pr:1")
    assert r is not None
    assert r["task_id"] == "t1"
    assert r["session_id"] == "s1"
    assert r["stage"] == "pr_review"
    assert r["branch"] == "auto/issue-1"
    assert r["worker_kind"] == "claude"
    assert r["attempt"] == 1
    assert "first_seen_at" in r
    assert "last_seen_at" in r


def test_record_increments_attempt_on_update(tmp_path):
    """Second record com mesma key incrementa attempt e mantém first_seen_at."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("pr:1", task_id="t1", session_id="s1")
    r1 = ledger.get("pr:1")
    time.sleep(0.01)
    ledger.record("pr:1", task_id="t1", session_id="s1")
    r2 = ledger.get("pr:1")
    assert r2["attempt"] == 2
    assert r2["first_seen_at"] == r1["first_seen_at"]
    assert r2["last_seen_at"] >= r1["last_seen_at"]


def test_clear_removes_record(tmp_path):
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("pr:1", task_id="t", session_id="s")
    assert ledger.get("pr:1") is not None
    ledger.clear("pr:1")
    assert ledger.get("pr:1") is None
    # clear de chave ausente é no-op (não levanta).
    ledger.clear("pr:1")
    ledger.clear("inexistente")


def test_persists_to_disk(tmp_path):
    """Record grava no disco; nova instância lê de volta."""
    path = tmp_path / "l.json"
    ledger1 = DispatchLedger(path=path)
    ledger1.record("issue:42", task_id="t42", session_id="s42")
    # Nova instância (sem cache compartilhado).
    ledger2 = DispatchLedger(path=path)
    r = ledger2.get("issue:42")
    assert r is not None
    assert r["task_id"] == "t42"


def test_handles_missing_file(tmp_path):
    """Sem arquivo no disco — get retorna None, sem crash."""
    ledger = DispatchLedger(path=tmp_path / "nope.json")
    assert ledger.get("pr:1") is None
    assert ledger.list_all() == {}


def test_handles_corrupted_file(tmp_path):
    """Arquivo com JSON inválido → starta vazio (logger.warning)."""
    path = tmp_path / "l.json"
    path.write_text("{ broken json ::: ::")
    ledger = DispatchLedger(path=path)
    assert ledger.get("pr:1") is None  # vazio (corrupted)


def test_handles_malformed_root(tmp_path):
    """Arquivo com JSON válido mas formato inesperado (lista em vez de dict)
    → starta vazio."""
    path = tmp_path / "l.json"
    path.write_text(json.dumps(["not", "a", "dict"]))
    ledger = DispatchLedger(path=path)
    assert ledger.get("pr:1") is None


def test_atomic_write_uses_tmp_file(tmp_path):
    """Implementação usa write-tmp + os.replace — verifica que NÃO existe
    .json.tmp pendurado após operação normal."""
    path = tmp_path / "l.json"
    ledger = DispatchLedger(path=path)
    ledger.record("pr:1", task_id="t", session_id="s")
    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()


def test_list_all_returns_snapshot(tmp_path):
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("pr:1", task_id="a", session_id="x")
    ledger.record("issue:99", task_id="b", session_id="y")
    snapshot = ledger.list_all()
    assert set(snapshot.keys()) == {"pr:1", "issue:99"}
    # Modificação no snapshot não afeta o ledger.
    snapshot["pr:1"]["task_id"] = "tampered"
    assert ledger.get("pr:1")["task_id"] == "a"


def test_empty_key_or_task_id_skipped(tmp_path):
    """record com key vazia ou task_id vazio é no-op (logger.warning)."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("", task_id="t", session_id="s")
    ledger.record("pr:1", task_id="", session_id="s")
    assert ledger.list_all() == {}


def test_schema_version_in_file(tmp_path):
    """Arquivo gerado contém o campo version (schema migrável)."""
    path = tmp_path / "l.json"
    ledger = DispatchLedger(path=path)
    ledger.record("pr:1", task_id="t", session_id="s")
    data = json.loads(path.read_text())
    assert data["version"] == LEDGER_SCHEMA_VERSION


def test_env_override_path(tmp_path, monkeypatch):
    """``DEILE_PIPELINE_LEDGER_PATH`` env var sobrescreve o default."""
    custom = tmp_path / "custom.json"
    monkeypatch.setenv("DEILE_PIPELINE_LEDGER_PATH", str(custom))
    assert _default_ledger_path() == custom


def test_default_path_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("DEILE_PIPELINE_LEDGER_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    p = _default_ledger_path()
    assert p.name == "dispatches.json"
    assert p.parent.name == "pipeline"


def test_invalidate_cache_re_reads_disk(tmp_path):
    """invalidate_cache força reload do disco."""
    path = tmp_path / "l.json"
    ledger = DispatchLedger(path=path)
    ledger.record("pr:1", task_id="t", session_id="s")
    # Modifica direto no disco (simulating other writer — não acontece na
    # prática, single-writer, mas testa o mecanismo).
    data = json.loads(path.read_text())
    data["dispatches"]["pr:1"]["task_id"] = "modified-externally"
    path.write_text(json.dumps(data))
    # Sem invalidate, ledger ainda usa cache.
    assert ledger.get("pr:1")["task_id"] == "t"
    ledger.invalidate_cache()
    assert ledger.get("pr:1")["task_id"] == "modified-externally"


# ------------------------------------------------------------------ #
# extra field — campo livre para metadados (ex.: guard de convergência)
# ------------------------------------------------------------------ #

def test_record_with_extra_roundtrip(tmp_path):
    """record com extra → get devolve entry com extra preservado."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record(
        "issue:7",
        task_id="t7", session_id="s7",
        stage="refine",
        extra={"before_body": "corpo antes do refino", "round": 1},
    )
    r = ledger.get("issue:7")
    assert r is not None
    assert r["extra"] == {"before_body": "corpo antes do refino", "round": 1}


def test_record_without_extra_retrocompat(tmp_path):
    """record sem extra → entry sem campo extra (retrocompat total)."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("pr:1", task_id="t1", session_id="s1", stage="pr_review")
    r = ledger.get("pr:1")
    assert r is not None
    assert "extra" not in r
    # Campos obrigatórios continuam presentes.
    assert r["task_id"] == "t1"
    assert r["attempt"] == 1


def test_record_extra_persists_disk_roundtrip(tmp_path):
    """extra sobrevive a flush + reload (save/load do JSON)."""
    path = tmp_path / "l.json"
    ledger1 = DispatchLedger(path=path)
    ledger1.record(
        "issue:42",
        task_id="t42", session_id="s42",
        extra={"before_body": "x", "score": 0.9},
    )
    # Nova instância — sem cache compartilhado.
    ledger2 = DispatchLedger(path=path)
    r = ledger2.get("issue:42")
    assert r is not None
    assert r["extra"] == {"before_body": "x", "score": 0.9}
    # Verifica também no JSON bruto.
    raw = json.loads(path.read_text())
    assert raw["dispatches"]["issue:42"]["extra"] == {"before_body": "x", "score": 0.9}
