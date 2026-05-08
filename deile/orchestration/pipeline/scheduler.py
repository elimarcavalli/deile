"""Per-monitor cron schedule store with catch-up.

The pipeline owns a small YAML file per monitor under ``config/`` of the
base repo. The file is the authority for *when* each pipeline action
fires (recurring crons + ad-hoc one-shots).

::

    config/pipeline_schedule_<monitor_id>.yaml

    recurring:
      - id: review_loop
        action: review
        cron: "*/5 * * * *"
        enabled: true
        last_run_at: 2026-05-06T01:23:00Z

    oneshot:
      - id: oneshot-impl-99
        action: implement
        target_issue: 99
        run_at: 2026-05-06T18:00:00Z
        completed: false

Two-phase semantics on monitor startup:

1. ``compute_pending(now)`` returns every entry whose next scheduled run
   is ``<= now`` — sorted ascending. These are the "missed" runs to
   catch up on.
2. The monitor drains the catch-up queue **sequentially**, then enters
   the normal poll loop where each tick checks ``compute_pending(now)``
   again.

Catch-up coalesces by default: N runs missed during downtime → 1 run
right now, with ``last_run_at`` advanced to *now* so we don't replay
every minute. Set ``replay_all=True`` on a recurring entry for the rare
case where every missed slot must run individually.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import yaml

from deile.core.exceptions import DEILEError
from deile.orchestration.pipeline.cron import CronExpressionError, next_after

logger = logging.getLogger(__name__)


VALID_ACTIONS = {"review", "implement", "pr_review", "classify"}


class ScheduleError(DEILEError):
    """Raised for invalid schedule configuration or operations."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip().rstrip("Z")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as exc:
            raise ScheduleError(f"invalid ISO datetime: {value!r}") from exc
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise ScheduleError(f"unsupported datetime value: {value!r}")


def _serialize_dt(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RecurringEntry:
    """A cron-driven recurring schedule for one pipeline action."""

    id: str
    action: str
    cron: str
    enabled: bool = True
    last_run_at: Optional[datetime] = None
    replay_all: bool = False

    def __post_init__(self) -> None:
        if not self.id or not self.id.replace("_", "").replace("-", "").isalnum():
            raise ScheduleError(f"invalid id: {self.id!r}")
        if self.action not in VALID_ACTIONS:
            raise ScheduleError(
                f"action must be one of {sorted(VALID_ACTIONS)}, got {self.action!r}"
            )
        # Validate cron eagerly.
        try:
            next_after(self.cron, _now_utc())
        except CronExpressionError as exc:
            raise ScheduleError(f"invalid cron {self.cron!r}: {exc}") from exc

    def next_run_at(self, *, after: Optional[datetime] = None) -> datetime:
        anchor = after or self.last_run_at or _now_utc()
        return next_after(self.cron, anchor)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "cron": self.cron,
            "enabled": self.enabled,
            "last_run_at": _serialize_dt(self.last_run_at),
            "replay_all": self.replay_all,
        }


@dataclass
class OneshotEntry:
    """A one-shot schedule for a specific datetime."""

    id: str
    action: str
    run_at: datetime
    target_issue: Optional[int] = None
    target_pr: Optional[int] = None
    completed: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise ScheduleError("oneshot id required")
        if self.action not in VALID_ACTIONS:
            raise ScheduleError(
                f"action must be one of {sorted(VALID_ACTIONS)}, got {self.action!r}"
            )
        if not isinstance(self.run_at, datetime):
            raise ScheduleError("run_at must be a datetime")
        if self.run_at.tzinfo is None:
            self.run_at = self.run_at.replace(tzinfo=timezone.utc)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "run_at": _serialize_dt(self.run_at),
            "target_issue": self.target_issue,
            "target_pr": self.target_pr,
            "completed": self.completed,
        }


@dataclass
class PendingRun:
    """A scheduled action whose time has come (or passed)."""

    when: datetime
    entry_id: str
    action: str
    is_oneshot: bool
    target_issue: Optional[int] = None
    target_pr: Optional[int] = None


