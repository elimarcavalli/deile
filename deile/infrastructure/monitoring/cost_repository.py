"""SQLite persistence for ``CostTracker`` (SRP extraction).

``CostTracker`` was a god object mixing pricing config, in-memory state,
budget enforcement, alerts and direct ``sqlite3.connect(...)`` transactions.
This module owns the SQLite layer exclusively. The public interface uses
plain tuples/floats so the data shapes used by ``CostTracker`` (the
dataclasses ``CostEntry`` / ``BudgetLimit``) can evolve without changing
the repository — and so the repository has no circular dependency on
``cost_tracker``.

All transactions use parameterised queries; the only ``f"…{where_sql}…"``
strings build their clauses from hardcoded literals (timestamp/category
filters) — every value is bound via the ``params`` list.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class CostRepository:
    """SQLite persistence for ``cost_entries``, ``budget_limits`` and ``cost_alerts``."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def init_schema(self) -> None:
        """Create the 3 tables + indices if absent. Idempotent."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cost_entries (
                    id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT NOT NULL,
                    amount REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    description TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    session_id TEXT,
                    user_id TEXT,
                    created_at REAL DEFAULT (datetime('now'))
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS budget_limits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    period TEXT NOT NULL,
                    limit_amount REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    alert_threshold REAL DEFAULT 0.8,
                    hard_limit BOOLEAN DEFAULT FALSE,
                    created_at REAL DEFAULT (datetime('now')),
                    active BOOLEAN DEFAULT TRUE
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS cost_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    period TEXT,
                    current_amount REAL NOT NULL,
                    limit_amount REAL NOT NULL,
                    threshold_percentage REAL NOT NULL,
                    triggered_at REAL DEFAULT (datetime('now')),
                    acknowledged BOOLEAN DEFAULT FALSE
                )
            """)

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cost_timestamp ON cost_entries(timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cost_category ON cost_entries(category)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cost_session ON cost_entries(session_id)"
            )

    def fetch_active_budgets(self) -> List[Tuple]:
        """Return all active budget rows as raw tuples.

        Columns: ``(category, period, limit_amount, currency, alert_threshold,
        hard_limit, created_at)``.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT category, period, limit_amount, currency,
                       alert_threshold, hard_limit, created_at
                FROM budget_limits
                WHERE active = TRUE
            """)
            return list(cursor.fetchall())

    def insert_cost_entry(
        self,
        entry_id: str,
        timestamp: float,
        category: str,
        subcategory: str,
        amount: float,
        currency: str,
        description: str,
        metadata_json: str,
        session_id: Optional[str],
        user_id: Optional[str],
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO cost_entries
                (id, timestamp, category, subcategory, amount, currency,
                 description, metadata, session_id, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id, timestamp, category, subcategory, amount,
                    currency, description, metadata_json, session_id, user_id,
                ),
            )

    def replace_budget_limit(
        self,
        category: str,
        period: str,
        limit_amount: float,
        currency: str,
        alert_threshold: float,
        hard_limit: bool,
    ) -> None:
        """Deactivate any existing limit for ``(category, period)`` then insert."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE budget_limits SET active = FALSE WHERE category = ? AND period = ?",
                (category, period),
            )
            conn.execute(
                """
                INSERT INTO budget_limits
                (category, period, limit_amount, currency, alert_threshold, hard_limit)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (category, period, limit_amount, currency, alert_threshold, hard_limit),
            )

    def period_usage_sum(self, category: str, start_timestamp: float) -> float:
        """Sum of cost ``amount`` for ``category`` from ``start_timestamp`` onward."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM cost_entries
                WHERE category = ? AND timestamp >= ?
                """,
                (category, start_timestamp),
            )
            result = cursor.fetchone()
            return float(result[0] if result and result[0] else 0)

    def insert_alert(
        self,
        alert_type: str,
        category: str,
        period: Optional[str],
        current_amount: float,
        limit_amount: float,
        threshold_percentage: float,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO cost_alerts
                (alert_type, category, period, current_amount, limit_amount, threshold_percentage)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (alert_type, category, period, current_amount, limit_amount, threshold_percentage),
            )

    def summary_aggregates(
        self,
        start_timestamp: float,
        end_timestamp: float,
        category: Optional[str] = None,
    ) -> Tuple[float, int, List[Tuple[str, float]], List[Tuple[str, str, float, str, float]]]:
        """Return aggregates for the period.

        ``(total_amount, entry_count, by_category, top_expenses)`` where:
          - ``by_category`` is ``[(category, summed_amount), …]`` desc by amount.
          - ``top_expenses`` is up to 10 rows
            ``(category, subcategory, amount, description, timestamp)`` desc by amount.
        """
        where_clauses = ["timestamp >= ?", "timestamp <= ?"]
        params: List[Any] = [start_timestamp, end_timestamp]
        if category:
            where_clauses.append("category = ?")
            params.append(category)
        where_sql = " AND ".join(where_clauses)

        with sqlite3.connect(self.db_path) as conn:
            # nosec B608 — where_sql is built from hardcoded clause strings only;
            # all user-controlled values are bound via the `params` list.
            cursor = conn.execute(
                f"""
                SELECT COALESCE(SUM(amount), 0), COUNT(*)
                FROM cost_entries
                WHERE {where_sql}
                """,
                params,
            )  # nosec B608
            total_amount, entry_count = cursor.fetchone()

            cursor = conn.execute(
                f"""
                SELECT category, COALESCE(SUM(amount), 0)
                FROM cost_entries
                WHERE {where_sql}
                GROUP BY category
                ORDER BY SUM(amount) DESC
                """,
                params,
            )  # nosec B608
            by_category = list(cursor.fetchall())

            cursor = conn.execute(
                f"""
                SELECT category, subcategory, amount, description, timestamp
                FROM cost_entries
                WHERE {where_sql}
                ORDER BY amount DESC
                LIMIT 10
                """,
                params,
            )  # nosec B608
            top_expenses = list(cursor.fetchall())

        return total_amount, entry_count, by_category, top_expenses

    def fetch_entries_in_range(
        self, start_timestamp: float, end_timestamp: float
    ) -> List[Tuple]:
        """Return raw rows for the export path.

        Columns: ``(id, timestamp, category, subcategory, amount, currency,
        description, metadata, session_id, user_id)``.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT id, timestamp, category, subcategory, amount, currency,
                       description, metadata, session_id, user_id
                FROM cost_entries
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp DESC
                """,
                (start_timestamp, end_timestamp),
            )
            return list(cursor.fetchall())
