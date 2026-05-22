"""Stage handlers for the autonomous pipeline.

This module holds the seven stage handlers that :class:`PipelineMonitor`
drives on each tick / scheduled run. They were extracted from ``monitor.py``
so the monitor keeps a single responsibility: lifecycle + scheduling.

Each handler is a free ``async def`` that receives the monitor as its first
argument and operates on its collaborators (``github``, ``claude``,
``notifier``, ``_stats``, ``config``). The logic, logging, error handling and
return values are preserved verbatim from the original methods — this module
only *moves* code, it does not rewrite behaviour.

To avoid an import cycle, this module does **not** import
:class:`PipelineMonitor` at module scope; the monitor is received as a
parameter and only type-hinted under ``TYPE_CHECKING``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from deile.orchestration.pipeline.constants import PIPELINE_MSG_TRUNCATE_CHARS
from deile.orchestration.pipeline.follow_up_detector import detect_follow_ups
from deile.orchestration.pipeline.github_client import (CommentRef,
                                                        GhCommandError)
from deile.orchestration.pipeline.labels import (REVIEW_CONCLUDED,
                                                 REVIEW_IN_PROGRESS,
                                                 REVIEW_PENDING,
                                                 WORKFLOW_IMPLEMENTING,
                                                 WORKFLOW_NEW, WORKFLOW_PR,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from deile.orchestration.pipeline.monitor import PipelineMonitor

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"https://github\.com/[^\s\"'<>]+/pull/\d+", re.IGNORECASE)


async def _record_gh_error(
    monitor: "PipelineMonitor",
    description: str,
    exc: Exception,
    *,
    notifier_label: Optional[str] = None,
) -> None:
    """Bump ``errors`` + ``gh_errors`` counters, log, optionally notify.

    Centralises the four-line pattern (counters + logger.error + optional
    notifier) that recurred ~10 times across the stage handlers. The
    ``description`` becomes the log prefix (``"<description>: <exc>"``);
    when ``notifier_label`` is given, a Discord error notification is
    posted with ``str(exc)`` as detail.

    The function is ``async`` because the optional notifier path
    (``monitor.notifier.error(...)``) is async; the counter bump and log
    call themselves are sync.  Keeping the signature uniform lets every
    call-site simply ``await`` it, whether or not it ends up notifying.
    """
    monitor._stats.errors += 1
    monitor._stats.gh_errors += 1
    logger.error("%s: %s", description, exc)
    if notifier_label is not None:
        await monitor.notifier.error(notifier_label, str(exc))


_CLASSIFY_COMMENT = (
    f"🤖 **DEILE auto-classificação** — esta issue foi adicionada à fila do pipeline "
    f"autônomo (`{WORKFLOW_NEW}`).\n\n"
    f"Para excluir da fila, remova o label `{WORKFLOW_NEW}`."
)


# ----- stage 0: auto-classify new issues ---------------------------------

async def classify_new_issues(monitor: "PipelineMonitor") -> None:
    """Apply ``~workflow:nova`` to open issues that are eligible but unclassified.

    An issue is eligible when:
    - it has at least one label in ``config.classifiable_labels``
    - it has no label in ``config.classify_skip_labels``
    - it has no pipeline labels (nothing starting with ``~``)
    - it falls in this monitor's shard
    - body may be empty — we accept it and post a "fill the template" comment

    gap #6: Stage 0 now uses ``claim_with_batch`` to reduce the TOCTOU
    race window with parallel monitors.
    """
    try:
        issues = await monitor.github.list_unclassified_issues()
    except GhCommandError as exc:
        await _record_gh_error(
            monitor, "could not list unclassified issues (gh error)", exc,
            notifier_label="classify/list",
        )
        return
    except Exception as exc:  # noqa: BLE001 — JSON parse error etc.
        monitor._stats.errors += 1
        logger.error("could not list unclassified issues: %s", exc)
        return

    for issue in issues:
        # Defense-in-depth: never touch an issue that already has a pipeline label.
        if any(lb.startswith("~") for lb in issue.labels):
            continue
        if not any(lb in monitor.config.classifiable_labels for lb in issue.labels):
            continue
        if any(lb in monitor.config.classify_skip_labels for lb in issue.labels):
            continue
        if not monitor.identity.owns(issue.title):
            continue
        empty_body = not issue.body.strip()
        if empty_body:
            logger.info(
                "issue #%s has empty body; auto-classifying and requesting template fill",
                issue.number,
            )
        # Claim before labelling to reduce the TOCTOU window with parallel monitors.
        try:
            batch = await monitor.github.claim_with_batch("issue", issue.number, issue.title)
        except GhCommandError as exc:
            await _record_gh_error(
                monitor, f"auto-classify claim #{issue.number} failed", exc,
                notifier_label=f"auto-classify claim #{issue.number}",
            )
            continue
        if batch is None:
            logger.debug("issue #%s already claimed by another monitor; skipping", issue.number)
            continue
        try:
            await monitor.github.add_labels("issue", issue.number, [WORKFLOW_NEW])
        except GhCommandError as exc:
            await _record_gh_error(
                monitor, f"auto-classify label #{issue.number} failed", exc,
                notifier_label=f"auto-classify #{issue.number}",
            )
            continue
        except Exception as exc:  # noqa: BLE001 — best-effort, never abort loop
            monitor._stats.errors += 1
            logger.error("auto-classify label #%s failed: %s", issue.number, exc)
            await monitor.notifier.error(f"auto-classify #{issue.number}", f"{type(exc).__name__}: {exc}")
            continue
        # Release the classify claim so Stage 1 (review) can pick the issue up
        # via its own claim — review_one_new_issue only considers issues with
        # ``batch_id is None``. Mirrors classify_new_prs; without this the
        # auto-classify → review handoff deadlocks (the issue stays ~nova
        # forever, batch-locked). Best-effort: the ~workflow:nova label is
        # already applied, so a clear failure must not abort the loop.
        try:
            await monitor.github.clear_batch_label("issue", issue.number)
        except Exception as exc:  # noqa: BLE001 — label applied; clear is best-effort
            logger.warning("auto-classify: could not clear batch on #%s: %s", issue.number, exc)
        monitor._stats.issues_classified += 1
        logger.info("auto-classified issue #%s as %s", issue.number, WORKFLOW_NEW)
        await monitor.notifier.issue_auto_classified(issue.number, issue.title, issue.url)
        # Post the standard "added to pipeline" comment, optionally with template reminder
        if empty_body:
            comment = (
                f"🤖 **DEILE auto-classificação** — esta issue foi adicionada à fila do pipeline "
                f"(`{WORKFLOW_NEW}`) mas o **corpo está vazio**.\n\n"
                f"Por favor, preencha o template da issue para que a revisão automática "
                f"possa acontecer. Issues com corpo vazio serão processadas mas podem "
                f"gerar implementações incompletas.\n\n"
                f"Para excluir da fila, remova o label `{WORKFLOW_NEW}`."
            )
        else:
            comment = _CLASSIFY_COMMENT
        try:
            await monitor.github.comment_on_issue(issue.number, comment)
        except Exception as exc:  # noqa: BLE001 — comment is best-effort; label already applied
            logger.warning("auto-classify comment #%s failed (label applied): %s", issue.number, exc)


# ----- PR triage: classify open non-draft PRs with no pipeline labels ----

async def classify_new_prs(monitor: "PipelineMonitor") -> None:
    """Apply ``~review:pendente`` to open non-draft PRs that have no pipeline labels."""
    try:
        prs = await monitor.github.list_unclassified_prs()
    except GhCommandError as exc:
        await _record_gh_error(
            monitor, "could not list unclassified PRs (gh error)", exc,
            notifier_label="pr_triage/list",
        )
        return
    except Exception as exc:  # noqa: BLE001
        monitor._stats.errors += 1
        logger.error("could not list unclassified PRs: %s", exc)
        return

    for pr in prs:
        if any(lb.startswith("~") for lb in pr.labels):
            continue
        try:
            batch = await monitor.github.claim_with_batch("pr", pr.number, pr.title)
        except GhCommandError as exc:
            await _record_gh_error(
                monitor, f"pr_triage claim #{pr.number} failed", exc,
            )
            continue
        if batch is None:
            logger.debug("PR #%s already claimed; skipping pr_triage", pr.number)
            continue
        try:
            await monitor.github.add_labels("pr", pr.number, [REVIEW_PENDING])
        except GhCommandError as exc:
            await _record_gh_error(
                monitor, f"pr_triage label #{pr.number} failed", exc,
                notifier_label=f"pr_triage #{pr.number}",
            )
            continue
        # Release the batch claim so Stage 3 can pick this PR up via its own claim.
        await monitor.github.clear_batch_label("pr", pr.number)
        monitor._stats.prs_classified += 1
        logger.info("pr_triage: classified PR #%s with %s", pr.number, REVIEW_PENDING)
        await monitor.notifier.pr_auto_classified(pr.number, pr.title, pr.url)


# ----- mention handling: dispatch @deile-one comments to Claude ----------

async def process_mentions(monitor: "PipelineMonitor") -> None:
    """Poll issue/PR comments since cursor, dispatch @deile-one mentions to Claude."""
    since = monitor._load_mention_cursor()
    handle = monitor.config.mention_handle.lower()
    try:
        issue_comments = await monitor.github.list_issue_comments_since(since)
        pr_comments = await monitor.github.list_pr_review_comments_since(since)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mention poll failed: %s", exc)
        return
    all_comments: list[CommentRef] = issue_comments + pr_comments
    now = datetime.now(tz=timezone.utc)
    for ref in all_comments:
        if handle not in ref.body.lower():
            continue
        try:
            outcome = await monitor.implementer.mention(monitor, ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mention dispatch error for %s: %s", ref.html_url, exc)
            continue
        if not outcome.ok:
            logger.warning(
                "mention dispatch failed for %s: %s", ref.html_url, outcome.error
            )
            continue
        monitor._stats.mentions_processed += 1
        logger.info("mention processed: %s by @%s", ref.html_url, ref.author)
        await monitor.notifier.mention_processed(ref.html_url, ref.author)
    monitor._save_mention_cursor(now)


# ----- stage 1: review ---------------------------------------------------

async def review_one_new_issue(monitor: "PipelineMonitor") -> None:
    try:
        issues = await monitor.github.list_issues_with_label(WORKFLOW_NEW, limit=50)
    except GhCommandError as exc:
        await _record_gh_error(
            monitor, "could not list new issues (gh error)", exc,
            notifier_label="review/list",
        )
        return
    # Shard filter: only consider issues whose hash falls in our shard.
    target = next(
        (i for i in issues if i.batch_id is None and monitor.identity.owns(i.title)),
        None,
    )
    if target is None:
        return
    batch = await monitor.github.claim_with_batch("issue", target.number, target.title)
    if batch is None:
        return
    # Tag ownership so other monitors can identify who claimed this.
    await monitor.github.add_labels("issue", target.number, [monitor.identity.ownership_label()])
    await monitor.notifier.issue_picked_up(target.number, target.title, target.url)
    try:
        # Atomic: if review_callback or final transition fails, revert to WORKFLOW_NEW.
        await monitor.github.transition_issue(
            target.number, from_label=WORKFLOW_NEW, to_label=WORKFLOW_REVIEWING
        )
        review_failed = False
        try:
            if monitor._review_cb is not None:
                comment = await monitor._review_cb(target)
                if comment:
                    await monitor.github.comment_on_issue(target.number, comment)
            await monitor.github.transition_issue(
                target.number, from_label=WORKFLOW_REVIEWING, to_label=WORKFLOW_REVIEWED
            )
        except GhCommandError:
            monitor._stats.errors += 1
            monitor._stats.gh_errors += 1
            review_failed = True
            raise
        except Exception:  # noqa: BLE001
            review_failed = True
            raise
        finally:
            if review_failed:
                # Revert to WORKFLOW_NEW so the issue isn't stuck in em_revisao
                try:
                    await monitor.github.transition_issue(
                        target.number,
                        from_label=WORKFLOW_REVIEWING,
                        to_label=WORKFLOW_NEW,
                    )
                except Exception:  # noqa: BLE001 — rollback is best-effort
                    logger.warning(
                        "could not revert issue #%d from em_revisao to nova after review failure",
                        target.number,
                    )
    except Exception as exc:  # noqa: BLE001 — surface and continue
        logger.exception("review of #%s failed", target.number)
        await monitor.notifier.error(
            f"review issue #{target.number}", f"{type(exc).__name__}: {exc}"
        )
        return
    monitor._stats.issues_reviewed += 1
    await monitor.notifier.issue_reviewed(target.number, target.title, target.url)


# ----- stage 2: implement ------------------------------------------------

async def implement_one_reviewed_issue(monitor: "PipelineMonitor") -> None:
    try:
        issues = await monitor.github.list_issues_with_label(WORKFLOW_REVIEWED, limit=50)
    except GhCommandError as exc:
        await _record_gh_error(
            monitor, "could not list reviewed issues (gh error)", exc,
            notifier_label="implement/list",
        )
        return
    # Accept issues without ~batch: when the ownership label proves this monitor did the
    # review (e.g. operator manually promoted to ~workflow:revisada or batch label removed).
    ownership_label = monitor.identity.ownership_label()
    target = next(
        (
            i for i in issues
            if WORKFLOW_PR not in i.labels
            # Defense-in-depth: never re-pick an issue already claimed for
            # implementation. The list query is scoped to WORKFLOW_REVIEWED so a
            # cleanly-claimed issue already drops out, but this guards the edge
            # case of an issue transiently carrying both labels (partial
            # transition, operator mislabel) — the bug class behind #253.
            and WORKFLOW_IMPLEMENTING not in i.labels
            and monitor._this_monitor_owns(i)
            and (i.batch_id is not None or ownership_label in i.labels)
        ),
        None,
    )
    if target is None:
        return
    # Best-effort claim (sequential-tick lock) BEFORE any notification or work:
    # move the issue out of ~workflow:revisada and into ~workflow:em_implementacao.
    # The candidate query (list_issues_with_label) only returns
    # ~workflow:revisada issues, so once claimed the issue drops out of the set
    # for every LATER tick — which is what stops the SAME issue from being
    # re-picked across sequential ticks. NOTE: transition_issue is remove-then-add
    # over two REST calls (not a single atomic op), so two genuinely concurrent
    # monitors could still both observe ~workflow:revisada and double-claim;
    # multi-monitor safety relies on the PID lock + single-replica Recreate +
    # hash sharding of the shipped deile-pipeline deployment, not on this label
    # flip. Without this claim, an issue that never produces a PR (e.g. a
    # vague/meta issue the worker cannot implement) was re-selected and
    # re-dispatched on every tick, flooding the operator with duplicate
    # "Implementação iniciada" DMs.
    try:
        await monitor.github.transition_issue(
            target.number, from_label=WORKFLOW_REVIEWED, to_label=WORKFLOW_IMPLEMENTING
        )
    except GhCommandError as exc:
        await _record_gh_error(
            monitor, f"could not claim issue #{target.number} for implementation", exc,
            notifier_label=f"implement claim #{target.number}",
        )
        return
    branch = monitor.branch_for_issue(target.number)
    await monitor.notifier.implementation_started(target.number, target.title, branch)
    # Delegate the actual implementation to the configured strategy
    # (claude -p in a worktree, or a dispatch to the deile-worker). The
    # strategy returns a uniform outcome; label orchestration stays here.
    outcome = await monitor.implementer.implement(monitor, target)
    pr_url = _extract_pr_url(outcome.text)
    # A real failure (worker errored / unreachable): the issue is now parked in
    # ~workflow:em_implementacao (NOT reverted to revisada), so it will not be
    # re-picked. Ping the operator once; requeue is a deliberate human action
    # (move the label back to ~workflow:revisada). Auto-retry-forever was what
    # produced the duplicate-DM storm.
    if not outcome.ok:
        monitor._stats.errors += 1
        monitor._stats.claude_errors += 1
        err_detail = (outcome.error or "implementation failed")[:PIPELINE_MSG_TRUNCATE_CHARS]
        logger.error(
            "implement #%d failed: %s — parked in %s",
            target.number, err_detail, WORKFLOW_IMPLEMENTING,
        )
        await monitor.notifier.implementation_parked(target.number, err_detail)
        return
    # ok=True but no PR URL: the agent finished its turn without actually
    # opening a PR (common for vague/meta issues). Same treatment as a failure
    # — park in ~workflow:em_implementacao and notify once, never silently
    # retry (the silent retry kept the issue in revisada and re-fired the
    # "Implementação iniciada" DM every tick).
    if not pr_url:
        monitor._stats.errors += 1
        monitor._stats.claude_errors += 1
        logger.warning(
            "implement #%d: agent finished but produced no PR URL — parked in %s",
            target.number, WORKFLOW_IMPLEMENTING,
        )
        await monitor.notifier.implementation_parked(
            target.number, "o agente finalizou sem abrir PR"
        )
        return
    # Success: a real PR exists. Move ~workflow:em_implementacao → ~workflow:em_pr.
    try:
        await monitor.github.transition_issue(
            target.number, from_label=WORKFLOW_IMPLEMENTING, to_label=WORKFLOW_PR
        )
    except GhCommandError as exc:
        await _record_gh_error(
            monitor, f"could not transition issue #{target.number} to em_pr", exc,
        )
    monitor._stats.issues_implemented += 1
    await monitor.notifier.implementation_finished(target.number, pr_url)


# ----- stage 3: review PR ------------------------------------------------

async def review_one_open_pr(monitor: "PipelineMonitor") -> None:
    try:
        prs = await monitor.github.list_open_prs(limit=50)
    except GhCommandError as exc:
        await _record_gh_error(
            monitor, "could not list PRs (gh error)", exc,
            notifier_label="pr_review/list",
        )
        return
    # Scope stage 3 to PRs whose head branch belongs to THIS monitor
    # (so we never review a peer's PR). Default-identity monitors keep
    # the legacy behaviour: any PR with a matching head_ref or none.
    target = next(
        (
            pr
            for pr in prs
            if not pr.is_draft
            and REVIEW_CONCLUDED not in pr.labels
            and REVIEW_IN_PROGRESS not in pr.labels
            and pr.batch_id is None
            and monitor._owns_pr_branch(pr.head_ref, pr_number=pr.number)
        ),
        None,
    )
    if target is None:
        return
    batch = await monitor.github.claim_with_batch("pr", target.number, target.title)
    if batch is None:
        return
    # Tag ownership so other monitors can identify who claimed this PR —
    # mirrors the identical pattern in stage 1 for issues.
    await monitor.github.add_labels("pr", target.number, [monitor.identity.ownership_label()])
    await monitor.notifier.pr_picked_up(target.number, target.title, target.url)
    try:
        await monitor.github.transition_pr(
            target.number, from_label=REVIEW_PENDING, to_label=REVIEW_IN_PROGRESS
        )
    except GhCommandError:
        # ~review:pendente may not be set; that's ok.
        await monitor.github.add_labels("pr", target.number, [REVIEW_IN_PROGRESS])
    # Delegate the review/merge work to the configured strategy. The Claude
    # strategy checks out the branch in a worktree; the worker strategy clones
    # and runs ``gh pr checkout`` inside its own sandbox.
    outcome = await monitor.implementer.review(monitor, target)
    merged = outcome.ok and "merged" in outcome.text.lower()
    if not outcome.ok:
        monitor._stats.errors += 1
        monitor._stats.claude_errors += 1
        logger.error(
            "pr_review #%d failed: %s", target.number,
            (outcome.error or "review failed")[:PIPELINE_MSG_TRUNCATE_CHARS],
        )
    try:
        await monitor.github.transition_pr(
            target.number, from_label=REVIEW_IN_PROGRESS, to_label=REVIEW_CONCLUDED
        )
    except GhCommandError as exc:
        await _record_gh_error(
            monitor, f"could not transition PR #{target.number} to concluida", exc,
        )
    # Remove lock label so the PR doesn't accumulate an orphaned ~batch: forever.
    await monitor.github.clear_batch_label("pr", target.number)
    monitor._stats.prs_reviewed += 1
    await monitor.notifier.pr_reviewed(target.number, target.title, target.url, merged=merged)
    if merged and monitor.config.enable_follow_ups:
        await monitor._stage4_follow_ups(target.number, target.title, target.url)
    if merged and monitor._post_merge_cb is not None:
        try:
            await monitor._post_merge_cb(target.number, target.title, target.url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("post_merge_callback failed for PR #%d: %s", target.number, exc)


# ----- stage 4: follow-up issues from merged PR --------------------------

async def stage4_follow_ups(
    monitor: "PipelineMonitor", pr_number: int, pr_title: str, pr_url: str
) -> None:
    """Open GitHub issues for non-breaking follow-ups found in the merged PR.

    This stage is best-effort: any exception is logged but never propagates
    to the caller (stage 3 already finished successfully).
    """
    try:
        pr_body = await monitor.github.get_pr_body(pr_number)
        pr_comments = await monitor.github.list_pr_comments(pr_number)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage 4: could not fetch PR #%s content: %s", pr_number, exc)
        return

    follow_ups = detect_follow_ups(pr_body, pr_comments)
    if not follow_ups:
        logger.debug("stage 4: no follow-ups detected in PR #%s", pr_number)
        return

    opened: list[tuple[str, int]] = []
    skipped: list[tuple[str, str]] = []

    for fu in follow_ups:
        if fu.is_breaking:
            skipped.append((fu.title, "breaking change — requer revisão humana"))
            monitor._stats.follow_ups_skipped += 1
            continue
        issue_body = (
            f"{fu.title}\n\n"
            f"---\n\n"
            f"Origem: PR #{pr_number} — [{pr_title}]({pr_url})"
        )
        try:
            number = await monitor.github.create_issue(
                fu.title, issue_body, labels=["intent"]
            )
            if number:
                opened.append((fu.title, number))
                monitor._stats.follow_ups_opened += 1
            else:
                skipped.append((fu.title, "gh create_issue não retornou número"))
                monitor._stats.follow_ups_skipped += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("stage 4: create_issue %r failed: %s", fu.title[:60], exc)
            skipped.append((fu.title, str(exc)[:120]))
            monitor._stats.follow_ups_skipped += 1

    report = _render_follow_up_report(pr_number, opened, skipped)
    try:
        await monitor.github.comment_on_pr(pr_number, report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage 4: could not post follow-up report on PR #%s: %s", pr_number, exc)

    await monitor.notifier.follow_ups_processed(pr_number, len(opened), len(skipped))


# ----- standalone stage 4: follow_ups action -----------------------------

async def standalone_follow_ups(monitor: "PipelineMonitor") -> None:
    """Process follow-ups for recently merged PRs that haven't been processed yet.

    This is the standalone version of stage 4, invocable via the schedule
    without requiring a concurrent stage 3 run.  Idempotency is enforced by
    the ``~follow_ups:processed`` label: PRs that already have this label
    are skipped.
    """
    _PROCESSED_LABEL = "~follow_ups:processed"
    try:
        merged_prs = await monitor.github.list_recently_merged_prs()
    except Exception as exc:  # noqa: BLE001
        logger.warning("standalone follow_ups: could not list merged PRs: %s", exc)
        return

    for pr in merged_prs:
        if _PROCESSED_LABEL in pr.labels:
            continue
        await monitor._stage4_follow_ups(pr.number, pr.title, pr.url)
        try:
            await monitor.github.add_labels("pr", pr.number, [_PROCESSED_LABEL])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "standalone follow_ups: could not mark PR #%d processed: %s",
                pr.number, exc,
            )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _extract_pr_url(text: str) -> Optional[str]:
    """Return the last GitHub PR URL found in *text* (gap #14).

    Using the last match avoids picking up example URLs or log lines that
    appear earlier in the output before the actual PR URL that Claude outputs
    on the final line.
    """
    if not text:
        return None
    matches = _PR_URL_RE.findall(text)
    return matches[-1] if matches else None


def _render_follow_up_report(
    pr_number: int,
    opened: list[tuple[str, int]],
    skipped: list[tuple[str, str]],
) -> str:
    lines = [f"## 🤖 Stage 4 — Follow-ups detectados na PR #{pr_number}\n"]
    if opened:
        lines.append("### ✅ Issues abertas")
        for title, number in opened:
            lines.append(f"- #{number} — {title}")
        lines.append("")
    if skipped:
        lines.append("### ❌ Itens não abertos")
        for title, reason in skipped:
            lines.append(f"- **{title}** — {reason}")
        lines.append("")
    if not opened and not skipped:
        lines.append("_Nenhum follow-up detectado._")
    return "\n".join(lines)
