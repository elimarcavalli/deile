"""Paridade de preço entre os adapters da frota e a tabela de substring (FIX A).

``jsonl_cost.FLEET_PRICING_BY_SUBSTRING`` duplica, por substring de model-id, os
números declarados em ``ModelInfo.price_in``/``price_out`` dos adapters
(``cli_adapters/*.py``). As duas fontes coexistem (a tabela cobre model-ids
podados/históricos que o adapter não lista mais; o adapter prevalece em runtime
via ``declared``), mas quando AMBAS descrevem o MESMO modelo elas precisam bater
— senão o custo histórico (ledger podado, sem ``declared``) diverge silenciosamente
do custo live. Este teste trava essa divergência.

Para cada ``ModelInfo`` com preço declarado, resolve a substring que casaria via
o MESMO algoritmo de ``fleet_pricing_for`` (primeira substring contida no id,
ordem específico→genérico) e exige ``price_in==in`` e ``price_out==out``.

O pacote ``cli_adapters``/``jsonl_cost`` vivem em ``infra/k8s/`` (fora do pacote
``deile``); o path é inserido manualmente — mesma convenção dos demais testes de
infra.
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
import jsonl_cost  # noqa: E402


def _matching_substring_price(model_id: str):
    """Primeira entrada de ``FLEET_PRICING_BY_SUBSTRING`` cuja substring casa.

    Espelha exatamente o laço de ``jsonl_cost.fleet_pricing_for`` (case-insensitive,
    primeira substring contida vence). ``None`` se nenhuma casa.
    """
    m = (model_id or "").lower()
    for needle, price in jsonl_cost.FLEET_PRICING_BY_SUBSTRING:
        if needle in m:
            return needle, price
    return None


def _declared_models():
    """Pares ``(kind, ModelInfo)`` com ``price_in`` ou ``price_out`` não-nulo."""
    pairs = []
    for kind, adapter in cli_adapters.ADAPTERS.items():
        try:
            models = adapter.list_models()
        except Exception:  # noqa: BLE001 — list_models é best-effort
            continue
        for m in models:
            if m.price_in is None and m.price_out is None:
                continue
            pairs.append((kind, m))
    return pairs


def test_há_modelos_declarados_para_validar():
    # Guarda contra um teste vacuamente verde (frota vazia / list_models quebrado).
    assert _declared_models(), "nenhum ModelInfo com preço declarado encontrado"


@pytest.mark.parametrize(
    "kind,model",
    _declared_models(),
    ids=lambda v: getattr(v, "id", v) if isinstance(v, object) else str(v),
)
def test_preco_adapter_bate_com_tabela_substring(kind, model):
    matched = _matching_substring_price(model.id)
    if matched is None:
        pytest.skip(
            f"{kind}:{model.id} não casa nenhuma substring de FLEET_PRICING_BY_SUBSTRING"
        )
    needle, price = matched
    assert model.price_in == price["in"], (
        f"DRIFT de preço (input): adapter {kind!r} declara price_in={model.price_in} "
        f"para id={model.id!r}, mas FLEET_PRICING_BY_SUBSTRING[{needle!r}]={price['in']}"
    )
    assert model.price_out == price["out"], (
        f"DRIFT de preço (output): adapter {kind!r} declara price_out={model.price_out} "
        f"para id={model.id!r}, mas FLEET_PRICING_BY_SUBSTRING[{needle!r}]={price['out']}"
    )
