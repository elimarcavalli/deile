"""Forge layer — GitHub + GitLab adapters behind a single ABC.

Public API:

    from deile.orchestration.forge import (
        ForgeClient, ForgeConfig, ForgeKind, ForgeUrl,
        GitHubForge, GitLabForge,
        ForgeError, ForgeConfigError, ForgeDetectionError,
        ForgeCliNotFound, ForgeCommandError,
        MergeBlocked, MergeBlockedByPipeline,
        IssueRef, PrRef, MrRef, CommentRef, MentionTrigger,
        compute_batch_id_for_number,
        build_forge, build_forge_config, detect_forge_kind, declared_hosts,
        parse_forge_url, find_first_pr_url,
        ForgeRouter, get_forge_router,
    )

Two top-level factories:

- :func:`build_forge` — one-shot for a single project. Internally calls
  :func:`build_forge_config` + the right adapter constructor.
- :class:`ForgeRouter` (singleton via :func:`get_forge_router`) — caches
  one :class:`ForgeClient` per ``(host, project_path)`` so multi-repo
  sessions (interactive CLI) do not re-resolve the same config every
  turn.

The pipeline uses :func:`build_forge` once at startup. The agent CLI
(``deile-shell``) uses the router.
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Dict, Mapping, Optional, Tuple

from deile.orchestration.forge.base import (ForgeClient, ForgeCliNotFound,
                                            ForgeCommandError, ForgeConfig,
                                            ForgeConfigError,
                                            ForgeDetectionError, ForgeError,
                                            ForgeKind, MergeBlocked,
                                            MergeBlockedByPipeline,
                                            WorkItemDetails, discover_cli)
from deile.orchestration.forge.detection import (build_forge_config,
                                                 declared_hosts,
                                                 detect_forge_kind)
from deile.orchestration.forge.github_forge import GhCommandError, GitHubForge
from deile.orchestration.forge.gitlab_forge import GitLabForge
from deile.orchestration.forge.refs import (CommentRef, IssueRef,
                                            MentionTrigger, MrRef, PrRef,
                                            compute_batch_id_for_number)
from deile.orchestration.forge.url_parser import (ForgeUrl, find_first_pr_url,
                                                  find_last_pr_url,
                                                  parse_forge_url)

logger = logging.getLogger(__name__)


def build_forge(
    *,
    project_path: str,
    env: Optional[Mapping[str, str]] = None,
    forge_kind: Optional[ForgeKind] = None,
    host_override: Optional[str] = None,
) -> ForgeClient:
    """Build a ready-to-use :class:`ForgeClient` for *project_path*.

    Thin wrapper around :func:`build_forge_config` that picks the right
    concrete adapter for the resolved :class:`ForgeKind`. Most callers
    should use this; only tests that need a hand-built :class:`ForgeConfig`
    should bypass it.
    """
    config = build_forge_config(
        project_path=project_path,
        env=env,
        forge_kind=forge_kind,
        host_override=host_override,
    )
    if config.kind is ForgeKind.GITHUB:
        return GitHubForge(config)
    if config.kind is ForgeKind.GITLAB:
        return GitLabForge(config)
    raise ForgeConfigError(f"unknown forge kind: {config.kind!r}")


# ---------------------------------------------------------------------------
# Multi-target router (CLI session)
# ---------------------------------------------------------------------------


class ForgeRouter:
    """Cache one :class:`ForgeClient` per ``(host, project_path)``.

    The interactive CLI may operate on several repos in a single session
    (GitHub project A, GitLab project B). Re-resolving the forge on every
    turn is wasteful (`shutil.which` + env reads + GitLab project-ID
    lookups). The router materialises the client on first use and reuses
    it thereafter — same posture as `requests.Session`.

    Thread-safe via a single :class:`threading.Lock`. The map is small
    (typically <10 entries per session) so a global lock has zero
    measurable contention.
    """

    def __init__(self) -> None:
        self._cache: Dict[Tuple[str, str], ForgeClient] = {}
        self._lock = Lock()

    def route(
        self,
        *,
        project_path: Optional[str] = None,
        url: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> ForgeClient:
        """Resolve a :class:`ForgeClient` for the target.

        Either ``project_path`` or ``url`` must be supplied. When ``url``
        is given the host comes from the parsed URL; otherwise the env
        rules in :func:`build_forge_config` decide.
        """
        if env is None:
            # Pilar 03 §7 — config-centralizado. ``settings_as_env`` projeta os
            # campos forge_* do Settings singleton de volta no shape DEILE_*.
            from deile.orchestration.forge.detection import settings_as_env
            env = settings_as_env()
        if url and not project_path:
            parsed = parse_forge_url(url, **declared_hosts(env))
            if parsed is None:
                raise ForgeDetectionError(
                    f"cannot route URL {url!r} — unknown host. Set "
                    f"DEILE_GITHUB_HOST or DEILE_GITLAB_HOST."
                )
            forge_kind = parsed.kind
            host_override = parsed.host
            project_path = parsed.project_path
        else:
            if not project_path:
                raise ValueError("route() requires project_path or url")
            forge_kind = None
            host_override = None
        config = build_forge_config(
            project_path=project_path,
            env=env,
            forge_kind=forge_kind,
            host_override=host_override,
        )
        key = (config.host, config.project_path)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            client: ForgeClient
            if config.kind is ForgeKind.GITHUB:
                client = GitHubForge(config)
            else:
                client = GitLabForge(config)
            self._cache[key] = client
            return client

    def clear(self) -> None:
        """Drop every cached client (mainly for tests)."""
        with self._lock:
            self._cache.clear()


# Process-wide singleton — built lazily.
_router: Optional[ForgeRouter] = None
_router_lock = Lock()


def get_forge_router() -> ForgeRouter:
    """Return the process-wide :class:`ForgeRouter` singleton."""
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = ForgeRouter()
    return _router


__all__ = [
    # Contracts
    "ForgeClient",
    "ForgeConfig",
    "ForgeKind",
    "ForgeUrl",
    "WorkItemDetails",
    # Concrete adapters
    "GitHubForge",
    "GitLabForge",
    # Errors
    "ForgeError",
    "ForgeConfigError",
    "ForgeDetectionError",
    "ForgeCliNotFound",
    "ForgeCommandError",
    "GhCommandError",
    "MergeBlocked",
    "MergeBlockedByPipeline",
    # Refs
    "IssueRef",
    "PrRef",
    "MrRef",
    "CommentRef",
    "MentionTrigger",
    "compute_batch_id_for_number",
    # Factories
    "build_forge",
    "build_forge_config",
    "detect_forge_kind",
    "declared_hosts",
    "discover_cli",
    # URL parsing
    "parse_forge_url",
    "find_first_pr_url",
    "find_last_pr_url",
    # Router
    "ForgeRouter",
    "get_forge_router",
]
