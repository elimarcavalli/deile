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

import asyncio
import logging
import re
import time
import warnings
from typing import TYPE_CHECKING, Optional

from deile.orchestration.forge import (CommentRef, GhCommandError,
                                       MentionTrigger, declared_hosts,
                                       find_last_pr_url)
from deile.orchestration.pipeline._time_utils import now_utc
from deile.orchestration.pipeline.constants import PIPELINE_MSG_TRUNCATE_CHARS
from deile.orchestration.pipeline.follow_up_detector import detect_follow_ups
from deile.orchestration.pipeline.implementer import (parse_critique_verdict,
                                                      parse_decompose_result,
                                                      parse_refine_verdict)
from deile.orchestration.pipeline.labels import (FOLLOW_UPS_PROCESSED,
                                                 MENTION_DONE, REFINAR,
                                                 REFINE_WORKFLOW_STATES,
                                                 REVIEW_CONCLUDED,
                                                 REVIEW_IN_PROGRESS,
                                                 REVIEW_PENDING, TYPE_INTENT,
                                                 WORKFLOW_BLOCKED,
                                                 WORKFLOW_DECOMPOSED,
                                                 WORKFLOW_IMPLEMENTING,
                                                 WORKFLOW_NEW, WORKFLOW_PR,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING,
                                                 WORKFLOW_WAITING,
                                                 current_attempt_from_labels,
                                                 is_attempt_label,
                                                 is_batch_label,
                                                 issue_type_from_labels,
                                                 make_attempt_label,
                                                 refine_workflow_state)

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

# Legacy regex kept ONLY for tests that import it directly. Production code
# uses :func:`find_last_pr_url` (forge-aware) — see ``_extract_pr_url``.
_PR_URL_RE = re.compile(r"https://github\.com/[^\s\"'<>]+/pull/\d+", re.IGNORECASE)


def _monotonic() -> float:
    """Monotonic clock used for resume cadence (wrapped for test injection)."""
    return time.monotonic()


