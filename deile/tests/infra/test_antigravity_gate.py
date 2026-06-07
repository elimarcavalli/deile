"""Testes do GATE do Antigravity (Fase 5 — frota multi-CLI, Tier 3 ⚠️ GATED).

O Antigravity NÃO entra na frota enquanto o spike obrigatório (plano §2.6 /
Fase E1) não provar auth headless + one-shot determinístico. Estes testes
asseguram a invariante do gate:

  1. O módulo ``cli_adapters.antigravity`` existe e declara a sentinela
     :data:`ANTIGRAVITY_GATED = True` (decisão versionada junto do código).
  2. NÃO expõe ``ADAPTER`` nem ``get_adapter`` → o auto-discovery o IGNORA.
  3. ``antigravity`` NÃO está em ``cli_adapters.ADAPTERS`` e
     ``antigravity-worker`` NÃO é um dispatcher válido — não quebra ``k8s up``,
     não aparece no painel, não vira destino de dispatch.

Se algum dia o gate for liberado (spike passou), estes testes mudam JUNTO com a
decisão #51 — falham de propósito até serem atualizados, sinalizando que a
liberação é consciente.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cli_adapters  # noqa: E402
from cli_adapters import antigravity as ag_mod  # noqa: E402


@pytest.mark.unit
def test_gate_sentinel_is_closed():
    assert ag_mod.ANTIGRAVITY_GATED is True
    # Porta reservada para quando o gate liberar (§1.13).
    assert ag_mod.ANTIGRAVITY_RESERVED_PORT == 8776


@pytest.mark.unit
def test_module_exposes_no_adapter_instance():
    # Enquanto gated, o módulo NÃO exporta ADAPTER nem get_adapter — é o que o
    # mantém fora do auto-discovery.
    assert not hasattr(ag_mod, "ADAPTER")
    assert not hasattr(ag_mod, "get_adapter")


@pytest.mark.unit
def test_draft_class_is_not_instantiated_at_module_level():
    # A classe-rascunho existe (custo de retomada baixo), mas NÃO há instância
    # dela exposta no módulo — varredura do registro não acha CliAdapter.
    from cli_adapters import base

    instances = [
        getattr(ag_mod, n)
        for n in dir(ag_mod)
        if not n.startswith("_")
    ]
    assert not any(isinstance(obj, base.CliAdapter) for obj in instances)


@pytest.mark.unit
def test_antigravity_not_registered():
    cli_adapters.reload_adapters()
    assert "antigravity" not in cli_adapters.ADAPTERS


@pytest.mark.unit
def test_antigravity_worker_is_not_valid_dispatcher():
    from deile.orchestration.pipeline import dispatch_resolver as dr

    valid = dr.get_valid_dispatchers()
    assert "antigravity-worker" not in valid
    assert dr.is_valid_dispatcher("antigravity-worker") is False
    assert dr.is_valid_dispatcher("antigravity") is False
