"""Unit tests for ``dispatch_resolver`` — espelha ``test_model_resolver.py``.

Cobre:
- Fallback chain (stage env → global env → built-in default)
- Whitelist enforcement (only 'deile-worker' | 'claude-worker' accepted)
- ValueError para stage inválido (programming bug)
- Endpoint mapping (deile-worker → :8766, claude-worker → :8767)
- resolve_stage_timeout_s / resolve_stage_max_retries (issue #391)
"""

import pytest

from deile.orchestration.pipeline.dispatch_resolver import (
    PIPELINE_STAGES, VALID_DISPATCHERS, get_endpoint_for, is_valid_dispatcher,
    resolve_stage_dispatcher, resolve_stage_max_retries, resolve_stage_timeout_s)


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


def test_resolve_invalid_dispatcher_in_env_raises(monkeypatch):
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


def test_resolve_accepts_legacy_worker_aliases(monkeypatch):
    """Aliases legacy de WORKER_ALIASES (PR #330) devem mapear para canônico
    'deile-worker'. Compat retroativa com DEILE_PIPELINE_DISPATCH_MODE setado
    em deployments existentes."""
    _clear_env(monkeypatch)
    for legacy in ("deile_worker", "worker", "deile"):
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", legacy)
        assert resolve_stage_dispatcher("implement") == "deile-worker", \
            f"alias {legacy!r} should canonicalize to 'deile-worker'"


def test_resolve_accepts_legacy_claude_aliases(monkeypatch):
    """Aliases legacy de CLAUDE_ALIASES (PR #330) devem mapear para canônico
    'claude-worker'."""
    _clear_env(monkeypatch)
    for legacy in ("claude", "claude_code", "claude-code"):
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", legacy)
        assert resolve_stage_dispatcher("implement") == "claude-worker", \
            f"alias {legacy!r} should canonicalize to 'claude-worker'"


def test_is_valid_dispatcher_accepts_legacy_aliases():
    """is_valid_dispatcher também aceita legacy aliases (case-insensitive)."""
    for legacy in ("deile_worker", "worker", "deile", "claude", "claude_code", "claude-code"):
        assert is_valid_dispatcher(legacy) is True, \
            f"legacy alias {legacy!r} should validate"
    # Sanity: garbage still rejected
    assert is_valid_dispatcher("garbage") is False


# ===== resolve_stage_timeout_s (issue #391) ==================================

def _clear_timeout_env(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_TIMEOUT_S_{stage.upper()}", raising=False)


def test_timeout_invalid_stage_raises(monkeypatch):
    """Stage fora de PIPELINE_STAGES → ValueError."""
    _clear_timeout_env(monkeypatch)
    with pytest.raises(ValueError, match="unknown stage"):
        resolve_stage_timeout_s("garbage_stage")


def test_timeout_default_for_claude_stages(monkeypatch):
    """Sem override, stages claude (implement/pr_review) retornam 1800."""
    _clear_timeout_env(monkeypatch)
    # Dispatcher env vars must be set for implement/pr_review → claude-worker
    # (default global dispatcher is deile-worker).
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_PIPELINE_DISPATCH_MODE", raising=False)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_PR_REVIEW", "claude-worker")
    from deile.config.settings import reset_settings
    reset_settings()
    assert resolve_stage_timeout_s("implement") == 1800
    assert resolve_stage_timeout_s("pr_review") == 1800


def test_timeout_default_for_deile_stages(monkeypatch):
    """Sem override, stages deile (classify/refine/follow_ups) retornam 900."""
    _clear_timeout_env(monkeypatch)
    from deile.config.settings import reset_settings
    reset_settings()
    assert resolve_stage_timeout_s("classify") == 900
    assert resolve_stage_timeout_s("refine") == 900
    assert resolve_stage_timeout_s("follow_ups") == 900


def test_timeout_env_var_per_stage(monkeypatch):
    """DEILE_PIPELINE_TIMEOUT_S_<STAGE> sobrescreve o default."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_IMPLEMENT", "600")
    from deile.config.settings import reset_settings
    reset_settings()
    assert resolve_stage_timeout_s("implement") == 600
    # Other stages unaffected
    assert resolve_stage_timeout_s("classify") == 900


def test_timeout_env_var_invalid_raises(monkeypatch):
    """Valor inválido em env → ValueError (fail-fast)."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_IMPLEMENT", "not_a_number")
    from deile.config.settings import reset_settings
    reset_settings()
    with pytest.raises(ValueError):
        resolve_stage_timeout_s("implement")