async def _record_forge_error(
    monitor: "PipelineMonitor",
    description: str,
    exc: Exception,
    *,
    notifier_label: Optional[str] = None,
) -> None:
    """Bump ``errors`` + ``forge_errors`` counters, log, optionally notify.

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
    monitor._stats.forge_errors += 1
    logger.error("%s: %s", description, exc)
    if notifier_label is not None:
        await monitor.notifier.error(notifier_label, str(exc))


async def _record_gh_error(
    monitor: "PipelineMonitor",
    description: str,
    exc: Exception,
    *,
    notifier_label: Optional[str] = None,
) -> None:
    """Deprecated alias for :func:`_record_forge_error`.

    .. deprecated::
        Use ``_record_forge_error`` directly. This shim will be removed in the
        next major release.
    """
    warnings.warn(
        "_record_gh_error is deprecated; use _record_forge_error instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    await _record_forge_error(
        monitor, description, exc, notifier_label=notifier_label,
    )


async def _claim_for_classify(
    monitor: "PipelineMonitor",
    kind: str,
    number: int,
    *,
    error_context: str,
    notifier_label: Optional[str] = None,
) -> bool:
    """Claim the ``~batch:`` lock before classifying ``kind`` #``number``.

    The claim only matters with parallel monitors; a single monitor would
    add+remove the lock label in the same pass (timeline noise) — and the
    items are already shard-filtered by the callers — so single-monitor
    deployments skip the claim entirely and always return ``True``.

    Returns ``True`` when the caller may proceed to label the item, ``False``
    when it must skip it (already claimed by another monitor, or a gh error was
    recorded). On ``GhCommandError`` the error is recorded via
    :func:`_record_gh_error` (using ``error_context`` as the log prefix and the
    optional ``notifier_label`` for the Discord notification).
    """
    if monitor.identity.shard_count <= 1:
        return True
    try:
        batch = await monitor.forge.claim_with_batch(kind, number)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, f"{error_context} #{number} failed", exc,
            notifier_label=notifier_label,
        )
        return False
    if batch is None:
        logger.debug("%s #%s already claimed by another monitor; skipping", kind, number)
        return False
    return True


async def _release_classify_claim(monitor: "PipelineMonitor", kind: str, number: int) -> None:
    """Release the ``~batch:`` lock so the next stage can re-claim the item.

    Best-effort: the workflow label is already applied, so a clear failure must
    not abort the loop. No-op for single-monitor deployments (they never claim).
    """
    if monitor.identity.shard_count <= 1:
        return
    try:
        await monitor.forge.clear_batch_label(kind, number)
    except Exception as exc:  # noqa: BLE001 — label applied; clear is best-effort
        logger.warning("%s: could not clear batch on #%s: %s", kind, number, exc)


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
        issues = await monitor.forge.list_unclassified_issues()
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, "could not list unclassified issues (forge error)", exc,
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
        # Claim before labelling to reduce the TOCTOU window with parallel
        # monitors (no-op for a single monitor — see _claim_for_classify).
        if not await _claim_for_classify(
            monitor, "issue", issue.number,
            error_context="auto-classify claim",
            notifier_label=f"auto-classify claim #{issue.number}",
        ):
            continue
        try:
            await monitor.forge.add_labels("issue", issue.number, [WORKFLOW_NEW])
        except GhCommandError as exc:
            await _record_forge_error(
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
        # ``batch_id is None``. Without this the auto-classify → review handoff
        # deadlocks (the issue stays ~nova forever, batch-locked).
        await _release_classify_claim(monitor, "issue", issue.number)
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
            await monitor.forge.comment_on_issue(issue.number, comment)
        except Exception as exc:  # noqa: BLE001 — comment is best-effort; label already applied
            logger.warning("auto-classify comment #%s failed (label applied): %s", issue.number, exc)


# ----- PR triage: classify open non-draft PRs with no pipeline labels ----

async def classify_new_prs(monitor: "PipelineMonitor") -> None:
    """Apply ``~review:pendente`` to open non-draft PRs that have no pipeline labels."""
    try:
        prs = await monitor.forge.list_unclassified_prs()
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, "could not list unclassified PRs (forge error)", exc,
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
        # Only triage PRs this monitor would actually review (Stage 3 claims by
        # branch ownership — ``auto/issue-*`` for default identity, or any branch
        # when ``enable_review_human_prs``). Without this, ``~review:pendente``
        # is applied to PRs the pipeline never reviews (e.g. human/foreign
        # branches), leaving them stuck "pendente" forever.
        if not monitor._owns_pr_branch(pr.head_ref, pr_number=pr.number):
            continue
        if not await _claim_for_classify(monitor, "pr", pr.number, error_context="pr_triage claim"):
            continue
        try:
            await monitor.forge.add_labels("pr", pr.number, [REVIEW_PENDING])
        except GhCommandError as exc:
            await _record_forge_error(
                monitor, f"pr_triage label #{pr.number} failed", exc,
                notifier_label=f"pr_triage #{pr.number}",
            )
            continue
        # Release the batch claim so Stage 3 can pick this PR up via its own claim.
        await _release_classify_claim(monitor, "pr", pr.number)
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
        monitor._save_mention_cursor(now_utc())
        return

    # ---- Deduplicate by target ------------------------------------------
    groups: dict[str, list[MentionTrigger]] = {}
    for t in triggers:
        groups.setdefault(t.dedup_key, []).append(t)

    now = now_utc()
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
        issue_comments = await monitor.forge.list_issue_comments_since(since)
        pr_comments = await monitor.forge.list_pr_review_comments_since(since)
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

    for issue in await _poll("assigned issues", monitor.forge.list_issues_assigned_to(gh_login)):
        if MENTION_DONE not in issue.labels:
            triggers.append(MentionTrigger(trigger_type="assignee", issue=issue))
    for pr in await _poll("assigned PRs", monitor.forge.list_prs_assigned_to(gh_login)):
        if MENTION_DONE not in pr.labels:
            triggers.append(MentionTrigger(trigger_type="assignee", pr=pr))
    for pr in await _poll("review requests", monitor.forge.list_prs_with_review_requests(gh_login)):
        if MENTION_DONE not in pr.labels:
            triggers.append(MentionTrigger(trigger_type="reviewer", pr=pr))

    try:
        body_issues, body_prs = await monitor.forge.search_items_mentioning(handle)
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

    # Comment mention on an ISSUE: integrate with the flow it is ALREADY in
    # (issue #257) instead of spawning a parallel one-shot. Mentioning the target
    # by name in a comment is NORMAL and must NOT pull an issue out of the gate.
    if kind == "issue":
        try:
            gated = await monitor.forge.get_issue(number)
            glabels = set(gated.labels)
        except Exception:  # noqa: BLE001 — best-effort; fall through to one-shot
            glabels = set()
        if WORKFLOW_WAITING in glabels:
            # The comment IS the stakeholder's decision → lift the pause so the
            # refine loop resumes (the refiner reads this comment on its next pass).
            try:
                await monitor.forge.remove_labels("issue", number, [WORKFLOW_WAITING])
            except Exception as exc:  # noqa: BLE001
                logger.warning("mention #%d: could not lift aguardando_stakeholder: %s", number, exc)
            logger.info("mention #%d: decisão do stakeholder → retoma refino (sem one-shot)", number)
            return
        active = next((lb for lb in glabels if lb.startswith("~workflow:")), None)
        if active:
            # Already owned by the gate — do NOT spawn a parallel flow; the gate's
            # next worker dispatch reads the new comment.
            logger.info("mention #%d ignorada p/ roteamento: já está no gate (%s)", number, active)
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
        # ``review_only`` is deliberately NOT marked ~mention:processado: once
        # DEILE submits the review, GitHub removes it from requested_reviewers
        # (natural idempotency for the reviewer trigger), and leaving the marker
        # OFF lets the *assignee* trigger — the author DEILE just assigned back —
        # fire on the next tick, so a DEILE-authored PR self-completes
        # (assignee → work_merge → merge) without a human removing a label
        # (Decisão #32). Other sticky modes still get the marker.
        if mode != "review_only":
            await _mark_mention_done(monitor, kind, number)
        elif kind == "pr":
            # Silent-failure guard (regression #277): a "successful" outcome
            # whose review never reached GitHub leaves the reviewer trigger
            # armed → re-fires every tick → token storm. If our login is STILL
            # requested as reviewer, the worker did NOT post a review — break
            # the loop ourselves.
            try:
                still_requested = await monitor.forge.pr_reviewer_still_requested(
                    number, gh_login,
                )
            except Exception as exc:  # noqa: BLE001 — guard is best-effort
                logger.warning(
                    "post-review verification failed for pr#%d: %s — skipping guard",
                    number, exc,
                )
                still_requested = False
            if still_requested:
                logger.warning(
                    "review_only pr#%d: worker reported ok but no review posted "
                    "(reviewer still requested); applying %s to break the loop",
                    number, MENTION_DONE,
                )
                try:
                    await monitor.forge.comment_on_pr(
                        number,
                        f"⚠️ DEILE não conseguiu postar a review (worker terminou "
                        f"sem registrar review apesar de retornar ok). Aplicando "
                        f"`{MENTION_DONE}` para cortar o loop de re-disparo. "
                        f"Remova esse label para tentar de novo.",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("could not comment on pr#%d: %s", number, exc)
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
        await monitor.forge.add_labels(kind, number, [MENTION_DONE])
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
            await monitor.forge.comment_on_pr(number, msg)
        else:
            await monitor.forge.comment_on_issue(number, msg)
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
            await monitor.forge.add_labels("issue", number, [WORKFLOW_NEW])
            logger.info("mention: routed issue #%d into pipeline (%s)", number, WORKFLOW_NEW)
        await monitor.forge.add_labels("issue", number, [MENTION_DONE])
    except Exception as exc:  # noqa: BLE001 — never abort the loop
        logger.warning("mention: could not route issue #%d: %s", number, exc)
        return
    monitor._stats.mentions_processed += 1
    await monitor.notifier.mention_processed(
        issue.url if issue is not None else dedup_key, gh_login
    )


# ----- stage 1: review ---------------------------------------------------

async def review_one_new_issue(monitor: "PipelineMonitor") -> None:
    """Stage 1. With the refinement gate ON (issue #257) this is the CRITIQUE of
    scope: dispatch the type's persona to judge CLARO/VAGO; clear → revisada,
    poor → refinar + the type's refine state (analyst→em_refinamento, others→
    em_arquitetura), with a block-to-author after ``refine_max_attempts`` passes.
    With the gate OFF it keeps the legacy no-op transition (nova→revisada)."""
    try:
        issues = await monitor.forge.list_issues_with_label(WORKFLOW_NEW, limit=50)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, "could not list new issues (forge error)", exc,
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

    if monitor.config.enable_refinement_gate:
        await _critique_one_issue(monitor, target)
        return

    # ---- Legacy path (Claude/no-gate): no-op transition through review --------
    batch = await monitor.forge.claim_with_batch("issue", target.number)
    if batch is None:
        return
    # Tag ownership so other monitors can identify who claimed this.
    await monitor.forge.add_labels("issue", target.number, [monitor.identity.ownership_label()])
    await monitor.notifier.issue_picked_up(target.number, target.title, target.url)
    try:
        # Atomic: if review_callback or final transition fails, revert to WORKFLOW_NEW.
        await monitor.forge.transition_issue(
            target.number, from_label=WORKFLOW_NEW, to_label=WORKFLOW_REVIEWING
        )
        review_failed = False
        try:
            if monitor._review_cb is not None:
                comment = await monitor._review_cb(target)
                if comment:
                    await monitor.forge.comment_on_issue(target.number, comment)
            await monitor.forge.transition_issue(
                target.number, from_label=WORKFLOW_REVIEWING, to_label=WORKFLOW_REVIEWED
            )
        except GhCommandError:
            monitor._stats.errors += 1
            monitor._stats.forge_errors += 1
            review_failed = True
            raise
        except Exception:  # noqa: BLE001
            review_failed = True
            raise
        finally:
            if review_failed:
                # Revert to WORKFLOW_NEW so the issue isn't stuck in em_revisao
                try:
                    await monitor.forge.transition_issue(
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


async def _critique_one_issue(monitor: "PipelineMonitor", target) -> None:
    """Critique gate (issue #257): judge scope, route to revisada or refinement."""
    number = target.number
    # Single-monitor production needs no batch lock (the nova→em_revisao flip is
    # the lock, and a lingering ~batch: would break the re-critique loop); a
    # sharded deployment claims to close the TOCTOU window and clears it after.
    multi = monitor.identity.shard_count > 1
    if multi:
        if await monitor.forge.claim_with_batch("issue", number) is None:
            return
    # Ownership tag lets the implement stage accept this issue without a batch.
    await monitor.forge.add_labels("issue", number, [monitor.identity.ownership_label()])
    await monitor.notifier.issue_picked_up(number, target.title, target.url)
    try:
        await monitor.forge.transition_issue(
            number, from_label=WORKFLOW_NEW, to_label=WORKFLOW_REVIEWING
        )
    except GhCommandError as exc:
        await _record_forge_error(monitor, f"could not claim issue #{number} for critique", exc)
        return

    outcome = await monitor.implementer.critique(monitor, target)
    if multi:
        await monitor.forge.clear_batch_label("issue", number)
    if not outcome.ok:
        # Critique dispatch failed → revert to nova so a later tick retries.
        try:
            await monitor.forge.transition_issue(
                number, from_label=WORKFLOW_REVIEWING, to_label=WORKFLOW_NEW
            )
        except Exception:  # noqa: BLE001 — rollback is best-effort
            logger.warning("could not revert #%d to nova after critique failure", number)
        logger.warning("critique #%d failed: %s", number, (outcome.error or "")[:200])
        return

    is_clear, reason = parse_critique_verdict(outcome.text)
    issue_type = issue_type_from_labels(target.labels)
    if is_clear:
        await monitor.forge.transition_issue(
            number, from_label=WORKFLOW_REVIEWING, to_label=WORKFLOW_REVIEWED
        )
        # Scope is clear now: drop the refinement marker AND any stale refine
        # state (em_refinamento/em_arquitetura) so the issue carries exactly one
        # ~workflow: label — defensive against residue from a raced cycle.
        await monitor.forge.remove_labels("issue", number, [REFINAR, *REFINE_WORKFLOW_STATES])
        monitor._stats.issues_reviewed += 1
        await monitor.notifier.issue_reviewed(number, target.title, target.url)
        return

    # POOR — block to the author once the refinement budget is exhausted.
    if monitor._resume_tracker.refine_attempt(number) >= monitor.config.refine_max_attempts:
        await _block_refinement(monitor, target, reason)
        return
    # Send to the type-specific refinement state and mark it for the refine stage.
    refine_state = refine_workflow_state(issue_type)
    await monitor.forge.add_labels("issue", number, [REFINAR])
    try:
        await monitor.forge.transition_issue(
            number, from_label=WORKFLOW_REVIEWING, to_label=refine_state
        )
    except GhCommandError as exc:
        await _record_forge_error(monitor, f"could not move #{number} to {refine_state}", exc)
        return
    logger.info("critique #%d VAGO → %s (%s)", number, refine_state, reason[:120])


async def refine_one_issue(monitor: "PipelineMonitor") -> None:
    """Stage 1b (issue #257): refine ONE issue carrying ``refinar``.

    Candidate = any open issue with ``refinar`` that this monitor owns and is NOT
    paused (``aguardando_stakeholder``), blocked, or already past refinement. An
    issue not yet in a refine state (a human applied ``refinar`` by hand) is
    rehydrated into its type's refine state. Otherwise the type's persona rewrites
    the body: ``REFINO: OK`` → back to ``nova`` for re-critique (counts toward the
    ceiling); ``AGUARDA_STAKEHOLDER`` → pause via the waiting overlay.
    """
    if not monitor.config.enable_refinement_gate:
        return
    try:
        issues = await monitor.forge.list_issues_with_label(REFINAR, limit=50)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, "could not list issues to refine (forge error)", exc,
            notifier_label="refine/list",
        )
        return
    _excluded = (WORKFLOW_WAITING, WORKFLOW_BLOCKED, WORKFLOW_IMPLEMENTING,
                 WORKFLOW_PR, WORKFLOW_DECOMPOSED)
    target = next(
        (i for i in issues
         if not any(lb in i.labels for lb in _excluded)
         and monitor.identity.owns(i.title)),
        None,
    )
    if target is None:
        return
    number = target.number
    issue_type = issue_type_from_labels(target.labels)

    # Rehydrate a hand-applied ``refinar`` (issue not yet in a refine state).
    if not any(s in target.labels for s in REFINE_WORKFLOW_STATES):
        refine_state = refine_workflow_state(issue_type)
        cur = next(
            (s for s in (WORKFLOW_NEW, WORKFLOW_REVIEWING, WORKFLOW_REVIEWED) if s in target.labels),
            None,
        )
        try:
            if cur:
                await monitor.forge.transition_issue(number, from_label=cur, to_label=refine_state)
            else:
                await monitor.forge.add_labels("issue", number, [refine_state])
        except GhCommandError as exc:
            await _record_forge_error(monitor, f"could not rehydrate #{number} into {refine_state}", exc)
        return  # refined on the next tick

    # Ceiling guard (belt-and-suspenders with the critique-side check).
    if monitor._resume_tracker.refine_attempt(number) >= monitor.config.refine_max_attempts:
        await _block_refinement(monitor, target, "teto de refinamentos atingido")
        return

    outcome = await monitor.implementer.refine(monitor, target)
    if not outcome.ok:
        # Count the failed attempt so a DETERMINISTIC failure (e.g. a payload the
        # worker rejects) hits the ceiling → block, instead of looping forever.
        monitor._resume_tracker.bump_refine(number)
        logger.warning(
            "refine #%d failed (passe %d): %s", number,
            monitor._resume_tracker.refine_attempt(number), (outcome.error or "")[:200],
        )
        return

    verdict = parse_refine_verdict(outcome.text)
    if verdict == "waiting":
        # The worker posted 2-3 suggestions and assigned the author; pause refino.
        await monitor.forge.add_labels("issue", number, [WORKFLOW_WAITING])
        logger.info("refine #%d → aguardando stakeholder", number)
        return
    # OK / unknown → count it and send back for re-critique (the safety net).
    monitor._resume_tracker.bump_refine(number)
    refine_state = next(
        (s for s in REFINE_WORKFLOW_STATES if s in target.labels),
        refine_workflow_state(issue_type),
    )
    try:
        await monitor.forge.transition_issue(number, from_label=refine_state, to_label=WORKFLOW_NEW)
    except GhCommandError as exc:
        await _record_forge_error(monitor, f"could not return #{number} to nova after refine", exc)
        return
    logger.info("refine #%d OK (passe %d) → nova (re-crítica)", number,
                monitor._resume_tracker.refine_attempt(number))


