"""Tests for dispatch_resolver settings.json integration (issue #359).

Verifica a cadeia de precedência completa (5 camadas) de
``resolve_stage_dispatcher``:

  1. env var per-stage  → fail-fast em valor inválido
  2. settings per-stage → warn + fallback em valor inválido
  3. env var global     → fail-fast em valor inválido
  4. settings global    → warn + fallback em valor inválido
  5. default hardcoded  → "deile-worker"

Cobre os 8 cenários listados na issue:
  - test_resolver_env_per_stage_wins_over_settings_per_stage
  - test_resolver_settings_per_stage_wins_over_env_global
  - test_resolver_env_global_wins_over_settings_global
  - test_resolver_settings_global_wins_over_default
  - test_resolver_default_when_nothing_set
  - test_resolver_handles_all_5_stages_consistently
  - test_resolver_invalid_dispatcher_value_falls_through_with_warning
  - test_resolver_does_not_call_kubectl_or_settings_io_per_call
"""
from __future__ import annotations

import logging

import pytest

from deile.config.settings import get_settings, reset_settings
from deile.orchestration.pipeline.dispatch_resolver import (
    PIPELINE_STAGES, resolve_stage_dispatcher)


def _clear_env(monkeypatch):
    """Remove all dispatch-related env vars so each test starts clean."""
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_PIPELINE_DISPATCH_MODE", raising=False)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Each test starts with a fresh Settings singleton and clean env.

    Also resets logging.disable() — some tests in the suite call it without
    restoring, which silences WARNING records and breaks caplog assertions
    (known suite-wide issue, documented in test_file_context_truncation.py).
    """
    _clear_env(monkeypatch)
    reset_settings()
    _saved_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    yield
    reset_settings()
    logging.disable(_saved_disable)


# ---------------------------------------------------------------------------
# Precedência 1 vs 2: env per-stage bate settings per-stage
# ---------------------------------------------------------------------------

def test_resolver_env_per_stage_wins_over_settings_per_stage(monkeypatch):
    """Env var per-stage sempre vence sobre settings per-stage."""
    # settings says claude-worker
    reset_settings()
    get_settings().pipeline_dispatcher_implement = "claude-worker"

    # env says deile-worker → must win
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "deile-worker")
    assert resolve_stage_dispatcher("implement") == "deile-worker"


def test_resolver_settings_per_stage_wins_over_env_global(monkeypatch):
    """settings per-stage vence sobre env var global."""
    # global env says deile-worker
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", "deile-worker")
    reset_settings()

    # settings per-stage says claude-worker → must win
    get_settings().pipeline_dispatcher_implement = "claude-worker"
    assert resolve_stage_dispatcher("implement") == "claude-worker"


# ---------------------------------------------------------------------------
# Precedência 3 vs 4: env global bate settings global
# ---------------------------------------------------------------------------

def test_resolver_env_global_wins_over_settings_global(monkeypatch):
    """Env var global vence sobre settings.pipeline_dispatch_mode."""
    # env global says claude-worker
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", "claude-worker")
    reset_settings()

    # settings global says deile-worker (the default — or explicit)
    get_settings().pipeline_dispatch_mode = "deile_worker"

    assert resolve_stage_dispatcher("implement") == "claude-worker"
    assert resolve_stage_dispatcher("classify") == "claude-worker"


def test_resolver_settings_global_wins_over_default(monkeypatch):
    """settings.pipeline_dispatch_mode vence sobre o hardcoded default."""
    # No env vars at all
    reset_settings()
    get_settings().pipeline_dispatch_mode = "claude-worker"

    assert resolve_stage_dispatcher("implement") == "claude-worker"
    assert resolve_stage_dispatcher("refine") == "claude-worker"


# ---------------------------------------------------------------------------
# Precedência 5: nada setado → default
# ---------------------------------------------------------------------------

def test_resolver_default_when_nothing_set():
    """Sem env vars e sem settings explícito → deile-worker (default)."""
    # settings.pipeline_dispatch_mode is "deile_worker" by default, which
    # canonicalizes to "deile-worker" — same as the hardcoded default.
    assert resolve_stage_dispatcher("implement") == "deile-worker"
    assert resolve_stage_dispatcher("classify") == "deile-worker"
    assert resolve_stage_dispatcher("refine") == "deile-worker"
    assert resolve_stage_dispatcher("pr_review") == "deile-worker"
    assert resolve_stage_dispatcher("follow_ups") == "deile-worker"


# ---------------------------------------------------------------------------
# Todos os 5 stages se comportam consistentemente
# ---------------------------------------------------------------------------

def test_resolver_handles_all_5_stages_consistently():
    """Todos os 5 stages têm comportamento idêntico (sem env, sem settings)."""
    for stage in PIPELINE_STAGES:
        assert resolve_stage_dispatcher(stage) == "deile-worker", (
            f"stage {stage!r} broke default resolution"
        )


def test_resolver_all_stages_via_settings_per_stage():
    """Cada stage pode ser overriden individualmente via settings."""
    settings = get_settings()
    for stage in PIPELINE_STAGES:
        attr = f"pipeline_dispatcher_{stage}"
        setattr(settings, attr, "claude-worker")
        assert resolve_stage_dispatcher(stage) == "claude-worker", (
            f"stage {stage!r} failed settings per-stage override"
        )
        # Reset for next stage
        setattr(settings, attr, None)


def test_resolver_stages_are_independent_per_settings():
    """Override de um stage não vaza para os outros."""
    get_settings().pipeline_dispatcher_implement = "claude-worker"

    assert resolve_stage_dispatcher("implement") == "claude-worker"
    assert resolve_stage_dispatcher("classify") == "deile-worker"
    assert resolve_stage_dispatcher("refine") == "deile-worker"
    assert resolve_stage_dispatcher("pr_review") == "deile-worker"
    assert resolve_stage_dispatcher("follow_ups") == "deile-worker"


# ---------------------------------------------------------------------------
# Valor inválido em settings.json cai pro próximo nível (warn, não raise)
# ---------------------------------------------------------------------------

def test_resolver_invalid_settings_per_stage_falls_through_with_warning(caplog):
    """Valor inválido em settings per-stage → warning + fallback to global."""
    with caplog.at_level(logging.WARNING, logger="deile.orchestration.pipeline.dispatch_resolver"):
        get_settings().pipeline_dispatcher_implement = "bogus-engine"
        result = resolve_stage_dispatcher("implement")

    # Falls through to the global default ("deile-worker")
    assert result == "deile-worker"
    assert any("bogus-engine" in r.message for r in caplog.records), (
        "expected a warning log mentioning the invalid value"
    )


def test_resolver_invalid_settings_global_falls_through_with_warning(caplog):
    """Valor inválido em settings global → warning + hardcoded fallback."""
    with caplog.at_level(logging.WARNING, logger="deile.orchestration.pipeline.dispatch_resolver"):
        get_settings().pipeline_dispatch_mode = "totally-unknown"
        result = resolve_stage_dispatcher("implement")

    assert result == "deile-worker"
    assert any("totally-unknown" in r.message for r in caplog.records), (
        "expected a warning log mentioning the invalid global value"
    )


def test_resolver_invalid_dispatcher_value_falls_through_with_warning(caplog):
    """Combined test: invalid per-stage → falls to global default."""
    with caplog.at_level(logging.WARNING, logger="deile.orchestration.pipeline.dispatch_resolver"):
        get_settings().pipeline_dispatcher_classify = "not-a-worker"
        result = resolve_stage_dispatcher("classify")

    assert result == "deile-worker"
    assert caplog.records, "expected at least one warning"


# ---------------------------------------------------------------------------
# Env var inválido ainda levanta (fail-fast para configs de ops)
# ---------------------------------------------------------------------------

def test_resolver_invalid_env_per_stage_still_raises(monkeypatch):
    """Env var per-stage inválida ainda levanta ValueError (fail-fast)."""
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_CLASSIFY", "garbage")
    with pytest.raises(ValueError, match="unknown dispatcher"):
        resolve_stage_dispatcher("classify")


# ---------------------------------------------------------------------------
# Perf: get_settings() é singleton — sem I/O por chamada
# ---------------------------------------------------------------------------

def test_resolver_does_not_call_kubectl_or_settings_io_per_call(monkeypatch):
    """resolve_stage_dispatcher usa o singleton de settings — sem I/O extra.

    Verifica que múltiplas chamadas consecutivas retornam consistentemente
    sem recarregar settings do disco. O contador de chamadas a
    ``_load_layered_settings`` fica em 0 após o singleton estar aquecido.
    """
    # Warm up singleton
    _ = resolve_stage_dispatcher("implement")

    call_count = 0
    original_load = None

    import deile.config.settings as settings_mod
    original_load = settings_mod._load_layered_settings

    def counting_load():
        nonlocal call_count
        call_count += 1
        return original_load()

    monkeypatch.setattr(settings_mod, "_load_layered_settings", counting_load)

    # Multiple calls — none should trigger a reload
    for stage in PIPELINE_STAGES:
        resolve_stage_dispatcher(stage)

    assert call_count == 0, (
        f"_load_layered_settings was called {call_count} times; "
        "resolve_stage_dispatcher should use the cached singleton"
    )


# ---------------------------------------------------------------------------
# Legacy aliases from settings.json are canonicalized correctly
# ---------------------------------------------------------------------------

def test_resolver_settings_per_stage_canonicalizes_legacy_aliases():
    """Legacy alias stored in settings.json is canonicalized to canonical form."""
    settings = get_settings()
    for alias, expected in [("deile_worker", "deile-worker"), ("claude_code", "claude-worker")]:
        settings.pipeline_dispatcher_refine = alias
        assert resolve_stage_dispatcher("refine") == expected, (
            f"alias {alias!r} should canonicalize to {expected!r}"
        )
    settings.pipeline_dispatcher_refine = None


def test_resolver_full_precedence_chain_all_layers(monkeypatch):
    """End-to-end precedence: env per-stage > settings per-stage > env global > settings global > default."""
    settings = get_settings()

    # Layer 5: only default — deile-worker
    assert resolve_stage_dispatcher("pr_review") == "deile-worker"

    # Layer 4: settings global — claude-worker
    settings.pipeline_dispatch_mode = "claude-worker"
    assert resolve_stage_dispatcher("pr_review") == "claude-worker"

    # Layer 3: env global — deile-worker (beats settings global)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", "deile-worker")
    assert resolve_stage_dispatcher("pr_review") == "deile-worker"

    # Layer 2: settings per-stage — claude-worker (beats env global)
    settings.pipeline_dispatcher_pr_review = "claude-worker"
    assert resolve_stage_dispatcher("pr_review") == "claude-worker"

    # Layer 1: env per-stage — deile-worker (beats all)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_PR_REVIEW", "deile-worker")
    assert resolve_stage_dispatcher("pr_review") == "deile-worker"
