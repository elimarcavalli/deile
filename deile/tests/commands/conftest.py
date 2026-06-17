"""Fixtures de isolamento para testes de comandos.

CostTracker usa um singleton global em cost_tracker._cost_tracker_instance
cujo caminho padrão é ~/.deile/costs.db.  Em execução paralela (-n auto)
múltiplos workers competiriam pelo mesmo arquivo SQLite.  Esta fixture
substitui o singleton por uma instância isolada com tmp_path para cada
função de teste.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolated_cost_tracker(tmp_path, monkeypatch):
    from deile.infrastructure.monitoring import cost_tracker as ct_mod

    tracker = ct_mod.CostTracker(db_path=str(tmp_path / "costs.db"))
    monkeypatch.setattr(ct_mod, "_cost_tracker_instance", tracker)
