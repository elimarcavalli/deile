"""WorktreeTool — LLM-callable interface to worktree management.

Exposes :class:`WorktreeManager` (``deile/orchestration/pipeline/worktree_manager.py``)
to the LLM so users can create, list and remove branch worktrees without
dropping to the shell.

Actions
-------
ensure_main
    Ensure ``.worktrees/main`` exists and is up-to-date. Returns the path.
create
    Create a per-branch worktree under ``.worktrees/<subdir>/<branch>`` (or
    ``.worktrees/<branch>`` when *subdir* is omitted). Returns the path + branch.
list
    List all entries discovered under ``.worktrees/``. Returns an array of
    ``{path, branch, base_repo}`` objects.
remove
    Remove ``.worktrees/<subdir>/<branch>`` (or ``.worktrees/<branch>``) after
    safety checks. Uses ``shutil.rmtree``.

Schema note
-----------
``parameters`` is a **JSON Schema object** (``{"type": "object", "properties": {…},
"required": […]}``) — *not* a raw dict.  The regression test
``test_schema_is_json_schema_object`` explicitly guards this shape.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional

from deile.orchestration.pipeline.worktree_manager import (WorktreeError,
                                                           WorktreeManager)
from deile.tools._pipeline_paths import resolve_base_path as _resolve_base_path
from deile.tools.base import (SecurityLevel, Tool, ToolCategory, ToolContext,
                              ToolResult, ToolSchema)

logger = logging.getLogger(__name__)

_PROTECTED_SUBDIRS = {"main"}  # worktrees that must never be removed


class WorktreeTool(Tool):
    """Create, list and remove branch worktrees via the LLM."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="worktree",
                description=(
                    "Manage isolated branch worktrees under .worktrees/. "
                    "Use action='ensure_main' to initialise or refresh the shared clean "
                    "main clone; action='create' to set up a new branch sandbox; "
                    "action='list' to see existing worktrees; action='remove' to delete "
                    "a branch worktree (safety-checked — cannot remove main)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["ensure_main", "create", "list", "remove"],
                            "description": "Worktree operation to perform.",
                        },
                        "branch": {
                            "type": "string",
                            "description": (
                                "Branch name. Required for 'create' and 'remove'."
                            ),
                        },
                        "subdir": {
                            "type": "string",
                            "description": (
                                "Optional namespace subdirectory under .worktrees/ "
                                "(e.g. 'agents', 'feat'). Defaults to no subdirectory."
                            ),
                        },
                        "base_path": {
                            "type": "string",
                            "description": (
                                "Absolute path to the base git repository. "
                                "Defaults to auto-detected repo root."
                            ),
                        },
                    },
                    "required": ["action"],
                },
                required=["action"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.SYSTEM,
            )
        )


    async def execute(self, context: ToolContext) -> ToolResult:  # noqa: C901
        action = (context.parsed_args.get("action") or "").strip().lower()
        valid_actions = {"ensure_main", "create", "list", "remove"}
        if action not in valid_actions:
            return ToolResult.error_result(
                message=(
                    f"action must be one of {sorted(valid_actions)!r}, got {action!r}"
                ),
                error_code="INVALID_ACTION",
            )

        base_path_raw: Optional[str] = context.parsed_args.get("base_path")
        branch: Optional[str] = context.parsed_args.get("branch")
        subdir: Optional[str] = context.parsed_args.get("subdir") or None

        try:
            base_path = _resolve_base_path(base_path_raw)

            if action == "ensure_main":
                return await self._ensure_main(base_path, subdir)

            if action == "create":
                if not branch:
                    return ToolResult.error_result(
                        message="'branch' is required for action='create'",
                        error_code="MISSING_BRANCH",
                    )
                return await self._create(base_path, branch, subdir)

            if action == "list":
                return await self._list(base_path)

            if action == "remove":
                if not branch:
                    return ToolResult.error_result(
                        message="'branch' is required for action='remove'",
                        error_code="MISSING_BRANCH",
                    )
                return await self._remove(base_path, branch, subdir)

        except WorktreeError as exc:
            return ToolResult.error_result(
                message=str(exc),
                error=exc,
                error_code="WORKTREE_ERROR",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("worktree %s failed: %s", action, exc, exc_info=True)
            return ToolResult.error_result(
                message=f"worktree {action} failed: {type(exc).__name__}: {exc}",
                error=exc,
                error_code="WORKTREE_OP_FAILED",
            )

        # Unreachable — all branches return above.  Satisfies type-checker.
        return ToolResult.error_result(  # pragma: no cover
            message="internal error: unhandled action",
            error_code="INTERNAL",
        )

    # ------------------------------------------------------------------
    # private action helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _ensure_main(base_path: Path, subdir: Optional[str]) -> ToolResult:
        mgr = WorktreeManager(base_path, subdir=subdir)
        path = await mgr.ensure_main()
        return ToolResult.success_result(
            data={"path": str(path)},
            message=f"main worktree ready at {path}",
        )

    @staticmethod
    async def _create(
        base_path: Path, branch: str, subdir: Optional[str]
    ) -> ToolResult:
        mgr = WorktreeManager(base_path, subdir=subdir)
        wt = await mgr.create_branch_worktree(branch)
        return ToolResult.success_result(
            data={
                "path": str(wt.path),
                "branch": wt.branch,
                "base_repo": str(wt.base_repo),
            },
            message=f"worktree for {branch!r} ready at {wt.path}",
        )

    @staticmethod
    async def _list(base_path: Path) -> ToolResult:
        """Scan ``.worktrees/`` and return entries that look like git repos."""
        worktrees_dir = base_path / ".worktrees"
        if not worktrees_dir.is_dir():
            return ToolResult.success_result(
                data={"worktrees": []},
                message="no .worktrees/ directory found",
            )

        entries = []
        for item in sorted(worktrees_dir.iterdir()):
            if not item.is_dir():
                continue
            if (item / ".git").exists():
                entries.append({"path": str(item), "branch": item.name, "base_repo": str(base_path)})
            else:
                # One level deeper for subdir-namespaced worktrees (e.g. .worktrees/<monitor>/<branch>)
                for sub in sorted(item.iterdir()):
                    if sub.is_dir() and (sub / ".git").exists():
                        entries.append({"path": str(sub), "branch": sub.name, "base_repo": str(base_path)})

        return ToolResult.success_result(
            data={"worktrees": entries},
            message=f"found {len(entries)} worktree(s)",
        )

    @staticmethod
    async def _remove(
        base_path: Path, branch: str, subdir: Optional[str]
    ) -> ToolResult:
        """Remove a branch worktree after safety checks."""
        # Safety: refuse to remove the shared clean main clone.
        if branch in _PROTECTED_SUBDIRS:
            return ToolResult.error_result(
                message=(
                    f"cannot remove protected worktree {branch!r} — "
                    "it is the shared clean main clone used by all pipelines"
                ),
                error_code="REMOVE_PROTECTED",
            )

        worktrees_dir = base_path / ".worktrees"
        if subdir:
            target = worktrees_dir / subdir / branch
        else:
            target = worktrees_dir / branch

        if not target.exists():
            return ToolResult.error_result(
                message=f"worktree path does not exist: {target}",
                error_code="NOT_FOUND",
            )

        await asyncio.to_thread(shutil.rmtree, str(target))
        return ToolResult.success_result(
            data={"removed": str(target)},
            message=f"removed worktree at {target}",
        )