async def _block_refinement(monitor: "PipelineMonitor", issue, reason: str) -> None:
    """Block a poor-scoped issue back to its author after the refine ceiling.

    Rests the issue in its type's refine state (so removing ``bloqueada`` resumes
    refinement with a fresh count — :func:`_block` clears the tracker), keeps
    ``refinar``, and assigns the author so the stakeholder is pinged to refine it
    by hand. No ``@``-mention in the comment (that would re-trigger mention
    handling when the author is DEILE itself)."""
    number = issue.number
    issue_type = issue_type_from_labels(issue.labels)
    refine_state = refine_workflow_state(issue_type)
    # First, scrub every stage/refine-state label that ISN'T the resting one.
    # Pre-fix the issue could end up wearing 4+ workflow labels at once
    # (~workflow:em_revisao + em_arquitetura + refinar + bloqueada), which left
    # humans confused about the actual state — observed on #281 on 2026-05-23.
    stale = [
        s for s in (WORKFLOW_REVIEWING, WORKFLOW_NEW, WORKFLOW_IMPLEMENTING,
                    *REFINE_WORKFLOW_STATES)
        if s in issue.labels and s != refine_state
    ]
    if stale:
        try:
            await monitor.forge.remove_labels("issue", number, stale)
        except GhCommandError as exc:
            await _record_forge_error(
                monitor, f"could not strip stale labels {stale} from #{number}", exc,
            )
    if refine_state not in issue.labels:
        try:
            await monitor.forge.add_labels("issue", number, [refine_state])
        except GhCommandError as exc:
            await _record_forge_error(monitor, f"could not rest #{number} in {refine_state}", exc)
    await monitor.forge.add_labels("issue", number, [REFINAR])
    if getattr(issue, "author", ""):
        await monitor.forge.assign_issue(number, issue.author)
    short = reason[:PIPELINE_MSG_TRUNCATE_CHARS]
    comment = (
        f"⛔ **Refino atingiu o teto de {monitor.config.refine_max_attempts} tentativas** "
        f"e o escopo ainda está vago.\n\n"
        f"**Falta:** {short}\n\n"
        f"Autor: por favor refine esta issue manualmente (preencha o template) e remova o "
        f"label `{WORKFLOW_BLOCKED}` para o pipeline retomar o refinamento."
    )
    await _block(monitor, "issue", number, short, comment=comment)


