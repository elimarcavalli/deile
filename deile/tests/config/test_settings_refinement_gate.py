"""Testes de regressão para o desacoplamento do gate de refino (issue #85).

Garante que ``enable_refinement_gate`` em ``build_default_pipeline_config``
é resolvido de ``settings.pipeline_refinement_gate`` e NÃO de
``dispatch_mode`` — ou seja, rodar frota toda em claude não desliga o gate.

Cenários:
- DEILE_PIPELINE_DISPATCH_MODE=claude sem override do gate → gate ON (default)
- DEILE_PIPELINE_DISPATCH_MODE=deile_worker sem override → gate ON (default)
- DEILE_PIPELINE_REFINEMENT_GATE=0 → gate OFF, independente do dispatch_mode
- DEILE_PIPELINE_REFINEMENT_GATE=true → gate ON explicitamente
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from deile.config.settings import (Settings, _apply_env_overrides,
                                    reset_settings)


@pytest.fixture(autouse=True)
def _reset():
    reset_settings()
    yield
    reset_settings()


def _patch_build_deps(monkeypatch, settings, tmp_path):
    monkeypatch.setattr("deile.config.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "deile.orchestration.pipeline.constants.resolve_pipeline_repo",
        lambda: "owner/repo",
    )
    monkeypatch.setattr(
        "deile.tools._pipeline_paths.resolve_base_path", lambda: tmp_path
    )


def _make_settings(**kwargs) -> Settings:
    s = Settings()
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


class TestRefinementGateDecoupling:
    def test_gate_on_by_default_under_claude_dispatch_mode(self, monkeypatch, tmp_path):
        """DEILE_PIPELINE_DISPATCH_MODE=claude NÃO desliga o gate (issue #85)."""
        s = _make_settings(pipeline_dispatch_mode="claude")
        # pipeline_refinement_gate usa o default (True)
        _patch_build_deps(monkeypatch, s, tmp_path)

        from deile.orchestration.pipeline.monitor import build_default_pipeline_config
        cfg = build_default_pipeline_config()

        assert cfg.enable_refinement_gate is True

    def test_gate_on_by_default_under_deile_worker_dispatch_mode(self, monkeypatch, tmp_path):
        """dispatch_mode=deile_worker também não altera o gate — default ON."""
        s = _make_settings(pipeline_dispatch_mode="deile_worker")
        _patch_build_deps(monkeypatch, s, tmp_path)

        from deile.orchestration.pipeline.monitor import build_default_pipeline_config
        cfg = build_default_pipeline_config()

        assert cfg.enable_refinement_gate is True

    def test_gate_off_when_env_var_is_zero(self, monkeypatch, tmp_path):
        """DEILE_PIPELINE_REFINEMENT_GATE=0 desliga o gate, independente de dispatch_mode."""
        s = _make_settings(pipeline_dispatch_mode="claude", pipeline_refinement_gate=False)
        _patch_build_deps(monkeypatch, s, tmp_path)

        from deile.orchestration.pipeline.monitor import build_default_pipeline_config
        cfg = build_default_pipeline_config()

        assert cfg.enable_refinement_gate is False

    def test_gate_on_when_env_var_is_true_under_claude(self, monkeypatch, tmp_path):
        """pipeline_refinement_gate=True explícito sob claude → gate ON."""
        s = _make_settings(pipeline_dispatch_mode="claude", pipeline_refinement_gate=True)
        _patch_build_deps(monkeypatch, s, tmp_path)

        from deile.orchestration.pipeline.monitor import build_default_pipeline_config
        cfg = build_default_pipeline_config()

        assert cfg.enable_refinement_gate is True


class TestRefinementGateEnvParse:
    """Parse do env var VIVO via ``_env_bool`` (issue #85).

    Ao contrário do espelho ``enable_resume`` (cujo env var foi REMOVIDO na #309
    fase 3), ``DEILE_PIPELINE_REFINEMENT_GATE`` está registrado em
    ``_ENV_OVERRIDES`` — então o parse ``"0"``/``"true"``/``"on"`` → bool é
    exercido de fato, fechando a malha que os testes acima (que setam o field
    direto no ``Settings``) deixavam aberta.
    """

    def test_env_zero_turns_gate_off(self, monkeypatch):
        """DEILE_PIPELINE_REFINEMENT_GATE=0 → field False."""
        monkeypatch.setenv("DEILE_PIPELINE_REFINEMENT_GATE", "0")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_refinement_gate is False

    def test_env_true_keeps_gate_on(self, monkeypatch):
        """DEILE_PIPELINE_REFINEMENT_GATE=true → field True."""
        monkeypatch.setenv("DEILE_PIPELINE_REFINEMENT_GATE", "true")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_refinement_gate is True

    def test_env_on_is_lenient_truthy(self, monkeypatch):
        """_env_bool é leniente: "on" também liga (paridade com os demais bools)."""
        monkeypatch.setenv("DEILE_PIPELINE_REFINEMENT_GATE", "on")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_refinement_gate is True

    def test_env_unset_preserves_default_true(self, monkeypatch):
        """Sem o env var, o default (True) é preservado."""
        monkeypatch.delenv("DEILE_PIPELINE_REFINEMENT_GATE", raising=False)
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_refinement_gate is True


class TestRefinementGateSettingsJson:
    """Override via ``settings.json`` (``pipeline.refinement_gate`` → ``_to_bool``)."""

    def test_apply_overrides_off(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"refinement_gate": False}})
        assert s.pipeline_refinement_gate is False

    def test_apply_overrides_bool_string_coercion(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"refinement_gate": "false"}})
        assert s.pipeline_refinement_gate is False

    def test_default_is_on(self):
        assert Settings().pipeline_refinement_gate is True
