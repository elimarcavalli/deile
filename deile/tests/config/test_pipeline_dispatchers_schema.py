"""Schema validation para ``pipeline.dispatchers.<stage>`` em settings.json.

Espelha o padrão de :mod:`test_settings_pipeline_models` (issue #305): cinco
overrides flat por stage (``pipeline_dispatcher_<stage>``), strict converter
``_to_optional_dispatcher`` rodando no caminho ``apply_overrides`` /
``Settings.load_from_file``, loose loader ``_apply_nested_dict`` aceitando os
mesmos paths ``pipeline.dispatchers.<stage>`` e env vars
``DEILE_PIPELINE_DISPATCH_<STAGE>`` rodando o mesmo validator.

A CLI persistence layer (``~/.deile/settings.json``) fica em paridade com o
cluster path (env vars na Deployment do worker) — ambos passam pelo mesmo
validator ``is_valid_dispatcher`` de :mod:`dispatch_resolver`, que aceita o
formato canônico (``deile-worker`` / ``claude-worker``) e os aliases legacy
de PR #330 (``deile_worker``, ``claude_code``, etc).
"""

from __future__ import annotations

import pytest

from deile.config.settings import (Settings, _apply_env_overrides,
                                   _apply_nested_dict, _to_optional_dispatcher)


class TestDispatcherValidator:
    """`_to_optional_dispatcher` é o strict converter plugado em
    ``_OVERRIDE_HANDLERS``. Um dispatcher inválido silenciosamente rotearia
    todo dispatch para o engine errado."""

    def test_none_and_empty_collapse_to_none(self):
        assert _to_optional_dispatcher(None) is None
        assert _to_optional_dispatcher("") is None
        assert _to_optional_dispatcher("   ") is None

    def test_canonical_values_pass_through(self):
        assert _to_optional_dispatcher("deile-worker") == "deile-worker"
        assert _to_optional_dispatcher("claude-worker") == "claude-worker"

    def test_legacy_aliases_accepted(self):
        # Compat com deployments existentes (PR #330) — aliases legacy
        # ainda são aceitos. Canonicalization fica a cargo do dispatch_resolver
        # no momento da resolução (não é responsabilidade da persistência).
        assert _to_optional_dispatcher("claude_code") == "claude_code"
        assert _to_optional_dispatcher("deile_worker") == "deile_worker"
        assert _to_optional_dispatcher("worker") == "worker"
        assert _to_optional_dispatcher("claude") == "claude"

    def test_strips_surrounding_whitespace(self):
        assert _to_optional_dispatcher("  deile-worker  ") == "deile-worker"

    @pytest.mark.parametrize("bad", [
        "garbage-worker",
        "anthropic",        # provedor, não dispatcher
        "openai",
        "random_value",
    ])
    def test_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            _to_optional_dispatcher(bad)

    def test_rejects_non_string(self):
        with pytest.raises(TypeError):
            _to_optional_dispatcher(42)
        with pytest.raises(TypeError):
            _to_optional_dispatcher(["deile-worker"])


class TestSettingsDefaults:
    def test_five_dispatcher_fields_default_to_none(self):
        s = Settings()
        assert s.pipeline_dispatcher_classify is None
        assert s.pipeline_dispatcher_refine is None
        assert s.pipeline_dispatcher_implement is None
        assert s.pipeline_dispatcher_pr_review is None
        assert s.pipeline_dispatcher_follow_ups is None


class TestApplyOverrides:
    """``apply_overrides`` roda a tabela strict ``_OVERRIDE_HANDLERS``.

    Usado por ``SettingsManager.set_setting`` e ``Settings.load_from_file``.
    """

    def test_writes_per_stage_fields_from_nested_json(self):
        s = Settings()
        s.apply_overrides({
            "pipeline": {
                "dispatchers": {
                    "implement": "claude-worker",
                    "pr_review": "claude-worker",
                    "classify": "deile-worker",
                }
            }
        })
        assert s.pipeline_dispatcher_implement == "claude-worker"
        assert s.pipeline_dispatcher_pr_review == "claude-worker"
        assert s.pipeline_dispatcher_classify == "deile-worker"
        # Stages não-setados continuam None (sem fallback implícito nessa camada).
        assert s.pipeline_dispatcher_refine is None
        assert s.pipeline_dispatcher_follow_ups is None

    def test_legacy_aliases_preserved_in_storage(self):
        """Aliases legacy persistem como recebidos; ``dispatch_resolver`` é
        que canonicaliza no momento da resolução. Garante compat com #330
        e mantém zero side-effects no caminho da persistência."""
        s = Settings()
        s.apply_overrides({
            "pipeline": {
                "dispatchers": {
                    "implement": "claude_code",  # legacy alias
                    "classify": "deile_worker",  # legacy alias
                }
            }
        })
        assert s.pipeline_dispatcher_implement == "claude_code"
        assert s.pipeline_dispatcher_classify == "deile_worker"

    def test_invalid_dispatcher_is_skipped_with_warning(self, caplog):
        s = Settings()
        # Pré-seta um valor válido pra provar que o reject não clobbera.
        s.pipeline_dispatcher_implement = "deile-worker"
        with caplog.at_level("WARNING", logger="deile.config.settings"):
            s.apply_overrides({
                "pipeline": {"dispatchers": {"implement": "garbage-worker"}}
            })
        # Valor anterior preservado.
        assert s.pipeline_dispatcher_implement == "deile-worker"
        # E o warning foi emitido (safety net do caminho strict).
        assert any("pipeline.dispatchers.implement" in r.message
                   for r in caplog.records)


class TestNestedDictLoader:
    """``_apply_nested_dict`` é o caminho looser usado por
    ``_load_layered_settings``. Deve aceitar ``pipeline.dispatchers.<stage>``
    para que o loader em camadas (``~/.deile/settings.json``) funcione."""

    def test_per_stage_keys_round_trip(self):
        s = Settings()
        _apply_nested_dict(s, {
            "pipeline": {
                "dispatchers": {
                    "refine": "claude-worker",
                    "follow_ups": "deile-worker",
                }
            }
        })
        assert s.pipeline_dispatcher_refine == "claude-worker"
        assert s.pipeline_dispatcher_follow_ups == "deile-worker"


class TestEnvOverrides:
    """As env vars ``DEILE_PIPELINE_DISPATCH_<STAGE>`` são conveniência pra
    ops (override one-off sem editar settings.json). Passam pelo mesmo
    validator strict do path JSON."""

    def test_env_vars_apply(self, monkeypatch):
        s = Settings()
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_PR_REVIEW", "claude-worker")
        _apply_env_overrides(s)
        assert s.pipeline_dispatcher_implement == "claude-worker"
        assert s.pipeline_dispatcher_pr_review == "claude-worker"

    def test_env_var_with_invalid_dispatcher_is_dropped(self, monkeypatch):
        s = Settings()
        s.pipeline_dispatcher_implement = "deile-worker"
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "garbage-worker")
        # ``_apply_env_overrides`` engole ValueError por contrato da tabela;
        # o valor anterior PRECISA sobreviver (regression guard contra typo
        # em env var silenciosamente clobberando dispatcher válido).
        _apply_env_overrides(s)
        assert s.pipeline_dispatcher_implement == "deile-worker"
