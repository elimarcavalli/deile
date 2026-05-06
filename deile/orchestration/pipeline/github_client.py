"""Async wrapper around the `gh` CLI for issue/PR/label operations.

The autonomous pipeline does not need (or want) a full GitHub-API client —
``gh`` is already authenticated locally, and the operations are simple. This
module wraps the relevant subcommands behind an async interface so the polling
loop stays non-blocking.

Each public function returns plain dicts (parsed JSON). Errors raise
:class:`GhCommandError` carrying stdout/stderr for diagnostics.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from deile.core.exceptions import DEILEError
from deile.orchestration.pipeline.labels import (LABEL_COLORS,
                                                 LABEL_DESCRIPTIONS,
                                                 REVIEW_LABELS,
                                                 WORKFLOW_LABELS,
                                                 batch_id_from_label,
                                                 is_batch_label,
                                                 make_batch_label)

logger = logging.getLogger(__name__)


class GhCommandError(DEILEError):
    """Raised when the `gh` CLI exits non-zero."""

    def __init__(self, cmd: Sequence[str], returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(f"gh {' '.join(cmd[1:])} failed ({returncode}): {stderr.strip()[:300]}")
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class IssueRef:
    number: int
    title: str
    url: str
    labels: Tuple[str, ...]
    body: str = ""
    state: str = "open"

    @property
    def batch_id(self) -> Optional[str]:
        for label in self.labels:
            if is_batch_label(label):
                return batch_id_from_label(label)
        return None


@dataclass(frozen=True)
class PrRef:
    number: int
    title: str
    url: str
    labels: Tuple[str, ...]
    head_ref: str = ""
    base_ref: str = "main"
    state: str = "open"
    is_draft: bool = False

    @property
    def batch_id(self) -> Optional[str]:
        for label in self.labels:
            if is_batch_label(label):
                return batch_id_from_label(label)
        return None


def compute_batch_id(title: str) -> str:
    """SHA-8 of the trimmed title — matches the format described in #87."""
    digest = hashlib.sha256(title.strip().encode("utf-8")).hexdigest()
    return digest[:8]


