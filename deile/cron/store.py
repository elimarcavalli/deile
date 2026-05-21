"""SQLite-backed persistence for user-scheduled prompts (intent #86).

Design choice: SQLite over YAML because (a) atomic writes are critical when
multiple processes/threads might add entries, (b) querying "what fires
between T1 and T2?" is trivial, (c) the data/ directory already houses
``usage.db`` so we keep the operational footprint consistent.

A single table backs both recurring and one-shot entries — they share most
columns; ``cron`` is NULL for one-shots and ``run_at`` is NULL for cron
entries. The ``next_fire_at`` column is denormalized so the runner can
``ORDER BY next_fire_at LIMIT 1`` in O(log N).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

from deile.core.exceptions import DEILEError
from deile.cron.constants import CRON_RESULT_MAX_CHARS
from deile.orchestration.pipeline._time_utils import (format_iso_utc, now_utc,
                                                      parse_iso_utc)
from deile.orchestration.pipeline.cron import CronExpressionError, next_after

logger = logging.getLogger(__name__)


def resolve_db_path() -> Path:
    """Return the CronStore DB path from settings or a cwd-relative default."""
    from deile.config.settings import get_settings

    s = get_settings()
    if s.cron_db_path:
        return s.cron_db_path.resolve()
    if s.pipeline_base_path:
        return s.pipeline_base_path.resolve() / "data" / "cron.db"
    return Path.cwd() / "data" / "cron.db"


class CronStoreError(DEILEError):
    """Raised on scheduling / persistence problems."""


@dataclass
class CronEntry:
    """A scheduled prompt — recurring (cron) OR one-shot (run_at)."""

    id: str
    prompt: str
    cron: Optional[str] = None
    run_at: Optional[datetime] = None  # for one-shot
    next_fire_at: Optional[datetime] = None
    last_fired_at: Optional[datetime] = None
    created_by: Optional[str] = None  # provider:user_id (e.g. "discord:1234")
    notify_user_id: Optional[str] = None  # Discord snowflake to DM result to
    enabled: bool = True
    created_at: datetime = field(default_factory=now_utc)
    last_result: Optional[str] = None  # short summary of last fire outcome

    def __post_init__(self) -> None:
        if not self.id:
            raise CronStoreError("id required")
        if not self.prompt or not self.prompt.strip():
            raise CronStoreError("prompt required and must be non-empty")
        if self.cron and self.run_at:
            raise CronStoreError("provide either cron OR run_at, not both")
        if not self.cron and not self.run_at:
            raise CronStoreError("must provide cron OR run_at")
        # Validate cron eagerly.
        if self.cron:
            try:
                anchor = self.last_fired_at or self.created_at
                self.next_fire_at = next_after(self.cron, anchor)
            except CronExpressionError as exc:
                raise CronStoreError(f"invalid cron {self.cron!r}: {exc}") from exc
        else:
            # one-shot: tz-normalize run_at, set next_fire_at = run_at
            if self.run_at.tzinfo is None:  # type: ignore[union-attr]
                self.run_at = self.run_at.replace(tzinfo=timezone.utc)  # type: ignore[union-attr]
            self.next_fire_at = self.run_at

    @property
    def is_oneshot(self) -> bool:
        return self.cron is None

    def advance(self, *, after: Optional[datetime] = None) -> None:
        """Move ``next_fire_at`` forward after a fire (recurring) or mark done."""
        if self.is_oneshot:
            self.enabled = False
            self.next_fire_at = None
        else:
            anchor = after or now_utc()
            try:
                self.next_fire_at = next_after(self.cron, anchor)  # type: ignore[arg-type]
            except CronExpressionError:
                self.enabled = False
                self.next_fire_at = None

    def to_dict(self) -> dict:
        """Return a JSON-serializable view (datetimes rendered as ISO strings).

        Datetimes use ``format_iso_utc`` so the wire format is byte-identical
        to the strings persisted in SQLite (``YYYY-MM-DDTHH:MM:SSZ``);
        previously ``dt.isoformat()`` rendered ``+00:00``, diverging from the
        write path for the same instant.
        """
        return {
            "id": self.id,
            "prompt": self.prompt,
            "cron": self.cron,
            "run_at": format_iso_utc(self.run_at),
            "next_fire_at": format_iso_utc(self.next_fire_at),
            "last_fired_at": format_iso_utc(self.last_fired_at),
            "enabled": self.enabled,
            "is_oneshot": self.is_oneshot,
            "created_by": self.created_by,
            "notify_user_id": self.notify_user_id,
            "last_result": self.last_result,
        }


class CronStore:
    """Thread-safe SQLite-backed CRUD for :class:`CronEntry`."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS cron_entries (
        id TEXT PRIMARY KEY,
        prompt TEXT NOT NULL,
        cron TEXT,
        run_at TEXT,
        next_fire_at TEXT,
        last_fired_at TEXT,
        created_by TEXT,
        notify_user_id TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_result TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_cron_next_fire
        ON cron_entries(enabled, next_fire_at);
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # SQLite is synchronous — wrap in a lock so concurrent async tasks
        # don't trample each other. We open per-call to avoid threading
        # surprises with the default check_same_thread=True.
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # -- CRUD -------------------------------------------------------

    def add(self, entry: CronEntry) -> None:
        with self._connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO cron_entries
                       (id, prompt, cron, run_at, next_fire_at, last_fired_at,
                        created_by, notify_user_id, enabled, created_at, last_result)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.id, entry.prompt, entry.cron,
                        format_iso_utc(entry.run_at), format_iso_utc(entry.next_fire_at),
                        format_iso_utc(entry.last_fired_at), entry.created_by,
                        entry.notify_user_id, int(entry.enabled),
                        format_iso_utc(entry.created_at), entry.last_result,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CronStoreError(f"id already exists: {entry.id}") from exc

    def get(self, entry_id: str) -> Optional[CronEntry]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cron_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def list_all(self, *, only_enabled: bool = False) -> List[CronEntry]:
        sql = "SELECT * FROM cron_entries"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY next_fire_at IS NULL, next_fire_at ASC"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def list_due(self, *, now: Optional[datetime] = None) -> List[CronEntry]:
        """Return enabled entries whose ``next_fire_at <= now``."""
        now = now or now_utc()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM cron_entries
                   WHERE enabled = 1 AND next_fire_at IS NOT NULL
                     AND next_fire_at <= ?
                   ORDER BY next_fire_at ASC""",
                (format_iso_utc(now),),
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def remove(self, entry_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM cron_entries WHERE id = ?", (entry_id,)
            )
            return cur.rowcount > 0

    def mark_fired(self, entry_id: str, *, when: Optional[datetime] = None,
                   result: Optional[str] = None) -> None:
        """Update ``last_fired_at`` + ``next_fire_at`` after firing (atomic)."""
        when = when or now_utc()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cron_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                return
            entry = self._row_to_entry(row)
            entry.last_fired_at = when
            if result is not None:
                entry.last_result = result[:CRON_RESULT_MAX_CHARS]
            entry.advance(after=when)
            conn.execute(
                """UPDATE cron_entries
                   SET last_fired_at = ?, next_fire_at = ?, enabled = ?,
                       last_result = ?
                   WHERE id = ?""",
                (
                    format_iso_utc(entry.last_fired_at),
                    format_iso_utc(entry.next_fire_at),
                    int(entry.enabled),
                    entry.last_result,
                    entry_id,
                ),
            )

    def set_enabled(self, entry_id: str, enabled: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE cron_entries SET enabled = ? WHERE id = ?",
                (int(enabled), entry_id),
            )
            return cur.rowcount > 0

    # -- helpers ----------------------------------------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> CronEntry:
        # Build via __new__ to bypass __post_init__'s next_fire_at recompute
        # (we trust the persisted value for next_fire_at).
        e = CronEntry.__new__(CronEntry)
        e.id = row["id"]
        e.prompt = row["prompt"]
        e.cron = row["cron"]
        e.run_at = parse_iso_utc(row["run_at"])
        e.next_fire_at = parse_iso_utc(row["next_fire_at"])
        e.last_fired_at = parse_iso_utc(row["last_fired_at"])
        e.created_by = row["created_by"]
        e.notify_user_id = row["notify_user_id"]
        e.enabled = bool(row["enabled"])
        e.created_at = parse_iso_utc(row["created_at"]) or now_utc()
        e.last_result = row["last_result"]
        # Guard: cron XOR run_at — catches DB corruption early.
        assert bool(e.cron) != bool(e.run_at), (
            f"DB row {e.id!r}: expected exactly one of cron/run_at, "
            f"got cron={e.cron!r} run_at={e.run_at!r}"
        )
        return e


def make_id() -> str:
    """Return a short, unique entry id."""
    return f"cron-{uuid.uuid4().hex[:10]}"


def open_cron_store() -> CronStore:
    """Open the :class:`CronStore` at the configured DB path.

    Single entry point for cron-store consumers (the ``cron_*`` tools and the
    ``/pipeline`` command) so the store-construction idiom lives with its
    owner instead of being inlined at each call site.
    """
    return CronStore(resolve_db_path())
