# SLO: GC Label Staleness

## Objective
Transient pipeline labels on **closed issues** and **merged/closed PRs** must be
removed within the P99 targets below. "Staleness" = time from item close/merge
to last transient label removed.

## Commitments

| Path | P99 target | Notes |
|------|-----------|-------|
| Webhook / reconcile path | < 60 s | Detected on the next tick (poll_interval = 60 s) |
| Batch / retroactive path | < 24 h | Manual or scheduled run of retroactive_gc.py |

## Instruments
Terminal GC emits structured log lines via the standard pipeline logger.
Query fields:
- `outcome`: one of `success`, `noop`, `partial`
- `timestamp`: ISO-8601 UTC close/merge detection time

## Alert Query (log-based)
```
filter outcome = "partial" OR (outcome = "success" AND latency_s > 60)
```
Threshold: > 5 events in a 10-minute window → investigate.

## Exclusions
- Items closed while the pipeline monitor is stopped: GC applies on next startup/tick.
- Items closed before this SLO was established (2026-06-06): handled by retroactive_gc.py.
