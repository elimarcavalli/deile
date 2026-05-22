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
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from deile.orchestration.pipeline.constants import PIPELINE_MSG_TRUNCATE_CHARS
from deile.orchestration.pipeline.follow_up_detector import detect_follow_ups
from deile.orchestration.pipeline.github_client import (CommentRef,
                                                        GhCommandError,
                                                        MentionTrigger)
from deile.orchestration.pipeline.labels import (MENTION_DONE,
                                                 REVIEW_CONCLUDED,
                                                 REVIEW_IN_PROGRESS,
                                                 REVIEW_PENDING,
                                                 WORKFLOW_BLOCKED,
                                                 WORKFLOW_IMPLEMENTING,
                                                 WORKFLOW_NEW, WORKFLOW_PR,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING)

# Mention triggers that describe a STICKY state (they re-appear on every poll
# until the underlying GitHub state changes), as opposed to "comment", which is
# bounded by the timestamp cursor. Sticky triggers need the ``MENTION_DONE``
# marker to avoid re-dispatching the same work every tick (issue #253 storm).
_STICKY_TRIGGER_TYPES = frozenset({"assignee", "reviewer", "body"})

if TYPE_CHECKING:  # pragma: no cover - typing only
    from deile.orchestration.pipeline.implementer import WorkOutcome
    from deile.orchestration.pipeline.monitor import PipelineMonitor

# Worker structured-result ``ended`` values (issue #254). Mirrors the constants
# in ``infra/k8s/_worker_resume.py`` — kept as plain literals here to avoid the
# pipeline importing from the infra tree (different sys.path at runtime).
_ENDED_CONCLUIDO = "concluido"
_ENDED_INCOMPLETO = "incompleto"
_ENDED_BLOQUEADO = "bloqueado"

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"https://github\.com/[^\s\"'<>]+/pull/\d+", re.IGNORECASE)


def _monotonic() -> float:
    """Monotonic clock used for resume cadence (wrapped for test injection)."""
    return time.monotonic()


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
            batch = await monitor.github.claim_with_batch("issue", issue.number)
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
            batch = await monitor.github.claim_with_batch("pr", pr.number)
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


# ----- mention handling: unified trigger polling (issue #253) ----------

async def process_mentions(monitor: "PipelineMonitor") -> None:
    """Unified mention processing: poll ALL trigger types and dispatch deduplicated.

    Trigger types monitored (RF1):
    - Comment mentions (@deile-one in issue/PR comments)
    - Body mentions (@deile-one in issue/PR body)
    - Assignee (DEILE assigned to an issue/PR)
    - Reviewer (DEILE requested as reviewer on a PR)

    Deduplication has TWO axes:

    - **Within a tick**: triggers targeting the same issue/PR are grouped by
      ``dedup_key`` and dispatched once with the full context of all trigger
      types.
    - **Across ticks**: comment mentions are bounded by the timestamp cursor
      (only comments newer than ``since`` fire). The STICKY triggers
      (assignee / reviewer / body) have no such timestamp — the underlying state
      re-appears on every poll — so they are gated by the ``~mention:processado``
      label: a target carrying it is skipped, and the label is applied after a
      successful dispatch whose group included a sticky trigger. Without this,
      a single assignment / review-request re-dispatched the same work on every
      tick (the duplicate-DM storm bug). A NEW comment still re-triggers (the
      cursor governs comments; they ignore the label). A human removes the label
      to force a re-handle.
    """
    handle = monitor.config.mention_handle.lower()
    gh_login = handle.lstrip("@")  # "deile-one"

    triggers = await _collect_mention_triggers(monitor, handle, gh_login)
    if not triggers:
        monitor._save_mention_cursor(datetime.now(tz=timezone.utc))
        return

    # ---- Deduplicate by target ------------------------------------------
    groups: dict[str, list[MentionTrigger]] = {}
    for t in triggers:
        groups.setdefault(t.dedup_key, []).append(t)

    now = datetime.now(tz=timezone.utc)
    mono = _monotonic()
    for dedup_key, group in groups.items():
        await _dispatch_mention_group(monitor, dedup_key, group, gh_login, mono)

    monitor._save_mention_cursor(now)


