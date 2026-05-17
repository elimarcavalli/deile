"""Tiny cron expression evaluator (5-field minute-precision).

Supports the standard 5-field crontab syntax:

    minute  hour  day_of_month  month  day_of_week
    0-59    0-23  1-31          1-12   0-6 (Sun=0)

Field tokens:
    *           any value
    N           literal
    N-M         range (inclusive)
    A,B,C       list
    */N         every N starting from field minimum
    A-B/N       step within a range

Special predefined strings:
    @hourly = "0 * * * *"
    @daily  = "0 0 * * *"
    @weekly = "0 0 * * 0"
    @monthly= "0 0 1 * *"

The implementation is deliberately minimal — no L/W/# extensions, no
seconds field. This is enough for the autonomous pipeline use cases:
"every N minutes", "at HH:MM weekdays", "at HH:MM on the 1st", etc.

Why no external dep: ``croniter`` is fine but cron is small. We avoid an
extra wheel/version churn for ~150 lines.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

_FIELD_RANGES: List[Tuple[int, int]] = [
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day_of_month
    (1, 12),  # month
    (0, 6),   # day_of_week (Sun=0)
]

_PREDEFINED = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}


class CronExpressionError(ValueError):
    """Raised for malformed cron expressions."""


def _parse_field(token: str, lo: int, hi: int) -> List[int]:
    """Expand one cron field token into a sorted list of integers."""
    if token == "*":
        return list(range(lo, hi + 1))
    out: List[int] = []
    for part in token.split(","):
        part = part.strip()
        if not part:
            raise CronExpressionError(f"empty field token in {token!r}")
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError as exc:
                raise CronExpressionError(f"invalid step {step_str!r}") from exc
            if step <= 0:
                raise CronExpressionError(f"step must be > 0, got {step}")
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            try:
                a, b = base.split("-", 1)
                start, end = int(a), int(b)
            except ValueError as exc:
                raise CronExpressionError(f"invalid range {base!r}") from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise CronExpressionError(f"invalid literal {base!r}") from exc
        if start < lo or end > hi or start > end:
            raise CronExpressionError(
                f"field value out of range [{lo}, {hi}]: {base!r}"
            )
        out.extend(range(start, end + 1, step))
    return sorted(set(out))


_WHITESPACE = re.compile(r"\s+")


def parse(expression: str) -> List[List[int]]:
    """Parse a cron expression into 5 lists of allowed values."""
    expr = expression.strip()
    if expr in _PREDEFINED:
        expr = _PREDEFINED[expr]
    fields = _WHITESPACE.split(expr)
    if len(fields) != 5:
        raise CronExpressionError(
            f"expected 5 fields (m h dom mon dow), got {len(fields)}: {expression!r}"
        )
    return [
        _parse_field(token, lo, hi)
        for token, (lo, hi) in zip(fields, _FIELD_RANGES)
    ]


def matches(expression: str, when: datetime) -> bool:
    """Return True iff ``when`` (UTC, minute precision) satisfies the cron."""
    minute_set, hour_set, dom_set, mon_set, dow_set = parse(expression)
    # Vixie-cron rule: when both DOM and DOW are restricted (not "*"), ANY
    # match counts. We approximate "is restricted" by len < full range.
    dom_restricted = len(dom_set) != 31
    dow_restricted = len(dow_set) != 7
    dom_match = when.day in dom_set
    # datetime.weekday(): Mon=0..Sun=6. cron: Sun=0..Sat=6.
    cron_dow = (when.weekday() + 1) % 7
    dow_match = cron_dow in dow_set
    if dom_restricted and dow_restricted:
        date_ok = dom_match or dow_match
    else:
        date_ok = dom_match and dow_match
    return (
        when.minute in minute_set
        and when.hour in hour_set
        and when.month in mon_set
        and date_ok
    )


def next_after(expression: str, after: datetime, *, max_iterations: int = 525600) -> datetime:
    """Return the next datetime *strictly after* ``after`` that matches.

    Search is by 1-minute increments — fast enough for short horizons,
    bounded by ``max_iterations`` (default 1 year of minutes) to avoid
    pathological infinite loops on impossible expressions.
    """
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    # Round up to the next minute boundary (crons fire on minute boundaries).
    candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(max_iterations):
        if matches(expression, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    raise CronExpressionError(
        f"no match within {max_iterations} minutes for {expression!r}"
    )