async def decompose_one_reviewed_intent(monitor: "PipelineMonitor") -> None:
    """Stage 2 (intent path, issue #257): an architect decomposes a CLEAR intent
    into independent derived issues, then the intent stays OPEN as a decomposed
    epic (``~workflow:decomposta``)."""
    if not monitor.config.enable_refinement_gate:
        return
    try:
        issues = await monitor.forge.list_issues_with_label(WORKFLOW_REVIEWED, limit=50)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, "could not list reviewed intents (forge error)", exc,
            notifier_label="decompose/list",
        )
        return
    ownership_label = monitor.identity.ownership_label()
    target = next(
        (i for i in issues
         if issue_type_from_labels(i.labels) == TYPE_INTENT
         and WORKFLOW_DECOMPOSED not in i.labels
         and WORKFLOW_BLOCKED not in i.labels
         and monitor._this_monitor_owns(i)
         and (i.batch_id is not None or ownership_label in i.labels)),
        None,
    )
    if target is None:
        return
    # The decompose dispatch is wait=True, so it blocks this (sequential) tick —
    # no concurrent re-pick. On success the intent leaves the revisada queue.
    outcome = await monitor.implementer.decompose(monitor, target)
    derived = parse_decompose_result(outcome.text)
    if not outcome.ok and not derived:
        logger.warning("decompose #%d failed: %s", target.number, (outcome.error or "")[:200])
        return  # stays revisada — retry next tick
    # Diagnostic (#2): the parser returned [] but the architect may still have
    # created issues via gh in its run. Log the tail of the outcome so we can
    # see what format escaped the regex+fallback, and fall back to scraping the
    # GitHub state directly (architect references them with #N in the comment).
    if outcome.ok and not derived:
        tail = (outcome.text or "")[-600:].replace("\n", " | ")
        logger.warning(
            "decompose #%d: ok but parser returned [] — outcome tail: %s",
            target.number, tail,
        )
    # Mark decomposed when derived issues were created (even if the ok flag is
    # noisy) so we never re-decompose and duplicate the derived issues.
    try:
        await monitor.forge.transition_issue(
            target.number, from_label=WORKFLOW_REVIEWED, to_label=WORKFLOW_DECOMPOSED
        )
    except GhCommandError as exc:
        await _record_forge_error(monitor, f"could not mark #{target.number} decomposed", exc)
    logger.info("decompose #%d → derivadas %s", target.number, derived)
    await monitor.notifier.issue_reviewed(
        target.number, f"{target.title} (decomposta em {len(derived)})", target.url
    )


# ----- stage 2: implement ------------------------------------------------

