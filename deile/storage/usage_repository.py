"""UsageRepository — SQLite-backed storage for provider usage records + BudgetGuard."""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path.home() / ".deile" / "db" / "usage.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS usage_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    provider_id     TEXT    NOT NULL,
    model_id        TEXT    NOT NULL,
    tier            TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    success         INTEGER NOT NULL DEFAULT 1,
    error_type      TEXT
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_usage_provider_ts
ON usage_records (provider_id, timestamp)
"""


@dataclass
class UsageRecord:
    provider_id: str
    model_id: str
    tier: str
    session_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    success: bool = True
    error_type: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class UsageRepository:
    """Append-only SQLite store for per-request usage records.

    Thread-safe for single-process use (each call gets its own connection).
    """

    def __init__(self, db_path: Path = _DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_INDEX)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record(self, r: UsageRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_records
                  (timestamp, provider_id, model_id, tier, session_id,
                   prompt_tokens, completion_tokens, cached_tokens, total_tokens,
                   cost_usd, latency_ms, success, error_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r.timestamp,
                    r.provider_id,
                    r.model_id,
                    r.tier,
                    r.session_id,
                    r.prompt_tokens,
                    r.completion_tokens,
                    r.cached_tokens,
                    r.total_tokens,
                    r.cost_usd,
                    r.latency_ms,
                    int(r.success),
                    r.error_type,
                ),
            )

    async def record_from_provider(
        self,
        provider_id: str,
        model_id: str,
        tier: Any,
        session_id: str,
        usage: Any,  # ModelUsage
        latency_ms: int,
        success: bool,
        error_envelope: Optional[Any] = None,
    ) -> None:
        """Async-compatible shim — runs synchronously (SQLite write is fast)."""
        error_type: Optional[str] = None
        if error_envelope is not None:
            error_type = getattr(error_envelope, "error_type", None)

        tier_value = tier.value if hasattr(tier, "value") else str(tier)

        prompt_tokens = getattr(usage, "prompt_tokens", 0)
        completion_tokens = getattr(usage, "completion_tokens", 0)
        cost_usd = getattr(usage, "cost_estimate", 0.0)

        # Observabilidade do "custo silencioso" (regressão do provider Gemini que
        # gravava cost=0.0 sem aviso): uma chamada bem-sucedida que faturou tokens
        # mas reporta custo zero é quase sempre pricing ausente/não-computado, não
        # custo real zero. Logamos em WARNING para tornar o bug detectável sem
        # alterar o valor persistido (mantém /cost, [T]okens e agregações iguais).
        # Não dispara em falhas (success=False), nem em chamadas sem tokens
        # faturáveis (prompt+completion==0), nem em auth por assinatura (sem tokens
        # reais de API).
        if success and cost_usd == 0.0 and (prompt_tokens + completion_tokens) > 0:
            logger.warning(
                "usage recorded with cost_usd=0 for a successful call with "
                "billable tokens (provider=%s, model=%s, prompt=%s, completion=%s) "
                "— pricing likely missing or uncomputed",
                provider_id,
                model_id,
                prompt_tokens,
                completion_tokens,
            )

        r = UsageRecord(
            provider_id=provider_id,
            model_id=model_id,
            tier=tier_value,
            session_id=session_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=getattr(usage, "cached_tokens", 0),
            total_tokens=getattr(usage, "total_tokens", 0),
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            success=success,
            error_type=error_type,
        )
        self.record(r)

    def cost_for_provider_since(self, provider_id: str, since_ts: float) -> float:
        """Total cost_usd accumulated by *provider_id* since *since_ts* (epoch seconds)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM usage_records WHERE provider_id=? AND timestamp>=?",
                (provider_id, since_ts),
            ).fetchone()
        return float(row[0])

    def cost_for_session(self, session_id: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM usage_records WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return float(row[0])

    def records_for_session(self, session_id: str) -> List[UsageRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM usage_records WHERE session_id=? ORDER BY timestamp",
                (session_id,),
            ).fetchall()
        return [
            UsageRecord(
                provider_id=r["provider_id"],
                model_id=r["model_id"],
                tier=r["tier"],
                session_id=r["session_id"],
                prompt_tokens=r["prompt_tokens"],
                completion_tokens=r["completion_tokens"],
                cached_tokens=r["cached_tokens"],
                total_tokens=r["total_tokens"],
                cost_usd=r["cost_usd"],
                latency_ms=r["latency_ms"],
                success=bool(r["success"]),
                error_type=r["error_type"],
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    def records_for_stage_model(
        self,
        stage: str,
        model_id: str,
        limit: int = 10,
    ) -> List[UsageRecord]:
        """Return the most recent *limit* records for a stage + model pair.

        Stage is matched by ``session_id`` prefix ``pipeline-<stage>-``
        (the canonical format produced by ``WorkerImplementer`` when building
        the channel_id: ``pipeline-issue-<N>`` or ``pipeline-pr-<N>`` after
        the stage prefix).  A looser match ``pipeline-`` + stage is used so
        both ``pipeline-issue-`` and ``pipeline-pr-`` are captured.

        Returns records ordered by descending timestamp (newest first).
        """
        # Match session_ids that encode the stage name.  WorkerImplementer uses
        # "pipeline-issue-<N>" / "pipeline-pr-<N>" so we search for rows whose
        # session_id contains the stage name AND model_id matches.
        # The LIKE pattern is intentionally broad because the stage is not
        # stored as a separate column — this is a best-effort heuristic.
        stage_pattern = f"%{stage}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM usage_records
                WHERE (session_id LIKE ? OR session_id LIKE ?)
                  AND model_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (
                    f"pipeline-{stage}-%",
                    stage_pattern,
                    model_id,
                    limit,
                ),
            ).fetchall()
        return [
            UsageRecord(
                provider_id=r["provider_id"],
                model_id=r["model_id"],
                tier=r["tier"],
                session_id=r["session_id"],
                prompt_tokens=r["prompt_tokens"],
                completion_tokens=r["completion_tokens"],
                cached_tokens=r["cached_tokens"],
                total_tokens=r["total_tokens"],
                cost_usd=r["cost_usd"],
                latency_ms=r["latency_ms"],
                success=bool(r["success"]),
                error_type=r["error_type"],
                timestamp=r["timestamp"],
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# BudgetGuard
# ---------------------------------------------------------------------------


class BudgetExceeded(Exception):
    """Raised when a budget limit would be breached by the requested call."""

    def __init__(self, msg: str, provider_id: str, limit_type: str) -> None:
        super().__init__(msg)
        self.provider_id = provider_id
        self.limit_type = limit_type


class BudgetGuard:
    """Checks accumulated spend against configured limits before each call.

    Limits come from the ``budget`` section of model_providers.yaml.
    """

    def __init__(
        self,
        repository: UsageRepository,
        per_session_usd: float = 5.0,
        per_provider_daily: Optional[Dict[str, float]] = None,
        per_provider_monthly: Optional[Dict[str, float]] = None,
        enabled: bool = True,
        alert_threshold: float = 0.8,
    ) -> None:
        self._repo = repository
        self._per_session = per_session_usd
        self._daily = per_provider_daily or {}
        self._monthly = per_provider_monthly or {}
        self._enabled = enabled
        self._alert_threshold = alert_threshold

    @classmethod
    def from_yaml(cls, yaml_path: Path, repository: UsageRepository) -> "BudgetGuard":
        import yaml as _yaml

        with open(yaml_path) as f:
            data = _yaml.safe_load(f)
        budget = data.get("budget", {})
        return cls(
            repository=repository,
            per_session_usd=float(budget.get("per_session_usd", 5.0)),
            per_provider_daily=budget.get("per_provider_daily_usd", {}),
            per_provider_monthly=budget.get("per_provider_monthly_usd", {}),
            enabled=bool(budget.get("enabled", True)),
            alert_threshold=float(budget.get("alert_threshold_pct", 80)) / 100.0,
        )

    def check_session(self, session_id: str, estimated_cost: float = 0.0) -> None:
        """Raise BudgetExceeded if adding *estimated_cost* would exceed per-session limit."""
        if not self._enabled:
            return
        current = self._repo.cost_for_session(session_id)
        projected = current + estimated_cost
        if projected > self._per_session:
            raise BudgetExceeded(
                f"Session {session_id} would exceed per-session limit "
                f"${self._per_session:.4f} (current=${current:.4f}, est=${estimated_cost:.4f})",
                provider_id="(session)",
                limit_type="per_session",
            )
        if (
            self._per_session > 0
            and projected / self._per_session >= self._alert_threshold
        ):
            logger.warning(
                "Budget alert: session %s at %.0f%% of $%.2f limit",
                session_id,
                projected / self._per_session * 100,
                self._per_session,
            )

    def check_provider_daily(
        self, provider_id: str, estimated_cost: float = 0.0
    ) -> None:
        """Raise BudgetExceeded if provider's 24h spend would exceed daily limit."""
        if not self._enabled:
            return
        limit = self._daily.get(provider_id)
        if limit is None:
            return
        day_ago = time.time() - 86_400
        current = self._repo.cost_for_provider_since(provider_id, day_ago)
        projected = current + estimated_cost
        if projected > limit:
            raise BudgetExceeded(
                f"Provider {provider_id} would exceed daily limit "
                f"${limit:.2f} (current=${current:.4f}, est=${estimated_cost:.4f})",
                provider_id=provider_id,
                limit_type="daily",
            )
        if limit > 0 and projected / limit >= self._alert_threshold:
            logger.warning(
                "Budget alert: provider %s daily at %.0f%% of $%.2f limit",
                provider_id,
                projected / limit * 100,
                limit,
            )

    def check_provider_monthly(
        self, provider_id: str, estimated_cost: float = 0.0
    ) -> None:
        """Raise BudgetExceeded if provider's 30-day spend would exceed monthly limit."""
        if not self._enabled:
            return
        limit = self._monthly.get(provider_id)
        if limit is None:
            return
        thirty_days_ago = time.time() - 30 * 86_400
        current = self._repo.cost_for_provider_since(provider_id, thirty_days_ago)
        projected = current + estimated_cost
        if projected > limit:
            raise BudgetExceeded(
                f"Provider {provider_id} would exceed monthly limit "
                f"${limit:.2f} (current=${current:.4f}, est=${estimated_cost:.4f})",
                provider_id=provider_id,
                limit_type="monthly",
            )
        if limit > 0 and projected / limit >= self._alert_threshold:
            logger.warning(
                "Budget alert: provider %s monthly at %.0f%% of $%.2f limit",
                provider_id,
                projected / limit * 100,
                limit,
            )

    def check_all(
        self,
        session_id: str,
        provider_id: str,
        estimated_cost: float = 0.0,
    ) -> None:
        """Run all budget checks for one call."""
        self.check_session(session_id, estimated_cost)
        self.check_provider_daily(provider_id, estimated_cost)
        self.check_provider_monthly(provider_id, estimated_cost)

    def snapshot(self) -> Dict[str, Any]:
        """Return a public read-only view of the configured limits.

        Use this from /model budget rather than reaching into private attributes.
        """
        return {
            "enabled": self._enabled,
            "per_session_usd": self._per_session,
            "per_provider_daily_usd": dict(self._daily),
            "per_provider_monthly_usd": dict(self._monthly),
            "alert_threshold_pct": int(self._alert_threshold * 100),
        }


# ---------------------------------------------------------------------------
# Stage-level cost cap (issue #392)
# ---------------------------------------------------------------------------


class StageCostCapExceeded(Exception):
    """Raised when the estimated cost of a stage run exceeds the configured cap.

    Attributes:
        stage: canonical stage name (classify/refine/implement/pr_review/follow_ups).
        estimated_usd: estimated cost in USD as Decimal.
        cap_usd: configured cap in USD as Decimal.
    """

    def __init__(self, stage: str, estimated_usd: Decimal, cap_usd: Decimal) -> None:
        super().__init__(
            f"stage '{stage}' estimated cost ${estimated_usd} exceeds cap ${cap_usd}"
        )
        self.stage = stage
        self.estimated_usd = estimated_usd
        self.cap_usd = cap_usd


class StageBudgetGuard:
    """Per-stage pre-dispatch cost guard (issue #392).

    Checks whether the estimated cost of a single stage run would exceed the
    per-stage cost cap resolved by ``resolve_stage_cost_cap_usd``. When the
    cap is exceeded, raises ``StageCostCapExceeded`` so the caller can
    escalate the issue to ``~workflow:bloqueada`` before posting the dispatch.

    ``None`` cap means "no enforcement" — the check passes silently.
    ``Decimal(0)`` estimate (unknown pricing) also passes silently.
    """

    def __init__(self, estimator: "StageCostEstimator") -> None:  # noqa: F821
        self._estimator = estimator

    def check_stage_run(
        self,
        stage: str,
        model_slug: str,
        payload_size_tokens: int = 0,
    ) -> None:
        """Raise StageCostCapExceeded if estimated cost > resolved cap.

        Args:
            stage: canonical stage name.
            model_slug: provider:model slug for the dispatch.
            payload_size_tokens: token count hint for the payload (0 = use history).

        Raises:
            StageCostCapExceeded: when estimated cost exceeds the cap.
        """
        # Lazy import to avoid import cycle and to allow resolve_stage_cost_cap_usd
        # to be imported after the module is fully initialized.
        from deile.orchestration.pipeline.dispatch_resolver import (  # noqa: PLC0415
            resolve_stage_cost_cap_usd,
        )

        cap = resolve_stage_cost_cap_usd(stage)
        if cap is None:
            return  # No cap configured — pass through.

        estimated = self._estimator.estimate_run_cost(
            stage=stage,
            model_slug=model_slug,
            payload_size_tokens=payload_size_tokens,
        )

        if estimated == Decimal(0):
            # Cannot estimate (pricing unknown) — pass through silently.
            logger.debug(
                "StageBudgetGuard: estimate=0 for stage=%s model=%s "
                "(pricing unknown) — skipping cap check",
                stage,
                model_slug,
            )
            return

        if estimated > cap:
            logger.warning(
                "StageBudgetGuard: stage=%s model=%s estimated=$%s > cap=$%s "
                "— raising StageCostCapExceeded",
                stage,
                model_slug,
                estimated,
                cap,
            )
            raise StageCostCapExceeded(
                stage=stage,
                estimated_usd=estimated,
                cap_usd=cap,
            )

        logger.debug(
            "StageBudgetGuard: stage=%s model=%s estimated=$%s <= cap=$%s — OK",
            stage,
            model_slug,
            estimated,
            cap,
        )


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_usage_repository: Optional[UsageRepository] = None


def get_usage_repository(db_path: Optional[Path] = None) -> UsageRepository:
    """Return the singleton UsageRepository."""
    global _usage_repository
    if _usage_repository is None:
        _usage_repository = UsageRepository(db_path or _DEFAULT_DB_PATH)
    return _usage_repository


def reset_usage_repository() -> None:
    """Reset singleton (test helper)."""
    global _usage_repository
    _usage_repository = None
