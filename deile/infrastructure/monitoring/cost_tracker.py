"""
Cost Tracking System for DEILE
===================================

Comprehensive cost tracking and monitoring system for API calls, resource usage,
and operational expenses with detailed analytics and budget management.

Author: DEILE
"""

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from deile.infrastructure.monitoring.cost_repository import CostRepository

logger = logging.getLogger(__name__)


class CostCategory(Enum):
    """Cost categories for tracking"""

    API_CALLS = "api_calls"
    COMPUTE = "compute"
    STORAGE = "storage"
    NETWORK = "network"
    MODEL_USAGE = "model_usage"
    SANDBOX = "sandbox"
    INFRASTRUCTURE = "infrastructure"
    EXTERNAL_SERVICES = "external_services"


class BudgetPeriod(Enum):
    """Budget period types"""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


@dataclass
class CostEntry:
    """Individual cost entry"""

    id: str
    timestamp: float
    category: str
    subcategory: str
    amount: Decimal
    currency: str
    description: str
    metadata: Dict[str, Any]
    session_id: Optional[str] = None
    user_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        data = asdict(self)
        data["amount"] = float(self.amount)  # JSON serializable
        return data


@dataclass
class BudgetLimit:
    """Budget limit configuration"""

    category: str
    period: str
    limit_amount: Decimal
    currency: str
    alert_threshold: float = 0.8  # Alert at 80%
    hard_limit: bool = False  # Stop operations when exceeded
    created_at: float = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()


@dataclass
class CostSummary:
    """Cost summary for a period"""

    period_start: float
    period_end: float
    total_amount: Decimal
    currency: str
    categories: Dict[str, Decimal]
    entry_count: int
    top_expenses: List[Dict[str, Any]]


