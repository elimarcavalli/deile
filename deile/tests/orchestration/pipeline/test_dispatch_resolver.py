"""Unit tests for ``dispatch_resolver`` — espelha ``test_model_resolver.py``.

Cobre:
- Fallback chain (stage env → global env → built-in default)
- Whitelist enforcement (only 'deile-worker' | 'claude-worker' accepted)
- ValueError para stage inválido (programming bug)
- Endpoint mapping (deile-worker → :8766, claude-worker → :8767)
"""

import pytest

from deile.orchestration.pipeline.dispatch_resolver import (
    PIPELINE_STAGES, VALID_DISPATCHERS, get_endpoint_for, is_valid_dispatcher,
    resolve_stage_dispatcher)


def _clear_env(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_PIPELINE_DISPATCH_MODE", raising=False)
    monkeypatch.delenv("DEILE_WORKER_ENDPOINT", raising=False)
    monkeypatch.delenv("DEILE_CLAUDE_WORKER_ENDPOINT", raising=False)


def test_stages_canonical_order():
    """Stage tuple keeps operational lifecycle order."""
    assert PIPELINE_STAGES == ("classify", "refine", "implement", "pr_review", "follow_ups")


def test_valid_dispatchers_frozen():
    assert "deile-worker" in VALID_DISPATCHERS
    assert "claude-worker" in VALID_DISPATCHERS
    assert len(VALID_DISPATCHERS) == 2


def test_resolve_default_returns_deile_worker(monkeypatch):
    """Sem nenhuma env var, default built-in = deile-worker."""
    _clear_env(monkeypatch)
    assert resolve_stage_dispatcher("implement") == "deile-worker"


def test_resolve_global_env(monkeypatch):
    """DEILE_PIPELINE_DISPATCH_MODE sobrescreve built-in default."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", "claude-worker")
    assert resolve_stage_dispatcher("implement") == "claude-worker"
    assert resolve_stage_dispatcher("classify") == "claude-worker"


def test_resolve_stage_overrides_global(monkeypatch):
    """DEILE_PIPELINE_DISPATCH_<STAGE> sobrescreve global."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", "deile-worker")
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
    assert resolve_stage_dispatcher("implement") == "claude-worker"
    assert resolve_stage_dispatcher("classify") == "deile-worker"


def test_resolve_invalid_stage_raises(monkeypatch):
    """Stage fora de PIPELINE_STAGES → ValueError (programming bug)."""
    _clear_env(monkeypatch)
    with pytest.raises(ValueError, match="unknown stage"):
        resolve_stage_dispatcher("non_existent")


def test_resolve_invalid_dispatcher_in_env_falls_through(monkeypatch):
    """Valor inválido em env → ValueError com mensagem clara."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "garbage")
    with pytest.raises(ValueError, match="unknown dispatcher"):
        resolve_stage_dispatcher("implement")


def test_resolve_empty_string_treated_as_unset(monkeypatch):
    """Empty value → fallback continues."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "")
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", "claude-worker")
    assert resolve_stage_dispatcher("implement") == "claude-worker"


def test_get_endpoint_for_deile_worker(monkeypatch):
    _clear_env(monkeypatch)
    assert get_endpoint_for("deile-worker") == "http://deile-worker:8766"


def test_get_endpoint_for_claude_worker(monkeypatch):
    _clear_env(monkeypatch)
    assert get_endpoint_for("claude-worker") == "http://claude-worker:8767"


def test_get_endpoint_for_honors_env_override(monkeypatch):
    """Env override útil pra dev local que não usa Service DNS."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ENDPOINT", "http://localhost:9090")
    assert get_endpoint_for("claude-worker") == "http://localhost:9090"


def test_get_endpoint_for_unknown_raises():
    with pytest.raises(ValueError, match="unknown dispatcher"):
        get_endpoint_for("magical-worker")


def test_is_valid_dispatcher_table():
    assert is_valid_dispatcher("deile-worker") is True
    assert is_valid_dispatcher("claude-worker") is True
    assert is_valid_dispatcher("DEILE-WORKER") is True  # case-insensitive
    assert is_valid_dispatcher("garbage") is False
    assert is_valid_dispatcher("") is False
    assert is_valid_dispatcher(None) is False
