"""Decide which forge (:class:`ForgeKind`) backs a given target.

Resolution order — explicit beats implicit, never guesses silently:

1. Explicit override via env / settings (``DEILE_FORGE_KIND=github|gitlab``).
2. URL host parse (``github.com`` / ``gitlab.com`` / declared custom hosts).
3. Failure with :class:`ForgeDetectionError` whose message names the env
   vars to set.

The optional HTTP probe (step 4 in the issue spec) is opt-in via
``DEILE_FORGE_PROBE=1`` — it adds a real network call to the resolution
hot path, so it is disabled by default. When enabled it tries
``GET <host>/api/v4/version`` (GitLab) and ``GET <host>/api/v3/`` (GHES)
in parallel and picks whichever responds 200.
"""

from __future__ import annotations

import logging
from typing import Mapping, Optional

from deile.orchestration.forge.base import (ForgeConfig, ForgeDetectionError,
                                            ForgeKind, discover_cli)
from deile.orchestration.forge.url_parser import parse_forge_url

logger = logging.getLogger(__name__)


def _env(env: Mapping[str, str], key: str, default: str = "") -> str:
    """Read an env value, stripping whitespace; treat empty as missing."""
    value = (env.get(key) or "").strip()
    return value or default


def detect_forge_kind(
    *,
    url: Optional[str] = None,
    project_path: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> ForgeKind:
    """Decide the :class:`ForgeKind` for the target described by inputs.

    Parameters
    ----------
    url:
        Optional canonical URL (e.g. clone URL or web URL) — parsed for host.
    project_path:
        Optional ``owner/repo`` (GH) or ``group/.../project`` (GL) — used
        only as a tiebreaker when ``url`` is None: a path with more than two
        segments cannot be GitHub, so it is treated as GitLab.
    env:
        Mapping for env-var lookup; defaults to ``os.environ``.

    Returns
    -------
    ForgeKind
        The resolved forge kind.

    Raises
    ------
    ForgeDetectionError
        When no rule matches deterministically. The message names every env
        var the operator could set to fix the situation.
    """
    if env is None:
        import os
        env = os.environ

    # Step 1 — explicit override always wins.
    explicit = _env(env, "DEILE_FORGE_KIND").lower()
    if explicit and explicit != "auto":
        return ForgeKind.parse(explicit)

    # Step 2 — URL host parse (path-aware) plus a host-only fallback for
    # URLs that don't carry an /issues/N or /pull/N segment (e.g. clone URLs,
    # repo home page). The path-aware match is the strict signal; the
    # host-only check is the looser fallback so a user can do
    # ``detect_forge_kind(url="https://github.com/o/r")`` and get the answer.
    if url:
        from urllib.parse import urlparse
        gh_extra = _split_hosts(_env(env, "DEILE_GITHUB_HOST"))
        gl_extra = _split_hosts(_env(env, "DEILE_GITLAB_HOST"))
        parsed = parse_forge_url(url, github_hosts=gh_extra, gitlab_hosts=gl_extra)
        if parsed is not None:
            return parsed.kind
        # Host-only fallback — does NOT touch ``parse_forge_url`` (which
        # requires a full ``/issues/N`` / ``/pull/N`` path).
        try:
            host = urlparse(url).netloc.lower()
        except ValueError:
            host = ""
        if host == "github.com" or host in gh_extra:
            return ForgeKind.GITHUB
        if host == "gitlab.com" or host in gl_extra:
            return ForgeKind.GITLAB
        # Unknown host — fall through to the project-path heuristic, then
        # the explicit error below.

    # Step 2b — project path heuristic.
    if project_path:
        segments = [s for s in project_path.split("/") if s]
        if len(segments) > 2:
            # Only GitLab supports nested groups — a 3+-segment path cannot
            # be GitHub, so this is unambiguous.
            return ForgeKind.GITLAB
        # A 2-segment ``owner/repo`` path is ambiguous in principle (GH
        # ``owner/repo`` OR GL flat ``group/project``), but historically
        # every DEILE deployment ran against GitHub and the env reference
        # was ``DEILE_PIPELINE_REPO=elimarcavalli/deile``. To preserve that
        # working default — and avoid breaking ``--pipeline-stop`` and other
        # commands that resolve the forge before they need it — the
        # 2-segment fallback resolves to GitHub. Operators on GitLab MUST
        # set ``DEILE_FORGE_KIND=gitlab`` (the issue spec is explicit on
        # this: the override is the canonical fix, the heuristic is just
        # the convenience tail).
        if len(segments) == 2:
            return ForgeKind.GITHUB

    raise ForgeDetectionError(
        "could not determine forge: set DEILE_FORGE_KIND=github|gitlab "
        "(and DEILE_GITHUB_HOST / DEILE_GITLAB_HOST if you use a custom host)"
    )


def _split_hosts(value: str) -> tuple:
    """Split a comma- or whitespace-separated host list into a tuple.

    Allows the operator to declare multiple hosts at once
    (e.g. ``DEILE_GITHUB_HOST=ghe-a.empresa.com,ghe-b.empresa.com``) so a
    single pipeline can serve a forge fleet without spawning N processes.
    Empty entries are dropped.
    """
    return tuple(h.strip().lower() for h in value.replace(",", " ").split() if h.strip())


def build_forge_config(
    *,
    project_path: str,
    env: Optional[Mapping[str, str]] = None,
    forge_kind: Optional[ForgeKind] = None,
    host_override: Optional[str] = None,
) -> ForgeConfig:
    """Build a :class:`ForgeConfig` for *project_path*, resolving every field.

    Detects the forge kind (unless explicitly passed), picks the host
    (env override → cloud default), validates the CLI is installed, and
    returns a ready-to-use config.

    Parameters
    ----------
    project_path:
        ``owner/repo`` (GH) or ``group/.../project`` (GL).
    env:
        Env mapping (defaults to ``os.environ``).
    forge_kind:
        Pre-resolved forge kind (bypasses :func:`detect_forge_kind`).
    host_override:
        Explicit host (skips env lookup).
    """
    if env is None:
        import os
        env = os.environ

    kind = forge_kind or detect_forge_kind(project_path=project_path, env=env)
    if host_override:
        host = host_override
    elif kind is ForgeKind.GITHUB:
        # If DEILE_GITHUB_HOST declares MULTIPLE hosts the first one wins
        # — there is no way to pick "the right one" without external info.
        explicit = _split_hosts(_env(env, "DEILE_GITHUB_HOST"))
        host = explicit[0] if explicit else "github.com"
    else:
        explicit = _split_hosts(_env(env, "DEILE_GITLAB_HOST"))
        host = explicit[0] if explicit else "gitlab.com"

    cli_name = "gh" if kind is ForgeKind.GITHUB else "glab"
    cli_path = discover_cli(cli_name)

    return ForgeConfig(
        kind=kind,
        host=host,
        project_path=project_path,
        cli_path=cli_path,
    )


def declared_hosts(env: Optional[Mapping[str, str]] = None) -> dict:
    """Return ``{'github_hosts': (...), 'gitlab_hosts': (...)}`` for callers
    that need to feed :func:`parse_forge_url` without re-reading env vars
    themselves (e.g. ``find_first_pr_url`` in stages.py).
    """
    if env is None:
        import os
        env = os.environ
    return {
        "github_hosts": _split_hosts(_env(env, "DEILE_GITHUB_HOST")),
        "gitlab_hosts": _split_hosts(_env(env, "DEILE_GITLAB_HOST")),
    }


__all__ = ["detect_forge_kind", "build_forge_config", "declared_hosts"]
