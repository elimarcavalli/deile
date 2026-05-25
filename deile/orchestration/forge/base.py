"""Forge-agnostic contracts: ABC, config, errors, enums.

Single import surface for every forge implementation
(:mod:`deile.orchestration.forge.github_forge`,
:mod:`deile.orchestration.forge.gitlab_forge`). The pipeline depends only
on this module's abstractions — it never imports a concrete forge.

Design notes:

- :class:`ForgeKind` is a string enum so it survives serialization to JSON,
  YAML and env vars without custom encoders.
- :class:`ForgeConfig` is a *mutable* dataclass (not frozen) because the
  GitLab adapter caches the numeric project ID on it after the first lookup
  — see :meth:`GitLabForge._resolve_project_id`. The cache lives on the
  config so it shares scope with the client.
- :class:`ForgeClient` declares the **entire surface** the pipeline uses.
  The legacy ``GitHubClient`` had 28 public methods; this ABC keeps every
  one of them (same names, same signatures, same contracts), plus a small
  set of forge-router helpers (``kind``, ``config``, ``web_pr_url``,
  ``web_issue_url``, ``set_draft``, ``get_ci_status``, ``merge_pr``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import (Any, Callable, Iterable, List, Literal, Optional, Sequence,
                    Tuple)

from deile.core.exceptions import DEILEError
from deile.orchestration.forge.refs import (CommentRef, IssueRef,
                                            PrRef,
                                            compute_batch_id_for_number)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ForgeKind(str, Enum):
    """The two forges DEILE supports today.

    String-valued so it round-trips through env vars / YAML / JSON without
    a custom encoder. Future forges (Bitbucket, Gitea, ...) become new
    members here.
    """

    GITHUB = "github"
    GITLAB = "gitlab"

    @classmethod
    def parse(cls, value: Any) -> "ForgeKind":
        """Coerce ``value`` to a :class:`ForgeKind` (case-insensitive).

        Raises :class:`ForgeConfigError` if the value is not one of the
        canonical members — never silently picks a default.
        """
        if isinstance(value, cls):
            return value
        if value is None:
            raise ForgeConfigError("forge kind is None — set DEILE_FORGE_KIND")
        text = str(value).strip().lower()
        for member in cls:
            if member.value == text:
                return member
        raise ForgeConfigError(
            f"unknown forge kind: {value!r} — must be one of {[m.value for m in cls]}"
        )


# ---------------------------------------------------------------------------
# Errors (all subclass DEILEError so callers can ``except DEILEError`` once)
# ---------------------------------------------------------------------------


class ForgeError(DEILEError):
    """Base class for every forge-layer error."""


class ForgeConfigError(ForgeError, ValueError):
    """Raised when a :class:`ForgeConfig` cannot be built or validated.

    Also subclasses :class:`ValueError` so legacy callers that did
    ``except ValueError`` (the previous ``GitHubClient`` constructor raised
    a bare ``ValueError`` for invalid repos) keep working without changes.
    """


class ForgeDetectionError(ForgeError):
    """Raised when :func:`detect_forge_kind` cannot decide deterministically.

    The message always names the env vars the operator should set
    (``DEILE_FORGE_KIND`` plus the per-forge ``DEILE_*_HOST``) so the fix is
    obvious from the error alone.
    """


class ForgeCliNotFound(ForgeError):
    """Raised at adapter construction time when the required CLI binary
    (``gh`` for GitHub, ``glab`` for GitLab) is not on ``$PATH``.

    Fails *at construction*, not at first call, so a misconfigured pipeline
    surfaces the error immediately instead of mid-stage.
    """


class ForgeCommandError(ForgeError):
    """Raised when a forge CLI subprocess exits non-zero.

    Carries the full command line, return code, stdout and stderr. The
    :attr:`stderr` is the most useful field for diagnostics; the others are
    preserved for tests and audit.
    """

    def __init__(self, cmd: Sequence[str], returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(
            f"{cmd[0] if cmd else 'forge'} {' '.join(cmd[1:])} failed "
            f"({returncode}): {stderr.strip()[:300]}"
        )
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class MergeBlocked(ForgeError):
    """Raised by :meth:`ForgeClient.merge_pr` when the forge refuses to merge.

    Concrete reasons populate :attr:`reason` — e.g. ``"merge_status=cannot_be_merged"``,
    ``"approval_rules_unmet"``, ``"protected_branch"``. The pipeline maps this
    error to a `BLOQUEADO:` impediment so the human stakeholder is the one to act.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MergeBlockedByPipeline(MergeBlocked):
    """Specialisation: GitLab merge refused because "Pipelines must succeed".

    Carried as a distinct type so the pipeline can give a more specific
    diagnostic ("CI pipeline is <status>; wait green or relax the rule"
    instead of the generic merge_status reason).
    """


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


