"""Per-monitor identity for the autonomous pipeline.

The identity ties together everything that must NOT collide between
parallel monitors:

- ``monitor_id`` — appears in worktree paths, branch names, and ownership labels
- ``shard_index`` / ``shard_count`` — hash-based sharding so two monitors never
  compete for the same issue/PR

Single-monitor deployments (no env vars set) default to
``monitor_id="default"``, ``shard_index=0``, ``shard_count=1`` — equivalent to
the pre-multi-monitor behaviour. Backwards-compatible.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Optional


_MONITOR_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,32}$")


class IdentityError(ValueError):
    """Raised for malformed identity configuration."""


@dataclass(frozen=True)
class MonitorIdentity:
    """Identifies one autonomous monitor within a deployment."""

    monitor_id: str = "default"
    shard_index: int = 0
    shard_count: int = 1

    def __post_init__(self) -> None:
        if not _MONITOR_ID_RE.match(self.monitor_id):
            raise IdentityError(
                f"monitor_id must match {_MONITOR_ID_RE.pattern}, got {self.monitor_id!r}"
            )
        if self.shard_count < 1:
            raise IdentityError(f"shard_count must be >= 1, got {self.shard_count}")
        if not (0 <= self.shard_index < self.shard_count):
            raise IdentityError(
                f"shard_index must be in [0, {self.shard_count}), got {self.shard_index}"
            )

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "MonitorIdentity":
        e = env if env is not None else os.environ
        return cls(
            monitor_id=e.get("DEILE_PIPELINE_MONITOR_ID", "default"),
            shard_index=int(e.get("DEILE_PIPELINE_SHARD_INDEX", "0")),
            shard_count=int(e.get("DEILE_PIPELINE_SHARD_COUNT", "1")),
        )

    @property
    def is_default(self) -> bool:
        """True for the legacy single-monitor configuration."""
        return (
            self.monitor_id == "default"
            and self.shard_count == 1
            and self.shard_index == 0
        )

    def owns(self, key: str) -> bool:
        """Return True if `key` is in this monitor's shard.

        Uses SHA-256 of the key modulo ``shard_count``. Two monitors with
        consistent shard_count agree deterministically on ownership.
        """
        if self.shard_count == 1:
            return True
        h = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)
        return (h % self.shard_count) == self.shard_index

    def branch_prefix(self, action: str = "auto") -> str:
        """Return the branch prefix this monitor uses (no trailing slash).

        - default identity → ``auto`` (legacy behaviour)
        - other identities → ``auto/<monitor_id>``
        """
        if self.is_default:
            return action
        return f"{action}/{self.monitor_id}"

    def worktree_subdir(self) -> Optional[str]:
        """Return the per-monitor worktree subdirectory, or None for default.

        - default identity → None (worktrees go straight to ``.worktrees/<branch>``)
        - other identities → ``<monitor_id>`` (worktrees go to ``.worktrees/<monitor_id>/<branch>``)
        """
        return None if self.is_default else self.monitor_id

    def ownership_label(self) -> str:
        """Label that identifies ownership of a claimed issue/PR."""
        return f"~by:{self.monitor_id}"

    def lockfile_name(self) -> str:
        """Filename of the PID lock for this monitor instance."""
        return f".deile-pipeline-{self.monitor_id}.lock"