@dataclass
class Schedule:
    """All schedule entries for one monitor."""

    recurring: List[RecurringEntry] = field(default_factory=list)
    oneshot: List[OneshotEntry] = field(default_factory=list)

    # ---- query / mutation ----------------------------------------

    def get_recurring(self, entry_id: str) -> Optional[RecurringEntry]:
        return next((e for e in self.recurring if e.id == entry_id), None)

    def get_oneshot(self, entry_id: str) -> Optional[OneshotEntry]:
        return next((e for e in self.oneshot if e.id == entry_id), None)

    def add_recurring(self, entry: RecurringEntry) -> None:
        if self.get_recurring(entry.id):
            raise ScheduleError(f"recurring id already exists: {entry.id!r}")
        self.recurring.append(entry)

    def add_oneshot(self, entry: OneshotEntry) -> None:
        if self.get_oneshot(entry.id):
            raise ScheduleError(f"oneshot id already exists: {entry.id!r}")
        self.oneshot.append(entry)

    def remove(self, entry_id: str) -> bool:
        before = len(self.recurring) + len(self.oneshot)
        self.recurring = [e for e in self.recurring if e.id != entry_id]
        self.oneshot = [e for e in self.oneshot if e.id != entry_id]
        return (len(self.recurring) + len(self.oneshot)) < before

    # ---- catch-up + tick selection -------------------------------

    def compute_pending(self, now: Optional[datetime] = None) -> List[PendingRun]:
        """Return all schedule entries whose run time is <= now.

        Sorted by ``when`` ascending (oldest miss first). For coalescing
        recurring entries, only ONE PendingRun is emitted per entry —
        the most recent missed slot. With ``replay_all=True``, every
        missed slot becomes its own PendingRun.
        """
        now = now or _now_utc()
        out: List[PendingRun] = []

        for r in self.recurring:
            if not r.enabled:
                continue
            try:
                # First fire after last_run_at (or epoch if never).
                anchor = r.last_run_at or datetime(1970, 1, 1, tzinfo=timezone.utc)
                next_at = next_after(r.cron, anchor)
            except CronExpressionError:
                logger.warning("invalid cron in recurring %s", r.id)
                continue
            if next_at > now:
                continue  # not due yet
            if not r.replay_all:
                # Coalesce: collapse N misses to 1, fire at the latest miss.
                latest = next_at
                while True:
                    try:
                        candidate = next_after(r.cron, latest)
                    except CronExpressionError:
                        break
                    if candidate > now:
                        break
                    latest = candidate
                out.append(PendingRun(
                    when=latest,
                    entry_id=r.id,
                    action=r.action,
                    is_oneshot=False,
                ))
            else:
                cursor = next_at
                while cursor <= now:
                    out.append(PendingRun(
                        when=cursor,
                        entry_id=r.id,
                        action=r.action,
                        is_oneshot=False,
                    ))
                    try:
                        cursor = next_after(r.cron, cursor)
                    except CronExpressionError:
                        break

        for o in self.oneshot:
            if o.completed or o.run_at > now:
                continue
            out.append(PendingRun(
                when=o.run_at,
                entry_id=o.id,
                action=o.action,
                is_oneshot=True,
                target_issue=o.target_issue,
                target_pr=o.target_pr,
            ))

        out.sort(key=lambda p: p.when)
        return out

    def mark_run(self, run: PendingRun, *, when: Optional[datetime] = None) -> None:
        """Update last_run_at / completed after a PendingRun executes."""
        when = when or _now_utc()
        if run.is_oneshot:
            o = self.get_oneshot(run.entry_id)
            if o is not None:
                o.completed = True
        else:
            r = self.get_recurring(run.entry_id)
            if r is not None:
                r.last_run_at = when

    def gc_completed_oneshots(self, *, max_age_days: int = 7) -> int:
        """Remove completed oneshot entries older than *max_age_days* (gap #25).

        Returns the number of entries removed.
        """
        cutoff = _now_utc().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        from datetime import timedelta
        cutoff = _now_utc() - timedelta(days=max_age_days)
        before = len(self.oneshot)
        self.oneshot = [
            o for o in self.oneshot
            if not (o.completed and o.run_at < cutoff)
        ]
        return before - len(self.oneshot)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class ScheduleStore:
    """Load/save :class:`Schedule` to YAML, keyed by monitor_id."""

    def __init__(self, base_repo_path: Path, monitor_id: str = "default") -> None:
        self.base_repo_path = Path(base_repo_path).resolve()
        self.monitor_id = monitor_id
        self.config_dir = self.base_repo_path / "config"
        self.path = self.config_dir / f"pipeline_schedule_{monitor_id}.yaml"

    def load(self) -> Schedule:
        """Load the schedule. Returns an empty Schedule if file is missing."""
        if not self.path.exists():
            return Schedule()
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ScheduleError(f"could not parse {self.path}: {exc}") from exc
        recurring = [self._build_recurring(d) for d in (data.get("recurring") or [])]
        oneshot = [self._build_oneshot(d) for d in (data.get("oneshot") or [])]
        return Schedule(recurring=recurring, oneshot=oneshot)

    def save(self, schedule: Schedule) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "recurring": [e.to_dict() for e in schedule.recurring],
            "oneshot": [e.to_dict() for e in schedule.oneshot],
        }
        self.path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    @staticmethod
    def _build_recurring(d: dict) -> RecurringEntry:
        return RecurringEntry(
            id=d["id"],
            action=d["action"],
            cron=d["cron"],
            enabled=bool(d.get("enabled", True)),
            last_run_at=_parse_dt(d.get("last_run_at")),
            replay_all=bool(d.get("replay_all", False)),
        )

    @staticmethod
    def _build_oneshot(d: dict) -> OneshotEntry:
        return OneshotEntry(
            id=d["id"],
            action=d["action"],
            run_at=_parse_dt(d["run_at"]),
            target_issue=d.get("target_issue"),
            target_pr=d.get("target_pr"),
            completed=bool(d.get("completed", False)),
        )
