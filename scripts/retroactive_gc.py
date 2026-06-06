"""Retroactive GC script — strips transient labels from legacy closed items (issue #590).

Usage:
    python3 scripts/retroactive_gc.py [--dry-run] [--audit-orphan-batch-labels]
    python3 scripts/retroactive_gc.py --help

Flags:
    --dry-run                    List planned operations without mutating labels.
    --audit-orphan-batch-labels  Find ~batch:<sha8> labels with no open items and
                                 delete them from the repo label definitions.
    --limit N                    Max items to process (default: 200).
    --checkpoint FILE            Checkpoint JSON for resume (default: retroactive_gc_checkpoint.json).

Rollback: not supported. GitHub has no label-history API. To recover from an
accidental run, re-add labels manually via `gh issue edit <N> --add-label <label>`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import List

logger = logging.getLogger("retroactive_gc")

_WORKFLOW_PREFIX = "~workflow:"
_REVIEW_PREFIX = "~review:"
_BY_PREFIX = "~by:"
_BATCH_PREFIX = "~batch:"
_ATTEMPT_PREFIX = "~attempt:"
_REFINE_PREFIX = "~refine:"

_ISSUE_PRESERVE_WORKFLOW = {"~workflow:decomposta", "~workflow:concluida"}


def _should_strip_from_issue(label: str) -> bool:
    if label.startswith(_WORKFLOW_PREFIX):
        return label not in _ISSUE_PRESERVE_WORKFLOW
    if label.startswith(_BY_PREFIX):
        return True
    if label.startswith(_BATCH_PREFIX):
        return True
    if label.startswith(_ATTEMPT_PREFIX):
        return True
    if label.startswith(_REFINE_PREFIX):
        return True
    if label in ("~mention:feita", "refinar"):
        return True
    return False


def _should_strip_from_pr(label: str) -> bool:
    if label.startswith(_REVIEW_PREFIX):
        return True
    if label.startswith(_BY_PREFIX):
        return True
    if label.startswith(_BATCH_PREFIX):
        return True
    if label.startswith(_ATTEMPT_PREFIX):
        return True
    if label.startswith(_WORKFLOW_PREFIX):
        return True
    if label == "~follow-ups:processed":
        return True
    return False


_MUTATION_TIMESTAMPS: List[float] = []
_RATE_LIMIT = 100


def _rate_limit_wait() -> float:
    """Return seconds to sleep to stay within 100 mutations/min."""
    now = time.monotonic()
    window = [t for t in _MUTATION_TIMESTAMPS if now - t < 60.0]
    if len(window) >= _RATE_LIMIT:
        oldest = min(window)
        return max(0.0, 60.0 - (now - oldest) + 0.1)
    return 0.0


def _record_mutation() -> None:
    now = time.monotonic()
    _MUTATION_TIMESTAMPS.append(now)
    if len(_MUTATION_TIMESTAMPS) > 200:
        del _MUTATION_TIMESTAMPS[:100]


def _load_checkpoint(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"processed": [], "last_item": None}


def _save_checkpoint(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


async def run_retroactive_gc(
    forge,
    *,
    dry_run: bool = False,
    limit: int = 200,
    checkpoint_path: Path = Path("retroactive_gc_checkpoint.json"),
) -> dict:
    """Run retroactive GC on closed issues in ~workflow:em_pr.

    Args:
        forge: ForgeClient instance.
        dry_run: If True, compute ops but do not call remove_labels/add_labels.
        limit: Maximum number of items to process.
        checkpoint_path: Path to the resume checkpoint file.

    Returns:
        dict with keys: processed, noop, errors, dry_run.
    """
    from deile.orchestration.pipeline.gc import GCOnOpenItemError, run_terminal_gc

    checkpoint = _load_checkpoint(checkpoint_path)
    processed_set = set(checkpoint.get("processed") or [])

    stats = {"processed": 0, "noop": 0, "errors": 0, "dry_run": dry_run}

    try:
        em_pr_issues = await forge.list_issues_with_label("~workflow:em_pr", limit=limit)
    except Exception:
        em_pr_issues = []

    items_to_process = []
    for issue in em_pr_issues:
        try:
            current = await forge.get_issue(issue.number)
            if current and current.state == "closed":
                items_to_process.append(("issue", issue.number, "closed"))
        except Exception:
            pass

    count = 0
    for item_type, number, state in items_to_process:
        if count >= limit:
            break
        key = f"{item_type}:{number}"
        if key in processed_set:
            stats["noop"] += 1
            continue

        if dry_run:
            logger.info("[DRY-RUN] would GC %s #%d (state=%s)", item_type, number, state)
            stats["processed"] += 1
            count += 1
            continue

        wait = _rate_limit_wait()
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            result = await run_terminal_gc(forge, item_type, number, state)
            if result == "noop":
                stats["noop"] += 1
            else:
                stats["processed"] += 1
                _record_mutation()
            processed_set.add(key)
            checkpoint["processed"] = list(processed_set)
            checkpoint["last_item"] = key
            _save_checkpoint(checkpoint_path, checkpoint)
        except GCOnOpenItemError:
            stats["noop"] += 1
        except Exception as exc:
            logger.warning("retroactive_gc: failed for %s #%d: %s", item_type, number, exc)
            stats["errors"] += 1
        count += 1

    return stats


async def audit_orphan_batch_labels(
    forge,
    *,
    dry_run: bool = False,
) -> dict:
    """Find ~batch:<sha8> label definitions with no remaining references and delete them.

    Args:
        forge: ForgeClient instance.
        dry_run: If True, list but do not delete.

    Returns:
        dict with keys: orphans_found, deleted, dry_run.
    """
    stats = {"orphans_found": 0, "deleted": 0, "dry_run": dry_run}

    try:
        all_labels = await forge.list_repo_labels()
    except Exception as exc:
        logger.error("audit_orphan_batch_labels: could not list repo labels: %s", exc)
        return stats

    batch_labels = [lb for lb in all_labels if lb.startswith("~batch:")]
    logger.info("Found %d ~batch:* label definitions", len(batch_labels))

    for label_name in batch_labels:
        try:
            items = await forge.list_issues_with_label(label_name, limit=1)
        except Exception:
            items = []

        if items:
            logger.debug("Keeping %s — has %d reference(s)", label_name, len(items))
            continue

        stats["orphans_found"] += 1
        if dry_run:
            logger.info("[DRY-RUN] would delete orphan label: %s", label_name)
            continue

        wait = _rate_limit_wait()
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            await forge.delete_label(label_name)
            _record_mutation()
            stats["deleted"] += 1
            logger.info("Deleted orphan label: %s", label_name)
        except Exception as exc:
            logger.warning("Could not delete label %s: %s", label_name, exc)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retroactive GC: strip transient labels from closed issues/PRs (issue #590).",
        epilog=(
            "Rollback: not supported. GitHub has no label-history API. "
            "Re-add removed labels manually with 'gh issue edit <N> --add-label <label>'."
        ),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="List operations without mutating labels (0 API mutations)")
    parser.add_argument("--audit-orphan-batch-labels", action="store_true",
                        help="Find and delete orphan ~batch:<sha8> label definitions")
    parser.add_argument("--limit", type=int, default=200, metavar="N",
                        help="Max items to process (default: 200)")
    parser.add_argument("--checkpoint", type=Path, default=Path("retroactive_gc_checkpoint.json"),
                        metavar="FILE", help="Checkpoint file for resume support")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    async def _run():
        from deile.orchestration.forge import build_forge
        from deile.config.settings import get_settings
        settings = get_settings()
        forge = build_forge(settings.repo, settings)

        if args.audit_orphan_batch_labels:
            result = await audit_orphan_batch_labels(forge, dry_run=args.dry_run)
        else:
            result = await run_retroactive_gc(
                forge,
                dry_run=args.dry_run,
                limit=args.limit,
                checkpoint_path=args.checkpoint,
            )
        print(json.dumps(result, indent=2))

    asyncio.run(_run())


if __name__ == "__main__":
    main()