async def _collect_mention_triggers(
    monitor: "PipelineMonitor", handle: str, gh_login: str
) -> list["MentionTrigger"]:
    """Poll all four mention sources and return the raw trigger list.

    Comment mentions are cursor-bounded (only comments newer than the saved
    cursor fire). The sticky sources (assignee / reviewer / body) carry the
    target's labels with no timestamp, so any target already marked
    ``~mention:processado`` is filtered out here — see :func:`process_mentions`
    for the cross-tick dedup rationale.
    """
    triggers: list[MentionTrigger] = []

    # ---- 1. Comment mentions (cursor-based polling) ---------------------
    since = monitor._load_mention_cursor()
    try:
        issue_comments = await monitor.github.list_issue_comments_since(since)
        pr_comments = await monitor.github.list_pr_review_comments_since(since)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mention poll (comments) failed: %s", exc)
        issue_comments = []
        pr_comments = []
    all_comments: list[CommentRef] = list(issue_comments) + list(pr_comments)
    for ref in all_comments:
        if handle in ref.body.lower():
            triggers.append(MentionTrigger(trigger_type="comment", comment=ref))

    # ---- 2-4. Sticky triggers (assignee / reviewer / body) --------------
    async def _poll(label: str, coro) -> list:
        try:
            return list(await coro)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mention poll (%s) failed: %s", label, exc)
            return []

    for issue in await _poll("assigned issues", monitor.github.list_issues_assigned_to(gh_login)):
        if MENTION_DONE not in issue.labels:
            triggers.append(MentionTrigger(trigger_type="assignee", issue=issue))
    for pr in await _poll("assigned PRs", monitor.github.list_prs_assigned_to(gh_login)):
        if MENTION_DONE not in pr.labels:
            triggers.append(MentionTrigger(trigger_type="assignee", pr=pr))
    for pr in await _poll("review requests", monitor.github.list_prs_with_review_requests(gh_login)):
        if MENTION_DONE not in pr.labels:
            triggers.append(MentionTrigger(trigger_type="reviewer", pr=pr))

    try:
        body_issues, body_prs = await monitor.github.search_items_mentioning(handle)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mention poll (body search) failed: %s", exc)
        body_issues = []
        body_prs = []
    for issue in body_issues:
        if MENTION_DONE not in issue.labels:
            triggers.append(MentionTrigger(trigger_type="body", issue=issue))
    for pr in body_prs:
        if MENTION_DONE not in pr.labels:
            triggers.append(MentionTrigger(trigger_type="body", pr=pr))

    return triggers


