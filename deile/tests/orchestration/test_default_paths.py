"""Testes de resolução de caminhos padrão para SQLiteTaskManager, PlanManager e RunManager.

Verifica:
(a) Novo default aponta para .deile/...
(b) Legado preservado quando o diretório/arquivo legado existe e não está vazio.
"""

from __future__ import annotations

from unittest.mock import patch

from deile.orchestration.plan_manager import PlanManager
from deile.orchestration.run_manager import RunManager
from deile.orchestration.sqlite_task_manager import SQLiteTaskManager

# ---------------------------------------------------------------------------
# SQLiteTaskManager
# ---------------------------------------------------------------------------


class TestSQLiteTaskManagerDefaultPath:
    def _make(self, *args, **kwargs) -> SQLiteTaskManager:
        with patch("asyncio.create_task"), patch.object(
            SQLiteTaskManager, "_initialize_database", return_value=None
        ):
            return SQLiteTaskManager(*args, **kwargs)

    def test_novo_default_aponta_para_deile_db(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mgr = self._make()
        assert mgr.db_path == tmp_path / ".deile" / "db" / "tasks.db"
        assert mgr.db_path.parent.is_dir()

    def test_legado_preservado_quando_existe(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        legacy = tmp_path / "deile_tasks.db"
        legacy.touch()
        mgr = self._make()
        assert mgr.db_path == legacy

    def test_explicit_path_respeitado(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        explicit = tmp_path / "custom.db"
        mgr = self._make(db_path=explicit)
        assert mgr.db_path == explicit


# ---------------------------------------------------------------------------
# PlanManager
# ---------------------------------------------------------------------------


class TestPlanManagerDefaultPaths:
    def test_novo_default_aponta_para_deile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mgr = PlanManager()
        assert mgr.plans_dir == tmp_path / ".deile" / "plans"
        assert mgr.runs_dir == tmp_path / ".deile" / "runs"
        assert mgr.plans_dir.is_dir()
        assert mgr.runs_dir.is_dir()

    def test_legado_plans_preservado_quando_nao_vazio(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        legacy_plans = tmp_path / "PLANS"
        legacy_plans.mkdir()
        (legacy_plans / "plan.json").touch()
        mgr = PlanManager()
        assert mgr.plans_dir == legacy_plans

    def test_legado_runs_preservado_quando_nao_vazio(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        legacy_runs = tmp_path / "RUNS"
        legacy_runs.mkdir()
        (legacy_runs / "run.json").touch()
        mgr = PlanManager()
        assert mgr.runs_dir == legacy_runs

    def test_explicit_paths_respeitados(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mgr = PlanManager(plans_dir=tmp_path / "myplans", runs_dir=tmp_path / "myruns")
        assert mgr.plans_dir == tmp_path / "myplans"
        assert mgr.runs_dir == tmp_path / "myruns"


# ---------------------------------------------------------------------------
# RunManager
# ---------------------------------------------------------------------------


class TestRunManagerDefaultPath:
    def test_novo_default_aponta_para_deile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mgr = RunManager()
        assert mgr.runs_dir == tmp_path / ".deile" / "runs"
        assert mgr.runs_dir.is_dir()

    def test_legado_preservado_quando_nao_vazio(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        legacy = tmp_path / "RUNS"
        legacy.mkdir()
        (legacy / "run.json").touch()
        mgr = RunManager()
        assert mgr.runs_dir == legacy

    def test_explicit_runs_dir_respeitado(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        explicit = tmp_path / "custom_runs"
        mgr = RunManager(runs_dir=explicit)
        assert mgr.runs_dir == explicit