class GitHubClient:
    """Thin async wrapper around `gh` for the pipeline."""

    def __init__(self, repo: str, *, gh_path: Optional[str] = None) -> None:
        if "/" not in repo:
            raise ValueError(f"repo must be 'owner/name', got {repo!r}")
        self.repo = repo
        self._gh = gh_path or shutil.which("gh") or "gh"

    # -- low-level subprocess plumbing --------------------------------

    async def _run(self, *args: str, capture_stdout: bool = True) -> Tuple[int, str, str]:
        cmd = [self._gh, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE if capture_stdout else None,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        return proc.returncode or 0, stdout, stderr

    async def _run_checked(self, *args: str) -> str:
        rc, out, err = await self._run(*args)
        if rc != 0:
            raise GhCommandError(args, rc, out, err)
        return out

    # -- issues -------------------------------------------------------

    async def list_issues_with_label(self, label: str, *, limit: int = 50) -> List[IssueRef]:
        """Return open issues having ``label`` (and not having any later-stage workflow label)."""
        out = await self._run_checked(
            "issue", "list",
            "--repo", self.repo,
            "--state", "open",
            "--label", label,
            "--limit", str(limit),
            "--json", "number,title,url,labels,body,state",
        )
        data = json.loads(out or "[]")
        return [
            IssueRef(
                number=item["number"],
                title=item["title"],
                url=item["url"],
                labels=tuple(lab["name"] for lab in item.get("labels", [])),
                body=item.get("body", "") or "",
                state=item.get("state", "open"),
            )
            for item in data
        ]

    async def get_issue(self, number: int) -> IssueRef:
        out = await self._run_checked(
            "issue", "view", str(number),
            "--repo", self.repo,
            "--json", "number,title,url,labels,body,state",
        )
        item = json.loads(out)
        return IssueRef(
            number=item["number"],
            title=item["title"],
            url=item["url"],
            labels=tuple(lab["name"] for lab in item.get("labels", [])),
            body=item.get("body", "") or "",
            state=item.get("state", "open"),
        )

    async def get_pr(self, number: int) -> Optional[PrRef]:
        """Fetch a single PR by number; returns None if not found / not open."""
        try:
            out = await self._run_checked(
                "pr", "view", str(number),
                "--repo", self.repo,
                "--json", "number,title,url,labels,headRefName,baseRefName,state,isDraft",
            )
        except GhCommandError:
            return None
        item = json.loads(out)
        if item.get("state", "open").lower() not in ("open",):
            return None
        return PrRef(
            number=item["number"],
            title=item["title"],
            url=item["url"],
            labels=tuple(lab["name"] for lab in item.get("labels", [])),
            head_ref=item.get("headRefName", ""),
            base_ref=item.get("baseRefName", "main"),
            state=item.get("state", "open"),
            is_draft=bool(item.get("isDraft", False)),
        )

    # -- pull requests ------------------------------------------------

    async def list_open_prs(self, *, limit: int = 50) -> List[PrRef]:
        out = await self._run_checked(
            "pr", "list",
            "--repo", self.repo,
            "--state", "open",
            "--limit", str(limit),
            "--json", "number,title,url,labels,headRefName,baseRefName,state,isDraft",
        )
        data = json.loads(out or "[]")
        return [
            PrRef(
                number=item["number"],
                title=item["title"],
                url=item["url"],
                labels=tuple(lab["name"] for lab in item.get("labels", [])),
                head_ref=item.get("headRefName", ""),
                base_ref=item.get("baseRefName", "main"),
                state=item.get("state", "open"),
                is_draft=bool(item.get("isDraft", False)),
            )
            for item in data
        ]

    # -- labels -------------------------------------------------------

    async def add_labels(self, kind: str, number: int, labels: Iterable[str]) -> None:
        labels_list = list(labels)
        if not labels_list:
            return
        await self._run_checked(
            kind, "edit", str(number),
            "--repo", self.repo,
            "--add-label", ",".join(labels_list),
        )

    async def remove_labels(self, kind: str, number: int, labels: Iterable[str]) -> None:
        labels_list = list(labels)
        if not labels_list:
            return
        await self._run_checked(
            kind, "edit", str(number),
            "--repo", self.repo,
            "--remove-label", ",".join(labels_list),
        )

    async def transition(
        self,
        kind: str,
        number: int,
        *,
        from_label: Optional[str],
        to_label: str,
    ) -> None:
        """Swap a workflow label on an issue or PR (kind='issue'|'pr')."""
        if from_label is not None:
            await self.remove_labels(kind, number, [from_label])
        await self.add_labels(kind, number, [to_label])

    async def transition_issue(
        self, number: int, *, from_label: Optional[str], to_label: str,
    ) -> None:
        await self.transition("issue", number, from_label=from_label, to_label=to_label)

    async def transition_pr(
        self, number: int, *, from_label: Optional[str], to_label: str,
    ) -> None:
        await self.transition("pr", number, from_label=from_label, to_label=to_label)

    async def claim_with_batch(
        self,
        kind: str,
        number: int,
        title: str,
    ) -> Optional[str]:
        """Try to claim an issue/PR by attaching a batch lock label.

        Returns the batch_id on success, or None if the issue already has a
        ``~batch:`` label (someone else picked it up).
        """
        # Re-read the latest labels — list-then-add is racy across runners but
        # acceptable for a single-instance pipeline (the typical deployment).
        if kind == "issue":
            current = await self.get_issue(number)
        elif kind == "pr":
            current = await self.get_pr(number)
            if current is None:
                return None
        else:
            raise ValueError(f"kind must be 'issue' or 'pr', got {kind!r}")
        if current.batch_id is not None:
            return None
        batch_id = compute_batch_id(title)
        label = make_batch_label(batch_id)
        await self._ensure_label(label, color="d73a4a", description="Pipeline batch lock")
        await self.add_labels(kind, number, [label])
        return batch_id

    async def _ensure_label(self, name: str, *, color: str, description: str) -> None:
        """Create label if it doesn't exist; ignore "already exists" errors."""
        rc, _, err = await self._run(
            "label", "create", name,
            "--repo", self.repo,
            "--color", color,
            "--description", description,
        )
        if rc != 0 and "already exists" not in err.lower():
            logger.debug("ensure_label %s: rc=%d err=%s", name, rc, err.strip()[:200])

    async def ensure_pipeline_labels(self) -> None:
        """Create all pipeline-managed labels on the repo if they don't exist."""
        async def _create_one(label: str) -> None:
            color = LABEL_COLORS.get(label, "ededed")
            description = LABEL_DESCRIPTIONS.get(label, "Pipeline-managed label")
            rc, _, _ = await self._run(
                "label", "create", label,
                "--repo", self.repo,
                "--color", color,
                "--description", description,
            )
            # rc != 0 typically means "already exists"; we ignore that case.
            if rc != 0:
                logger.debug("label %s already exists or could not be created", label)

        await asyncio.gather(*[_create_one(label) for label in (*WORKFLOW_LABELS, *REVIEW_LABELS)])

    async def comment_on_issue(self, number: int, text: str) -> None:
        await self._run_checked(
            "issue", "comment", str(number),
            "--repo", self.repo,
            "--body", text,
        )

    async def comment_on_pr(self, number: int, text: str) -> None:
        await self._run_checked(
            "pr", "comment", str(number),
            "--repo", self.repo,
            "--body", text,
        )