async def implement_one_reviewed_issue(monitor: "PipelineMonitor") -> None:
    """Stage 2 (code path). Claim up to ``max_parallel`` reviewed feature/bug/
    refactor issues and dispatch their implementations CONCURRENTLY (issue #257):
    ``asyncio.gather`` of independent worker dispatches that the k8s Service
    spreads across the worker replicas. Each opens its own branch + PR, so the
    only contention surfaces at merge time. ``intent`` issues are excluded — they
    go to :func:`decompose_one_reviewed_intent`. The tick blocks for the duration
    of the SLOWEST dispatch in the batch (not the sum). Per-issue finalization
    reuses :func:`_finalize_implement_outcome` unchanged.
    """
    try:
        issues = await monitor.forge.list_issues_with_label(WORKFLOW_REVIEWED, limit=50)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, "could not list reviewed issues (forge error)", exc,
            notifier_label="implement/list",
        )
        return
    # Accept issues without ~batch: when the ownership label proves this monitor did the
    # review (e.g. operator manually promoted to ~workflow:revisada or batch label removed).
    ownership_label = monitor.identity.ownership_label()
    candidates = [
        i for i in issues
        # intent decomposes (separate stage), it does not implement code.
        if issue_type_from_labels(i.labels) != TYPE_INTENT
        # Defense-in-depth: never re-pick an issue already claimed for
        # implementation, or one parked/blocked (the bug class behind #253/#254).
        and WORKFLOW_PR not in i.labels
        and WORKFLOW_IMPLEMENTING not in i.labels
        and WORKFLOW_BLOCKED not in i.labels
        and monitor._this_monitor_owns(i)
        and (i.batch_id is not None or ownership_label in i.labels)
    ]
    # Cap concurrency at ``max_parallel`` (>=1). Each claimed issue leaves the
    # revisada set on its transition, so later ticks won't re-pick it.
    batch = candidates[: max(1, monitor.config.max_parallel)]
    if not batch:
        return

    claimed = []
    for target in batch:
        # Dedup guard (issue #257), gate-only: if an OPEN PR already implements
        # this issue — belt-and-suspenders behind the mention/gate integration —
        # do NOT open a second PR. Park it in em_pr so it leaves the queue (the
        # existing PR is the work).
        if monitor.config.enable_refinement_gate and await monitor.forge.has_open_pr_for_issue(target.number):
            logger.info("implement #%d: PR aberta já existe — parkando em em_pr (sem duplicar)", target.number)
            try:
                await monitor.forge.transition_issue(
                    target.number, from_label=WORKFLOW_REVIEWED, to_label=WORKFLOW_PR
                )
            except GhCommandError as exc:
                await _record_forge_error(monitor, f"could not park #{target.number} in em_pr", exc)
            # Drop any stale refine residue so the issue carries one ~workflow:.
            await monitor.forge.remove_labels(
                "issue", target.number, [REFINAR, *REFINE_WORKFLOW_STATES]
            )
            monitor._resume_tracker.clear(target.number)
            continue
        # Best-effort claim (sequential-tick lock): revisada → em_implementacao.
        # transition_issue is remove-then-add (not atomic); multi-monitor safety
        # relies on the PID lock + single-replica Recreate + hash sharding.
        try:
            await monitor.forge.transition_issue(
                target.number, from_label=WORKFLOW_REVIEWED, to_label=WORKFLOW_IMPLEMENTING
            )
        except GhCommandError as exc:
            await _record_forge_error(
                monitor, f"could not claim issue #{target.number} for implementation", exc,
                notifier_label=f"implement claim #{target.number}",
            )
            continue
        # Defensive (gate-only): an issue reaching implementation carries exactly
        # one ~workflow: state — drop any refine residue (em_arquitetura/refinar)
        # left by a raced gate cycle.
        if monitor.config.enable_refinement_gate:
            await monitor.forge.remove_labels(
                "issue", target.number, [REFINAR, *REFINE_WORKFLOW_STATES]
            )
        await monitor.notifier.implementation_started(
            target.number, target.title, monitor.branch_for_issue(target.number)
        )
        monitor._resume_tracker.record_dispatch(target.number, _monotonic())
        claimed.append(target)
    if not claimed:
        return

    # Dispatch all claimed implementations concurrently; the worker Service load-
    # balances them across replicas. ``return_exceptions`` so one failure does not
    # cancel its siblings — each is finalized independently from ground truth.
    # Wrap em ``wait_for`` com cap generoso (3h) — defesa contra HTTP-hang
    # silencioso onde nem o worker timeout (10min) nem o TCP keepalive
    # disparam: o gather inteiro travaria, segurando o stop_event do monitor.
    # Pilar 03 §1 — toda I/O externa precisa de teto explícito de tempo.
    #
    # LIMITAÇÃO conhecida (PR #297 review iter 2): quando o cap dispara, o
    # ``await`` é cancelado client-side mas o worker continua processando
    # server-side até completar — não há endpoint DELETE /v1/tasks/<id> hoje.
    # Risco residual: worker pode abrir PR DEPOIS que o monitor já marcou
    # a issue como falha. Mitigação operacional: o finalizador checa
    # ground-truth (PR existe / branch tem commits) ANTES de declarar
    # resultado, então o próximo tick reconcilia a issue (não é silenciosa-
    # mente perdida — pior caso é re-tentativa duplicada, capturada pela
    # idempotência de label ``~workflow:em_pr``).
    _DISPATCH_HARD_CAP_S = 3 * 60 * 60
    try:
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                *[monitor.implementer.implement(monitor, t, resume=False) for t in claimed],
                return_exceptions=True,
            ),
            timeout=_DISPATCH_HARD_CAP_S,
        )
    except asyncio.TimeoutError:
        logger.error(
            "implement gather() excedeu o cap de %ds com %d targets; abortando o "
            "batch client-side (worker pode continuar — sem cancel explícito; "
            "próximo tick reconcilia via ground-truth)",
            _DISPATCH_HARD_CAP_S, len(claimed),
        )
        outcomes = [
            asyncio.TimeoutError(f"dispatch hard cap exceeded ({_DISPATCH_HARD_CAP_S}s)")
            for _ in claimed
        ]
    for target, outcome in zip(claimed, outcomes):
        if isinstance(outcome, BaseException):
            logger.exception("implement dispatch for #%d raised", target.number, exc_info=outcome)
            from deile.orchestration.pipeline.implementer import WorkOutcome
            outcome = WorkOutcome(
                ok=False, text="", error=f"{type(outcome).__name__}: {outcome}"[:500]
            )
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
        issues = await monitor.forge.list_issues_with_label(WORKFLOW_IMPLEMENTING, limit=50)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, "could not list in-progress issues (forge error)", exc,
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
        # Adaptive escalation (#6): two consecutive failures of the same kind
        # (TIMEOUT, WORKER_UNREACHABLE, etc.) usually point at a non-transient
        # cause — escalate to block instead of burning the full resume ceiling.
        err_kind = _classify_outcome_error(outcome.error or "")
        # Issue #309 fase 3 (estratégia C — resiliência auth): se o
        # claude-worker reportou OAuth expirado, BLOQUEAR direto (sem
        # streak, sem retry) — token só renova via host, retentar é
        # desperdício. Comment + label deterministicos + ação clara.
        if err_kind == "WORKER_AUTH_EXPIRED":
            logger.warning(
                "implement #%d: claude-worker auth expired — block fast",
                number,
            )
            await _block_issue(monitor, number, AUTH_EXPIRED_BLOCK_MSG)
            return
        streak = monitor._resume_tracker.record_failure(number, err_kind)
        if streak >= 2 and err_kind in _ESCALATE_ON_REPEAT:
            logger.warning(
                "implement #%d: 2x %s consecutive — escalating to block",
                number, err_kind,
            )
            await _block_issue(
                monitor, number,
                f"falha repetida ({err_kind}) em duas tentativas seguidas — "
                f"causa provavelmente não-transitória; humano deve intervir.",
            )
            return
        logger.error(
            "implement #%d failed: %s — parked in %s",
            number, err_detail, WORKFLOW_IMPLEMENTING,
        )
        await _park_or_keep(monitor, number, err_detail, resume=resume)
        return

    # 3. CONCLUÍDO — a real PR exists (and, when expected, was merged).
    if ended == _ENDED_CONCLUIDO or (not ended and outcome.ok and pr_url):
        try:
            await monitor.forge.transition_issue(
                number, from_label=WORKFLOW_IMPLEMENTING, to_label=WORKFLOW_PR
            )
        except GhCommandError as exc:
            await _record_forge_error(
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
    # Dedicated ceiling for "agent finished but no PR" (#10) — this class of
    # failure tends to be irrecoverable (the LLM gave up on the task structure
    # or fundamentally misunderstood the brief), so a tighter cap than
    # ``resume_max_attempts`` makes sense. #283 hit 50+ of these before the
    # operator blocked it manually.
    incomplete_count = monitor._resume_tracker.bump_incomplete_no_pr(number)
    ceiling = getattr(monitor.config, "incomplete_no_pr_max", 3)
    if incomplete_count >= ceiling:
        logger.warning(
            "implement #%d: %d-th 'incompleto sem PR' — escalating to block",
            number, incomplete_count,
        )
        await _block_issue(
            monitor, number,
            f"agente finalizou sem abrir PR {incomplete_count}x consecutivas "
            f"(teto {ceiling}) — provável incapacidade de cumprir o escopo; "
            f"humano deve revisar a issue.",
        )
        return
    logger.warning(
        "implement #%d: incompleto (sem PR) %d/%d — parked in %s%s",
        number, incomplete_count, ceiling, WORKFLOW_IMPLEMENTING,
        " (será retomada)" if resume else "",
    )
    await _park_or_keep(
        monitor, number, "o agente finalizou sem abrir PR", resume=resume
    )


# --- Adaptive resume escalation (#6) -----------------------------------------
# When the same kind of failure repeats N times in a row on the same issue, the
# cause is almost certainly NOT transient. Burning the full resume ceiling
# (10 dispatches × ~10min each) hitting the same wall wastes ~$5-10. Two
# consecutive identical failures suffice to escalate to block.

#: Error kinds whose 2x-in-a-row recurrence triggers immediate block. Excluded:
#: WORKER_UNREACHABLE (transient — pod restart, network blip) and unknown.
_ESCALATE_ON_REPEAT = frozenset({"TIMEOUT", "BAD_REQUEST"})


def _classify_outcome_error(error: str) -> str:
    """Return a short signature for an outcome error message (or '' if empty).

    Adicionado em #309 fase 3 (estratégia C — resiliência auth):
    ``WORKER_AUTH_EXPIRED`` é o sinal explícito do claude-worker server
    quando o ``claude -p`` detecta OAuth token expirado/inválido. O
    monitor trata esse caso BLOQUEANDO a issue/PR com mensagem clara,
    em vez de retentar (token só renova via host).
    """
    if not error:
        return ""
    e = error.upper()
    if "WORKER_AUTH_EXPIRED" in e:
        return "WORKER_AUTH_EXPIRED"
    if "TIMEOUT" in e:
        return "TIMEOUT"
    if "WORKER_UNREACHABLE" in e or "CONNECTERROR" in e or "REMOTEPROTOCOL" in e:
        return "WORKER_UNREACHABLE"
    if "BAD_REQUEST" in e or "VALIDATION" in e:
        return "BAD_REQUEST"
    return "OTHER"


#: Texto fixo apresentado ao operador quando o claude-worker reporta
#: ``WORKER_AUTH_EXPIRED``. Citado como ``comment`` no ``_block``: o
#: bloqueio é DETERMINÍSTICO (token só renova via host) e a ação está
#: claramente documentada em 1 comando.
AUTH_EXPIRED_BLOCK_MSG = (
    "⛔ claude-worker reportou OAuth token expirado/inválido "
    "(`WORKER_AUTH_EXPIRED`). Não vou retentar — token só pode ser "
    "renovado via host.\n\n"
    "**Como destravar (1 comando):**\n"
    "```bash\n"
    "python3 infra/k8s/deploy.py k8s claude-renew\n"
    "```\n"
    "Depois remova esta label `~workflow:bloqueada` para o pipeline "
    "tentar de novo."
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
        monitor.forge.comment_on_issue if kind == "issue"
        else monitor.forge.comment_on_pr
    )
    try:
        await commenter(number, comment)
    except Exception as exc:  # noqa: BLE001 — comment is best-effort; label still applied
        logger.warning("block %s: could not comment on #%d: %s", kind, number, exc)
    try:
        await monitor.forge.add_labels(kind, number, [WORKFLOW_BLOCKED])
    except GhCommandError as exc:
        await _record_forge_error(
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
        prs = await monitor.forge.list_open_prs(limit=50)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, "could not list PRs (forge error)", exc,
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
    batch = await monitor.forge.claim_with_batch("pr", target.number)
    if batch is None:
        return
    # Tag ownership so other monitors can identify who claimed this PR —
    # mirrors the identical pattern in stage 1 for issues.
    await monitor.forge.add_labels("pr", target.number, [monitor.identity.ownership_label()])
    if is_resume:
        state = monitor._resume_tracker.get(target.number)
        # Attempt ceiling for review/merge — same block flow as implement.
        if state.attempt >= monitor.config.resume_max_attempts:
            await monitor.forge.clear_batch_label("pr", target.number)
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
            await monitor.forge.transition_pr(
                target.number, from_label=REVIEW_PENDING, to_label=REVIEW_IN_PROGRESS
            )
        except GhCommandError:
            # ~review:pendente may not be set; that's ok.
            await monitor.forge.add_labels("pr", target.number, [REVIEW_IN_PROGRESS])
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
        # Issue #309 fase 3 (estratégia C — auth-expired guard): bloqueia
        # fast com mensagem clara em vez de cair em retry/escalation
        # genérico. claude-worker já não pode entregar nada até renovar.
        if _classify_outcome_error(outcome.error or "") == "WORKER_AUTH_EXPIRED":
            logger.warning(
                "pr_review #%d: claude-worker auth expired — block fast",
                target.number,
            )
            await monitor.forge.clear_batch_label("pr", target.number)
            await _block_pr(
                monitor, target.number, target.title, target.url,
                AUTH_EXPIRED_BLOCK_MSG,
            )
            return
        # Issue #309 fase 3.5 — Bug A fix: erro NÃO-auth do worker NÃO
        # deve fluir pro fast-finish legacy abaixo (que marcava
        # ~review:concluida sem proof-of-work — vide R2/PR #344, 5s).
        # Libera o batch; reaper retoma no próximo tick (resume real
        # se sessão claude sobreviveu, fresh dispatch caso contrário).
        # Skip dispatch-skipped-still-running (já intencional do resumer).
        if "DISPATCH_SKIPPED_STILL_RUNNING" in (outcome.error or ""):
            logger.info(
                "pr_review #%d: dispatch skipped (claude ainda alive) — "
                "manter em_andamento", target.number,
            )
            await monitor.forge.clear_batch_label("pr", target.number)
            return
        logger.warning(
            "pr_review #%d: worker error não-auth (%s); liberando batch pra reaper "
            "retomar (não marca concluida sem proof-of-work — Bug A fix)",
            target.number, (outcome.error or "")[:120],
        )
        await monitor.forge.clear_batch_label("pr", target.number)
        return

    if blocked:
        await monitor.forge.clear_batch_label("pr", target.number)
        await _block_pr(
            monitor, target.number, target.title, target.url,
            outcome.motivo_bloqueio or "o agente declarou BLOQUEADO sem motivo",
        )
        return

    if merged:
        try:
            await monitor.forge.transition_pr(
                target.number, from_label=REVIEW_IN_PROGRESS, to_label=REVIEW_CONCLUDED
            )
        except GhCommandError as exc:
            await _record_forge_error(
                monitor, f"could not transition PR #{target.number} to concluida", exc,
            )
        await monitor.forge.clear_batch_label("pr", target.number)
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
            await monitor.forge.clear_batch_label("pr", target.number)
            await _block_pr(
                monitor, target.number, target.title, target.url,
                "duas tentativas de review/merge sem progresso (diff idêntico)",
            )
            return
        # Release the batch lock so the next tick can re-claim; keep em_andamento.
        await monitor.forge.clear_batch_label("pr", target.number)
        logger.info("pr_review #%d incompleto — em_andamento (será retomada)", target.number)
        return

    # Issue #309 fase 3.5 — Bug B fix: proof-of-work check antes de marcar
    # CONCLUDED no caminho legacy (resume desligado). Sem evidência (comment
    # do bot, review formal, merge, novo commit) NÃO marca concluida —
    # libera batch pra reaper retomar (impede review-theatre silencioso
    # observado no R2 da PR #344 onde labels alternaram em 5s sem qualquer
    # ação real do worker).
    bot_login = await _resolve_bot_login(monitor)
    has_proof = await _assert_review_proof_of_work(
        monitor.forge, "pr", target.number, bot_login,
        since_ts=int(time.time() - 7200),  # janela: últimas 2h
    )
    if not has_proof:
        logger.warning(
            "pr_review #%d: worker reportou ok=True mas SEM proof-of-work "
            "(zero comments, zero reviews, zero novos commits) — não marcando "
            "concluida (Bug B fix). Libera batch; reaper retoma.",
            target.number,
        )
        await monitor.forge.clear_batch_label("pr", target.number)
        return

    try:
        await monitor.forge.transition_pr(
            target.number, from_label=REVIEW_IN_PROGRESS, to_label=REVIEW_CONCLUDED
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, f"could not transition PR #{target.number} to concluida", exc,
        )
    await monitor.forge.clear_batch_label("pr", target.number)
    monitor._stats.prs_reviewed += 1
    await monitor.notifier.pr_reviewed(target.number, target.title, target.url, merged=False)


async def _resolve_bot_login(monitor: "PipelineMonitor") -> str:
    """Resolve o login do bot (best-effort). Default 'deile-one'.

    Pra ser usado no proof-of-work check: precisa saber QUAL author é o bot
    pra distinguir comment seu vs comment humano. Hardcoded em V1 (default
    do pipeline); pode evoluir pra ler de settings/identity.
    """
    return "deile-one"


async def _assert_review_proof_of_work(
    forge,
    kind: str,
    number: int,
    bot_login: str,
    *,
    since_ts: int,
) -> bool:
    """True se há pelo menos UMA evidência de trabalho real desde ``since_ts``:

    1. Bot postou comment no PR/issue
    2. Bot postou review formal (APPROVE/REQUEST_CHANGES/COMMENT)
    3. PR foi merged
    4. Há commit novo no branch

    Sem suporte do forge (métodos retornam None/raise): assume True (não
    bloqueia o fluxo legacy onde forge antigo está em uso — fail-open
    porque é guard defensivo, não autorização).
    """
    try:
        if hasattr(forge, "has_bot_activity_since"):
            return await forge.has_bot_activity_since(
                kind, number, bot_login, since_ts=since_ts,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "proof_of_work check: forge.has_bot_activity_since raised: %s — "
            "assuming true (fail-open)", exc,
        )
        return True
    # Forge não suporta proof-of-work check — fail-open.
    return True


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
        pr_body = await monitor.forge.get_pr_body(pr_number)
        pr_comments = await monitor.forge.list_pr_comments(pr_number)
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
            number = await monitor.forge.create_issue(
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
        await monitor.forge.comment_on_pr(pr_number, report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage 4: could not post follow-up report on PR #%s: %s", pr_number, exc)

    await monitor.notifier.follow_ups_processed(pr_number, len(opened), len(skipped))


# ----- standalone stage 4: follow_ups action -----------------------------

async def standalone_follow_ups(monitor: "PipelineMonitor") -> None:
    """Process follow-ups for recently merged PRs that haven't been processed yet.

    This is the standalone version of stage 4, invocable via the schedule
    without requiring a concurrent stage 3 run.  Idempotency is enforced by
    the :data:`FOLLOW_UPS_PROCESSED` marker label: PRs that already have
    this label are skipped.
    """
    try:
        merged_prs = await monitor.forge.list_recently_merged_prs()
    except Exception as exc:  # noqa: BLE001
        logger.warning("standalone follow_ups: could not list merged PRs: %s", exc)
        return

    for pr in merged_prs:
        if FOLLOW_UPS_PROCESSED in pr.labels:
            continue
        await monitor._stage4_follow_ups(pr.number, pr.title, pr.url)
        try:
            await monitor.forge.add_labels("pr", pr.number, [FOLLOW_UPS_PROCESSED])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "standalone follow_ups: could not mark PR #%d processed: %s",
                pr.number, exc,
            )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _extract_pr_url(text: str) -> Optional[str]:
    """Return the last PR/MR URL found in *text* (gap #14).

    Using the last match avoids picking up example URLs or log lines that
    appear earlier in the output before the actual PR/MR URL the agent
    prints on the final line.

    Forge-aware (issue #297): recognises both GitHub ``/pull/N`` and GitLab
    ``/-/merge_requests/N`` URLs, plus any extra custom hosts declared via
    ``DEILE_GITHUB_HOST`` / ``DEILE_GITLAB_HOST``.
    """
    if not text:
        return None
    return find_last_pr_url(text, **declared_hosts())


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


# --------------------------------------------------------------------------- #
# Issue #309 fase 3.5 — Reaper de claim órfão
# --------------------------------------------------------------------------- #


async def reap_orphan_claims(monitor: "PipelineMonitor") -> None:
    """Scan ~review:em_andamento e ~workflow:em_implementacao com idade >
    ``config.reaper_stale_seconds`` sem progresso e libera (próximo tick
    re-claim via resume). Best-effort: catch + log nas operações de label.

    Mecânica:
    1. Lista PRs abertas e issues abertas com label terminal-stale.
    2. Pra cada uma, lê ``label_applied_at`` da label terminal.
    3. Se idade > threshold:
       - Lê ``current_attempt`` das labels ~attempt:N (default 0).
       - Se ``attempt + 1 >= reaper_max_attempts``: marca ~workflow:bloqueada
         + ~retry:exhausted (não retorna pra fila — humano decide).
       - Senão: remove ~review:em_andamento (ou ~workflow:em_implementacao),
         remove batch_label e ownership, adiciona ~attempt:(N+1), recoloca
         label inicial (~review:pendente ou ~workflow:nova).

    Não toca em PRs sem dispatch do nosso monitor (ownership label) —
    apenas escopa às próprias.
    """
    threshold = monitor.config.reaper_stale_seconds
    max_attempts = monitor.config.reaper_max_attempts
    if threshold <= 0:
        return
    now_ts = int(time.time())
    own_label = monitor.identity.ownership_label()

    # PRs com ~review:em_andamento (stuck no review).
    try:
        prs = await monitor.forge.list_open_prs()
    except GhCommandError as exc:
        await _record_forge_error(monitor, "reaper: list_open_prs failed", exc)
        return
    for pr in prs:
        if REVIEW_IN_PROGRESS not in pr.labels:
            continue
        # Só re-claim PRs deste monitor (ownership).
        if own_label not in pr.labels:
            continue
        applied_at = await monitor.forge.label_applied_at(
            "pr", pr.number, REVIEW_IN_PROGRESS,
        )
        if applied_at is None:
            continue  # forge sem suporte ou label sem timestamp
        age = now_ts - applied_at
        if age < threshold:
            continue
        await _reap_one(
            monitor, kind="pr", number=pr.number, labels=pr.labels,
            from_label=REVIEW_IN_PROGRESS, to_label=REVIEW_PENDING,
            max_attempts=max_attempts, age_seconds=age,
            description=f"PR #{pr.number} review stuck há {age // 60}min",
        )

    # Issues com ~workflow:em_implementacao (stuck no implement).
    try:
        impl_issues = await monitor.forge.list_issues_with_label(
            WORKFLOW_IMPLEMENTING,
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, "reaper: list_issues_with_label failed", exc,
        )
        return
    for issue in impl_issues:
        if own_label not in issue.labels:
            continue
        applied_at = await monitor.forge.label_applied_at(
            "issue", issue.number, WORKFLOW_IMPLEMENTING,
        )
        if applied_at is None:
            continue
        age = now_ts - applied_at
        if age < threshold:
            continue
        await _reap_one(
            monitor, kind="issue", number=issue.number, labels=issue.labels,
            from_label=WORKFLOW_IMPLEMENTING, to_label=WORKFLOW_REVIEWED,
            max_attempts=max_attempts, age_seconds=age,
            description=f"issue #{issue.number} implement stuck há {age // 60}min",
        )


async def _reap_one(
    monitor: "PipelineMonitor",
    *,
    kind: str,
    number: int,
    labels,
    from_label: str,
    to_label: str,
    max_attempts: int,
    age_seconds: int,
    description: str,
) -> None:
    """Reaper helper — libera UM claim órfão.

    Se ``current_attempt + 1 >= max_attempts``: marca bloqueada + retry:exhausted
    + post comment explicativo. Senão libera: remove from_label, batch, ownership,
    adiciona ~attempt:(N+1), recoloca to_label (pendente/nova). Falhas em
    operações individuais NÃO derrubam o tick — best-effort.
    """
    current_attempt = current_attempt_from_labels(labels)
    next_attempt = current_attempt + 1
    # Coleta labels a remover: a label terminal, batch label, ownership e o
    # ~attempt:N anterior (se existir — vamos colocar N+1).
    to_remove = [from_label]
    batch_labels = [lb for lb in labels if is_batch_label(lb)]
    to_remove.extend(batch_labels)
    own_label = monitor.identity.ownership_label()
    if own_label in labels:
        to_remove.append(own_label)
    old_attempts = [lb for lb in labels if is_attempt_label(lb)]
    to_remove.extend(old_attempts)

    if next_attempt >= max_attempts:
        # Esgotou: bloqueia em vez de liberar.
        try:
            await monitor.forge.remove_labels(kind, number, to_remove)
        except GhCommandError as exc:
            logger.warning(
                "reaper #%d: remove_labels failed: %s", number, exc,
            )
        try:
            await monitor.forge.add_labels(
                kind, number,
                [WORKFLOW_BLOCKED, make_attempt_label(next_attempt)],
            )
        except GhCommandError as exc:
            logger.warning(
                "reaper #%d: add bloqueada failed: %s", number, exc,
            )
        msg = (
            f"⛔ Reaper esgotou retries ({next_attempt}/{max_attempts}) — "
            f"{description}. Pipeline marca `~workflow:bloqueada` pra "
            f"intervenção humana. Remova o label pra reabrir o fluxo."
        )
        try:
            if kind == "pr":
                await monitor.forge.comment_on_pr(number, msg)
            else:
                await monitor.forge.comment_on_issue(number, msg)
        except GhCommandError as exc:
            logger.warning("reaper #%d: comment failed: %s", number, exc)
        monitor._stats.issues_blocked += 1
        logger.warning(
            "reaper BLOCKED %s #%d after %d attempts (age=%ds)",
            kind, number, next_attempt, age_seconds,
        )
        return

    # Libera: remove labels stale, adiciona ~attempt:(N+1) + label de retorno.
    try:
        await monitor.forge.remove_labels(kind, number, to_remove)
    except GhCommandError as exc:
        logger.warning("reaper #%d: remove_labels failed: %s", number, exc)
    try:
        await monitor.forge.add_labels(
            kind, number, [to_label, make_attempt_label(next_attempt)],
        )
    except GhCommandError as exc:
        logger.warning("reaper #%d: add_labels failed: %s", number, exc)
    logger.info(
        "reaper RELEASED %s #%d to %s (attempt %d/%d, age=%ds)",
        kind, number, to_label, next_attempt, max_attempts, age_seconds,
    )