async def _dispatch_mention_group(
    monitor: "PipelineMonitor", dedup_key: str, group: list["MentionTrigger"],
    gh_login: str, mono: float,
) -> None:
    """Route + dispatch one deduplicated mention group.

    The handler is a ROUTER, not a one-shot dispatcher:
      - issue + assignee/body → inject ~workflow:nova so the normal pipeline
        takes over (review → implement WITH resume #254 on an auto/issue-N
        branch → PR → review by the reviewer persona).
      - PR + assignee → work_merge (quality-gate review + resolve threads +
        fix + merge).
      - PR + reviewer (only) → review_only (review + assign author back, NO
        merge), per operator policy.
      - PR + comment/body → address (do what was asked + resolve threads).
      - issue + comment → do what the comment says (one-shot).
    """
    trigger_types = sorted(set(t.trigger_type for t in group))
    primary = group[0]
    kind = primary.target_kind
    number = primary.target_number
    has = set(trigger_types)
    sticky = bool(has & _STICKY_TRIGGER_TYPES)
    logger.info("mention group %s: triggers=%s", dedup_key, trigger_types)

    # Issue work → inject into the pipeline (handles its own dispatch).
    if kind == "issue" and ("assignee" in has or "body" in has):
        await _route_issue_to_pipeline(monitor, group, number, dedup_key, gh_login)
        return

    # Decide the dispatch mode from the role.
    if kind == "pr" and "assignee" in has:
        mode = "work_merge"
    elif kind == "pr" and "reviewer" in has:
        mode = "review_only"
    elif kind == "pr":
        mode = "address"
    else:
        mode = "comment"  # comment mention on an issue

    # Resume + attempt ceiling for STICKY PR work (mirrors implement stage).
    resume = False
    if sticky:
        st = monitor._resume_tracker.get(number)
        if st.attempt >= monitor.config.resume_max_attempts:
            logger.warning(
                "mention %s: attempt ceiling (%d) reached — marking done",
                dedup_key, st.attempt,
            )
            await _comment_mention_gave_up(monitor, kind, number, st.attempt)
            await _mark_mention_done(monitor, kind, number)
            monitor._resume_tracker.clear(number)
            return
        resume = st.attempt > 0
        monitor._resume_tracker.record_dispatch(number, mono)

    try:
        outcome = await monitor.implementer.mention(
            monitor, primary,
            trigger_types=trigger_types, all_triggers=group,
            mode=mode, resume=resume,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("mention dispatch error for %s: %s", dedup_key, exc)
        return

    if sticky:
        # Absorb the worker's ground-truth bookkeeping (attempt/fingerprint)
        # so the ceiling advances and a stuck loop is bounded.
        monitor._resume_tracker.update_from_worker(
            number, fingerprint=outcome.fingerprint,
            attempt=outcome.tentativa, budget_s=outcome.budget_acumulado_s,
        )

    if not outcome.ok:
        # No ~mention:processado → the sticky trigger retries next tick (in
        # RESUME mode, bounded by the ceiling above). Comment-driven work is
        # cursor-bounded, so it does not retry.
        logger.warning(
            "mention dispatch failed for %s: %s", dedup_key, outcome.error
        )
        return

    monitor._stats.mentions_processed += 1
    if sticky:
        await _mark_mention_done(monitor, kind, number)
        monitor._resume_tracker.clear(number)
    author = next((t.comment.author for t in group if t.comment is not None), "")
    await monitor.notifier.mention_processed(
        primary.comment.html_url if primary.comment else dedup_key,
        author or gh_login,
    )


async def _mark_mention_done(monitor: "PipelineMonitor", kind: str, number: int) -> None:
    """Best-effort apply ``~mention:processado`` so a sticky trigger stops re-firing."""
    try:
        await monitor.github.add_labels(kind, number, [MENTION_DONE])
    except Exception as exc:  # noqa: BLE001 — marker is best-effort
        logger.warning("could not mark %s #%d as %s: %s", kind, number, MENTION_DONE, exc)


async def _comment_mention_gave_up(
    monitor: "PipelineMonitor", kind: str, number: int, attempts: int
) -> None:
    """Surface that DEILE stopped retrying a mention after the attempt ceiling."""
    msg = (
        f"⛔ DEILE não concluiu esta solicitação após {attempts} tentativas. "
        f"Removido da fila de menção — remova `{MENTION_DONE}` para tentar de novo."
    )
    try:
        if kind == "pr":
            await monitor.github.comment_on_pr(number, msg)
        else:
            await monitor.github.comment_on_issue(number, msg)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("gave-up comment on %s #%d failed: %s", kind, number, exc)


async def _route_issue_to_pipeline(
    monitor: "PipelineMonitor",
    group: list["MentionTrigger"],
    number: int,
    dedup_key: str,
    gh_login: str,
) -> None:
    """Inject an assigned/body-mentioned issue into the normal pipeline.

    Adds ``~workflow:nova`` (unless the issue already carries a ``~workflow:*``
    label) so the review→implement(resume)→PR→review machinery handles it with
    the correct ``auto/issue-N`` branch and the reviewer-persona gate. Then marks
    ``~mention:processado`` so the mention stage does not re-route it every tick.
    """
    issue = next((t.issue for t in group if t.issue is not None), None)
    labels = set(issue.labels) if issue is not None else set()
    already_in_pipeline = any(lb.startswith("~workflow:") for lb in labels)
    try:
        if not already_in_pipeline:
            await monitor.github.add_labels("issue", number, [WORKFLOW_NEW])
            logger.info("mention: routed issue #%d into pipeline (%s)", number, WORKFLOW_NEW)
        await monitor.github.add_labels("issue", number, [MENTION_DONE])
    except Exception as exc:  # noqa: BLE001 — never abort the loop
        logger.warning("mention: could not route issue #%d: %s", number, exc)
        return
    monitor._stats.mentions_processed += 1
    await monitor.notifier.mention_processed(
        issue.url if issue is not None else dedup_key, gh_login
    )


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
    batch = await monitor.github.claim_with_batch("issue", target.number)
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
            # A blocked issue must never re-enter the implement queue (issue
            # #254). It keeps em_implementacao + bloqueada until a human removes
            # bloqueada, so this guard is belt-and-suspenders with the query.
            and WORKFLOW_BLOCKED not in i.labels
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
    monitor._resume_tracker.record_dispatch(target.number, _monotonic())
    # Delegate the actual implementation to the configured strategy
    # (claude -p in a worktree, or a dispatch to the deile-worker). The
    # strategy returns a uniform outcome; label orchestration stays here.
    outcome = await monitor.implementer.implement(monitor, target, resume=False)
    await _finalize_implement_outcome(monitor, target.number, outcome, resume=False)


# ----- stage 2b: resume parked, continuable implementations (issue #254) -----

async def resume_in_progress_issues(monitor: "PipelineMonitor") -> None:
    """Re-dispatch parked, continuable implementations in RESUME mode.

    Selects issues parked in ``~workflow:em_implementacao`` that are NOT
    ``~workflow:bloqueada`` (a block excludes from the auto-resume) and belong to
    this monitor. For the first eligible one (one issue per tick, mirroring the
    implement stage) it enforces, in order:

      1. **Cadence** (item 9): honor ``resume_interval`` since the last dispatch.
      2. **Attempt ceiling** (item 6): ``resume_max_attempts`` → block flow.
      3. **Budget ceiling** (item 6): accumulated ``resume_budget`` s → block flow.

    Then re-dispatches in RESUME mode (no reset; reuses branch + untracked) and
    finalizes the outcome via the shared ground-truth handler — which also runs
    the progress guard (item 4: identical substantive fingerprint = 0 progress
    → block flow).
    """
    try:
        issues = await monitor.github.list_issues_with_label(WORKFLOW_IMPLEMENTING, limit=50)
    except GhCommandError as exc:
        await _record_gh_error(
            monitor, "could not list in-progress issues (gh error)", exc,
            notifier_label="resume/list",
        )
        return
    now = _monotonic()
    target = next(
        (
            i for i in issues
            if WORKFLOW_BLOCKED not in i.labels
            and WORKFLOW_PR not in i.labels
            and monitor._this_monitor_owns(i)
            and monitor._resume_tracker.cadence_ok(
                i.number, now, monitor.config.resume_interval
            )
        ),
        None,
    )
    if target is None:
        return

    state = monitor._resume_tracker.get(target.number)
    # Attempt ceiling — block before spending another dispatch.
    if state.attempt >= monitor.config.resume_max_attempts:
        await _block_issue(
            monitor, target.number,
            f"teto de tentativas atingido ({state.attempt}/"
            f"{monitor.config.resume_max_attempts}) sem concluir",
        )
        return
    # Budget ceiling (0 = disabled).
    if monitor.config.resume_budget > 0 and state.budget_s >= monitor.config.resume_budget:
        await _block_issue(
            monitor, target.number,
            f"orçamento de tempo esgotado ({state.budget_s:.0f}s >= "
            f"{monitor.config.resume_budget}s) sem concluir",
        )
        return

    await monitor.notifier.implementation_resumed(target.number, state.attempt + 1)
    monitor._resume_tracker.record_dispatch(target.number, now)
    monitor._stats.resume_dispatches += 1
    outcome = await monitor.implementer.implement(monitor, target, resume=True)
    await _finalize_implement_outcome(monitor, target.number, outcome, resume=True)


def _absorb_progress(
    monitor: "PipelineMonitor", number: int, outcome: "WorkOutcome"
) -> bool:
    """Run the progress guard then absorb the worker's bookkeeping.

    Returns ``zero_progress`` computed against the PREVIOUS fingerprint BEFORE
    absorbing this attempt's fingerprint/attempt/budget — that order is
    load-bearing (comparing the new fingerprint against itself would always
    read as zero progress) and must stay identical across the implement and
    review stages.
    """
    zero_progress = monitor._resume_tracker.is_zero_progress(number, outcome.fingerprint)
    monitor._resume_tracker.update_from_worker(
        number,
        fingerprint=outcome.fingerprint,
        attempt=outcome.tentativa,
        budget_s=outcome.budget_acumulado_s,
    )
    return zero_progress


async def _finalize_implement_outcome(
    monitor: "PipelineMonitor",
    number: int,
    outcome: "WorkOutcome",
    *,
    resume: bool,
) -> None:
    """Decide CONCLUÍDO / INCOMPLETO / BLOQUEADO from ground truth (item 5).

    Ground-truth-first: a confirmed PR (the worker's structured ``ended`` or, on
    the Claude path, a PR URL in the text) means done; an agent-declared block
    means block; everything else is parked/resumable. Runs the progress guard
    against the PREVIOUS fingerprint, THEN absorbs the worker's new
    fingerprint/attempt into the resume tracker.
    """
    pr_url = outcome.pr_url or _extract_pr_url(outcome.text)
    ended = outcome.ended  # "" on the Claude path; ground-truth on the worker path

    zero_progress = _absorb_progress(monitor, number, outcome)

    # 1. BLOQUEADO — the agent declared a hard impediment.
    if ended == _ENDED_BLOQUEADO:
        reason = outcome.motivo_bloqueio or "o agente declarou BLOQUEADO sem motivo"
        await _block_issue(monitor, number, reason)
        return

    # 2. A transport/worker failure with no structured verdict: park (resumable).
    if not outcome.ok and not ended:
        monitor._stats.errors += 1
        monitor._stats.claude_errors += 1
        err_detail = (outcome.error or "implementation failed")[:PIPELINE_MSG_TRUNCATE_CHARS]
        logger.error(
            "implement #%d failed: %s — parked in %s",
            number, err_detail, WORKFLOW_IMPLEMENTING,
        )
        await _park_or_keep(monitor, number, err_detail, resume=resume)
        return

    # 3. CONCLUÍDO — a real PR exists (and, when expected, was merged).
    if ended == _ENDED_CONCLUIDO or (not ended and outcome.ok and pr_url):
        try:
            await monitor.github.transition_issue(
                number, from_label=WORKFLOW_IMPLEMENTING, to_label=WORKFLOW_PR
            )
        except GhCommandError as exc:
            await _record_gh_error(
                monitor, f"could not transition issue #{number} to em_pr", exc,
            )
        monitor._resume_tracker.clear(number)
        monitor._stats.issues_implemented += 1
        await monitor.notifier.implementation_finished(number, pr_url)
        return

    # 4. INCOMPLETO — no PR yet. Block when the progress guard fired (two
    # consecutive attempts with the SAME substantive fingerprint = 0 progress).
    if zero_progress:
        await _block_issue(
            monitor, number,
            "duas tentativas seguidas sem progresso substantivo (diff idêntico)",
        )
        return
    monitor._stats.errors += 1
    monitor._stats.claude_errors += 1
    logger.warning(
        "implement #%d: incompleto (sem PR) — parked in %s%s",
        number, WORKFLOW_IMPLEMENTING, " (será retomada)" if resume else "",
    )
    await _park_or_keep(
        monitor, number, "o agente finalizou sem abrir PR", resume=resume
    )


async def _park_or_keep(
    monitor: "PipelineMonitor", number: int, reason: str, *, resume: bool
) -> None:
    """Park an incomplete issue.

    When resume is enabled the issue simply stays in ``~workflow:em_implementacao``
    for the resume sweep to pick up — we DM "parked" only on the first
    (non-resume) attempt so the operator is not pinged on every resume tick. When
    resume is disabled, this preserves the legacy "park forever + DM once"
    behaviour.
    """
    if resume or monitor.config.enable_resume:
        # Will be retried by the resume sweep; stay quiet to avoid DM spam.
        logger.debug("issue #%d left parked for resume: %s", number, reason)
        return
    await monitor.notifier.implementation_parked(number, reason)


async def _block(
    monitor: "PipelineMonitor", kind: str, number: int, short: str, *, comment: str
) -> None:
    """Shared block flow (item 7): comment the real impediment, add
    ``~workflow:bloqueada``, clear the resume tracker, bump the blocked stat and
    DM. ``kind`` (``"issue"``/``"pr"``) selects the GitHub comment/label surface;
    the caller supplies the already-truncated ``short`` reason and the wording.
    The target keeps its stage label so it leaves the active queue (and the
    auto-resume) without re-entering it; a human removes the label to unblock.
    """
    commenter = (
        monitor.github.comment_on_issue if kind == "issue"
        else monitor.github.comment_on_pr
    )
    try:
        await commenter(number, comment)
    except Exception as exc:  # noqa: BLE001 — comment is best-effort; label still applied
        logger.warning("block %s: could not comment on #%d: %s", kind, number, exc)
    try:
        await monitor.github.add_labels(kind, number, [WORKFLOW_BLOCKED])
    except GhCommandError as exc:
        await _record_gh_error(
            monitor, f"could not apply {WORKFLOW_BLOCKED} to {kind} #{number}", exc,
        )
    monitor._resume_tracker.clear(number)
    monitor._stats.issues_blocked += 1
    logger.warning("%s #%d BLOCKED: %s", kind, number, short)
    await monitor.notifier.implementation_blocked(number, short)


async def _block_issue(monitor: "PipelineMonitor", number: int, reason: str) -> None:
    """Block an issue in the implement/resume stage (keeps em_implementacao)."""
    short = reason[:PIPELINE_MSG_TRUNCATE_CHARS]
    comment = (
        f"⛔ **Pipeline bloqueou esta issue** (`{WORKFLOW_BLOCKED}`).\n\n"
        f"**Motivo:** {short}\n\n"
        f"O trabalho parcial foi preservado na branch. Para retomar, remova o "
        f"label `{WORKFLOW_BLOCKED}` — o pipeline volta a retomar a implementação "
        f"de onde parou."
    )
    await _block(monitor, "issue", number, short, comment=comment)


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
    #
    # Resume (issue #254): when ``enable_resume`` is on, a PR left parked in
    # ~review:em_andamento by a prior incomplete review/merge IS a candidate
    # (it is re-dispatched in RESUME mode). Without resume, in-progress PRs stay
    # out of the set (legacy behaviour). A ~workflow:bloqueada PR is always
    # excluded. Cadence is honored so resume does not re-fire every tick.
    resume_enabled = monitor.config.enable_resume
    now = _monotonic()

    def _candidate(pr) -> bool:
        if pr.is_draft or REVIEW_CONCLUDED in pr.labels or WORKFLOW_BLOCKED in pr.labels:
            return False
        if not monitor._owns_pr_branch(pr.head_ref, pr_number=pr.number):
            return False
        if REVIEW_IN_PROGRESS in pr.labels:
            # In-progress: only resumable when resume is enabled, cadence ok,
            # and not currently batch-locked by another monitor's live attempt.
            return (
                resume_enabled
                and pr.batch_id is None
                and monitor._resume_tracker.cadence_ok(
                    pr.number, now, monitor.config.resume_interval
                )
            )
        # Fresh: unclaimed PR awaiting first review.
        return pr.batch_id is None

    target = next((pr for pr in prs if _candidate(pr)), None)
    if target is None:
        return
    is_resume = REVIEW_IN_PROGRESS in target.labels
    batch = await monitor.github.claim_with_batch("pr", target.number)
    if batch is None:
        return
    # Tag ownership so other monitors can identify who claimed this PR —
    # mirrors the identical pattern in stage 1 for issues.
    await monitor.github.add_labels("pr", target.number, [monitor.identity.ownership_label()])
    if is_resume:
        state = monitor._resume_tracker.get(target.number)
        # Attempt ceiling for review/merge — same block flow as implement.
        if state.attempt >= monitor.config.resume_max_attempts:
            await monitor.github.clear_batch_label("pr", target.number)
            await _block_pr(
                monitor, target.number, target.title, target.url,
                f"teto de tentativas atingido ({state.attempt}/"
                f"{monitor.config.resume_max_attempts}) sem mergear",
            )
            return
        await monitor.notifier.implementation_resumed(target.number, state.attempt + 1)
        monitor._stats.resume_dispatches += 1
    else:
        await monitor.notifier.pr_picked_up(target.number, target.title, target.url)
        try:
            await monitor.github.transition_pr(
                target.number, from_label=REVIEW_PENDING, to_label=REVIEW_IN_PROGRESS
            )
        except GhCommandError:
            # ~review:pendente may not be set; that's ok.
            await monitor.github.add_labels("pr", target.number, [REVIEW_IN_PROGRESS])
    monitor._resume_tracker.record_dispatch(target.number, now)
    # Delegate the review/merge work to the configured strategy. The Claude
    # strategy checks out the branch in a worktree; the worker strategy clones
    # and runs ``gh pr checkout`` inside its own sandbox.
    outcome = await monitor.implementer.review(monitor, target, resume=is_resume)
    zero_progress = _absorb_progress(monitor, target.number, outcome)
    # Ground-truth merge detection: the worker's structured ``ended`` is
    # authoritative; fall back to scanning the text for the MERGED marker.
    merged = outcome.ended == _ENDED_CONCLUIDO or (
        not outcome.ended and outcome.ok and "merged" in outcome.text.lower()
    )
    blocked = outcome.ended == _ENDED_BLOQUEADO
    if not outcome.ok:
        monitor._stats.errors += 1
        monitor._stats.claude_errors += 1
        logger.error(
            "pr_review #%d failed: %s", target.number,
            (outcome.error or "review failed")[:PIPELINE_MSG_TRUNCATE_CHARS],
        )

    if blocked:
        await monitor.github.clear_batch_label("pr", target.number)
        await _block_pr(
            monitor, target.number, target.title, target.url,
            outcome.motivo_bloqueio or "o agente declarou BLOQUEADO sem motivo",
        )
        return

    if merged:
        try:
            await monitor.github.transition_pr(
                target.number, from_label=REVIEW_IN_PROGRESS, to_label=REVIEW_CONCLUDED
            )
        except GhCommandError as exc:
            await _record_gh_error(
                monitor, f"could not transition PR #{target.number} to concluida", exc,
            )
        await monitor.github.clear_batch_label("pr", target.number)
        monitor._resume_tracker.clear(target.number)
        monitor._stats.prs_reviewed += 1
        await monitor.notifier.pr_reviewed(target.number, target.title, target.url, merged=True)
        await _post_merge_follow_ups(monitor, target)
        return

    # Not merged. With resume enabled, keep the PR in ~review:em_andamento for
    # the next resume tick (progress guard catches a stuck loop). Without
    # resume, preserve the legacy behaviour: mark concluded so the PR drops out.
    if resume_enabled:
        if zero_progress:
            await monitor.github.clear_batch_label("pr", target.number)
            await _block_pr(
                monitor, target.number, target.title, target.url,
                "duas tentativas de review/merge sem progresso (diff idêntico)",
            )
            return
        # Release the batch lock so the next tick can re-claim; keep em_andamento.
        await monitor.github.clear_batch_label("pr", target.number)
        logger.info("pr_review #%d incompleto — em_andamento (será retomada)", target.number)
        return

    try:
        await monitor.github.transition_pr(
            target.number, from_label=REVIEW_IN_PROGRESS, to_label=REVIEW_CONCLUDED
        )
    except GhCommandError as exc:
        await _record_gh_error(
            monitor, f"could not transition PR #{target.number} to concluida", exc,
        )
    await monitor.github.clear_batch_label("pr", target.number)
    monitor._stats.prs_reviewed += 1
    await monitor.notifier.pr_reviewed(target.number, target.title, target.url, merged=False)


async def _post_merge_follow_ups(monitor: "PipelineMonitor", target) -> None:
    """Run the post-merge follow-up + callback hooks (extracted for reuse)."""
    if monitor.config.enable_follow_ups:
        await monitor._stage4_follow_ups(target.number, target.title, target.url)
    if monitor._post_merge_cb is not None:
        try:
            await monitor._post_merge_cb(target.number, target.title, target.url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("post_merge_callback failed for PR #%d: %s", target.number, exc)


async def _block_pr(
    monitor: "PipelineMonitor", number: int, title: str, url: str, reason: str
) -> None:
    """Block a PR in the review/merge stage (keeps ~review:em_andamento).

    Mirrors :func:`_block_issue`; ``title``/``url`` are accepted for call-site
    symmetry but not used in the comment.
    """
    short = reason[:PIPELINE_MSG_TRUNCATE_CHARS]
    comment = (
        f"⛔ **Pipeline bloqueou o review/merge desta PR** (`{WORKFLOW_BLOCKED}`).\n\n"
        f"**Motivo:** {short}\n\n"
        f"Para retomar, remova o label `{WORKFLOW_BLOCKED}`."
    )
    await _block(monitor, "pr", number, short, comment=comment)


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