class CostTracker:
    """
    Comprehensive cost tracking system

    Features:
    - Real-time cost tracking
    - Category-based organization
    - Budget limits and alerts
    - Historical analysis
    - Cost forecasting
    - API call pricing
    - Resource usage costs
    - Export capabilities
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(Path.home() / ".deile" / "costs.db")

        # In-memory tracking
        self.current_session_costs = {}
        self.budget_limits = {}
        self.cost_alerts = []
        self.alert_callbacks = []

        # Thread safety
        self.lock = threading.RLock()

        # Pricing configurations
        self.pricing_config = self._load_pricing_config()

        # SQLite persistence (SRP — see cost_repository.py)
        self.repo = CostRepository(self.db_path)
        self.repo.init_schema()

        # Load budget limits
        self._load_budget_limits()

    def _load_pricing_config(self) -> Dict[str, Any]:
        """Load pricing configuration"""
        # Default pricing configuration
        return {
            "gemini": {
                "pro": {
                    "input_tokens": 0.000125,  # per 1K tokens
                    "output_tokens": 0.000375,  # per 1K tokens
                    "currency": "USD",
                },
                "flash": {
                    "input_tokens": 0.000075,  # per 1K tokens
                    "output_tokens": 0.0003,  # per 1K tokens
                    "currency": "USD",
                },
            },
            "openai": {
                "gpt-4": {
                    "input_tokens": 0.03,  # per 1K tokens
                    "output_tokens": 0.06,  # per 1K tokens
                    "currency": "USD",
                },
                "gpt-3.5-turbo": {
                    "input_tokens": 0.0015,  # per 1K tokens
                    "output_tokens": 0.002,  # per 1K tokens
                    "currency": "USD",
                },
            },
            "anthropic": {
                "claude-3-opus": {
                    "input_tokens": 0.015,  # per 1K tokens
                    "output_tokens": 0.075,  # per 1K tokens
                    "currency": "USD",
                },
                "claude-3-sonnet": {
                    "input_tokens": 0.003,  # per 1K tokens
                    "output_tokens": 0.015,  # per 1K tokens
                    "currency": "USD",
                },
            },
            "compute": {
                "cpu_hour": 0.05,  # per hour
                "memory_gb_hour": 0.01,  # per GB per hour
                "storage_gb_month": 0.10,  # per GB per month
                "network_gb": 0.09,  # per GB transfer
                "currency": "USD",
            },
            "sandbox": {
                "container_hour": 0.02,  # per container per hour
                "docker_build": 0.01,  # per build
                "volume_gb_month": 0.05,  # per GB per month
                "currency": "USD",
            },
        }

    def _load_budget_limits(self):
        """Load budget limits from database"""
        try:
            loaded: Dict[str, BudgetLimit] = {}
            for row in self.repo.fetch_active_budgets():
                budget = BudgetLimit(
                    category=row[0],
                    period=row[1],
                    limit_amount=Decimal(str(row[2])),
                    currency=row[3],
                    alert_threshold=row[4],
                    hard_limit=bool(row[5]),
                    created_at=row[6],
                )
                key = f"{budget.category}_{budget.period}"
                loaded[key] = budget

            # Atomic full replace under the lock: a reload is authoritative
            # (drops budgets deactivated in the DB) and concurrent readers
            # never observe a half-populated map. A DB failure preserves the
            # previously loaded limits (swap only happens on success).
            with self.lock:
                self.budget_limits = loaded

        except Exception as e:
            logger.error(f"Failed to load budget limits: {e}")

    def list_budget_limits(self) -> Dict[str, BudgetLimit]:
        """Reload budget limits from the DB and return the current map.

        Public read accessor so callers (e.g. ``/cost budget list``) get a
        fresh snapshot without reaching into the private
        ``_load_budget_limits`` or the ``budget_limits`` attribute directly.
        """
        self._load_budget_limits()
        return self.budget_limits

    def track_cost(
        self,
        category: Union[str, CostCategory],
        subcategory: str,
        amount: Union[float, Decimal],
        description: str,
        currency: str = "USD",
        metadata: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """Track a cost entry"""

        with self.lock:
            # Generate unique ID
            entry_id = f"cost_{int(time.time() * 1000000)}"

            # Create cost entry
            cost_entry = CostEntry(
                id=entry_id,
                timestamp=time.time(),
                category=(
                    category.value if isinstance(category, CostCategory) else category
                ),
                subcategory=subcategory,
                amount=Decimal(str(amount)),
                currency=currency,
                description=description,
                metadata=metadata or {},
                session_id=session_id or self._get_current_session_id(),
                user_id=self._get_current_user_id(),
            )

            # Store in database
            try:
                self.repo.insert_cost_entry(
                    entry_id=cost_entry.id,
                    timestamp=cost_entry.timestamp,
                    category=cost_entry.category,
                    subcategory=cost_entry.subcategory,
                    amount=float(cost_entry.amount),
                    currency=cost_entry.currency,
                    description=cost_entry.description,
                    metadata_json=json.dumps(cost_entry.metadata),
                    session_id=cost_entry.session_id,
                    user_id=cost_entry.user_id,
                )

                # Update session tracking
                session_key = cost_entry.session_id or "default"
                if session_key not in self.current_session_costs:
                    self.current_session_costs[session_key] = Decimal("0")
                self.current_session_costs[session_key] += cost_entry.amount

                # Check budget limits
                self._check_budget_limits(cost_entry)

                logger.info(
                    f"Tracked cost: {cost_entry.category}/{cost_entry.subcategory} - ${cost_entry.amount}"
                )

                return entry_id

            except Exception as e:
                logger.error(f"Failed to track cost: {e}")
                raise

    def track_api_call(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        description: str = "API call",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Track API call costs automatically"""

        # Get pricing for provider/model
        pricing = self.pricing_config.get(provider.lower(), {}).get(model.lower(), {})

        if not pricing:
            logger.warning(f"No pricing found for {provider}/{model}")
            # Use default pricing
            input_cost = input_tokens * 0.001 / 1000  # $0.001 per 1K tokens
            output_cost = output_tokens * 0.002 / 1000  # $0.002 per 1K tokens
        else:
            input_cost = input_tokens * pricing.get("input_tokens", 0) / 1000
            output_cost = output_tokens * pricing.get("output_tokens", 0) / 1000

        total_cost = input_cost + output_cost

        # Enhanced metadata
        api_metadata = {
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_cost": input_cost,
            "output_cost": output_cost,
            "cost_per_token": (
                total_cost / (input_tokens + output_tokens)
                if (input_tokens + output_tokens) > 0
                else 0
            ),
            **(metadata or {}),
        }

        return self.track_cost(
            category=CostCategory.API_CALLS,
            subcategory=f"{provider}/{model}",
            amount=total_cost,
            description=description,
            metadata=api_metadata,
        )

    def track_compute_usage(
        self,
        cpu_hours: float = 0,
        memory_gb_hours: float = 0,
        storage_gb_months: float = 0,
        network_gb: float = 0,
        description: str = "Compute usage",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Track compute resource costs"""

        compute_pricing = self.pricing_config.get("compute", {})

        cpu_cost = cpu_hours * compute_pricing.get("cpu_hour", 0.05)
        memory_cost = memory_gb_hours * compute_pricing.get("memory_gb_hour", 0.01)
        storage_cost = storage_gb_months * compute_pricing.get("storage_gb_month", 0.10)
        network_cost = network_gb * compute_pricing.get("network_gb", 0.09)

        total_cost = cpu_cost + memory_cost + storage_cost + network_cost

        compute_metadata = {
            "cpu_hours": cpu_hours,
            "memory_gb_hours": memory_gb_hours,
            "storage_gb_months": storage_gb_months,
            "network_gb": network_gb,
            "cpu_cost": cpu_cost,
            "memory_cost": memory_cost,
            "storage_cost": storage_cost,
            "network_cost": network_cost,
            **(metadata or {}),
        }

        return self.track_cost(
            category=CostCategory.COMPUTE,
            subcategory="resource_usage",
            amount=total_cost,
            description=description,
            metadata=compute_metadata,
        )

    def track_sandbox_usage(
        self,
        container_hours: float = 0,
        build_count: int = 0,
        volume_gb_months: float = 0,
        description: str = "Sandbox usage",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Track sandbox costs"""

        sandbox_pricing = self.pricing_config.get("sandbox", {})

        container_cost = container_hours * sandbox_pricing.get("container_hour", 0.02)
        build_cost = build_count * sandbox_pricing.get("docker_build", 0.01)
        volume_cost = volume_gb_months * sandbox_pricing.get("volume_gb_month", 0.05)

        total_cost = container_cost + build_cost + volume_cost

        sandbox_metadata = {
            "container_hours": container_hours,
            "build_count": build_count,
            "volume_gb_months": volume_gb_months,
            "container_cost": container_cost,
            "build_cost": build_cost,
            "volume_cost": volume_cost,
            **(metadata or {}),
        }

        return self.track_cost(
            category=CostCategory.SANDBOX,
            subcategory="docker_usage",
            amount=total_cost,
            description=description,
            metadata=sandbox_metadata,
        )

    def set_budget_limit(
        self,
        category: Union[str, CostCategory],
        period: Union[str, BudgetPeriod],
        limit_amount: Union[float, Decimal],
        currency: str = "USD",
        alert_threshold: float = 0.8,
        hard_limit: bool = False,
    ) -> bool:
        """Set a budget limit"""

        try:
            category_str = (
                category.value if isinstance(category, CostCategory) else category
            )
            period_str = period.value if isinstance(period, BudgetPeriod) else period

            budget = BudgetLimit(
                category=category_str,
                period=period_str,
                limit_amount=Decimal(str(limit_amount)),
                currency=currency,
                alert_threshold=alert_threshold,
                hard_limit=hard_limit,
            )

            # Store in database
            self.repo.replace_budget_limit(
                category=budget.category,
                period=budget.period,
                limit_amount=float(budget.limit_amount),
                currency=budget.currency,
                alert_threshold=budget.alert_threshold,
                hard_limit=budget.hard_limit,
            )

            # Update in memory
            key = f"{category_str}_{period_str}"
            self.budget_limits[key] = budget

            logger.info(
                f"Set budget limit: {category_str}/{period_str} - ${limit_amount}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to set budget limit: {e}")
            return False

    def _check_budget_limits(self, cost_entry: CostEntry):
        """Check if cost entry triggers budget limits"""

        category = cost_entry.category

        # Check all relevant budget limits
        for key, budget in self.budget_limits.items():
            if not key.startswith(f"{category}_"):
                continue

            # Calculate period usage
            period_usage = self._get_period_usage(budget.category, budget.period)
            new_usage = period_usage + cost_entry.amount

            # Check thresholds
            usage_percentage = float(new_usage / budget.limit_amount)

            if usage_percentage >= budget.alert_threshold:
                self._trigger_budget_alert(budget, new_usage, usage_percentage)

            if budget.hard_limit and new_usage > budget.limit_amount:
                raise BudgetExceededException(
                    f"Budget exceeded: {budget.category}/{budget.period} - "
                    f"${new_usage} > ${budget.limit_amount}"
                )

    def _get_period_usage(self, category: str, period: str) -> Decimal:
        """Get usage for a specific period"""

        now = datetime.now()

        # Calculate period start
        if period == "daily":
            period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "weekly":
            days_since_monday = now.weekday()
            period_start = (now - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        elif period == "monthly":
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "yearly":
            period_start = now.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        else:
            # Default to daily
            period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        start_timestamp = period_start.timestamp()

        try:
            return Decimal(str(self.repo.period_usage_sum(category, start_timestamp)))
        except Exception as e:
            logger.error(f"Failed to get period usage: {e}")
            return Decimal("0")

    def _trigger_budget_alert(
        self, budget: BudgetLimit, current_usage: Decimal, percentage: float
    ):
        """Trigger budget alert"""

        alert_data = {
            "alert_type": "budget_threshold",
            "category": budget.category,
            "period": budget.period,
            "current_amount": float(current_usage),
            "limit_amount": float(budget.limit_amount),
            "threshold_percentage": percentage * 100,
            "triggered_at": time.time(),
        }

        # Store alert
        try:
            self.repo.insert_alert(
                alert_type=alert_data["alert_type"],
                category=alert_data["category"],
                period=alert_data["period"],
                current_amount=alert_data["current_amount"],
                limit_amount=alert_data["limit_amount"],
                threshold_percentage=alert_data["threshold_percentage"],
            )
        except Exception as e:
            logger.error(f"Failed to store alert: {e}")

        # Add to in-memory alerts
        self.cost_alerts.append(alert_data)

        # Trigger callbacks
        for callback in self.alert_callbacks:
            try:
                callback(alert_data)
            except Exception as e:
                logger.error(f"Alert callback failed: {e}")

        logger.warning(
            f"Budget alert: {budget.category}/{budget.period} - "
            f"${current_usage:.4f} ({percentage:.1%}) of ${budget.limit_amount}"
        )

    def get_cost_summary(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        category: Optional[str] = None,
    ) -> CostSummary:
        """Get cost summary for a period"""

        # Default to last 30 days
        if not end_time:
            end_time = datetime.now()
        if not start_time:
            start_time = end_time - timedelta(days=30)

        try:
            total_amount, entry_count, by_category, top_rows = (
                self.repo.summary_aggregates(
                    start_time.timestamp(), end_time.timestamp(), category
                )
            )

            categories = {row[0]: Decimal(str(row[1])) for row in by_category}
            top_expenses = [
                {
                    "category": row[0],
                    "subcategory": row[1],
                    "amount": float(row[2]),
                    "description": row[3],
                    "timestamp": row[4],
                }
                for row in top_rows
            ]

            return CostSummary(
                period_start=start_time.timestamp(),
                period_end=end_time.timestamp(),
                total_amount=Decimal(str(total_amount)),
                currency="USD",
                categories=categories,
                entry_count=entry_count,
                top_expenses=top_expenses,
            )

        except Exception as e:
            logger.error(f"Failed to get cost summary: {e}")
            # Return empty summary
            return CostSummary(
                period_start=start_time.timestamp(),
                period_end=end_time.timestamp(),
                total_amount=Decimal("0"),
                currency="USD",
                categories={},
                entry_count=0,
                top_expenses=[],
            )

    def get_current_session_cost(self, session_id: Optional[str] = None) -> Decimal:
        """Get current session cost"""
        session_key = session_id or self._get_current_session_id() or "default"
        return self.current_session_costs.get(session_key, Decimal("0"))

    def register_alert_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Register callback for budget alerts"""
        self.alert_callbacks.append(callback)

    def export_costs(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        format_type: str = "json",
    ) -> str:
        """Export cost data"""

        if not end_time:
            end_time = datetime.now()
        if not start_time:
            start_time = end_time - timedelta(days=30)

        try:
            rows = self.repo.fetch_entries_in_range(
                start_time.timestamp(), end_time.timestamp()
            )

            entries = [
                {
                    "id": row[0],
                    "timestamp": row[1],
                    "datetime": datetime.fromtimestamp(row[1]).isoformat(),
                    "category": row[2],
                    "subcategory": row[3],
                    "amount": row[4],
                    "currency": row[5],
                    "description": row[6],
                    "metadata": json.loads(row[7]) if row[7] else {},
                    "session_id": row[8],
                    "user_id": row[9],
                }
                for row in rows
            ]

            if format_type == "json":
                return json.dumps(
                    {
                        "export_timestamp": datetime.now().isoformat(),
                        "period_start": start_time.isoformat(),
                        "period_end": end_time.isoformat(),
                        "total_entries": len(entries),
                        "entries": entries,
                    },
                    indent=2,
                )
            elif format_type == "csv":
                # Simple CSV export
                import csv
                import io

                output = io.StringIO()
                writer = csv.DictWriter(
                    output,
                    fieldnames=[
                        "id",
                        "datetime",
                        "category",
                        "subcategory",
                        "amount",
                        "currency",
                        "description",
                        "session_id",
                    ],
                )

                writer.writeheader()
                for entry in entries:
                    writer.writerow(
                        {k: v for k, v in entry.items() if k in writer.fieldnames}
                    )

                return output.getvalue()

        except Exception as e:
            logger.error(f"Failed to export costs: {e}")
            return ""

    def get_pricing_estimate(
        self, provider: str, model: str, estimated_tokens: int
    ) -> Dict[str, Any]:
        """Get pricing estimate for API call"""

        pricing = self.pricing_config.get(provider.lower(), {}).get(model.lower(), {})

        if not pricing:
            return {
                "error": f"No pricing found for {provider}/{model}",
                "estimated_cost": 0,
            }

        # Assume 70% input, 30% output split
        input_tokens = int(estimated_tokens * 0.7)
        output_tokens = int(estimated_tokens * 0.3)

        input_cost = input_tokens * pricing.get("input_tokens", 0) / 1000
        output_cost = output_tokens * pricing.get("output_tokens", 0) / 1000
        total_cost = input_cost + output_cost

        return {
            "provider": provider,
            "model": model,
            "estimated_total_tokens": estimated_tokens,
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens,
            "estimated_input_cost": input_cost,
            "estimated_output_cost": output_cost,
            "estimated_total_cost": total_cost,
            "currency": pricing.get("currency", "USD"),
            "cost_per_token": (
                total_cost / estimated_tokens if estimated_tokens > 0 else 0
            ),
        }

    def _get_current_session_id(self) -> Optional[str]:
        # No session context available at this layer (would violate Clean
        # Architecture: infrastructure must not depend on core). Callers can
        # pass session_id explicitly to track_cost().
        return None

    def _get_current_user_id(self) -> Optional[str]:
        # See _get_current_session_id: user context is not resolved here.
        return None


class BudgetExceededException(Exception):
    """Exception raised when budget limits are exceeded"""

    pass


# Global cost tracker instance
_cost_tracker_instance = None


def get_cost_tracker() -> CostTracker:
    """Get global cost tracker instance"""
    global _cost_tracker_instance
    if _cost_tracker_instance is None:
        _cost_tracker_instance = CostTracker()
    return _cost_tracker_instance


def track_cost(
    category: Union[str, CostCategory],
    subcategory: str,
    amount: Union[float, Decimal],
    description: str,
    **kwargs,
) -> str:
    """Convenience function to track cost"""
    return get_cost_tracker().track_cost(
        category, subcategory, amount, description, **kwargs
    )


def track_api_call(
    provider: str, model: str, input_tokens: int, output_tokens: int, **kwargs
) -> str:
    """Convenience function to track API call"""
    return get_cost_tracker().track_api_call(
        provider, model, input_tokens, output_tokens, **kwargs
    )


def get_session_cost(session_id: Optional[str] = None) -> Decimal:
    """Convenience function to get session cost"""
    return get_cost_tracker().get_current_session_cost(session_id)
