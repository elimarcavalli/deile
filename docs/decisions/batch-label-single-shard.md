# ADR: ~batch:<sha8> Labels — Single Shard (No Sharding)

## Status
Accepted — 2026-06-06

## Context
The pipeline uses `~batch:<sha8>` labels to coordinate ownership of in-flight
issues/PRs between monitor instances. Originally, sharding the batch namespace
(e.g. `~batch:<sha8>-shard:<n>`) was considered for high-throughput scenarios.

This ADR documents the decision to remain on the single-shard (no-sharding) design.

## Decision
Use a single global `~batch:<sha8>` label per item. No sharding.

## Consequences
- **Positive**: Simple; no shard-routing logic; GC is trivial (delete all ~batch: labels).
- **Negative**: Theoretical bottleneck if N >> 16 workers contend on the same item
  simultaneously — but empirically this has not occurred (max observed: 3 monitors).
- **Accepted risk**: ~84 orphan `~batch:<sha8>` definitions accumulate in the repo
  label list over time. Mitigated by `scripts/retroactive_gc.py --audit-orphan-batch-labels`.

## Alternatives Considered
- **Sharding by worker index**: Rejected. Adds routing complexity without addressing
  the real bottleneck (forge API rate limits, not label collisions).
- **No coordination labels**: Rejected. Required for idempotent dispatch in multi-
  monitor deployments (issue #373).