def test_timeout_env_var_zero_raises(monkeypatch):
    """Valor 0 (não-positivo) em env → ValueError (fail-fast, timeout deve ser > 0)."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_CLASSIFY", "0")
    from deile.config.settings import reset_settings
    reset_settings()
    with pytest.raises(ValueError):
        resolve_stage_timeout_s("classify")


def test_timeout_settings_per_stage(monkeypatch):
    """pipeline_timeout_s_<stage> via settings sobrescreve o default."""
    _clear_timeout_env(monkeypatch)
    from deile.config.settings import get_settings, reset_settings
    reset_settings()
    s = get_settings()
    s.pipeline_timeout_s_classify = 300
    assert resolve_stage_timeout_s("classify") == 300
    # Cleanup
    s.pipeline_timeout_s_classify = None


# ===== resolve_stage_max_retries (issue #391) ================================

def _clear_retries_env(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_RETRIES_{stage.upper()}", raising=False)


def test_retries_invalid_stage_raises(monkeypatch):
    """Stage fora de PIPELINE_STAGES → ValueError."""
    _clear_retries_env(monkeypatch)
    with pytest.raises(ValueError, match="unknown stage"):
        resolve_stage_max_retries("garbage_stage")


def test_retries_default_is_three(monkeypatch):
    """Sem override, todos os stages retornam 3 (built-in)."""
    _clear_retries_env(monkeypatch)
    from deile.config.settings import reset_settings
    reset_settings()
    for stage in PIPELINE_STAGES:
        assert resolve_stage_max_retries(stage) == 3, \
            f"stage {stage!r} should default to 3 retries"


def test_retries_env_var_per_stage(monkeypatch):
    """DEILE_PIPELINE_RETRIES_<STAGE> sobrescreve o default."""
    _clear_retries_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_RETRIES_IMPLEMENT", "1")
    from deile.config.settings import reset_settings
    reset_settings()
    assert resolve_stage_max_retries("implement") == 1
    # Other stages unaffected
    assert resolve_stage_max_retries("classify") == 3


def test_retries_env_zero_allowed(monkeypatch):
    """Valor 0 é válido (zero retries = fail fast)."""
    _clear_retries_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_RETRIES_IMPLEMENT", "0")
    from deile.config.settings import reset_settings
    reset_settings()
    assert resolve_stage_max_retries("implement") == 0


def test_retries_env_invalid_raises(monkeypatch):
    """Valor inválido em env → ValueError (fail-fast)."""
    _clear_retries_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_RETRIES_CLASSIFY", "not_a_number")
    from deile.config.settings import reset_settings
    reset_settings()
    with pytest.raises(ValueError):
        resolve_stage_max_retries("classify")


def test_retries_settings_per_stage(monkeypatch):
    """pipeline_retries_<stage> via settings sobrescreve o default."""
    _clear_retries_env(monkeypatch)
    from deile.config.settings import get_settings, reset_settings
    reset_settings()
    s = get_settings()
    s.pipeline_retries_implement = 5
    assert resolve_stage_max_retries("implement") == 5
    # Cleanup
    s.pipeline_retries_implement = None


# ===== Error message context (issue #478 finding #5) =========================

def test_timeout_env_invalid_message_contains_env_var_name(monkeypatch):
    """ValueError message must include the env var name for traceability."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_PR_REVIEW", "foo")
    from deile.config.settings import reset_settings
    reset_settings()
    with pytest.raises(ValueError, match="DEILE_PIPELINE_TIMEOUT_S_PR_REVIEW"):
        resolve_stage_timeout_s("pr_review")


def test_timeout_env_invalid_message_contains_raw_value(monkeypatch):
    """ValueError message must include the raw value that caused the failure."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_CLASSIFY", "foo")
    from deile.config.settings import reset_settings
    reset_settings()
    with pytest.raises(ValueError, match="foo"):
        resolve_stage_timeout_s("classify")


def test_retries_env_invalid_message_contains_env_var_name(monkeypatch):
    """ValueError message must include the env var name for traceability."""
    _clear_retries_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_RETRIES_IMPLEMENT", "bar")
    from deile.config.settings import reset_settings
    reset_settings()
    with pytest.raises(ValueError, match="DEILE_PIPELINE_RETRIES_IMPLEMENT"):
        resolve_stage_max_retries("implement")


def test_retries_env_invalid_message_contains_raw_value(monkeypatch):
    """ValueError message must include the raw value that caused the failure."""
    _clear_retries_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_RETRIES_FOLLOW_UPS", "bar")
    from deile.config.settings import reset_settings
    reset_settings()
    with pytest.raises(ValueError, match="bar"):
        resolve_stage_max_retries("follow_ups")
