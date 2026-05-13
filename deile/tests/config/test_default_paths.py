"""Regressão: ConfigManager e InstructionLoader não podem criar pastas
``deile/config`` ou ``deile/personas/instructions`` no cwd quando
instanciados sem argumentos.

Antes do fix usavam ``Path("deile/config")`` e
``Path("deile/personas/instructions")`` como default (paths relativos
ao cwd), o que sujava qualquer diretório de execução.

Agora resolvem a partir de ``__file__`` (pacote-relativo).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from deile.config.manager import ConfigManager
from deile.personas.instruction_loader import InstructionLoader


# parents[2] do test file aponta diretamente para o pacote ``deile/``.
_DEILE_PKG = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_config_manager_default_resolves_to_package(tmp_path, monkeypatch):
    """ConfigManager() sem argumentos não cria pasta no cwd."""
    monkeypatch.chdir(tmp_path)
    cm = ConfigManager()
    # Default aponta para deile/config (dentro do pacote), NÃO para cwd/deile/config.
    assert cm.config_dir == _DEILE_PKG / "config"
    # Nenhuma pasta nova surgiu no cwd.
    assert not (tmp_path / "deile").exists()


@pytest.mark.unit
def test_instruction_loader_default_resolves_to_package(tmp_path, monkeypatch):
    """InstructionLoader() sem argumentos não cria pasta no cwd."""
    monkeypatch.chdir(tmp_path)
    loader = InstructionLoader()
    assert loader.instructions_dir == _DEILE_PKG / "personas" / "instructions"
    assert not (tmp_path / "deile").exists()


@pytest.mark.unit
def test_config_manager_explicit_path_still_honored(tmp_path):
    """Argumento explícito não é sobrescrito pelo default."""
    custom = tmp_path / "custom-config"
    cm = ConfigManager(config_dir=custom)
    assert cm.config_dir == custom
    assert custom.exists()  # criado com parents=True


@pytest.mark.unit
def test_instruction_loader_explicit_path_still_honored(tmp_path):
    """Argumento explícito não é sobrescrito pelo default."""
    custom = tmp_path / "custom-instructions"
    loader = InstructionLoader(instructions_dir=custom)
    assert loader.instructions_dir == custom
    assert custom.exists()