# Project path validation regexes — defence in depth against shell metachars,
# whitespace and path traversal. GitHub is ``owner/repo`` (exactly two
# segments); GitLab supports nested groups (``group/sub*/project`` — 2+
# segments). Both reject ``..`` and any character outside the documented
# alphabet for project identifiers.
_GH_REPO_RE = re.compile(r"\A[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
_GL_PROJECT_RE = re.compile(
    r"\A[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+\Z"
)


def _default_api_base(kind: ForgeKind, host: str) -> str:
    """Return the canonical API base URL for *kind* + *host*.

    Cloud GitHub uses the dedicated ``api.github.com`` apex; every other
    case puts ``/api/<version>`` on the host itself (GHES + every GitLab
    instance, cloud or self-hosted).
    """
    if kind is ForgeKind.GITHUB:
        if host == "github.com":
            return "https://api.github.com"
        return f"https://{host}/api/v3"
    # GitLab — v4 is the only public version today.
    return f"https://{host}/api/v4"


@dataclass
class ForgeConfig:
    """Per-target forge configuration.

    Built once at pipeline startup (or per CLI command in the agent's
    ``ForgeRouter`` cache) and reused for every operation. Mutable on
    purpose so the GitLab adapter can stash the numeric project ID
    (``project_id``) after the first lookup — see
    :meth:`GitLabForge._resolve_project_id`.
    """

    kind: ForgeKind
    host: str
    project_path: str
    cli_path: str
    api_base: str = ""
    web_base: str = ""
    # GitLab-only: numeric project ID, cached after first ``GET /projects/<encoded>``.
    # Mutable on the dataclass so the cache shares the config's lifetime.
    project_id: Optional[str] = None
    # Default branch as reported by the forge (resolved lazily — see
    # :meth:`ForgeClient.default_branch`). ``None`` until first lookup.
    default_branch: Optional[str] = None

    def __post_init__(self) -> None:
        # Defensive validation: rejects everything that could escape the
        # ``repos/<repo>/...`` (GH) or ``projects/<id|encoded>/...`` (GL) prefix
        # in REST URL composition. The regexes deliberately do NOT match ``..``
        # or any shell metachar.
        if ".." in self.project_path:
            raise ForgeConfigError(f"invalid project path: {self.project_path!r}")
        if self.kind is ForgeKind.GITHUB:
            if not _GH_REPO_RE.fullmatch(self.project_path):
                raise ForgeConfigError(
                    f"invalid GitHub repo: {self.project_path!r} "
                    f"— expected 'owner/repo'"
                )
        elif self.kind is ForgeKind.GITLAB:
            if not _GL_PROJECT_RE.fullmatch(self.project_path):
                raise ForgeConfigError(
                    f"invalid GitLab project path: {self.project_path!r} "
                    f"— expected 'group/(subgroup/)*project'"
                )
        else:  # pragma: no cover - exhaustive enum
            raise ForgeConfigError(f"unknown forge kind: {self.kind!r}")
        if not self.host:
            raise ForgeConfigError("forge host required")
        # Derive the base URLs from kind+host if the caller did not provide
        # them explicitly. Keeps construction sites short while letting tests
        # override the bases without re-deriving.
        if not self.api_base:
            object.__setattr__(self, "api_base", _default_api_base(self.kind, self.host))
        if not self.web_base:
            object.__setattr__(self, "web_base", f"https://{self.host}")

    @property
    def encoded_project_path(self) -> str:
        """URL-encoded project path — used in GitLab REST URLs.

        GitLab REST accepts either the numeric ID or the URL-encoded path
        (``group%2Fsubgroup%2Fproject``). When :attr:`project_id` is cached
        the client should prefer it (one byte vs many); this helper exists
        for the first call.
        """
        from urllib.parse import quote
        return quote(self.project_path, safe="")

    def web_issue_url(self, number: int) -> str:
        """Return the web URL for an issue ``#number`` in this project."""
        if self.kind is ForgeKind.GITLAB:
            return f"{self.web_base}/{self.project_path}/-/issues/{number}"
        return f"{self.web_base}/{self.project_path}/issues/{number}"

    def web_pr_url(self, number: int) -> str:
        """Return the web URL for a PR (GH) or MR (GL) ``#number``."""
        if self.kind is ForgeKind.GITLAB:
            return f"{self.web_base}/{self.project_path}/-/merge_requests/{number}"
        return f"{self.web_base}/{self.project_path}/pull/{number}"


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class ForgeClient(ABC):
    """Abstract base for every forge adapter.

    Surface mirrors the legacy ``GitHubClient`` (kept stable so the pipeline
    refactor is non-breaking) plus the new forge-router helpers. Concrete
    subclasses live in :mod:`deile.orchestration.forge.github_forge` and
    :mod:`deile.orchestration.forge.gitlab_forge`.

    Every I/O-bound method is async — the pipeline polls in a single-thread
    asyncio loop and must not block.
    """

    def __init__(self, config: ForgeConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def kind(self) -> ForgeKind:
        return self._config.kind

    @property
    def config(self) -> ForgeConfig:
        return self._config

    @property
    def repo(self) -> str:
        """Project path (``owner/repo`` on GH, ``group/.../project`` on GL).

        Kept as ``repo`` (singular) because every caller migrated from
        ``GitHubClient.repo`` already uses that attribute. Aliased to
        :attr:`project_path` for readability.
        """
        return self._config.project_path

    @property
    def project_path(self) -> str:
        return self._config.project_path

    # ------------------------------------------------------------------
    # Subprocess plumbing — shared by every concrete forge
    # ------------------------------------------------------------------

    async def _run(self, *args: str) -> Tuple[int, str, str]:
        """Run the forge CLI (``gh`` or ``glab``) and capture rc/stdout/stderr.

        Never raises on non-zero exit — the caller decides via
        :meth:`_run_checked` whether to convert to :class:`ForgeCommandError`.
        """
        cmd = [self._config.cli_path, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        return (
            proc.returncode or 0,
            (stdout_b or b"").decode("utf-8", errors="replace"),
            (stderr_b or b"").decode("utf-8", errors="replace"),
        )

    async def _run_checked(self, *args: str) -> str:
        rc, out, err = await self._run(*args)
        if rc != 0:
            raise ForgeCommandError((self._config.cli_path,) + tuple(args), rc, out, err)
        return out

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    @abstractmethod
    async def list_issues_with_label(self, label: str, *, limit: int = 50) -> List[IssueRef]: ...

    @abstractmethod
    async def get_issue(self, number: int) -> IssueRef: ...

    @abstractmethod
    async def list_issues_assigned_to(self, login: str, *, limit: int = 100) -> List[IssueRef]: ...

    @abstractmethod
    async def list_unclassified_issues(self, *, limit: int = 100) -> List[IssueRef]: ...

    @abstractmethod
    async def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: Optional[List[str]] = None,
    ) -> int: ...

    @abstractmethod
    async def comment_on_issue(self, number: int, text: str) -> None: ...

    @abstractmethod
    async def assign_issue(self, number: int, login: str) -> None: ...

    # ------------------------------------------------------------------
    # Pull / Merge Requests
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_pr(self, number: int) -> Optional[PrRef]: ...

    @abstractmethod
    async def has_open_pr_for_issue(self, number: int) -> bool: ...

    @abstractmethod
    async def list_open_prs(self, *, limit: int = 50) -> List[PrRef]: ...

    @abstractmethod
    async def list_prs_assigned_to(self, login: str, *, limit: int = 100) -> List[PrRef]: ...

    @abstractmethod
    async def list_unclassified_prs(self) -> List[PrRef]: ...

    @abstractmethod
    async def list_recently_merged_prs(self, *, limit: int = 20) -> List[PrRef]: ...

    @abstractmethod
    async def pr_reviewer_still_requested(self, number: int, login: str) -> bool: ...

    @abstractmethod
    async def list_prs_with_review_requests(self, login: str) -> List[PrRef]: ...

    @abstractmethod
    async def comment_on_pr(self, number: int, text: str) -> None: ...

    @abstractmethod
    async def get_pr_body(self, number: int) -> str: ...

    @abstractmethod
    async def list_pr_comments(self, number: int) -> List[str]: ...

    @abstractmethod
    async def set_draft(self, number: int, draft: bool) -> None: ...

    @abstractmethod
    async def merge_pr(self, number: int, *, merge_method: str = "merge") -> None:
        """Merge a PR/MR. Raises :class:`MergeBlocked` if the forge refuses."""

    @abstractmethod
    async def get_ci_status(self, number: int) -> Literal["passing", "failing", "pending", "none"]:
        """Return a forge-uniform CI status for the PR/MR's latest pipeline."""

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    @abstractmethod
    async def add_labels(self, kind: str, number: int, labels: Iterable[str]) -> None: ...

    @abstractmethod
    async def remove_labels(self, kind: str, number: int, labels: Iterable[str]) -> None: ...

    @abstractmethod
    async def ensure_pipeline_labels(self) -> None: ...

    # ------------------------------------------------------------------
    # Transitions (shared, non-abstract — every adapter behaves the same)
    # ------------------------------------------------------------------

    async def transition_issue(
        self, number: int, *, from_label: Optional[str], to_label: str,
    ) -> None:
        """Swap a workflow label on an issue."""
        if from_label is not None:
            await self.remove_labels("issue", number, [from_label])
        await self.add_labels("issue", number, [to_label])

    async def transition_pr(
        self, number: int, *, from_label: Optional[str], to_label: str,
    ) -> None:
        """Swap a workflow label on a PR/MR."""
        if from_label is not None:
            await self.remove_labels("pr", number, [from_label])
        await self.add_labels("pr", number, [to_label])

    async def claim_with_batch(self, kind: str, number: int) -> Optional[str]:
        """Claim an issue/PR via the ``~batch:<sha>`` lock label.

        TOCTOU-safe: after adding our label, re-fetches and yields if another
        worker added a competing ``~batch:`` label between read and write.
        Returns the batch id on success, ``None`` if the lock could not be
        acquired (already held by another worker).
        """
        from deile.orchestration.pipeline.labels import (is_batch_label,
                                                         make_batch_label)

        if kind not in ("issue", "pr"):
            raise ValueError(f"kind must be 'issue' or 'pr', got {kind!r}")

        async def _fetch_current():
            if kind == "issue":
                return await self.get_issue(number)
            return await self.get_pr(number)

        current = await _fetch_current()
        if current is None:
            return None
        if current.batch_id is not None:
            return None

        batch_id = compute_batch_id_for_number(kind, number)
        label = make_batch_label(batch_id)
        await self._ensure_label(label, color="d73a4a", description="Pipeline batch lock")
        await self.add_labels(kind, number, [label])

        after = await _fetch_current()
        if after is None:
            return None
        foreign = [lb for lb in after.labels if is_batch_label(lb) and lb != label]
        if foreign:
            logger.warning(
                "claim_with_batch: TOCTOU race detected on %s #%d; "
                "foreign labels=%s; removing our label and yielding",
                kind, number, foreign,
            )
            try:
                await self.remove_labels(kind, number, [label])
            except ForgeCommandError as exc:
                logger.warning(
                    "claim_with_batch: could not remove our label after race: %s", exc
                )
            return None

        return batch_id

    async def clear_batch_label(self, kind: str, number: int) -> None:
        """Remove every ``~batch:*`` label from the target (post-merge cleanup).

        Best-effort: errors are logged at WARNING but never raised. Mirrors
        the legacy behaviour from ``GitHubClient.clear_batch_label``.
        """
        from deile.orchestration.pipeline.labels import BATCH_LABEL_PREFIX

        if kind == "issue":
            try:
                current = await self.get_issue(number)
            except ForgeCommandError as exc:
                logger.warning("clear_batch_label: could not fetch %s #%d: %s", kind, number, exc)
                return
        elif kind == "pr":
            current = await self.get_pr(number)
            if current is None:
                return
        else:
            raise ValueError(f"kind must be 'issue' or 'pr', got {kind!r}")

        batch_labels = [lb for lb in current.labels if lb.startswith(BATCH_LABEL_PREFIX)]
        if not batch_labels:
            return
        try:
            await self.remove_labels(kind, number, batch_labels)
            logger.debug("cleared batch labels %s from %s #%d", batch_labels, kind, number)
        except ForgeCommandError as exc:
            logger.warning("clear_batch_label: remove failed for %s #%d: %s", kind, number, exc)

    # ------------------------------------------------------------------
    # Comments / search
    # ------------------------------------------------------------------

    @abstractmethod
    async def list_issue_comments_since(self, since: datetime) -> List[CommentRef]: ...

    @abstractmethod
    async def list_pr_review_comments_since(self, since: datetime) -> List[CommentRef]: ...

    @abstractmethod
    async def search_items_mentioning(
        self, query: str, *, limit: int = 50
    ) -> Tuple[List[IssueRef], List[PrRef]]: ...

    # ------------------------------------------------------------------
    # Repo metadata
    # ------------------------------------------------------------------

    @abstractmethod
    async def default_branch(self) -> str:
        """Return the project's default branch name (e.g. ``main``).

        Cached in ``self._config.default_branch`` after the first call.
        """

    # ------------------------------------------------------------------
    # Internal — subclasses implement label creation per forge
    # ------------------------------------------------------------------

    @abstractmethod
    async def _ensure_label(self, name: str, *, color: str, description: str) -> None:
        """Create the label if it does not already exist (idempotent)."""

    # ------------------------------------------------------------------
    # URL helpers (delegate to ForgeConfig — exposed here for ergonomics)
    # ------------------------------------------------------------------

    def web_issue_url(self, number: int) -> str:
        return self._config.web_issue_url(number)

    def web_pr_url(self, number: int) -> str:
        return self._config.web_pr_url(number)

    # ------------------------------------------------------------------
    # Internal helper shared by every concrete forge's list operations
    # ------------------------------------------------------------------

    async def _list_refs(
        self,
        *args: str,
        factory: Callable[[dict], Any],
        log_label: Optional[str] = None,
    ) -> list:
        """Run a forge CLI list command and map each JSON item via *factory*.

        Centralizes the ``run_checked → json.loads → [factory(item) ...]``
        pattern. When ``log_label`` is given, :class:`ForgeCommandError` is
        logged at WARNING and an empty list is returned; otherwise the error
        propagates (the claim/triage stages rely on that).
        """
        import json

        try:
            out = await self._run_checked(*args)
        except ForgeCommandError as exc:
            if log_label is None:
                raise
            logger.warning("%s failed: %s", log_label, exc)
            return []
        return [factory(item) for item in json.loads(out or "[]")]


# ---------------------------------------------------------------------------
# Helpers shared by adapters
# ---------------------------------------------------------------------------


def discover_cli(cli_name: str) -> str:
    """Locate ``cli_name`` on ``$PATH`` or raise :class:`ForgeCliNotFound`.

    Used by both adapters at construction time so a missing CLI fails fast.
    """
    path = shutil.which(cli_name)
    if not path:
        raise ForgeCliNotFound(
            f"the {cli_name!r} CLI binary is required but was not found on PATH. "
            f"Install it (see CLAUDE.md → 'Forge — GitHub e GitLab') and retry."
        )
    return path


__all__ = [
    "ForgeKind",
    "ForgeConfig",
    "ForgeClient",
    "ForgeError",
    "ForgeConfigError",
    "ForgeDetectionError",
    "ForgeCliNotFound",
    "ForgeCommandError",
    "MergeBlocked",
    "MergeBlockedByPipeline",
    "discover_cli",
]
