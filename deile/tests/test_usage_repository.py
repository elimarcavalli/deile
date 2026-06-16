"""Tests: UsageRepository + BudgetGuard — Phase 11."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from deile.storage.usage_repository import (
    BudgetExceeded,
    BudgetGuard,
    UsageRecord,
    UsageRepository,
    get_usage_repository,
    reset_usage_repository,
)

_YAML_PATH = Path(__file__).parents[2] / "deile" / "config" / "model_providers.yaml"


@pytest.fixture()
def repo(tmp_path):
    return UsageRepository(db_path=tmp_path / "test_usage.db")


def _make_record(**overrides) -> UsageRecord:
    defaults = dict(
        provider_id="anthropic",
        model_id="claude-opus-4-8",
        tier="tier_1",
        session_id="sess-abc",
        prompt_tokens=100,
        completion_tokens=50,
        cached_tokens=0,
        total_tokens=150,
        cost_usd=0.05,
        latency_ms=300,
        success=True,
        error_type=None,
    )
    defaults.update(overrides)
    return UsageRecord(**defaults)


# ---------------------------------------------------------------------------
# UsageRepository — basic CRUD
# ---------------------------------------------------------------------------


class TestUsageRepositoryRecord:
    def test_record_stores_and_retrieves(self, repo):
        r = _make_record()
        repo.record(r)
        records = repo.records_for_session("sess-abc")
        assert len(records) == 1
        assert records[0].provider_id == "anthropic"
        assert records[0].cost_usd == 0.05

    def test_records_for_session_empty(self, repo):
        assert repo.records_for_session("nonexistent") == []

    def test_records_for_session_filters_by_session(self, repo):
        repo.record(_make_record(session_id="sess-1", cost_usd=0.10))
        repo.record(_make_record(session_id="sess-2", cost_usd=0.20))
        result = repo.records_for_session("sess-1")
        assert len(result) == 1
        assert result[0].session_id == "sess-1"

    def test_records_for_session_ordered_by_timestamp(self, repo):
        now = time.time()
        r1 = _make_record(session_id="s", timestamp=now + 1)
        r2 = _make_record(session_id="s", timestamp=now)
        repo.record(r1)
        repo.record(r2)
        records = repo.records_for_session("s")
        assert records[0].timestamp <= records[1].timestamp

    def test_record_preserves_all_fields(self, repo):
        r = _make_record(
            error_type="rate_limit",
            success=False,
            cached_tokens=20,
            latency_ms=1500,
        )
        repo.record(r)
        stored = repo.records_for_session("sess-abc")[0]
        assert stored.error_type == "rate_limit"
        assert stored.success is False
        assert stored.cached_tokens == 20
        assert stored.latency_ms == 1500


# ---------------------------------------------------------------------------
# cost_for_session
# ---------------------------------------------------------------------------


class TestCostForSession:
    def test_returns_zero_for_empty(self, repo):
        assert repo.cost_for_session("no-session") == 0.0

    def test_sums_multiple_records(self, repo):
        repo.record(_make_record(cost_usd=0.10))
        repo.record(_make_record(cost_usd=0.20))
        repo.record(_make_record(cost_usd=0.15))
        total = repo.cost_for_session("sess-abc")
        assert abs(total - 0.45) < 1e-6

    def test_ignores_other_sessions(self, repo):
        repo.record(_make_record(session_id="sess-abc", cost_usd=1.0))
        repo.record(_make_record(session_id="sess-xyz", cost_usd=9.0))
        assert abs(repo.cost_for_session("sess-abc") - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# cost_for_provider_since
# ---------------------------------------------------------------------------


class TestCostForProviderSince:
    def test_returns_zero_for_empty(self, repo):
        assert repo.cost_for_provider_since("anthropic", time.time()) == 0.0

    def test_sums_records_since_timestamp(self, repo):
        now = time.time()
        r_old = _make_record(cost_usd=5.0, timestamp=now - 100_000)
        r_new = _make_record(cost_usd=1.0, timestamp=now - 3600)
        repo.record(r_old)
        repo.record(r_new)
        # Only the recent record should be counted (since 24h ago = 86400s)
        result = repo.cost_for_provider_since("anthropic", now - 86_400)
        assert abs(result - 1.0) < 1e-6

    def test_filters_by_provider(self, repo):
        now = time.time()
        repo.record(
            _make_record(provider_id="anthropic", cost_usd=2.0, timestamp=now - 60)
        )
        repo.record(
            _make_record(provider_id="openai", cost_usd=3.0, timestamp=now - 60)
        )
        result = repo.cost_for_provider_since("anthropic", now - 3600)
        assert abs(result - 2.0) < 1e-6


# ---------------------------------------------------------------------------
# record_from_provider (async shim)
# ---------------------------------------------------------------------------


class TestRecordFromProvider:
    @pytest.mark.asyncio
    async def test_record_from_provider_stores_record(self, repo):
        class FakeUsage:
            prompt_tokens = 200
            completion_tokens = 80
            cached_tokens = 10
            total_tokens = 280
            cost_estimate = 0.08

        class FakeTier:
            value = "tier_2"

        await repo.record_from_provider(
            provider_id="openai",
            model_id="gpt-4o",
            tier=FakeTier(),
            session_id="sess-rp",
            usage=FakeUsage(),
            latency_ms=500,
            success=True,
        )
        records = repo.records_for_session("sess-rp")
        assert len(records) == 1
        r = records[0]
        assert r.provider_id == "openai"
        assert r.prompt_tokens == 200
        assert r.cost_usd == 0.08
        assert r.tier == "tier_2"

    @pytest.mark.asyncio
    async def test_record_from_provider_warns_on_silent_zero_cost(self, repo, caplog):
        """Custo silencioso: chamada bem-sucedida com tokens faturados mas cost=0.

        Regressão da família de bugs do provider Gemini que gravava cost=0.0
        sem aviso. O valor persistido NÃO muda (continua 0.0); apenas garantimos
        que o caso vire detectável via WARNING.
        """

        class FakeUsage:
            prompt_tokens = 500
            completion_tokens = 200
            cached_tokens = 0
            total_tokens = 700
            cost_estimate = 0.0  # pricing ausente/não-computado

        class FakeTier:
            value = "tier_1"

        with caplog.at_level("WARNING", logger="deile.storage.usage_repository"):
            await repo.record_from_provider(
                provider_id="gemini",
                model_id="gemini-2.5-pro",
                tier=FakeTier(),
                session_id="sess-zero",
                usage=FakeUsage(),
                latency_ms=400,
                success=True,
            )

        # Valor persistido inalterado.
        stored = repo.records_for_session("sess-zero")[0]
        assert stored.cost_usd == 0.0
        assert stored.prompt_tokens == 500
        # Mas o custo silencioso virou observável.
        assert any(
            "cost_usd=0" in rec.message and rec.levelname == "WARNING"
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_record_from_provider_no_warn_on_legitimate_zero_cost(
        self, repo, caplog
    ):
        """Casos legítimos de cost=0 NÃO disparam o warning.

        - Falha (success=False): pode ter tokens parciais sem custo cobrado.
        - Sem tokens faturáveis (prompt+completion==0): nada para faturar
          (ex.: auth por assinatura/OAuth que não conta tokens de API).
        """

        class FailedUsage:
            prompt_tokens = 300
            completion_tokens = 0
            cached_tokens = 0
            total_tokens = 300
            cost_estimate = 0.0

        class NoTokenUsage:
            prompt_tokens = 0
            completion_tokens = 0
            cached_tokens = 0
            total_tokens = 0
            cost_estimate = 0.0

        class FakeTier:
            value = "tier_1"

        class FakeError:
            error_type = "rate_limit"

        with caplog.at_level("WARNING", logger="deile.storage.usage_repository"):
            await repo.record_from_provider(
                provider_id="anthropic",
                model_id="claude-opus-4-8",
                tier=FakeTier(),
                session_id="sess-fail",
                usage=FailedUsage(),
                latency_ms=100,
                success=False,
                error_envelope=FakeError(),
            )
            await repo.record_from_provider(
                provider_id="anthropic",
                model_id="claude-opus-4-8",
                tier=FakeTier(),
                session_id="sess-notoken",
                usage=NoTokenUsage(),
                latency_ms=50,
                success=True,
            )

        assert not any(
            "cost_usd=0" in rec.message and rec.levelname == "WARNING"
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_record_from_provider_with_error_envelope(self, repo):
        class FakeUsage:
            prompt_tokens = 0
            completion_tokens = 0
            cached_tokens = 0
            total_tokens = 0
            cost_estimate = 0.0

        class FakeError:
            error_type = "auth_error"

        class FakeTier:
            value = "tier_1"

        await repo.record_from_provider(
            provider_id="anthropic",
            model_id="claude-opus-4-8",
            tier=FakeTier(),
            session_id="sess-err",
            usage=FakeUsage(),
            latency_ms=100,
            success=False,
            error_envelope=FakeError(),
        )
        records = repo.records_for_session("sess-err")
        assert records[0].error_type == "auth_error"
        assert records[0].success is False


# ---------------------------------------------------------------------------
# BudgetGuard — check_session
# ---------------------------------------------------------------------------


class TestBudgetGuardSession:
    def test_passes_within_limit(self, repo):
        guard = BudgetGuard(repository=repo, per_session_usd=5.0)
        repo.record(_make_record(cost_usd=1.0))
        guard.check_session("sess-abc", estimated_cost=1.0)  # 2.0 < 5.0 — no raise

    def test_raises_when_exceeded(self, repo):
        guard = BudgetGuard(repository=repo, per_session_usd=2.0)
        repo.record(_make_record(cost_usd=1.5))
        with pytest.raises(BudgetExceeded) as exc_info:
            guard.check_session("sess-abc", estimated_cost=1.0)  # 2.5 > 2.0
        assert exc_info.value.limit_type == "per_session"
        assert exc_info.value.provider_id == "(session)"

    def test_disabled_guard_never_raises(self, repo):
        guard = BudgetGuard(repository=repo, per_session_usd=0.0, enabled=False)
        repo.record(_make_record(cost_usd=999.0))
        guard.check_session("sess-abc", estimated_cost=999.0)  # should not raise


# ---------------------------------------------------------------------------
# BudgetGuard — check_provider_daily
# ---------------------------------------------------------------------------


class TestBudgetGuardProviderDaily:
    def test_passes_when_no_limit_configured(self, repo):
        guard = BudgetGuard(repository=repo, per_provider_daily=None)
        guard.check_provider_daily("anthropic", estimated_cost=100.0)  # no limit set

    def test_passes_within_daily_limit(self, repo):
        guard = BudgetGuard(repository=repo, per_provider_daily={"anthropic": 10.0})
        now = time.time()
        repo.record(_make_record(cost_usd=5.0, timestamp=now - 3600))
        guard.check_provider_daily("anthropic", estimated_cost=2.0)  # 7.0 < 10.0

    def test_raises_when_daily_exceeded(self, repo):
        guard = BudgetGuard(repository=repo, per_provider_daily={"anthropic": 5.0})
        now = time.time()
        repo.record(_make_record(cost_usd=4.0, timestamp=now - 3600))
        with pytest.raises(BudgetExceeded) as exc_info:
            guard.check_provider_daily("anthropic", estimated_cost=2.0)  # 6.0 > 5.0
        assert exc_info.value.limit_type == "daily"
        assert exc_info.value.provider_id == "anthropic"

    def test_old_records_excluded_from_daily(self, repo):
        guard = BudgetGuard(repository=repo, per_provider_daily={"anthropic": 5.0})
        now = time.time()
        # Record older than 24h should not count
        repo.record(_make_record(cost_usd=10.0, timestamp=now - 90_000))
        guard.check_provider_daily("anthropic", estimated_cost=4.0)  # should pass


# ---------------------------------------------------------------------------
# BudgetGuard — check_all
# ---------------------------------------------------------------------------


class TestBudgetGuardCheckAll:
    def test_check_all_passes_when_both_ok(self, repo):
        guard = BudgetGuard(
            repository=repo,
            per_session_usd=10.0,
            per_provider_daily={"anthropic": 20.0},
        )
        guard.check_all("sess-abc", "anthropic", estimated_cost=1.0)

    def test_check_all_raises_on_session_exceed(self, repo):
        guard = BudgetGuard(repository=repo, per_session_usd=1.0)
        repo.record(_make_record(cost_usd=0.9))
        with pytest.raises(BudgetExceeded):
            guard.check_all("sess-abc", "anthropic", estimated_cost=0.5)

    def test_check_all_raises_on_daily_exceed(self, repo):
        guard = BudgetGuard(
            repository=repo,
            per_session_usd=100.0,
            per_provider_daily={"anthropic": 1.0},
        )
        now = time.time()
        repo.record(_make_record(cost_usd=0.8, timestamp=now - 3600))
        with pytest.raises(BudgetExceeded):
            guard.check_all("sess-abc", "anthropic", estimated_cost=0.5)


# ---------------------------------------------------------------------------
# BudgetGuard.from_yaml
# ---------------------------------------------------------------------------


class TestBudgetGuardFromYaml:
    def test_loads_from_yaml(self, repo):
        guard = BudgetGuard.from_yaml(_YAML_PATH, repo)
        assert isinstance(guard, BudgetGuard)
        # enabled field exists
        assert guard._enabled in (True, False)

    def test_from_yaml_has_session_limit(self, repo):
        guard = BudgetGuard.from_yaml(_YAML_PATH, repo)
        assert guard._per_session > 0


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------


class TestGetUsageRepository:
    def setup_method(self):
        reset_usage_repository()

    def teardown_method(self):
        reset_usage_repository()

    def test_returns_singleton(self, tmp_path):
        with patch(
            "deile.storage.usage_repository._DEFAULT_DB_PATH", tmp_path / "u.db"
        ):
            r1 = get_usage_repository()
            r2 = get_usage_repository()
        assert r1 is r2

    def test_reset_clears_singleton(self, tmp_path):
        with patch(
            "deile.storage.usage_repository._DEFAULT_DB_PATH", tmp_path / "u.db"
        ):
            r1 = get_usage_repository()
            reset_usage_repository()
            r2 = get_usage_repository()
        assert r1 is not r2
