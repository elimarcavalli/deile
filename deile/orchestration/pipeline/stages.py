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
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, List, Optional, Tuple

from deile.orchestration.forge import (
    CommentRef,
    GhCommandError,
    IssueRef,
    MentionTrigger,
    declared_hosts,
    find_last_pr_url,
)
from deile.orchestration.forge.refs import compute_batch_id_for_number
from deile.orchestration.pipeline import pipeline_logger
from deile.orchestration.pipeline._time_utils import format_iso_utc, now_utc
from deile.orchestration.pipeline.constants import PIPELINE_MSG_TRUNCATE_CHARS
from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
from deile.orchestration.pipeline.dispatch_resolver import resolve_stage_max_retries
from deile.orchestration.pipeline.follow_up_detector import detect_follow_ups
from deile.orchestration.pipeline.gc import run_terminal_gc
from deile.orchestration.pipeline.implementer import (
    _review_was_blocked,
    parse_critique_verdict,
    parse_decompose_result,
    parse_refine_verdict,
)
from deile.orchestration.pipeline.labels import (
    FOLLOW_UPS_PROCESSED,
    GATE_REDISPATCHES_COMMENT,
    MENTION_DONE,
    PRIORITY_0,
    PRIORITY_1,
    PRIORITY_2,
    PRIORITY_3,
    REFINAR,
    REFINE_WORKFLOW_STATES,
    REVIEW_CONCLUDED,
    REVIEW_IN_PROGRESS,
    REVIEW_PENDING,
    TYPE_INTENT,
    WORKFLOW_ARCHITECTURE,
    WORKFLOW_BLOCKED,
    WORKFLOW_DECOMPOSED,
    WORKFLOW_IMPLEMENTING,
    WORKFLOW_NEW,
    WORKFLOW_PR,
    WORKFLOW_REFINING,
    WORKFLOW_REVIEWED,
    WORKFLOW_REVIEWING,
    WORKFLOW_WAITING,
    current_attempt_from_labels,
    current_refine_attempt_from_labels,
    is_attempt_label,
    is_batch_label,
    is_refine_attempt_label,
    issue_type_from_labels,
    make_attempt_label,
    make_refine_attempt_label,
    parse_priority_from_labels,
    persona_for_type,
    refine_workflow_state,
)
from deile.orchestration.pipeline.pipeline_logger import (
    log_auth_backoff,
    log_auth_fail,
    log_auth_recover,
    log_auth_skip,
    log_decomposition_fanout,
    log_reaper_block,
    log_reaper_unblock,
    log_refinement_critique,
    log_refinement_refine,
    log_routing_dropped,
    log_routing_mention,
    log_routing_pr_unified,
)

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

#: Fix #8 (issue #521) — teto de dispatches de auto-correção da PRÓPRIA PR.
#: Quando a review da nossa PR conclui REQUEST_CHANGES e o HEAD não muda, em
#: vez de bloquear direto (Fix A), o pipeline despacha UMA task de address
#: (implement + push) para o worker aplicar o fix. O HEAD muda → próxima review
#: valida e segue pro merge. Se após N tentativas o HEAD AINDA não mudou, o
#: worker não conseguiu → bloqueia para o humano. Começa em 1: uma chance de
#: auto-fix é o equilíbrio entre autonomia e queima de tokens — o worker recebe
#: o feedback exato do reviewer no brief, então uma passada deveria bastar; se
#: falhar, escalar para o humano é mais barato que rodar address↔review em loop.
MAX_ADDRESS_ATTEMPTS = 1

logger = logging.getLogger(__name__)

# --- Priority sorting (issue #369) ----------------------------------------


def sort_by_priority(candidates):
    """Sort *candidates* (IssueRef or PrRef) by priority — lower N = more urgent.

    Items with a ``~prioridade:N`` label are ordered by N (0 first, 3 last).
    Items without any priority label are placed after all prioritized items.
    Tiebreaker: lower issue/PR number wins (deterministic).

    The function is pure (no I/O) — it works client-side on the list already
    returned by the forge. More sophisticated tiebreakers (merge-base distance,
    commit count, diff size) are deferred to a future iteration.
    """

    def _key(c):
        p = parse_priority_from_labels(getattr(c, "labels", ()))
        # Priority group: 0 for items with a priority label (ordered by N),
        # 1 for items without (placed last).
        if p is not None:
            return (0, p, getattr(c, "number", 0))
        return (1, 0, getattr(c, "number", 0))

    return sorted(candidates, key=_key)


# --- Commit classification (issue #351) ------------------------------------

# File extensions considered "code" for the purpose of re-review classification.
_CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".ts",
        ".js",
        ".jsx",
        ".tsx",
        ".yaml",
        ".yml",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".toml",
        ".cfg",
        ".ini",
    }
)

# Extensions / path prefixes considered "docs-only".
_DOCS_EXTENSIONS = frozenset({".md", ".rst", ".txt", ".adoc"})
_DOCS_PREFIXES = ("docs/", "documentation/")

# Commit classification results (issue #351).
CLASS_DOCS_ONLY = "docs-only"
CLASS_COSMETIC = "cosmético"
CLASS_CODE = "código"


def _classify_new_commits(commits: list[dict]) -> str:
    """Classify a list of commits into docs-only / cosmético / código.

    Heuristic (Option A — issue #351):
    - **docs-only**: ALL changed files are in ``docs/**`` or end with
      ``.md`` / ``.rst`` / ``.txt`` / ``.adoc``.
    - **cosmético**: NO code files changed AND not docs-only (e.g.
      ``.gitignore``, ``README.md`` at repo root, CI config changes).
    - **código**: at least one code file (``.py``, ``.ts``, etc.) changed.

    When commit info is unavailable (empty list, no files), the safe default
    is ``código`` (full re-review).

    Note: "não-solicitado" classification (comparing diff against issue
    body) is NOT implemented here — the stakeholder's comment on #351
    explicitly said it is "adicional" (out of scope for this issue).
    """
    if not commits:
        return CLASS_CODE  # safety default

    all_files: list[str] = []
    for c in commits:
        files = c.get("files") or []
        if isinstance(files, list):
            all_files.extend(files)

    if not all_files:
        return CLASS_CODE  # no file info available

    # Check if all files are docs.
    def _is_docs(f: str) -> bool:
        f_lower = f.lower()
        return any(f_lower.endswith(ext) for ext in _DOCS_EXTENSIONS) or any(
            f_lower.startswith(pfx) for pfx in _DOCS_PREFIXES
        )

    if all(_is_docs(f) for f in all_files):
        return CLASS_DOCS_ONLY

    # Check if any code file was touched.
    has_code = any(
        any(f.lower().endswith(ext) for ext in _CODE_EXTENSIONS) for f in all_files
    )

    if has_code:
        return CLASS_CODE

    # Not docs-only, not code → cosmetic (config changes, etc.).
    return CLASS_COSMETIC


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
    :func:`_record_forge_error` (using ``error_context`` as the log prefix and
    the optional ``notifier_label`` for the Discord notification).
    """
    if monitor.identity.shard_count <= 1:
        return True
    try:
        batch = await monitor.forge.claim_with_batch(kind, number)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            f"{error_context} #{number} failed",
            exc,
            notifier_label=notifier_label,
        )
        return False
    if batch is None:
        logger.debug(
            "%s #%s already claimed by another monitor; skipping", kind, number
        )
        return False
    pipeline_logger.log_batch_claim(sha=batch, issues=[number], reason=error_context)
    return True


async def _release_classify_claim(
    monitor: "PipelineMonitor", kind: str, number: int
) -> None:
    """Release the ``~batch:`` lock so the next stage can re-claim the item.

    Best-effort: the workflow label is already applied, so a clear failure must
    not abort the loop. No-op for single-monitor deployments (they never claim).
    """
    if monitor.identity.shard_count <= 1:
        return
    try:
        await monitor.forge.clear_batch_label(kind, number)
        pipeline_logger.log_batch_release(
            sha=compute_batch_id_for_number(kind, number),
            reason="classify_released",
        )
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
            monitor,
            "could not list unclassified issues (forge error)",
            exc,
            notifier_label="classify/list",
        )
        return
    except Exception as exc:  # noqa: BLE001 — JSON parse error etc.
        monitor._stats.errors += 1
        logger.error("could not list unclassified issues: %s", exc)
        return

    for issue in sort_by_priority(issues):
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
            monitor,
            "issue",
            issue.number,
            error_context="auto-classify claim",
            notifier_label=f"auto-classify claim #{issue.number}",
        ):
            continue
        try:
            await monitor.forge.add_labels("issue", issue.number, [WORKFLOW_NEW])
        except GhCommandError as exc:
            await _record_forge_error(
                monitor,
                f"auto-classify label #{issue.number} failed",
                exc,
                notifier_label=f"auto-classify #{issue.number}",
            )
            continue
        except Exception as exc:  # noqa: BLE001 — best-effort, never abort loop
            monitor._stats.errors += 1
            logger.error("auto-classify label #%s failed: %s", issue.number, exc)
            await monitor.notifier.error(
                f"auto-classify #{issue.number}", f"{type(exc).__name__}: {exc}"
            )
            continue
        # Release the classify claim so Stage 1 (review) can pick the issue up
        # via its own claim — review_one_new_issue only considers issues with
        # ``batch_id is None``. Without this the auto-classify → review handoff
        # deadlocks (the issue stays ~nova forever, batch-locked).
        await _release_classify_claim(monitor, "issue", issue.number)
        monitor._stats.issues_classified += 1
        logger.info("auto-classified issue #%s as %s", issue.number, WORKFLOW_NEW)
        await monitor.notifier.issue_auto_classified(
            issue.number, issue.title, issue.url
        )
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
        except (
            Exception
        ) as exc:  # noqa: BLE001 — comment is best-effort; label already applied
            logger.warning(
                "auto-classify comment #%s failed (label applied): %s",
                issue.number,
                exc,
            )


# ----- PR triage: classify open non-draft PRs with no pipeline labels ----


async def classify_new_prs(monitor: "PipelineMonitor") -> None:
    """Apply ``~review:pendente`` to open non-draft PRs that have no pipeline labels."""
    try:
        prs = await monitor.forge.list_unclassified_prs()
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            "could not list unclassified PRs (forge error)",
            exc,
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
        if not await _claim_for_classify(
            monitor, "pr", pr.number, error_context="pr_triage claim"
        ):
            continue
        try:
            await monitor.forge.add_labels("pr", pr.number, [REVIEW_PENDING])
        except GhCommandError as exc:
            await _record_forge_error(
                monitor,
                f"pr_triage label #{pr.number} failed",
                exc,
                notifier_label=f"pr_triage #{pr.number}",
            )
            continue
        # Release the batch claim so Stage 3 can pick this PR up via its own claim.
        await _release_classify_claim(monitor, "pr", pr.number)
        monitor._stats.prs_classified += 1
        logger.info("pr_triage: classified PR #%s with %s", pr.number, REVIEW_PENDING)
        await monitor.notifier.pr_auto_classified(pr.number, pr.title, pr.url)

        # Priority inheritance (issue #369): if this PR closes an issue that
        # carries a ~prioridade:N label, inherit it onto the PR.
        try:
            inherited = await monitor.forge.inherit_priority_from_linked_issue(
                pr.number
            )
            if inherited is not None:
                priority_labels = {
                    0: PRIORITY_0,
                    1: PRIORITY_1,
                    2: PRIORITY_2,
                    3: PRIORITY_3,
                }
                label = priority_labels.get(inherited)
                if label is not None:
                    await monitor.forge.add_labels("pr", pr.number, [label])
                    logger.info(
                        "pr_triage: inherited %s from linked issue for PR #%d",
                        label,
                        pr.number,
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort; never block triage
            logger.debug(
                "pr_triage: priority inheritance for PR #%d failed: %s", pr.number, exc
            )


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
    cursor fire).

    Sticky trigger behaviour by source:

    * **reviewer (PR)** — NOT filtered by ``~mention:processado``. A review
      request self-clears when a *formal* review is submitted (GitHub drops
      ``deile-one`` from ``requested_reviewers``), so the natural PR state
      already dedups; a review that failed (401/transient/comment-only)
      legitimately retries on the next tick without a marker that would block
      it. Concurrency is NOT inferred here by summing labels — the authority on
      "how many claude run" is the claude-worker, which counts live leases on
      the shared PVC and returns 409 when full (global cross-pod cap,
      ``_count_live_leases``/``DEILE_CLAUDE_MAX_CONCURRENT``); the pipeline just
      dispatches and retries on 409.
    * **assignee (PR)** — NOT filtered by ``~mention:processado``.
      Discovery-by-state: ``assignee`` routes to ``work_merge`` which legitimately
      retries (CI pending, threads open) until merged/closed terminates it.
    * **assignee (issue)** — NOT filtered by ``~mention:processado``, but IS
      gated by ``~workflow:*``: issues already owned by the pipeline (gate label
      present) are skipped to prevent EVENTS panel flooding (issue #483).
    * **body** — still filtered by ``~mention:processado`` because the body is
      static: without the marker it would re-fire every tick indefinitely.
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
        # ANTI-ECO: drop comments where DEILE auto-mencionou. Sem isso, qualquer
        # comentário que o próprio DEILE postou citando seu handle viraria
        # trigger e dispararia trabalho redundante na próxima volta do loop.
        # A identidade do agente vem do .user.login do comentário, não do texto.
        if ref.author == gh_login:
            _kind = "pr" if (ref.kind == "pr_review" or ref.is_pr_comment) else "issue"
            _num = int(ref.issue_url.rstrip("/").rsplit("/", 1)[-1])
            log_routing_dropped(target_kind=_kind, target=_num, reason="self_mention")
            continue
        if handle in ref.body.lower():
            triggers.append(
                MentionTrigger(
                    trigger_type="comment",
                    comment=ref,
                    trigger_author=ref.author,
                )
            )

    # ---- 2-4. Sticky triggers (assignee / reviewer / body) --------------
    #
    # ``assignee`` (PR/issue) segue descoberta-por-estado: o worker abre a PR e
    # decide pelo estado real (HEAD vs último review, threads abertas) — assignee
    # roteia p/ ``work_merge``, que legitimamente re-tenta até merge/close.
    # ``reviewer`` NÃO é gateado por ``~mention:processado``: o review-request se
    # auto-limpa quando um review formal é submetido (o GitHub tira ``deile-one``
    # de ``requested_reviewers``) e um review que falhou re-tenta sozinho — a
    # concorrência é capada no claude-worker (409 por lease viva, ver bloco
    # abaixo), não inferida aqui. Só ``body`` é gateado: corpo estático, sem o
    # marker re-dispararia a cada tick indefinidamente.
    async def _poll(label: str, coro) -> list:
        try:
            return list(await coro)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mention poll (%s) failed: %s", label, exc)
            return []

    for issue in await _poll(
        "assigned issues", monitor.forge.list_issues_assigned_to(gh_login)
    ):
        # Gate: skip issues already owned by the pipeline (any ~workflow:* label).
        # Without this, every tick re-arms a MentionTrigger for every in-flight
        # issue, flooding the EVENTS panel (issue #483 — V1 fix).
        if any(lb.startswith("~workflow:") for lb in (issue.labels or [])):
            continue
        triggers.append(
            MentionTrigger(
                trigger_type="assignee",
                issue=issue,
                trigger_author=gh_login,
            )
        )
    for pr in await _poll("assigned PRs", monitor.forge.list_prs_assigned_to(gh_login)):
        triggers.append(
            MentionTrigger(
                trigger_type="assignee",
                pr=pr,
                trigger_author=gh_login,
            )
        )
    # Reviewer-request → um review por PR. NÃO contamos concorrência aqui: a
    # AUTORIDADE de "quantos claude rodam" é o claude-worker, que conta leases
    # vivas no PVC compartilhado e devolve 409 quando cheio (cap GLOBAL cross-pod
    # — ``_count_live_leases``/``DEILE_CLAUDE_MAX_CONCURRENT``). O pipeline é
    # burro: dispara; se 409, re-tenta no próximo tick. Somar labels p/ inferir
    # paralelos era frágil (ignorava comment/assignee; contava sessão morta pra
    # sempre → deadlock). Dedup do MESMO PR em voo = guarda per-channel do
    # worker; "já revisado" = o GitHub limpa o review-request ao submeter; review
    # que falhou re-tenta sozinho (sem marker que trave o retry).
    for pr in await _poll(
        "review requests", monitor.forge.list_prs_with_review_requests(gh_login)
    ):
        triggers.append(
            MentionTrigger(
                trigger_type="reviewer",
                pr=pr,
                trigger_author=gh_login,
            )
        )

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
    monitor: "PipelineMonitor",
    dedup_key: str,
    group: list["MentionTrigger"],
    gh_login: str,
    mono: float,
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

    # Issue work → inject into the pipeline (handles its own dispatch).
    if kind == "issue" and ("assignee" in has or "body" in has):
        await _route_issue_to_pipeline(monitor, group, number, dedup_key, gh_login)
        return

    # Comment mention on an ISSUE: route by a TRUTH TABLE keyed on whether the
    # issue's current state has a future worker dispatch that re-reads its
    # comments (issue #442). Mentioning the target by name in a comment is NORMAL
    # and must NOT pull an OPEN issue out of an active gate — but it must also
    # never be silently dropped on a TERMINAL/closed issue, where no gate will
    # ever run (the #442 limbo bug).
    if kind == "issue":
        try:
            gated = await monitor.forge.get_issue(number)
            glabels = set(gated.labels)
            gstate = gated.state
        except Exception:  # noqa: BLE001 — best-effort; fall through to one-shot
            glabels = set()
            gstate = "open"
        if WORKFLOW_WAITING in glabels:
            # The comment IS the stakeholder's decision → lift the pause so the
            # refine loop resumes (the refiner reads this comment on its next pass).
            try:
                await monitor.forge.remove_labels("issue", number, [WORKFLOW_WAITING])
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "mention #%d: could not lift aguardando_stakeholder: %s",
                    number,
                    exc,
                )
            logger.info(
                "mention #%d: decisão do stakeholder → retoma refino (sem one-shot)",
                number,
            )
            return
        active = next((lb for lb in glabels if lb.startswith("~workflow:")), None)
        if active in GATE_REDISPATCHES_COMMENT and gstate == "open":
            # An OPEN issue in a re-dispatched state: critique/refine/implement/
            # resume re-reads the issue comments on its next pass (briefs.py reads
            # ``gh issue view --comments``), so defer — do NOT spawn a parallel
            # one-shot.
            logger.info(
                "mention #%d ignorada p/ roteamento: já está no gate ativo (%s)",
                number,
                active,
            )
            log_routing_dropped(
                target_kind=kind, target=number, reason="deferred_active_gate"
            )
            return
        if active == WORKFLOW_BLOCKED:
            # Blocked is human-gated → DEFER silently (drop). Do NOT one-shot (a
            # blocked issue must not be worked) and do NOT post a status comment:
            # postar a cada tick criou um LOOP INFINITO — a menção re-dispara a
            # cada tick → status + one-shot → novo claude worker (incidente #446).
            # O humano vê o próprio comentário; remover ~workflow:bloqueada retoma.
            logger.info(
                "mention #%d ignorada p/ roteamento: %s (human-gated, sem one-shot nem status)",
                number,
                WORKFLOW_BLOCKED,
            )
            log_routing_dropped(
                target_kind=kind, target=number, reason="issue_human_gated"
            )
            return
        # Fall through to mode="comment" (one-shot) for: a TERMINAL state
        # (em_pr / decomposta), a CLOSED issue in any state, or no ~workflow:*
        # label. None of these has a future gate dispatch that would read the
        # comment, so the one-shot handler is the ONLY way it is acted upon —
        # and because it IS handled, the mention cursor may advance past it
        # safely (the #442 limbo came from advancing past a DROPPED comment).

    # Decide the dispatch mode from the role.
    #
    # "PR é o quadro": qualquer trigger sobre uma PR resolve para o brief
    # unificado ``pr_unified`` — o worker abre a PR, descobre o estado real
    # (papel, HEAD vs último review, threads abertas, comentários dirigidos
    # a mim sem resposta) e monta a work-list a partir DAÍ. O trigger só
    # informou QUAL PR olhar; o que fazer é deduzido do estado.
    if kind == "pr":
        # Dedup cross-path: a stage ``pr_review`` roda ANTES de
        # ``process_mentions`` no mesmo tick e já transiciona a PR para
        # ``~review:em_andamento`` + claim ``~batch:``. Como a Service
        # ``claude-worker`` faz load-balance, o guard "claude já vivo" é por-pod
        # e NÃO enxerga um claude rodando num pod irmão — sem este skip a mesma
        # PR seria revisada por DOIS workers ao mesmo tempo (budget jogado fora,
        # observado em #463). Se a PR já está em revisão/locked, o ``pr_review``
        # é o dono: o brief unificado dele já lê comments/threads dirigidos a
        # mim, então pular aqui é correto — um handler por PR ("a PR é o quadro").
        try:
            pr_now = await monitor.forge.get_pr(number)
        except Exception:  # noqa: BLE001 — best-effort; segue pro dispatch
            pr_now = None
        if pr_now is not None:
            pr_labels = set(pr_now.labels)
            if REVIEW_IN_PROGRESS in pr_labels or any(
                is_batch_label(lb) for lb in pr_labels
            ):
                logger.info(
                    "mention %s ignorada p/ roteamento: PR já em revisão pelo "
                    "pr_review (em_andamento/batch) — evita dispatch duplo",
                    dedup_key,
                )
                log_routing_dropped(
                    target_kind=kind, target=number, reason="pr_in_review"
                )
                return
            if WORKFLOW_BLOCKED in pr_labels:
                # Blocked PR is human-gated → DEFER silently (drop). pr_review já
                # EXCLUI PRs bloqueadas (stages.py:2080); a menção não pode auto-
                # despachar pr_unified (poderia mergear uma PR que o humano
                # bloqueou). NÃO postar status — postar por tick loopa (a menção
                # re-dispara a cada tick). O humano remove a label para retomar.
                logger.info(
                    "mention %s ignorada p/ roteamento: PR em %s (human-gated, sem dispatch nem status)",
                    dedup_key,
                    WORKFLOW_BLOCKED,
                )
                log_routing_dropped(
                    target_kind=kind, target=number, reason="pr_human_gated"
                )
                return
        mode = "pr_unified"
        role = (
            "requested_reviewer"
            if "reviewer" in has
            else "assignee" if "assignee" in has else "author"
        )
        log_routing_pr_unified(target=number, role=role, mode="pr_unified")
    else:
        mode = "comment"  # comment mention on an issue

    # Resume + attempt ceiling for STICKY PR work (mirrors implement stage).
    resume = False
    if sticky:
        st = monitor._resume_tracker.get(number)
        if st.attempt >= monitor.config.resume_max_attempts:
            logger.warning(
                "mention %s: attempt ceiling (%d) reached — marking done",
                dedup_key,
                st.attempt,
            )
            log_routing_dropped(
                target_kind=kind, target=number, reason="attempt_ceiling"
            )
            await _comment_mention_gave_up(monitor, kind, number, st.attempt)
            await _mark_mention_done(monitor, kind, number)
            monitor._resume_tracker.clear(number)
            return
        resume = st.attempt > 0
        monitor._resume_tracker.record_dispatch(number, mono)

    if mode == "comment" and kind == "issue":
        log_routing_mention(target_kind="issue", target=number, action="comment")
    try:
        outcome = await monitor.implementer.mention(
            monitor,
            primary,
            trigger_types=trigger_types,
            all_triggers=group,
            mode=mode,
            resume=resume,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("mention dispatch error for %s: %s", dedup_key, exc)
        return

    # Skip-because-still-running is NOT a real attempt — the previous dispatch
    # is still alive in the worker, so no new work happened this tick. Return
    # BEFORE ``update_from_worker`` (which bumps attempt +1 per call): a long
    # resume spanning more ticks than ``resume_max_attempts`` would otherwise
    # burn its whole budget on no-op skips and block a healthy PR in progress
    # (same root cause as the implement #509 and pr_review regressions).
    if not outcome.ok and "DISPATCH_SKIPPED_STILL_RUNNING" in (outcome.error or ""):
        logger.info(
            "mention %s: dispatch skipped (claude ainda alive) — sem consumir tentativa",
            dedup_key,
        )
        return

    if sticky:
        # Absorb the worker's ground-truth bookkeeping (attempt/fingerprint)
        # so the ceiling advances and a stuck loop is bounded.
        monitor._resume_tracker.update_from_worker(
            number,
            fingerprint=outcome.fingerprint,
            attempt=outcome.tentativa,
            budget_s=outcome.budget_acumulado_s,
        )

    if not outcome.ok:
        # No ~mention:processado → the sticky trigger retries next tick (in
        # RESUME mode, bounded by the ceiling above). Comment-driven work is
        # cursor-bounded, so it does not retry.
        logger.warning("mention dispatch failed for %s: %s", dedup_key, outcome.error)
        return

    monitor._stats.mentions_processed += 1

    # Issue #568: se a menção one-shot sobre uma issue produziu derivadas
    # (DECOMPOSTO: #n1 #n2...), aplica o handshake de decomposição — idêntico ao
    # que `decompose_one_reviewed_intent` executa — para garantir idempotência e
    # liberar o slot de in_flight. Sem isso, a issue fica em `em_arquitetura` e
    # a próxima passagem do refino re-decompõe gerando duplicatas.
    if kind == "issue" and mode == "comment":
        derived_from_mention = parse_decompose_result(outcome.text)
        if derived_from_mention:
            await _apply_decompose_handshake_from_mention(
                monitor, number, derived_from_mention
            )

    if sticky:
        # Após a refactor "PR é o quadro", todo dispatch sticky de sucesso é
        # marcado com ``~mention:processado``. O brief unificado já comenta o
        # que fez (mesmo que tenha sido apenas "HEAD igual, sem novidade"),
        # então o marker apenas evita re-dispatch redundante no próximo tick;
        # mudanças reais de estado (HEAD novo, threads novas, novos
        # assignees) voltam a entrar pelo trigger natural (uma nova PR review,
        # uma nova atribuição) — quem decide o quê fazer é o estado, não o
        # marker.
        await _mark_mention_done(monitor, kind, number)
        monitor._resume_tracker.clear(number)
    author = next((t.comment.author for t in group if t.comment is not None), "")
    await monitor.notifier.mention_processed(
        primary.comment.html_url if primary.comment else dedup_key,
        author or gh_login,
    )


async def _mark_mention_done(
    monitor: "PipelineMonitor", kind: str, number: int
) -> None:
    """Best-effort apply ``~mention:processado`` so a sticky trigger stops re-firing."""
    try:
        await monitor.forge.add_labels(kind, number, [MENTION_DONE])
    except Exception as exc:  # noqa: BLE001 — marker is best-effort
        logger.warning(
            "could not mark %s #%d as %s: %s", kind, number, MENTION_DONE, exc
        )


async def _apply_decompose_handshake_from_mention(
    monitor: "PipelineMonitor", number: int, derived: list[int]
) -> None:
    """Issue #568: aplica o handshake de decomposição após uma menção one-shot que
    criou issues derivadas.

    Idempotente: relê o estado atual da issue antes de transicionar para evitar
    re-aplicar o handshake se outra path já o fez (race no tick). Se a issue já
    está em ``~workflow:decomposta``, nada acontece. Caso contrário, transiciona
    do estado atual para ``WORKFLOW_DECOMPOSED`` e limpa labels de refino.
    """
    try:
        fresh = await monitor.forge.get_issue(number)
    except Exception as exc:  # noqa: BLE001 — best-effort; não bloqueia o flow
        logger.warning("decompose handshake #%d: get_issue falhou: %s", number, exc)
        return

    current_labels = set(fresh.labels)
    if WORKFLOW_DECOMPOSED in current_labels:
        logger.info("decompose handshake #%d: já decomposta, skip", number)
        return

    # Encontra o estado atual de workflow para transicionar a partir dele.
    refine_state = next(
        (lb for lb in current_labels if lb in set(REFINE_WORKFLOW_STATES)), None
    )
    try:
        if refine_state:
            await monitor.forge.transition_issue(
                number, from_label=refine_state, to_label=WORKFLOW_DECOMPOSED
            )
        else:
            await monitor.forge.add_labels("issue", number, [WORKFLOW_DECOMPOSED])
        # Limpa labels de refino residuais (best-effort).
        cleanup = [REFINAR] + [
            lb for lb in current_labels if is_refine_attempt_label(lb)
        ]
        cleanup = [lb for lb in cleanup if lb in current_labels]
        if cleanup:
            await monitor.forge.remove_labels("issue", number, cleanup)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            f"decompose handshake via menção: could not mark #{number} decomposed",
            exc,
        )
        return
    monitor._resume_tracker.clear(number)
    log_decomposition_fanout(intent=number, derivadas=derived, complexity=[])
    logger.info(
        "mention/decompose #%d → derivadas %s (handshake aplicado)", number, derived
    )


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
    action = "already_in_pipeline" if already_in_pipeline else "inject_workflow_nova"
    log_routing_mention(target_kind="issue", target=number, action=action)
    try:
        if not already_in_pipeline:
            await monitor.forge.add_labels("issue", number, [WORKFLOW_NEW])
            logger.info(
                "mention: routed issue #%d into pipeline (%s)", number, WORKFLOW_NEW
            )
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
            monitor,
            "could not list new issues (forge error)",
            exc,
            notifier_label="review/list",
        )
        return
    # Shard filter: only consider issues whose hash falls in our shard.
    # Sort by priority so the most urgent issue is critiqued first.
    # ``~workflow:bloqueada`` é um hard-block: congela a issue em TODOS os
    # estágios (não só o auto-resume). Sem esta exclusão, uma issue ``nova`` que
    # um humano travou ainda seria critecada uma vez (nova→em_revisao) — gasto
    # não-intencional — antes de os selectors downstream (refine/implement) a
    # congelarem. Espelha o filtro já presente no reconcile e no implement.
    candidates = [
        i
        for i in sort_by_priority(issues)
        if i.batch_id is None
        and monitor.identity.owns(i.title)
        and WORKFLOW_BLOCKED not in i.labels
    ]
    if not candidates:
        return

    if monitor.config.enable_refinement_gate:
        # Concorrência (issue #373): a crítica é fire-and-forget, então o tick
        # pode despachar até ``available`` issues de uma vez — distribuindo o
        # paralelismo pelos workers em vez de uma issue por tick. ``available``
        # = max_parallel menos o total já em voo (crítica/refino/implement/PR).
        in_flight = await _count_total_in_flight(monitor)
        available = max(0, monitor.config.max_parallel - in_flight)
        if available <= 0:
            logger.debug(
                "critique: todos os %d slots ocupados (%d em voo); skip novos claims",
                monitor.config.max_parallel,
                in_flight,
            )
            return
        for target in candidates[:available]:
            await _critique_one_issue(monitor, target)
        return

    # Legacy path (gate OFF): mantém uma issue por tick.
    target = candidates[0]

    # ---- Legacy path (Claude/no-gate): no-op transition through review --------
    batch = await monitor.forge.claim_with_batch("issue", target.number)
    if batch is None:
        return
    # Tag ownership so other monitors can identify who claimed this.
    await monitor.forge.add_labels(
        "issue", target.number, [monitor.identity.ownership_label()]
    )
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


async def _persist_refine_attempt(monitor: "PipelineMonitor", number: int) -> None:
    """Grava ``~refine:<N>`` na issue refletindo o contador in-memory atual.

    Remove a label ``~refine:*`` anterior (se houver) e adiciona a nova. Opera
    em best-effort: erro de label não derruba o stage — apenas registra warning.
    Chamado logo após cada :meth:`ResumeTracker.bump_refine` em
    :func:`refine_one_issue` para tornar o contador durável a restarts.
    """
    n = monitor._resume_tracker.refine_attempt(number)
    try:
        cur = await monitor.forge.get_issue(number)
        old = [lb for lb in cur.labels if is_refine_attempt_label(lb)]
        if old:
            await monitor.forge.remove_labels("issue", number, old)
        await monitor.forge.add_labels("issue", number, [make_refine_attempt_label(n)])
    except Exception as exc:  # noqa: BLE001 — label durável é best-effort
        logger.warning(
            "refine #%d: não foi possível persistir ~refine:%d: %s", number, n, exc
        )


async def _critique_one_issue(monitor: "PipelineMonitor", target) -> None:
    """Critique gate (issue #257/#373): CLAIM ``nova→em_revisao`` + DISPATCH
    fire-and-forget. NÃO espera o veredito — :func:`reconcile_critique_issues`
    processa CLARO/VAGO no tick seguinte, lendo o resultado do worker via
    resume-info (a issue fica travada em ``em_revisao`` = lock durável).

    Em caso de falha de dispatch (``outcome.ok`` False), reverte
    ``em_revisao→nova`` para um tick posterior re-tentar.
    """
    number = target.number
    # Single-monitor production needs no batch lock (the nova→em_revisao flip is
    # the lock, and a lingering ~batch: would break the re-critique loop); a
    # sharded deployment claims to close the TOCTOU window and clears it after.
    multi = monitor.identity.shard_count > 1
    if multi:
        if await monitor.forge.claim_with_batch("issue", number) is None:
            return
    # Ownership tag lets the implement stage accept this issue without a batch.
    try:
        await monitor.forge.add_labels(
            "issue", number, [monitor.identity.ownership_label()]
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            f"could not add ownership label to #{number} for critique",
            exc,
        )
        if multi:
            await monitor.forge.clear_batch_label("issue", number)
        return
    await monitor.notifier.issue_picked_up(number, target.title, target.url)
    try:
        await monitor.forge.transition_issue(
            number, from_label=WORKFLOW_NEW, to_label=WORKFLOW_REVIEWING
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, f"could not claim issue #{number} for critique", exc
        )
        return

    # Fire-and-forget: o implementer grava o task_id no DispatchLedger e devolve
    # imediatamente (não bloqueia o tick).
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
            logger.warning(
                "could not revert #%d to nova after critique failure", number
            )
        logger.warning("critique #%d failed: %s", number, (outcome.error or "")[:200])
        return
    logger.info(
        "critique #%d dispatched fire-and-forget (task_id=%s) — reconcile no "
        "próximo tick",
        number,
        getattr(outcome, "task_id", "") or "",
    )


async def _apply_critique_verdict(
    monitor: "PipelineMonitor", target, verdict_text: str
) -> None:
    """Aplica o veredito CLARO/VAGO de uma crítica concluída (migrado do
    dispatch-side por #373). ``target`` é o snapshot fresco da issue em
    ``em_revisao``; ``verdict_text`` é o ``last_result_full`` do worker.
    """
    number = target.number
    is_clear, reason = parse_critique_verdict(verdict_text)
    issue_type = issue_type_from_labels(target.labels)
    log_refinement_critique(
        issue=number,
        round=current_refine_attempt_from_labels(target.labels),
        persona=persona_for_type(issue_type),
        verdict="CLARO" if is_clear else "VAGO",
        gaps=reason[:200],
    )
    if is_clear:
        await monitor.forge.transition_issue(
            number, from_label=WORKFLOW_REVIEWING, to_label=WORKFLOW_REVIEWED
        )
        # Escopo claro: remove o marcador de refinamento, qualquer estado residual
        # de refino e o contador durável ~refine:N — o ciclo de refino encerrou.
        refine_labels_to_remove = [REFINAR, *REFINE_WORKFLOW_STATES]
        refine_labels_to_remove += [
            lb for lb in target.labels if is_refine_attempt_label(lb)
        ]
        await monitor.forge.remove_labels("issue", number, refine_labels_to_remove)
        monitor._stats.issues_reviewed += 1
        await monitor.notifier.issue_reviewed(number, target.title, target.url)
        return

    # POOR — reconcilia o contador in-memory com a label durável antes de checar
    # o teto. Após restart do pod, ~refine:N é a fonte da verdade.
    monitor._resume_tracker.set_refine_attempt(
        number, current_refine_attempt_from_labels(target.labels)
    )
    # Block to the author once the refinement budget is exhausted.
    if (
        monitor._resume_tracker.refine_attempt(number)
        >= monitor.config.refine_max_attempts
    ):
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
        await _record_forge_error(
            monitor, f"could not move #{number} to {refine_state}", exc
        )
        return
    logger.info("critique #%d VAGO → %s (%s)", number, refine_state, reason[:120])


async def reconcile_critique_issues(monitor: "PipelineMonitor") -> None:
    """Processa o veredito das críticas fire-and-forget (issue #373).

    Espelha :func:`reconcile_implementing_issues`: lista issues em
    ``~workflow:em_revisao`` deste monitor e, pra cada uma com entry no
    ``DispatchLedger``, consulta o worker via resume-info:

    - **rodando** → continue (mantém o lock; próximo tick re-checa).
    - **sumida** (404/erro/workdir perdido) → limpa o ledger e continue. NÃO
      mexe no label — o reaper libera por idade.
    - **concluída** → ``parse_critique_verdict(last_result_full)`` e aplica a
      transição CLARO/VAGO (via :func:`_apply_critique_verdict`), depois limpa
      o ledger.

    Issues sem entry no ledger são deixadas pro reaper (lock órfão por restart
    entre claim e dispatch).
    """
    if not monitor.config.enable_refinement_gate:
        return
    try:
        issues = await monitor.forge.list_issues_with_label(
            WORKFLOW_REVIEWING, limit=50
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            "could not list em_revisao issues for reconcile",
            exc,
        )
        return
    ledger = getattr(monitor.implementer, "_ledger", None)
    if ledger is None:
        return
    own = monitor.identity.ownership_label()
    for issue in sort_by_priority(issues):
        if WORKFLOW_BLOCKED in issue.labels:
            continue
        if not (monitor._this_monitor_owns(issue) or own in issue.labels):
            continue
        key = DispatchLedger.key_for_issue(issue.number)
        entry = ledger.get(key)
        if entry is None:
            continue  # reaper cuida do lock órfão
        task_id = entry.get("task_id") or ""
        if not task_id:
            ledger.clear(key)
            continue
        # Lockstep com o dispatch da crítica (implementer.critique usa
        # stage="classify"): o reconcile precisa resolver o MESMO worker pelo
        # stage="classify", senão consultaria o endpoint errado (404 → fresh →
        # double-dispatch). Refine continua em stage="refine".
        state, info = await _fetch_reconcile_state(
            monitor,
            task_id,
            "classify",
            channel_id=f"pipeline-issue-{issue.number}",
        )
        if state == _RECON_RUNNING:
            continue
        if state == _RECON_GONE:
            ledger.clear(key)
            continue
        # Concluída — relê o snapshot fresco da issue (labels podem ter mudado)
        # e aplica o veredito a partir do resultado completo do worker.
        try:
            fresh = await monitor.forge.get_issue(issue.number)
        except Exception as exc:  # noqa: BLE001 — sem snapshot fresco usa o atual
            logger.warning(
                "reconcile critique #%d: get_issue falhou (%s); usando snapshot do tick",
                issue.number,
                exc,
            )
            fresh = issue
        verdict_text = info.get("last_result_full") or ""
        await _apply_critique_verdict(monitor, fresh, verdict_text)
        ledger.clear(key)


async def refine_one_issue(monitor: "PipelineMonitor") -> None:
    """Stage 1b (issue #257/#373): DISPATCH fire-and-forget das issues em estado
    de refinamento. NÃO espera o veredito — :func:`reconcile_refine_issues`
    processa OK/AGUARDA_STAKEHOLDER + o guard de convergência no tick seguinte.

    Candidatas são issues que este monitor possui e que NÃO estão pausadas,
    bloqueadas ou além do refinamento. A seleção une três fontes:

    1. issues com ``refinar`` (label explícito — critério original)
    2. issues com ``~workflow:em_refinamento`` (estado por tipo intent)
    3. issues com ``~workflow:em_arquitetura`` (estado por tipo code)

    A união permite recuperar issues que perderam o label ``refinar`` por
    crash, edição manual ou race. Dedup por ``number``.

    Concorrência (issue #373): despacha até ``available`` issues por tick.
    Anti-double-dispatch: pula candidata que JÁ tem entry no ``DispatchLedger``
    (refino em voo aguardando reconcile).
    """
    if not monitor.config.enable_refinement_gate:
        return
    # Coleta candidatas das três fontes e deduplicamos por number.
    try:
        by_refinar = await monitor.forge.list_issues_with_label(REFINAR, limit=50)
        by_refining = await monitor.forge.list_issues_with_label(
            WORKFLOW_REFINING, limit=50
        )
        by_arch = await monitor.forge.list_issues_with_label(
            WORKFLOW_ARCHITECTURE, limit=50
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            "could not list issues to refine (forge error)",
            exc,
            notifier_label="refine/list",
        )
        return
    # Dedup preservando primeira ocorrência (by_refinar tem precedência).
    seen: dict = {}
    for issue in (*by_refinar, *by_refining, *by_arch):
        if issue.number not in seen:
            seen[issue.number] = issue
    issues = list(seen.values())

    _excluded = (
        WORKFLOW_WAITING,
        WORKFLOW_BLOCKED,
        WORKFLOW_IMPLEMENTING,
        WORKFLOW_PR,
        WORKFLOW_DECOMPOSED,
    )
    # Anti-loop (issue #418): pula issues promovidas a ``revisada`` NESTE tick.
    # ``reconcile_refine_issues`` roda antes (mesmo tick) e, ao convergir, marca a
    # issue aqui; o índice de labels do GitHub ainda a lista sob ``refinar`` por
    # eventual consistency, então sem este guard o rehydrate a rebaixaria de volta.
    _promoted = getattr(monitor, "_refine_promoted_this_tick", set())
    candidates = [
        i
        for i in sort_by_priority(issues)
        if not any(lb in i.labels for lb in _excluded)
        and i.number not in _promoted
        and monitor.identity.owns(i.title)
    ]
    if not candidates:
        return

    ledger = getattr(monitor.implementer, "_ledger", None)
    in_flight = await _count_total_in_flight(monitor)
    available = max(0, monitor.config.max_parallel - in_flight)
    if available <= 0:
        logger.debug(
            "refine: todos os %d slots ocupados (%d em voo); skip novos dispatches",
            monitor.config.max_parallel,
            in_flight,
        )
        return
    dispatched = 0
    for target in candidates:
        if dispatched >= available:
            break
        # Anti-double-dispatch: refino já em voo (ledger entry) aguarda reconcile.
        if ledger is not None and ledger.get(
            DispatchLedger.key_for_issue(target.number)
        ):
            continue
        if await _refine_one_issue_dispatch(monitor, target):
            dispatched += 1


async def _refine_one_issue_dispatch(monitor: "PipelineMonitor", target) -> bool:
    """Rehydrate + ceiling-check + DISPATCH fire-and-forget de UMA issue.

    Retorna ``True`` quando consumiu um slot (dispatch despachado). Retorna
    ``False`` em rehydrate-only, ceiling-block ou falha de dispatch (não conta
    pro paralelismo). Captura ``before_body`` ANTES do dispatch e o grava no
    ``extra`` do ledger pro guard de convergência reconciliar mais tarde.
    """
    number = target.number
    issue_type = issue_type_from_labels(target.labels)

    # Rehydrate a hand-applied ``refinar`` (issue not yet in a refine state).
    if not any(s in target.labels for s in REFINE_WORKFLOW_STATES):
        refine_state = refine_workflow_state(issue_type)
        cur = next(
            (
                s
                for s in (WORKFLOW_NEW, WORKFLOW_REVIEWING, WORKFLOW_REVIEWED)
                if s in target.labels
            ),
            None,
        )
        try:
            if cur:
                await monitor.forge.transition_issue(
                    number, from_label=cur, to_label=refine_state
                )
            else:
                await monitor.forge.add_labels("issue", number, [refine_state])
        except GhCommandError as exc:
            await _record_forge_error(
                monitor, f"could not rehydrate #{number} into {refine_state}", exc
            )
        return False  # refined on the next tick

    # Garante consistência: se a issue chegou pelo estado (em_refinamento /
    # em_arquitetura) mas sem o label ``refinar`` (crash, race, edição manual),
    # re-adiciona o label antes de refinar — idempotente, best-effort.
    if REFINAR not in target.labels:
        try:
            await monitor.forge.add_labels("issue", number, [REFINAR])
        except GhCommandError as exc:
            logger.warning(
                "refine #%d: could not re-add 'refinar' label: %s", number, exc
            )

    # Reconcilia o contador in-memory com a label durável ~refine:N ANTES de
    # checar o teto. Após restart do pod, a label é a fonte da verdade.
    monitor._resume_tracker.set_refine_attempt(
        number, current_refine_attempt_from_labels(target.labels)
    )
    # Ceiling guard (belt-and-suspenders with the critique-side check).
    if (
        monitor._resume_tracker.refine_attempt(number)
        >= monitor.config.refine_max_attempts
    ):
        await _block_refinement(monitor, target, "teto de refinamentos atingido")
        return False

    # Captura o body ANTES do dispatch — o guard de convergência do reconcile
    # compara com o body resultante (depois que o worker terminar de reescrever).
    before_body = (target.body or "").strip()

    outcome = await monitor.implementer.refine(monitor, target)
    if not outcome.ok:
        # Conta a tentativa falha para que falhas determinísticas (payload
        # rejeitado pelo worker) atinjam o teto e bloqueiem — evita loop eterno.
        monitor._resume_tracker.bump_refine(number)
        await _persist_refine_attempt(monitor, number)
        logger.warning(
            "refine #%d failed (passe %d): %s",
            number,
            monitor._resume_tracker.refine_attempt(number),
            (outcome.error or "")[:200],
        )
        return False

    # Re-grava o record com o ``before_body`` no ``extra`` (o implementer já
    # gravou o task_id; re-record sobrescreve mantendo a chave). O reconcile lê
    # ``entry["extra"]["before_body"]`` para o guard de convergência.
    ledger = getattr(monitor.implementer, "_ledger", None)
    task_id = getattr(outcome, "task_id", "") or ""
    if ledger is not None and task_id:
        ledger.record(
            DispatchLedger.key_for_issue(number),
            task_id=task_id,
            session_id="",
            stage="refine",
            extra={"before_body": before_body},
        )
    logger.info(
        "refine #%d dispatched fire-and-forget (task_id=%s) — reconcile no "
        "próximo tick",
        number,
        task_id,
    )
    return True


async def reconcile_refine_issues(monitor: "PipelineMonitor") -> None:
    """Processa o veredito dos refinos fire-and-forget (issue #373).

    Lista issues em ``~workflow:em_refinamento`` ∪ ``~workflow:em_arquitetura``
    deste monitor e, pra cada uma com entry no ledger, consulta o worker:

    - **rodando** → continue.
    - **sumida** → limpa ledger (reaper cuida do lock por idade).
    - **falha** (``last_is_error``) → ``bump_refine`` + persiste, deixa pro
      próximo tick (não limpa o ledger? limpa — o reaper/teto cobre; ver nota).
    - **concluída** → ``parse_refine_verdict``:
        - ``waiting`` → add ``~workflow:aguardando_stakeholder``.
        - ``ok``/``unknown`` → **guard de convergência**: relê o body atual;
          se igual ao ``before_body`` do ledger → promove a ``revisada``;
          se diferente → ``bump_refine`` + persiste + ``refine_state→nova``.
      Em todos os ramos concluídos, limpa o ledger no fim.
    """
    if not monitor.config.enable_refinement_gate:
        return
    try:
        by_refining = await monitor.forge.list_issues_with_label(
            WORKFLOW_REFINING, limit=50
        )
        by_arch = await monitor.forge.list_issues_with_label(
            WORKFLOW_ARCHITECTURE, limit=50
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            "could not list refine issues for reconcile",
            exc,
        )
        return
    ledger = getattr(monitor.implementer, "_ledger", None)
    if ledger is None:
        return
    own = monitor.identity.ownership_label()
    _excluded = (
        WORKFLOW_WAITING,
        WORKFLOW_BLOCKED,
        WORKFLOW_IMPLEMENTING,
        WORKFLOW_PR,
        WORKFLOW_DECOMPOSED,
    )
    seen: dict = {}
    for issue in (*by_refining, *by_arch):
        if issue.number not in seen:
            seen[issue.number] = issue
    for issue in sort_by_priority(list(seen.values())):
        if any(lb in issue.labels for lb in _excluded):
            continue
        if not (monitor._this_monitor_owns(issue) or own in issue.labels):
            continue
        key = DispatchLedger.key_for_issue(issue.number)
        entry = ledger.get(key)
        if entry is None:
            continue
        task_id = entry.get("task_id") or ""
        if not task_id:
            ledger.clear(key)
            continue
        state, info = await _fetch_reconcile_state(
            monitor,
            task_id,
            "refine",
            channel_id=f"pipeline-issue-{issue.number}",
        )
        if state == _RECON_RUNNING:
            continue
        if state == _RECON_GONE:
            ledger.clear(key)
            continue
        before_body = (entry.get("extra") or {}).get("before_body")
        await _apply_refine_verdict(monitor, issue, info, before_body)
        ledger.clear(key)


#: Fração máxima de mudança de tamanho do body para um refino ``REFINO: OK`` ser
#: considerado convergido (só cosmético, ex.: corrigir ``arquivo:linha``). Acima
#: disso o refino mudou o escopo de verdade → re-crítica. Fix do loop
#: critic↔architect: a guarda byte-idêntico nunca disparava com mudança cosmética.
_REFINE_CONVERGED_RATIO = 0.02


async def _promote_refine_to_reviewed(
    monitor: "PipelineMonitor",
    target,
    refine_state: str,
    comment: str,
    log_msg: str,
) -> None:
    """Promove uma issue refinada para ``~workflow:revisada`` e limpa os labels
    de refino. Compartilhada pelos dois caminhos de convergência em
    :func:`_apply_refine_verdict`: o veredito explícito ``REFINO: OK`` (o
    architect julgou o escopo suficiente) e a guarda body-inalterado (fallback
    ``unknown``)."""
    number = target.number
    cleanup = [REFINAR, *REFINE_WORKFLOW_STATES] + [
        lb for lb in target.labels if is_refine_attempt_label(lb)
    ]
    try:
        await monitor.forge.transition_issue(
            number, from_label=refine_state, to_label=WORKFLOW_REVIEWED
        )
        await monitor.forge.remove_labels("issue", number, cleanup)
        await monitor.forge.comment_on_issue(number, comment)
        monitor._resume_tracker.clear(number)
        # Anti-loop (issue #418): marca a issue como promovida NESTE tick para o
        # candidate-filter de ``refine_one_issue`` (mesmo tick) pular o snapshot
        # stale do índice do GitHub que ainda a lista sob ``refinar``.
        promoted = getattr(monitor, "_refine_promoted_this_tick", None)
        if promoted is not None:
            promoted.add(number)
        monitor._stats.issues_reviewed += 1
        logger.info(log_msg)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, f"could not promote #{number} to revisada", exc
        )


async def _apply_refine_verdict(
    monitor: "PipelineMonitor", target, info: dict, before_body
) -> None:
    """Aplica o veredito de um refino concluído (migrado do dispatch-side por
    #373). Preserva o guard de convergência: o ``before_body`` vem do ledger
    (capturado no dispatch); o ``after_body`` é relido aqui.
    """
    number = target.number
    issue_type = issue_type_from_labels(target.labels)
    last_is_error = bool(info.get("last_is_error"))
    verdict_text = info.get("last_result_full") or ""

    if last_is_error:
        # Falha determinística do worker: conta a tentativa pro teto bloquear.
        monitor._resume_tracker.set_refine_attempt(
            number, current_refine_attempt_from_labels(target.labels)
        )
        monitor._resume_tracker.bump_refine(number)
        await _persist_refine_attempt(monitor, number)
        logger.warning(
            "refine #%d concluiu com erro (passe %d) — deixa pro próximo tick",
            number,
            monitor._resume_tracker.refine_attempt(number),
        )
        return

    # Issue #568: se o architect criou derivadas (DECOMPOSTO: #n1 #n2...) em vez de
    # refinar o escopo, aplica o handshake de decomposição em vez de tratar como
    # veredito de refino. Isso garante idempotência (issue vira terminal) e libera
    # o slot de in_flight que `em_arquitetura` consumia indevidamente.
    derived_from_refine = parse_decompose_result(verdict_text)
    if derived_from_refine:
        refine_state_for_decompose = next(
            (s for s in REFINE_WORKFLOW_STATES if s in target.labels),
            refine_workflow_state(issue_type),
        )
        cleanup_labels = [REFINAR] + [
            lb for lb in target.labels if is_refine_attempt_label(lb)
        ]
        try:
            await monitor.forge.transition_issue(
                number,
                from_label=refine_state_for_decompose,
                to_label=WORKFLOW_DECOMPOSED,
            )
            await monitor.forge.remove_labels("issue", number, cleanup_labels)
        except GhCommandError as exc:
            await _record_forge_error(
                monitor,
                f"could not mark #{number} decomposed after refine",
                exc,
            )
        monitor._resume_tracker.clear(number)
        log_decomposition_fanout(
            intent=number, derivadas=derived_from_refine, complexity=[]
        )
        logger.info(
            "refine/decompose #%d → derivadas %s (handshake via refine)",
            number,
            derived_from_refine,
        )
        return

    verdict = parse_refine_verdict(verdict_text)
    log_refinement_refine(
        issue=number,
        round=current_refine_attempt_from_labels(target.labels),
        persona=persona_for_type(issue_type),
        body_chars=len(before_body or ""),
        verdict=verdict,
    )
    if verdict == "waiting":
        # The worker posted 2-3 suggestions and assigned the author; pause refino.
        await monitor.forge.add_labels("issue", number, [WORKFLOW_WAITING])
        logger.info("refine #%d → aguardando stakeholder", number)
        return

    # OK / unknown. Ordem: Fix B (anti-divergência) → convergência → re-crítica.
    refine_state = next(
        (s for s in REFINE_WORKFLOW_STATES if s in target.labels),
        refine_workflow_state(issue_type),
    )
    monitor._resume_tracker.set_refine_attempt(
        number, current_refine_attempt_from_labels(target.labels)
    )
    try:
        refreshed = await monitor.forge.get_issue(number)
        after_body = (refreshed.body or "").strip()
    except Exception:  # noqa: BLE001 — na dúvida, segue o fluxo normal de re-crítica
        after_body = None

    # Fix B — divergence early-stop: se o body CONTINUA CRESCENDO no 3º+ passe, o
    # escopo está divergindo (intent amplo demais — cada passe só acumula gaps).
    # Roda ANTES de qualquer promoção: intents divergentes retornam ``REFINO:OK``
    # a cada passe enquanto incham o body, então a promoção por OK não pode
    # pular esta guarda. Damos benefício da dúvida nos 2 primeiros passes.
    current_refine_attempt = monitor._resume_tracker.refine_attempt(number)
    if (
        after_body is not None
        and before_body is not None
        and current_refine_attempt >= 3
    ):
        after_len = len(after_body)
        prev_len = monitor._resume_tracker.get_prev_refine_body_len(number)
        if prev_len >= 0 and after_len > prev_len:
            logger.warning(
                "refine #%d: corpo ainda CRESCENDO no passe %d "
                "(%d → %d chars) — escopo divergindo; bloqueando early",
                number,
                current_refine_attempt,
                prev_len,
                after_len,
            )
            await _block_refinement(
                monitor,
                target,
                "refino divergindo: o escopo só cresce a cada passe "
                "(intent amplo demais) — divida em sub-issues menores ou "
                "escope manualmente, e remova ~workflow:bloqueada",
            )
            # _block_refinement → _block já chama monitor._resume_tracker.clear(number).
            return

    # GUARD DE CONVERGÊNCIA + fix do loop critic↔architect. Promove a ``revisada``
    # (sem re-crítica) quando ESTE passe NÃO mudou o body de forma substancial:
    #   • body idêntico (``after == before``) — convergência forte (qualquer veredito);
    #   • OU ``REFINO: OK`` + mudança ≤ ``_REFINE_CONVERGED_RATIO`` (só cosmético,
    #     ex.: corrigir ``arquivo:linha``). Antes a guarda exigia body byte-idêntico,
    #     então o architect declarava "Pronto" mas o body cosmético re-circulava
    #     pra re-crítica (2ª chamada LLM que reprovava inconsistente) → loop até o
    #     teto em issues triviais. O Fix B acima continua barrando a divergência real.
    converged = False
    if before_body is not None and after_body is not None:
        if after_body == before_body:
            converged = True
        elif verdict == "ok":
            denom = max(len(before_body), 1)
            if (
                abs(len(after_body) - len(before_body)) / denom
                <= _REFINE_CONVERGED_RATIO
            ):
                converged = True
    if converged:
        await _promote_refine_to_reviewed(
            monitor,
            target,
            refine_state,
            "✅ Refino convergiu: o passe não mudou o escopo de forma "
            "substancial — promovido a `~workflow:revisada` sem re-crítica. Se o "
            "escopo ainda estiver insuficiente, aplique `~workflow:bloqueada` "
            "para revisão manual.",
            f"refine #{number} convergiu (body estável) → revisada",
        )
        return

    # Grava o comprimento do after_body para o próximo passe comparar.
    if after_body is not None:
        monitor._resume_tracker.record_refine_body_len(number, len(after_body))

    # Body mudou de forma substancial → conta o passe, persiste e re-critica.
    monitor._resume_tracker.bump_refine(number)
    await _persist_refine_attempt(monitor, number)
    try:
        await monitor.forge.transition_issue(
            number, from_label=refine_state, to_label=WORKFLOW_NEW
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, f"could not return #{number} to nova after refine", exc
        )
        return
    logger.info(
        "refine #%d body mudou (passe %d) → nova (re-crítica)",
        number,
        monitor._resume_tracker.refine_attempt(number),
    )


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
    # Remove também o contador durável ~refine:N: o unblock começa com contagem
    # fresca (o tracker in-memory já foi limpo por _block → clear).
    stale = [
        s
        for s in (
            WORKFLOW_REVIEWING,
            WORKFLOW_NEW,
            WORKFLOW_IMPLEMENTING,
            *REFINE_WORKFLOW_STATES,
        )
        if s in issue.labels and s != refine_state
    ]
    stale += [lb for lb in issue.labels if is_refine_attempt_label(lb)]
    if stale:
        try:
            await monitor.forge.remove_labels("issue", number, stale)
        except GhCommandError as exc:
            await _record_forge_error(
                monitor,
                f"could not strip stale labels {stale} from #{number}",
                exc,
            )
    if refine_state not in issue.labels:
        try:
            await monitor.forge.add_labels("issue", number, [refine_state])
        except GhCommandError as exc:
            await _record_forge_error(
                monitor, f"could not rest #{number} in {refine_state}", exc
            )
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


async def _ensure_ownership_label(
    monitor: "PipelineMonitor", issues: List["IssueRef"]
) -> List["IssueRef"]:
    """Issue #375: auto-add ownership label to reviewed issues that this monitor
    owns (via ``_this_monitor_owns``) but that lack both ``~batch:`` and the
    ``~by:*`` label. Without this, issues manually promoted to
    ``~workflow:revisada`` (by removing ``~workflow:bloqueada`` outside the
    classification flow) are silently ignored.

    Returns a **new** snapshot list with the freshly-added ownership label
    reflected in memory for any issue it fixes (``IssueRef`` is frozen, so a new
    one is built via :func:`dataclasses.replace`). The input list is left
    untouched, so the caller keeps both views:

    - the **pre-ensure** input — what ``implement_one_reviewed_issue`` filters
      on (it always filtered its own un-ensured fetch, so an orphan code issue
      is adopted on the *next* tick), and
    - the **post-ensure** return — what ``decompose_one_reviewed_intent``
      filters on (it used to re-fetch a fresh snapshot that already carried the
      label, so an orphan intent is decomposed the *same* tick).

    Keeping both views lets the tick fetch the reviewed snapshot **once** (PR
    #380 follow-up) while preserving the exact per-stage pickup timing that the
    two independent fetches produced before."""
    ownership_label = monitor.identity.ownership_label()
    updated: List["IssueRef"] = []
    for issue in issues:
        if (
            WORKFLOW_BLOCKED not in issue.labels
            and issue.batch_id is None
            and ownership_label not in issue.labels
            and monitor._this_monitor_owns(issue)
        ):
            logger.warning(
                "issue #%d revisada sem ownership (~by:*) nem batch — "
                "adicionando %s (issue #375)",
                issue.number,
                ownership_label,
            )
            try:
                await monitor.forge.add_labels("issue", issue.number, [ownership_label])
                issue = replace(issue, labels=(*issue.labels, ownership_label))
            except GhCommandError as exc:
                logger.warning(
                    "could not add ownership label %s to #%d: %s",
                    ownership_label,
                    issue.number,
                    exc,
                )
        updated.append(issue)
    return updated


async def fetch_reviewed_and_ensure_ownership(
    monitor: "PipelineMonitor", *, notifier_label: str = "reviewed/list"
) -> "Tuple[Optional[List[IssueRef]], Optional[List[IssueRef]]]":
    """PR #380 follow-up (non-blocking review suggestion): fetch the
    ``~workflow:revisada`` snapshot **once** per tick and ensure ownership
    **once**, so the implement and decompose stages share a single forge list
    call instead of each issuing their own (they target disjoint issue types —
    non-intent vs intent — so a shared snapshot is safe).

    Returns ``(pre, post)``:

    - ``pre`` — the raw snapshot, before any ownership label was reflected
      in memory. ``implement_one_reviewed_issue`` filters on this, reproducing
      its prior behavior (it always filtered its own un-ensured fetch, so an
      orphan code issue is adopted on the *next* tick).
    - ``post`` — the ownership-ensured snapshot. ``decompose_one_reviewed_intent``
      filters on this, reproducing its prior behavior (it re-fetched a fresh
      snapshot that already carried the label, so an orphan intent is
      decomposed the *same* tick).

    Both are ``None`` on a forge error; each stage then falls back to its own
    self-contained fetch (see :func:`_resolve_reviewed_snapshot`)."""
    try:
        issues = await monitor.forge.list_issues_with_label(WORKFLOW_REVIEWED, limit=50)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            "could not list reviewed issues (forge error)",
            exc,
            notifier_label=notifier_label,
        )
        return None, None
    ensured = await _ensure_ownership_label(monitor, issues)
    return issues, ensured


async def _resolve_reviewed_snapshot(
    monitor: "PipelineMonitor",
    issues: Optional[List["IssueRef"]],
    *,
    notifier_label: str,
) -> Optional[List["IssueRef"]]:
    """Return the reviewed-issue snapshot a stage should operate on.

    When ``issues`` is provided, the tick already fetched it (and ensured
    ownership in a single shared pass) — use it as-is. When ``None`` (direct
    invocation, tests, or fallback after a centralized fetch error), fetch the
    snapshot and run the ownership side-effect inline, then return the **raw**
    (pre-ensure) view — matching the prior per-stage direct-call behavior, where
    ``_ensure_ownership_label`` updated the forge but the stage filtered its own
    un-mutated fetch."""
    if issues is not None:
        return issues
    try:
        raw = await monitor.forge.list_issues_with_label(WORKFLOW_REVIEWED, limit=50)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            "could not list reviewed issues (forge error)",
            exc,
            notifier_label=notifier_label,
        )
        return None
    # GitHub side-effect + audit log; the returned ensured view is discarded so
    # the stage keeps filtering on the raw snapshot (next-tick adoption).
    await _ensure_ownership_label(monitor, raw)
    return raw


async def decompose_one_reviewed_intent(
    monitor: "PipelineMonitor", issues: Optional[List["IssueRef"]] = None
) -> None:
    """Stage 2 (intent path, issue #257): an architect decomposes a CLEAR intent
    into independent derived issues, then the intent stays OPEN as a decomposed
    epic (``~workflow:decomposta``).

    ``issues`` is the shared reviewed snapshot when called from the tick (PR #380
    follow-up); ``None`` means fetch + ensure ownership inline (direct/legacy)."""
    if not monitor.config.enable_refinement_gate:
        return
    issues = await _resolve_reviewed_snapshot(
        monitor, issues, notifier_label="decompose/list"
    )
    if issues is None:
        return
    ownership_label = monitor.identity.ownership_label()
    target = next(
        (
            i
            for i in sort_by_priority(issues)
            if issue_type_from_labels(i.labels) == TYPE_INTENT
            and WORKFLOW_DECOMPOSED not in i.labels
            and WORKFLOW_BLOCKED not in i.labels
            and monitor._this_monitor_owns(i)
            and (i.batch_id is not None or ownership_label in i.labels)
        ),
        None,
    )
    if target is None:
        return
    # The decompose dispatch is wait=True, so it blocks this (sequential) tick —
    # no concurrent re-pick. On success the intent leaves the revisada queue.
    outcome = await monitor.implementer.decompose(monitor, target)
    derived = parse_decompose_result(outcome.text)
    log_decomposition_fanout(
        intent=target.number,
        derivadas=derived,
        complexity=[],
    )
    if not outcome.ok and not derived:
        logger.warning(
            "decompose #%d failed: %s", target.number, (outcome.error or "")[:200]
        )
        return  # stays revisada — retry next tick
    # Diagnostic (#2): the parser returned [] but the architect may still have
    # created issues via gh in its run. Log the tail of the outcome so we can
    # see what format escaped the regex+fallback, and fall back to scraping the
    # GitHub state directly (architect references them with #N in the comment).
    if outcome.ok and not derived:
        tail = (outcome.text or "")[-600:].replace("\n", " | ")
        logger.warning(
            "decompose #%d: ok but parser returned [] — outcome tail: %s",
            target.number,
            tail,
        )
    # Mark decomposed when derived issues were created (even if the ok flag is
    # noisy) so we never re-decompose and duplicate the derived issues.
    try:
        await monitor.forge.transition_issue(
            target.number, from_label=WORKFLOW_REVIEWED, to_label=WORKFLOW_DECOMPOSED
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor, f"could not mark #{target.number} decomposed", exc
        )
    logger.info("decompose #%d → derivadas %s", target.number, derived)
    await monitor.notifier.issue_reviewed(
        target.number, f"{target.title} (decomposta em {len(derived)})", target.url
    )


# ----- stage 2: implement ------------------------------------------------


# --------------------------------------------------------------------------- #
# Fire-and-forget reconcile (issue #373 — critique/refine/pr_review)
# --------------------------------------------------------------------------- #

# Resultado normalizado da consulta resume-info de um task_id no worker.
# - "running":   o worker ainda processa (last_completed_at None ou claude_alive).
# - "done":      concluiu — ``info`` carrega last_result_full/last_is_error.
# - "gone":      task sumiu (404 / workdir_exists False / erro de transporte).
_RECON_RUNNING = "running"
_RECON_DONE = "done"
_RECON_GONE = "gone"


async def _fetch_reconcile_state(
    monitor: "PipelineMonitor",
    task_id: str,
    stage: str,
    *,
    channel_id: str = "",
) -> Tuple[str, dict]:
    """Consulta ``/v1/dispatches/{task_id}/resume-info`` e normaliza o estado.

    Reusa o cliente + a resolução de endpoint per-stage do implementer (não
    re-implementa transporte). Mapeia a resposta crua em
    ``(_RECON_RUNNING|_RECON_DONE|_RECON_GONE, info_dict)``:

    - ``_RECON_GONE``: 404 / qualquer erro de transporte / ``workdir_exists``
      False / payload não-dict. O reconcile NÃO mexe no label (o reaper libera
      por idade) — apenas limpa o ledger.
    - ``_RECON_RUNNING``: ``last_completed_at is None`` OU ``claude_alive``
      True — o worker ainda está processando.
    - ``_RECON_DONE``: concluiu; ``info`` traz ``last_result_full`` /
      ``last_result_summary`` / ``last_is_error`` pro parser de veredito.

    Issue #638: quando o estado é DONE e o dispatch foi fire-and-forget num
    worker da frota CLI, o bloco ``usage`` do resume-info é persistido no
    UsageRepository central (dedupado por task_id) — a resposta do 202 foi
    descartada, então este é o único ponto de read-back de custo. Best-effort.
    """
    implementer = monitor.implementer
    client = getattr(implementer, "_client", None)
    if client is None or not task_id:
        return _RECON_GONE, {}
    try:
        url = implementer._resolve_endpoint(stage)
    except (
        Exception
    ):  # noqa: BLE001 — stage inválido é programming bug; trate como gone
        url = None
    try:
        info = await client.get_resume_info(task_id, endpoint_url=url)
    except Exception as exc:  # noqa: BLE001 — 404/transporte → gone (reaper cuida)
        logger.info(
            "reconcile: resume-info lookup falhou pra task_id=%s stage=%s: %s "
            "— tratando como sumida",
            task_id,
            stage,
            exc,
        )
        return _RECON_GONE, {}
    if not isinstance(info, dict):
        return _RECON_GONE, {}
    if not info.get("workdir_exists", True):
        return _RECON_GONE, info
    still_running = info.get("last_completed_at") is None or info.get("claude_alive")
    if still_running:
        return _RECON_RUNNING, info
    _record_fleet_usage_from_reconcile(url, info, stage, channel_id, task_id)
    return _RECON_DONE, info


async def _capture_implement_fleet_cost(
    monitor: "PipelineMonitor",
    issue_number: int,
) -> None:
    """Lê o resume-info do implement concluído e persiste o custo central (#638).

    O implement é fire-and-forget; quando o PR aparece (ground-truth), lemos a
    sessão uma vez para colher o uso. ``_fetch_reconcile_state`` (stage=implement)
    já dispara a escrita central dedupada quando o estado é DONE. Best-effort.
    """
    try:
        ledger = getattr(monitor.implementer, "_ledger", None)
        if ledger is None:
            return
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger

        entry = ledger.get(DispatchLedger.key_for_issue(issue_number))
        task_id = (entry or {}).get("task_id") or ""
        if not task_id:
            return
        await _fetch_reconcile_state(
            monitor,
            task_id,
            "implement",
            channel_id=f"pipeline-issue-{issue_number}",
        )
    except (
        Exception
    ) as exc:  # noqa: BLE001 — captura de custo nunca bloqueia o reconcile
        logger.debug(
            "captura de custo do implement #%d falhou (non-fatal): %s",
            issue_number,
            exc,
        )


def _record_fleet_usage_from_reconcile(
    url: Optional[str],
    info: dict,
    stage: str,
    channel_id: str,
    task_id: str,
) -> None:
    """Persiste custo central de um fire-and-forget concluído (issue #638).

    No-op para workers núcleo (deile/claude): o resume-info deles não traz o
    bloco ``usage`` (só o cli-worker o preenche), então o recorder retorna 0.
    Best-effort isolado — o recorder nunca propaga exceção.
    """
    from deile.orchestration.pipeline.fleet_cost_recorder import (
        record_fleet_usage_from_resume_info,
    )
    from deile.orchestration.pipeline.implementer import _worker_kind_from_url

    record_fleet_usage_from_resume_info(
        info,
        worker_kind=_worker_kind_from_url(url or ""),
        stage=stage,
        channel_id=channel_id,
        task_id=task_id,
    )


async def _count_total_in_flight(monitor: "PipelineMonitor") -> int:
    """Conta TODO o trabalho em voo deste monitor (issues + PRs) — soma os
    estados-lock de crítica, refino, implement e review.

    Cada despachador (crítica / refino) subtrai esse total de ``max_parallel``
    pra decidir quantos candidatos novos pode claimar no tick, distribuindo o
    paralelismo pelos três workers (issue #373). Estados bloqueada/em_pr/
    aguardando_stakeholder não contam (não consomem slot de worker).
    """
    own = monitor.identity.ownership_label()

    def _mine(ref) -> bool:
        return monitor._this_monitor_owns(ref) or own in ref.labels

    total = 0
    # Issues nos três estados-lock de issue (em_revisao, em_refinamento,
    # em_arquitetura, em_implementacao).
    seen_issue: set[int] = set()
    for label in (
        WORKFLOW_REVIEWING,
        WORKFLOW_REFINING,
        WORKFLOW_ARCHITECTURE,
        WORKFLOW_IMPLEMENTING,
    ):
        try:
            issues = await monitor.forge.list_issues_with_label(label, limit=50)
        except GhCommandError as exc:
            await _record_forge_error(
                monitor,
                f"in-flight count: list {label} failed",
                exc,
            )
            continue
        for i in issues:
            if i.number in seen_issue:
                continue
            # Parked states NÃO consomem slot de worker. ``bloqueada`` (block
            # duro) e ``aguardando_stakeholder`` (esperando humano por tempo
            # indefinido) ambos ficam num lock state (ex.: em_arquitetura) mas
            # SEM worker rodando. Contá-los esfomeia trabalho genuinamente novo:
            # um backlog de issues em aguardando_stakeholder fixa ``in_flight``
            # em ``max_parallel`` e bloqueia toda crítica/refino nova (observado:
            # #515 esfomeada com in_flight=3 = #508 órfã + #418/#416
            # aguardando_stakeholder). Espelha a exclusão já feita no candidate
            # filter do refino (``_excluded``).
            if (
                WORKFLOW_BLOCKED in i.labels
                or WORKFLOW_PR in i.labels
                or WORKFLOW_WAITING in i.labels
            ):
                continue
            if _mine(i):
                seen_issue.add(i.number)
                total += 1
    # PRs em review (em_andamento).
    try:
        prs = await monitor.forge.list_open_prs(limit=50)
    except GhCommandError as exc:
        await _record_forge_error(monitor, "in-flight count: list_open_prs failed", exc)
        prs = []
    for pr in prs:
        if REVIEW_IN_PROGRESS not in pr.labels or WORKFLOW_BLOCKED in pr.labels:
            continue
        if (
            monitor._owns_pr_branch(pr.head_ref, pr_number=pr.number)
            or own in pr.labels
        ):
            total += 1
    return total


async def _count_in_flight_issues(monitor: "PipelineMonitor") -> int:
    """Count issues in ``~workflow:em_implementacao`` owned by this monitor.

    These are issues that have been dispatched (fire-and-forget via issue #373)
    but whose outcome is not yet known. Subtracted from ``max_parallel`` to
    avoid over-dispatching beyond available worker capacity.

    Issues that are blocked (``~workflow:bloqueada``) or already transitioned
    to ``~workflow:em_pr`` are NOT counted — they do not consume a worker slot.
    """
    try:
        issues = await monitor.forge.list_issues_with_label(
            WORKFLOW_IMPLEMENTING,
            limit=50,
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            "could not list in-flight issues (forge error)",
            exc,
        )
        return 0
    ownership_label = monitor.identity.ownership_label()
    count = 0
    for i in issues:
        if WORKFLOW_BLOCKED in i.labels or WORKFLOW_PR in i.labels:
            continue
        # Mirror the same predicate used by implement_one_reviewed_issue so
        # in-flight count and dispatch eligibility are always consistent.
        # Using OR here would count issues this monitor can never pick up
        # (e.g. orphaned ~by:B labels after a shard migration), inflating
        # in_flight and starving available_slots.
        if monitor._this_monitor_owns(i) and (
            i.batch_id is not None or ownership_label in i.labels
        ):
            count += 1
    return count


async def reconcile_implementing_issues(monitor: "PipelineMonitor") -> None:
    """Check ground truth for issues in ``~workflow:em_implementacao`` (issue #373).

    Since the implement stage now dispatches fire-and-forget, the pipeline no
    longer gets an immediate result from the worker. This function checks GitHub
    ground truth on each tick:

    - If a PR exists for the issue → the worker finished! Transition to
      ``~workflow:em_pr`` and notify.
    - If no PR yet → leave the issue in ``em_implementacao`` (worker still
      running or not yet pushed).

    This runs BEFORE ``implement_one_reviewed_issue`` in the tick loop so that
    newly-completed issues free up capacity for new dispatches in the same tick.
    """
    try:
        issues = await monitor.forge.list_issues_with_label(
            WORKFLOW_IMPLEMENTING,
            limit=50,
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            "could not list implementing issues for reconcile",
            exc,
        )
        return
    ownership_label = monitor.identity.ownership_label()
    for issue in sort_by_priority(issues):
        # Only reconcile issues this monitor owns.
        if WORKFLOW_BLOCKED in issue.labels:
            continue
        if WORKFLOW_PR in issue.labels:
            continue
        if not (monitor._this_monitor_owns(issue) or ownership_label in issue.labels):
            continue
        # Check ground truth: did the worker open a PR?
        try:
            has_pr = await monitor.forge.has_open_pr_for_issue(issue.number)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "reconcile #%d: has_open_pr_for_issue failed: %s",
                issue.number,
                exc,
            )
            continue
        if not has_pr:
            # Worker still running or hasn't pushed yet. Leave in em_implementacao.
            continue
        # Worker finished! Transition to em_pr.
        logger.info(
            "reconcile #%d: PR detected via ground truth → transitioning to %s",
            issue.number,
            WORKFLOW_PR,
        )
        # Issue #638: o implement é fire-and-forget (resposta do 202 descartada).
        # Agora que o worker concluiu (PR existe), lê o resume-info uma vez para
        # persistir o custo central do dispatch (dedupado por task_id no recorder).
        # Best-effort: falha de leitura/escrita não bloqueia a transição.
        await _capture_implement_fleet_cost(monitor, issue.number)
        try:
            await monitor.forge.transition_issue(
                issue.number,
                from_label=WORKFLOW_IMPLEMENTING,
                to_label=WORKFLOW_PR,
            )
        except GhCommandError as exc:
            await _record_forge_error(
                monitor,
                f"could not transition reconciled issue #{issue.number} to em_pr",
                exc,
            )
            continue
        monitor._resume_tracker.clear(issue.number)
        monitor._stats.issues_implemented += 1
        # Notify without PR URL — the forge's ``get_pr`` takes a PR number,
        # not an issue number. The notification will say "sem PR" if pr_url
        # is None, which is acceptable for the reconcile path.
        await monitor.notifier.implementation_finished(issue.number, None)


async def implement_one_reviewed_issue(
    monitor: "PipelineMonitor", issues: Optional[List["IssueRef"]] = None
) -> None:
    """Stage 2 (code path). Claim up to ``max_parallel`` reviewed feature/bug/
    refactor issues and dispatch their implementations via fire-and-forget
    (issue #373). Dispatches are non-blocking: the worker returns 202 + task_id
    immediately and processes the task in the background. ``reconcile_implementing_issues``
    checks ground truth (PR existence via GitHub) on subsequent ticks.

    The number of NEW dispatches is capped by ``max_parallel`` minus the number
    of issues already in ``~workflow:em_implementacao`` (in-flight), so that N
    workers can process N issues in parallel without blocking the tick loop.

    ``issues`` is the shared reviewed snapshot when called from the tick (PR #380
    follow-up); ``None`` means fetch + ensure ownership inline (direct/legacy).
    """
    issues = await _resolve_reviewed_snapshot(
        monitor, issues, notifier_label="implement/list"
    )
    if issues is None:
        return
    # Count in-flight issues (issue #373): issues already in em_implementacao
    # that this monitor owns and that are not blocked / already with a PR.
    # These represent workers currently busy — subtract from max_parallel.
    in_flight = await _count_in_flight_issues(monitor)
    available_slots = max(0, monitor.config.max_parallel - in_flight)
    if available_slots <= 0:
        logger.debug(
            "implement: all %d slots busy (%d in-flight); skipping new claims",
            monitor.config.max_parallel,
            in_flight,
        )
        return
    # Accept issues without ~batch: when the ownership label proves this monitor did the
    # review (e.g. operator manually promoted to ~workflow:revisada or batch label removed).
    ownership_label = monitor.identity.ownership_label()
    candidates = [
        i
        for i in issues
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
    # Sort by priority so the most urgent issues are implemented first.
    candidates = sort_by_priority(candidates)
    # Cap concurrency at available slots (max_parallel minus in-flight).
    batch = candidates[:available_slots]
    if not batch:
        return

    claimed = []
    for target in batch:
        # Decisão #46 — backoff exponencial de auth: pula targets dentro
        # da janela de pausa para evitar queimar tentativas durante surtos
        # curtos de ``WORKER_AUTH_EXPIRED``. O target será reavaliado no
        # próximo tick depois que a janela expirar.
        if is_target_auth_paused(monitor, "issue", target.number):
            _paused_until = monitor._paused_until_ts.get(
                _auth_target_key("issue", target.number), 0.0
            )
            _rem = max(0, int(_paused_until - _monotonic()))
            log_auth_skip(
                target=_auth_target_key("issue", target.number),
                until_iso=format_iso_utc(now_utc() + timedelta(seconds=_rem)),
                remaining_s=_rem,
            )
            logger.debug(
                "implement #%d: pausado por backoff de auth — skip este tick",
                target.number,
            )
            continue
        # Dedup guard (issue #257), gate-only: if an OPEN PR already implements
        # this issue — belt-and-suspenders behind the mention/gate integration —
        # do NOT open a second PR. Park it in em_pr so it leaves the queue (the
        # existing PR is the work).
        if (
            monitor.config.enable_refinement_gate
            and await monitor.forge.has_open_pr_for_issue(target.number)
        ):
            logger.info(
                "implement #%d: PR aberta já existe — parkando em em_pr (sem duplicar)",
                target.number,
            )
            try:
                await monitor.forge.transition_issue(
                    target.number, from_label=WORKFLOW_REVIEWED, to_label=WORKFLOW_PR
                )
            except GhCommandError as exc:
                await _record_forge_error(
                    monitor, f"could not park #{target.number} in em_pr", exc
                )
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
                target.number,
                from_label=WORKFLOW_REVIEWED,
                to_label=WORKFLOW_IMPLEMENTING,
            )
        except GhCommandError as exc:
            await _record_forge_error(
                monitor,
                f"could not claim issue #{target.number} for implementation",
                exc,
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

    # Issue #373: fire-and-forget dispatch — each ``implement()`` call returns
    # immediately with a 202 + task_id. The worker processes the task in the
    # background; ``reconcile_implementing_issues`` checks ground truth (PR
    # existence via GitHub) on subsequent ticks. No more 3h hard cap needed
    # because the dispatch itself is non-blocking.
    outcomes = await asyncio.gather(
        *[monitor.implementer.implement(monitor, t, resume=False) for t in claimed],
        return_exceptions=True,
    )
    for target, outcome in zip(claimed, outcomes):
        if isinstance(outcome, BaseException):
            logger.exception(
                "implement #%d: fire-and-forget dispatch raised",
                target.number,
                exc_info=outcome,
            )
        else:
            task_id = getattr(outcome, "task_id", "") or ""
            logger.info(
                "implement #%d: dispatched fire-and-forget (task_id=%s, "
                "reconcile on next tick)",
                target.number,
                task_id,
            )


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
        issues = await monitor.forge.list_issues_with_label(
            WORKFLOW_IMPLEMENTING, limit=50
        )
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            "could not list in-progress issues (forge error)",
            exc,
            notifier_label="resume/list",
        )
        return
    now = _monotonic()
    # Sort by priority so the most urgent parked issue is resumed first.
    target = next(
        (
            i
            for i in sort_by_priority(issues)
            if WORKFLOW_BLOCKED not in i.labels
            and WORKFLOW_PR not in i.labels
            and i.number not in monitor._resume_in_flight
            and monitor._this_monitor_owns(i)
            and monitor._resume_tracker.cadence_ok(
                i.number, now, monitor.config.resume_interval
            )
        ),
        None,
    )
    if target is None:
        return

    # Ground-truth guard (anti-double-dispatch): se uma PR aberta já implementa
    # esta issue, o worker concluiu — NÃO re-despacha. O ``reconcile`` (que roda
    # antes do resume no tick) normalmente já a teria promovido a ``em_pr``; este
    # check cobre a corrida em que o PR surgiu entre o reconcile e este ponto, ou
    # uma falha transiente do reconcile. Deixa a issue para o próximo reconcile
    # transicionar — não bloqueia, não duplica.
    try:
        if await monitor.forge.has_open_pr_for_issue(target.number):
            logger.info(
                "resume #%d: PR aberta já existe (ground truth) — skip re-dispatch",
                target.number,
            )
            return
    except Exception as exc:  # noqa: BLE001 — best-effort; segue para o fluxo normal
        logger.warning(
            "resume #%d: has_open_pr_for_issue falhou (%s) — segue fluxo normal",
            target.number,
            exc,
        )

    state = monitor._resume_tracker.get(target.number)
    # Attempt ceiling — block before spending another dispatch.
    # Per-stage max_retries (issue #391) takes priority over global resume_max_attempts.
    _impl_max_attempts = resolve_stage_max_retries("implement")
    if state.attempt >= _impl_max_attempts:
        await _block_issue(
            monitor,
            target.number,
            f"teto de tentativas atingido ({state.attempt}/"
            f"{_impl_max_attempts}) sem concluir",
        )
        return
    # Budget ceiling (0 = disabled).
    if (
        monitor.config.resume_budget > 0
        and state.budget_s >= monitor.config.resume_budget
    ):
        await _block_issue(
            monitor,
            target.number,
            f"orçamento de tempo esgotado ({state.budget_s:.0f}s >= "
            f"{monitor.config.resume_budget}s) sem concluir",
        )
        return

    await monitor.notifier.implementation_resumed(target.number, state.attempt + 1)
    monitor._resume_tracker.record_dispatch(target.number, now)
    monitor._stats.resume_dispatches += 1

    # RESUME (issue #445): o dispatch bloqueante (``implement(resume=True)`` com
    # wait=True) + o processamento inline do outcome rodam em BACKGROUND para não
    # congelar o loop do monitor (visto tick de 604s). Espelha o caminho de review
    # (``_resume_review_one_pr``). O gate de cadência (record_dispatch acima) +
    # ``_resume_in_flight`` impedem re-dispatch concorrente da mesma issue; o
    # ground-truth de :func:`reconcile_implementing_issues` cobre a conclusão por
    # PR no tick seguinte. 1 alvo por tick.
    monitor._resume_in_flight.add(target.number)
    monitor.spawn_background(_resume_implement_one_issue(monitor, target))


async def _resume_implement_one_issue(
    monitor: "PipelineMonitor", target: "IssueRef"
) -> None:
    """Processamento BLOQUEANTE do resume de uma issue (implement), extraído de
    ``resume_in_progress_issues`` para rodar em background task — NÃO congela o
    loop do monitor. A lógica (dispatch resume + teto/block/_absorb_progress via
    ``_finalize_implement_outcome``) é IDÊNTICA à anterior; só deixou de bloquear
    o tick. O caller já fez seleção/teto-pré-dispatch/record_dispatch e marcou
    ``_resume_in_flight``.
    """
    try:
        outcome = await monitor.implementer.implement(monitor, target, resume=True)
        await _finalize_implement_outcome(monitor, target.number, outcome, resume=True)
    finally:
        monitor._resume_in_flight.discard(target.number)


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
    zero_progress = monitor._resume_tracker.is_zero_progress(
        number, outcome.fingerprint
    )
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

    # Skip-because-still-running is NOT a real attempt — the previous dispatch
    # is still alive in the worker, so no new work happened this tick. Return
    # BEFORE ``_absorb_progress`` (which bumps the attempt counter +1 per call
    # AND would record a failure streak below): a long resume spanning more
    # ticks than max_retries would otherwise burn its whole budget on no-op
    # skips and block while healthy (same root cause as the pr_review #509
    # regression). The durable ``em_implementacao`` label keeps the lock; the
    # reaper retoma no próximo tick.
    if not outcome.ok and "DISPATCH_SKIPPED_STILL_RUNNING" in (outcome.error or ""):
        logger.info(
            "implement #%d: dispatch skipped (claude ainda alive) — manter %s "
            "(sem consumir tentativa)",
            number,
            WORKFLOW_IMPLEMENTING,
        )
        return

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
        err_detail = (outcome.error or "implementation failed")[
            :PIPELINE_MSG_TRUNCATE_CHARS
        ]
        # Adaptive escalation (#6): two consecutive failures of the same kind
        # (TIMEOUT, WORKER_UNREACHABLE, etc.) usually point at a non-transient
        # cause — escalate to block instead of burning the full resume ceiling.
        err_kind = _classify_outcome_error(outcome.error or "")
        # Issue #309 fase 3 (estratégia C — resiliência auth): se o
        # claude-worker reportou OAuth expirado, BLOQUEAR direto (sem
        # streak, sem retry) — token só renova via host, retentar é
        # desperdício. Comment + label deterministicos + ação clara.
        if err_kind == "WORKER_AUTH_EXPIRED":
            # Decisão #46 — backoff exponencial antes de bloquear. Curtos
            # surtos de OAuth expirado (típicos durante refresh in-pod) não
            # devem queimar a issue em ``~workflow:bloqueada`` se o
            # próximo tick puder ter sucesso.
            count, pause_s = record_auth_failure_and_maybe_pause(
                monitor,
                "issue",
                number,
            )
            if pause_s > 0:
                logger.warning(
                    "implement #%d: WORKER_AUTH_EXPIRED #%d — pausando por %.0fs",
                    number,
                    count,
                    pause_s,
                )
                return  # target permanece em ~workflow:em_implementacao
            logger.warning(
                "implement #%d: WORKER_AUTH_EXPIRED #%d (abaixo do threshold) — "
                "manter parked; reaper retoma no próximo tick",
                number,
                count,
            )
            await _park_or_keep(
                monitor, number, "WORKER_AUTH_EXPIRED transitório", resume=resume
            )
            return
        streak = monitor._resume_tracker.record_failure(number, err_kind)
        if streak >= 2 and err_kind in _ESCALATE_ON_REPEAT:
            logger.warning(
                "implement #%d: 2x %s consecutive — escalating to block",
                number,
                err_kind,
            )
            await _block_issue(
                monitor,
                number,
                f"falha repetida ({err_kind}) em duas tentativas seguidas — "
                f"causa provavelmente não-transitória; humano deve intervir.",
            )
            return
        logger.error(
            "implement #%d failed: %s — parked in %s",
            number,
            err_detail,
            WORKFLOW_IMPLEMENTING,
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
                monitor,
                f"could not transition issue #{number} to em_pr",
                exc,
            )
        monitor._resume_tracker.clear(number)
        # Decisão #46 — sucesso real: reseta contadores de backoff de auth.
        log_auth_recover(target=_auth_target_key("issue", number), reason="success")
        reset_auth_failures(monitor, "issue", number)
        monitor._stats.issues_implemented += 1
        await monitor.notifier.implementation_finished(number, pr_url)
        return

    # 4. INCOMPLETO — no PR yet. Block when the progress guard fired (two
    # consecutive attempts with the SAME substantive fingerprint = 0 progress).
    if zero_progress:
        await _block_issue(
            monitor,
            number,
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
            number,
            incomplete_count,
        )
        await _block_issue(
            monitor,
            number,
            f"agente finalizou sem abrir PR {incomplete_count}x consecutivas "
            f"(teto {ceiling}) — provável incapacidade de cumprir o escopo; "
            f"humano deve revisar a issue.",
        )
        return
    logger.warning(
        "implement #%d: incompleto (sem PR) %d/%d — parked in %s%s",
        number,
        incomplete_count,
        ceiling,
        WORKFLOW_IMPLEMENTING,
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


#: Decisão #46 — limiar de backoff exponencial para ``WORKER_AUTH_EXPIRED``.
#: Antes de bloquear deterministicamente, aplicamos um backoff exponencial
#: por-target (issue/PR específico). Após este número de falhas consecutivas,
#: o target é pausado por ``min(2 ** count * 60, _AUTH_BACKOFF_MAX_S)``
#: segundos antes da próxima tentativa. A primeira tentativa após o pause
#: que tiver sucesso reseta o contador. Sem isso, um surto curto de OAuth
#: expirado (típico durante refresh) bloqueava issues que poderiam ter
#: continuado naturalmente alguns minutos depois.
_AUTH_BACKOFF_THRESHOLD: int = 3
_AUTH_BACKOFF_BASE_S: float = 60.0
_AUTH_BACKOFF_MAX_S: float = 1800.0  # 30 min — cap superior


def _auth_target_key(kind: str, number: int) -> str:
    """Identidade canônica do target para o backoff: ``pr:N`` ou ``issue:N``."""
    return f"{kind}:{number}"


def is_target_auth_paused(
    monitor: "PipelineMonitor",
    kind: str,
    number: int,
) -> bool:
    """True se o target ainda está dentro de uma janela de pausa por auth.

    Consultado pelos stage handlers ANTES de despachar; se True, o caller
    devolve sem trabalho (target será reavaliado no próximo tick).
    """
    key = _auth_target_key(kind, number)
    paused_until = monitor._paused_until_ts.get(key, 0.0)
    if paused_until <= 0:
        return False
    if _monotonic() >= paused_until:
        # Janela expirada — libera o target sem zerar o contador
        # (próxima falha pode ainda escalar; sucesso reseta tudo).
        monitor._paused_until_ts.pop(key, None)
        return False
    return True


def record_auth_failure_and_maybe_pause(
    monitor: "PipelineMonitor",
    kind: str,
    number: int,
) -> tuple[int, float]:
    """Incrementa o contador de falhas auth do target e, se necessário,
    agenda uma pausa.

    Returns:
        ``(count, paused_for_s)``. ``paused_for_s`` é ``0.0`` quando ainda
        abaixo do limiar; senão, é a duração do pause aplicado (em segundos).
    """
    key = _auth_target_key(kind, number)
    count = monitor._auth_failures_by_target.get(key, 0) + 1
    monitor._auth_failures_by_target[key] = count
    log_auth_fail(
        target=key,
        attempts=count,
        threshold=_AUTH_BACKOFF_THRESHOLD,
        reason="WORKER_AUTH_EXPIRED",
    )
    if count < _AUTH_BACKOFF_THRESHOLD:
        return count, 0.0
    backoff_s = min(_AUTH_BACKOFF_BASE_S * (2**count), _AUTH_BACKOFF_MAX_S)
    log_auth_backoff(
        target=key,
        attempts=count,
        until_iso=format_iso_utc(now_utc() + timedelta(seconds=backoff_s)),
        backoff_s=int(backoff_s),
    )
    monitor._paused_until_ts[key] = _monotonic() + backoff_s
    logger.warning(
        "auth backoff: target=%s count=%d pause_for=%.0fs",
        key,
        count,
        backoff_s,
    )
    return count, backoff_s


def reset_auth_failures(
    monitor: "PipelineMonitor",
    kind: str,
    number: int,
) -> None:
    """Reseta o contador e o timestamp de pausa para o target dado.

    Chamado pelo stage handler quando um dispatch retorna ok=True
    (sucesso real), encerrando o ciclo de backoff para aquele target.
    """
    key = _auth_target_key(kind, number)
    monitor._auth_failures_by_target.pop(key, None)
    monitor._paused_until_ts.pop(key, None)


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
        monitor.forge.comment_on_issue
        if kind == "issue"
        else monitor.forge.comment_on_pr
    )
    try:
        await commenter(number, comment)
    except (
        Exception
    ) as exc:  # noqa: BLE001 — comment is best-effort; label still applied
        logger.warning("block %s: could not comment on #%d: %s", kind, number, exc)
    try:
        await monitor.forge.add_labels(kind, number, [WORKFLOW_BLOCKED])
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            f"could not apply {WORKFLOW_BLOCKED} to {kind} #{number}",
            exc,
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


async def _handle_review_concluded_invalidation(
    monitor: "PipelineMonitor",
    pr,
) -> None:
    """Check if a PR with ``~review:concluida`` has new commits since the
    review was concluded and, if so, invalidate the label based on commit
    classification (issue #351).

    Heuristic (Option A — paths + diff):
    - **docs-only**: remove ``~review:concluida``, add ``~review:pendente``,
      post comment saying only docs fidelity needs checking.
    - **cosmético**: post comment noting cosmetic changes; keep concluded.
    - **código**: remove ``~review:concluida``, add ``~review:pendente``,
      post comment saying full re-review is needed.
    - No new commits: keep concluded (nothing to do).

    Best-effort: any transport error is logged and the PR stays concluded.
    """
    # 1. Get the timestamp of when ~review:concluida was applied.
    concluded_at = await monitor.forge.label_applied_at(
        "pr",
        pr.number,
        REVIEW_CONCLUDED,
    )
    if concluded_at is None:
        logger.debug(
            "invalidation #%d: could not determine when %s was applied; skipping",
            pr.number,
            REVIEW_CONCLUDED,
        )
        return

    # 2. Check for new commits since the label was applied.
    try:
        commits = await monitor.forge.get_pr_commits_since(
            pr.number,
            concluded_at,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "invalidation #%d: get_pr_commits_since failed: %s",
            pr.number,
            exc,
        )
        return

    if not commits:
        logger.debug(
            "invalidation #%d: no new commits since %s was applied",
            pr.number,
            REVIEW_CONCLUDED,
        )
        return

    # 3. Classify the new commits.
    classification = _classify_new_commits(commits)
    commit_count = len(commits)
    logger.info(
        "invalidation #%d: %d new commit(s) since review concluded — %s",
        pr.number,
        commit_count,
        classification,
    )

    # 4. Act on the classification.
    if classification == CLASS_COSMETIC:
        # Cosmetic changes — skip re-review, post comment.
        comment = (
            f"🤖 **Novos commits após revisão concluída** "
            f"(issue #351 — invalidate-on-new-commit)\n\n"
            f"**Classificação:** 🎨 `cosmético` — {commit_count} commit(s) "
            f"pós-`{REVIEW_CONCLUDED}` com apenas alterações de "
            f"configuração/formatação (sem código ou docs).\n\n"
            f"**Ação:** Nenhuma — revisão mantida como concluída. "
            f"Não é necessária re-revisão.\n\n"
            f"---\nBy [DEILE-One](mailto:deile@deile.info)"
        )
        try:
            await monitor.forge.comment_on_pr(pr.number, comment)
        except Exception as exc:  # noqa: BLE001
            logger.warning("invalidation #%d: comment failed: %s", pr.number, exc)
        return

    # docs-only or código → invalidate the concluded label.
    try:
        await monitor.forge.remove_labels("pr", pr.number, [REVIEW_CONCLUDED])
    except GhCommandError as exc:
        logger.warning(
            "invalidation #%d: could not remove %s: %s",
            pr.number,
            REVIEW_CONCLUDED,
            exc,
        )
        return
    try:
        await monitor.forge.add_labels("pr", pr.number, [REVIEW_PENDING])
    except GhCommandError as exc:
        logger.warning(
            "invalidation #%d: could not add %s: %s",
            pr.number,
            REVIEW_PENDING,
            exc,
        )
        # Best-effort recovery: re-add REVIEW_CONCLUDED so the PR
        # isn't left label-less.
        try:
            await monitor.forge.add_labels("pr", pr.number, [REVIEW_CONCLUDED])
        except Exception:  # noqa: BLE001
            pass
        return

    if classification == CLASS_DOCS_ONLY:
        action = (
            "📝 apenas arquivos de documentação (`docs/` ou `.md`) "
            "foram alterados — revisar apenas fidelidade docs↔código"
        )
    else:
        action = (
            "💻 pelo menos um arquivo de código foi alterado "
            "— revisão completa necessária"
        )

    comment = (
        f"🤖 **Novos commits após revisão concluída** "
        f"(issue #351 — invalidate-on-new-commit)\n\n"
        f"**Classificação:** `{classification}` — {commit_count} commit(s) "
        f"pós-`{REVIEW_CONCLUDED}`.\n\n"
        f"**Ação:** Removido `{REVIEW_CONCLUDED}`, "
        f"adicionado `{REVIEW_PENDING}`. {action}.\n\n"
        f"---\nBy [DEILE-One](mailto:deile@deile.info)"
    )
    try:
        await monitor.forge.comment_on_pr(pr.number, comment)
    except Exception as exc:  # noqa: BLE001
        logger.warning("invalidation #%d: comment failed: %s", pr.number, exc)


async def reconcile_review_prs(monitor: "PipelineMonitor") -> None:
    """Processa o veredito das reviews fresh fire-and-forget por GROUND-TRUTH
    (issue #373 — Saída B, espelha :func:`reconcile_implementing_issues`).

    Lista PRs em ``~review:em_andamento`` deste monitor com entry no ledger
    (fresh dispatches). Pra cada uma, consulta resume-info:

    - **rodando** → continue.
    - **sumida** → limpa ledger (reaper retoma por idade).
    - **concluída** → decide por GROUND-TRUTH (do mais seguro pro mais frouxo):
        - PR **merged/closed** (``forge.get_pr(n) is None``) → ``em_andamento→
          concluida`` + clear tracker/ledger + stats + notify + follow-ups.
        - veredito **BLOQUEADO** no ``last_result_full`` (ou ``last_is_error``)
          → ``_block_pr`` + clear ledger.
        - concluiu **sem merge nem block** (review postado, sem mergear) →
          ``em_andamento→concluida``. **Decisão:** o trabalho de review foi
          ENTREGUE (não há mais dispatch pendente pra essa task); marcar
          concluida evita loop infinito de re-review. O backstop contra
          "review-theatre" continua sendo o invalidate-on-new-commit (#351),
          que reabre a PR se houver commit novo.

    Resume (em_andamento SEM ledger entry) é território do caminho bloqueante
    de :func:`review_one_open_pr` — NÃO mexemos aqui.
    """
    try:
        prs = await monitor.forge.list_open_prs(limit=50)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            "could not list PRs for review reconcile",
            exc,
        )
        return
    ledger = getattr(monitor.implementer, "_ledger", None)
    if ledger is None:
        return
    own = monitor.identity.ownership_label()
    for pr in sort_by_priority(prs):
        if REVIEW_IN_PROGRESS not in pr.labels or WORKFLOW_BLOCKED in pr.labels:
            continue
        if not (
            monitor._owns_pr_branch(pr.head_ref, pr_number=pr.number)
            or own in pr.labels
        ):
            continue
        key = DispatchLedger.key_for_pr(pr.number)
        entry = ledger.get(key)
        if entry is None:
            continue  # resume/reaper território
        task_id = entry.get("task_id") or ""
        if not task_id:
            ledger.clear(key)
            continue
        state, info = await _fetch_reconcile_state(
            monitor,
            task_id,
            "pr_review",
            channel_id=f"pipeline-pr-{pr.number}",
        )
        if state == _RECON_RUNNING:
            continue
        if state == _RECON_GONE:
            ledger.clear(key)
            continue
        # Concluída — decide por ground-truth.
        try:
            still_open = await monitor.forge.get_pr(pr.number)
        except Exception as exc:  # noqa: BLE001 — na dúvida, trata como aberta
            logger.warning(
                "reconcile review #%d: get_pr falhou (%s); assume aberta",
                pr.number,
                exc,
            )
            still_open = pr
        if still_open is None:
            # MERGED/closed — sucesso. Espelha o ramo ``merged`` do handler.
            try:
                await monitor.forge.transition_pr(
                    pr.number, from_label=REVIEW_IN_PROGRESS, to_label=REVIEW_CONCLUDED
                )
            except GhCommandError as exc:
                await _record_forge_error(
                    monitor,
                    f"could not transition merged PR #{pr.number} to concluida",
                    exc,
                )
            await monitor.forge.clear_batch_label("pr", pr.number)
            monitor._resume_tracker.clear(pr.number)
            log_auth_recover(target=_auth_target_key("pr", pr.number), reason="success")
            reset_auth_failures(monitor, "pr", pr.number)
            ledger.clear(key)
            monitor._stats.prs_reviewed += 1
            await monitor.notifier.pr_reviewed(pr.number, pr.title, pr.url, merged=True)
            try:
                await run_terminal_gc(monitor.forge, "pr", pr.number, "merged")
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "reconcile_review_prs: GC failed for PR #%d: %s",
                    pr.number,
                    exc,
                )
            await _post_merge_follow_ups(monitor, pr)
            continue
        last_full = info.get("last_result_full") or ""
        if info.get("last_is_error") or _review_was_blocked_marker(last_full):
            await _block_pr(
                monitor,
                pr.number,
                pr.title,
                pr.url,
                "review/merge concluiu com erro ou marcador BLOQUEADO",
            )
            ledger.clear(key)
            continue
        # Concluiu sem merge nem block — review entregue. Marca concluida.
        try:
            await monitor.forge.transition_pr(
                pr.number, from_label=REVIEW_IN_PROGRESS, to_label=REVIEW_CONCLUDED
            )
        except GhCommandError as exc:
            await _record_forge_error(
                monitor,
                f"could not transition reviewed PR #{pr.number} to concluida",
                exc,
            )
        await monitor.forge.clear_batch_label("pr", pr.number)
        ledger.clear(key)
        monitor._stats.prs_reviewed += 1
        await monitor.notifier.pr_reviewed(pr.number, pr.title, pr.url, merged=False)


def _review_was_blocked_marker(text: str) -> bool:
    """True se o resultado da review carrega o marcador estruturado BLOQUEADO.

    Reusa a constante ``_ENDED_BLOQUEADO`` e procura tokens canônicos que o
    brief unificado emite quando o agente declara bloqueio (BLOQUEADO /
    REQUEST_CHANGES). Conservador: só bloqueia com sinal explícito.
    """
    if not text:
        return False
    low = text.lower()
    return _ENDED_BLOQUEADO in low or "bloqueado" in low or "request_changes" in low


async def _resume_review_one_pr(
    monitor: "PipelineMonitor", target, resume_enabled: bool
) -> None:
    """Processamento BLOQUEANTE do resume de uma PR (review/merge), extraído de
    ``review_one_open_pr`` para rodar em background task — NÃO congela o loop do
    monitor. A lógica (Fix A SHA-guard, Fix #8 auto-correção, teto, proof-of-work,
    merge/block) é IDÊNTICA à anterior; só deixou de bloquear o tick.
    O caller já fez claim/ceiling/record_dispatch e marcou ``_resume_in_flight``.
    """
    try:
        # RESUME (issue #254): caminho BLOQUEANTE preservado — o stage handler
        # precisa do resultado estruturado (ended, fingerprint, tentativa) pra
        # decidir concluido/incompleto/bloqueado inline.
        # Delegate the review/merge work to the configured strategy. The Claude
        # strategy checks out the branch in a worktree; the worker strategy clones
        # and runs ``gh pr checkout`` inside its own sandbox.
        outcome = await monitor.implementer.review(monitor, target, resume=True)
        # Skip-because-still-running is NOT a real attempt: the previous review is
        # still alive in the worker, so no new review/merge work happened this tick.
        # Returning BEFORE ``_absorb_progress`` is load-bearing — that helper calls
        # ``update_from_worker`` which unconditionally bumps the attempt counter
        # (+1 per call). A review that legitimately spans more ticks than the
        # ``pr_review`` max_retries would otherwise burn its whole budget on these
        # no-op skips and get blocked while perfectly healthy (#509: 4 skips →
        # "teto 4/4 sem mergear" on a CLEAN+MERGEABLE PR).
        if not outcome.ok and "DISPATCH_SKIPPED_STILL_RUNNING" in (outcome.error or ""):
            logger.info(
                "pr_review #%d: dispatch skipped (claude ainda alive) — manter "
                "em_andamento (sem consumir tentativa)",
                target.number,
            )
            await monitor.forge.clear_batch_label("pr", target.number)
            return
        zero_progress = _absorb_progress(monitor, target.number, outcome)

        # Fix A — deterministic re-review flood guard: se o HEAD SHA da PR não
        # mudou desde a última review incompleta, nenhum fix foi aplicado e
        # re-revisar o mesmo HEAD é um flood. Forçamos zero_progress = True para
        # que o block existente em ~linha 2936 dispare deterministicamente.
        # Só ativo quando head_sha é não-vazio (GitLab sem SHA → comportamento legacy).
        current_sha = getattr(target, "head_sha", "") or ""
        last_sha = monitor._resume_tracker.reviewed_sha(target.number)
        if current_sha and last_sha and current_sha == last_sha:
            logger.warning(
                "pr_review #%d: HEAD SHA %s não mudou desde a última review "
                "incompleta — nenhum fix foi aplicado; forçando zero_progress "
                "(re-review do mesmo HEAD é loop)",
                target.number,
                current_sha[:8],
            )
            zero_progress = True

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
                "pr_review #%d failed: %s",
                target.number,
                (outcome.error or "review failed")[:PIPELINE_MSG_TRUNCATE_CHARS],
            )
            # Issue #309 fase 3 (estratégia C — auth-expired guard): bloqueia
            # fast com mensagem clara em vez de cair em retry/escalation
            # genérico. claude-worker já não pode entregar nada até renovar.
            if _classify_outcome_error(outcome.error or "") == "WORKER_AUTH_EXPIRED":
                # Decisão #46 — backoff exponencial: surto curto de OAuth
                # expirado não deve bloquear deterministicamente em #1. Apenas
                # após o threshold paramos por uma janela; o reaper retoma
                # automaticamente quando a pausa expira.
                count, pause_s = record_auth_failure_and_maybe_pause(
                    monitor,
                    "pr",
                    target.number,
                )
                logger.warning(
                    "pr_review #%d: WORKER_AUTH_EXPIRED #%d (pause=%.0fs) — "
                    "liberando batch; reaper retoma após pausa",
                    target.number,
                    count,
                    pause_s,
                )
                await monitor.forge.clear_batch_label("pr", target.number)
                return
            # Issue #309 fase 3.5 — Bug A fix: erro NÃO-auth do worker NÃO
            # deve fluir pro fast-finish legacy abaixo (que marcava
            # ~review:concluida sem proof-of-work — vide R2/PR #344, 5s).
            # Libera o batch; reaper retoma no próximo tick (resume real
            # se sessão claude sobreviveu, fresh dispatch caso contrário).
            # (DISPATCH_SKIPPED_STILL_RUNNING já tratado antes do _absorb_progress
            # acima — não consome tentativa e não chega aqui.)
            logger.warning(
                "pr_review #%d: worker error não-auth (%s); liberando batch pra reaper "
                "retomar (não marca concluida sem proof-of-work — Bug A fix)",
                target.number,
                (outcome.error or "")[:120],
            )
            await monitor.forge.clear_batch_label("pr", target.number)
            return

        if blocked:
            await monitor.forge.clear_batch_label("pr", target.number)
            await _block_pr(
                monitor,
                target.number,
                target.title,
                target.url,
                outcome.motivo_bloqueio or "o agente declarou BLOQUEADO sem motivo",
            )
            return

        if merged:
            try:
                await monitor.forge.transition_pr(
                    target.number,
                    from_label=REVIEW_IN_PROGRESS,
                    to_label=REVIEW_CONCLUDED,
                )
            except GhCommandError as exc:
                await _record_forge_error(
                    monitor,
                    f"could not transition PR #{target.number} to concluida",
                    exc,
                )
            await monitor.forge.clear_batch_label("pr", target.number)
            monitor._resume_tracker.clear(target.number)
            # Decisão #46 — sucesso real: reseta contadores de backoff de auth.
            log_auth_recover(
                target=_auth_target_key("pr", target.number), reason="success"
            )
            reset_auth_failures(monitor, "pr", target.number)
            monitor._stats.prs_reviewed += 1
            await monitor.notifier.pr_reviewed(
                target.number, target.title, target.url, merged=True
            )
            await _post_merge_follow_ups(monitor, target)
            return

        # Not merged. With resume enabled, keep the PR in ~review:em_andamento for
        # the next resume tick (progress guard catches a stuck loop). Without
        # resume, preserve the legacy behaviour: mark concluded so the PR drops out.
        if resume_enabled:
            if zero_progress:
                # Mensagem contextual: indica se o block veio do SHA-guard (Fix A)
                # ou do fingerprint-guard clássico.
                _current_sha_now = getattr(target, "head_sha", "") or ""
                _last_sha_now = monitor._resume_tracker.reviewed_sha(target.number)
                _sha_guard_fired = bool(
                    _current_sha_now
                    and _last_sha_now
                    and _current_sha_now == _last_sha_now
                )
                # Fix #8 (issue #521) — auto-correção da PRÓPRIA PR. Quando o
                # SHA-guard disparou (HEAD inalterado) E a review pediu mudança
                # (REQUEST_CHANGES) numa PR nossa, NÃO bloqueia direto: despacha um
                # ADDRESS (implement + push) para o worker aplicar o fix. Só depois
                # de esgotar o teto de tentativas (HEAD ainda não mudou → worker não
                # conseguiu) é que cai no block do Fix A. O cap é o que impede o loop
                # infinito address↔review.
                if (
                    _sha_guard_fired
                    and _review_was_blocked(outcome.text)
                    and monitor._owns_pr_branch(
                        target.head_ref, pr_number=target.number
                    )
                    and monitor._resume_tracker.address_attempt(target.number)
                    < MAX_ADDRESS_ATTEMPTS
                ):
                    _k = monitor._resume_tracker.bump_address_attempt(target.number)
                    logger.info(
                        "pr_review #%d: review pediu mudança + HEAD %s inalterado — "
                        "despachando address (implement + push) tentativa %d/%d em "
                        "vez de bloquear (Fix #8)",
                        target.number,
                        _current_sha_now[:8],
                        _k,
                        MAX_ADDRESS_ATTEMPTS,
                    )
                    # NÃO regrava reviewed_sha: queremos detectar no próximo tick se
                    # o address mudou o HEAD. Mantém ~review:em_andamento; libera o
                    # batch para o próximo tick re-claimar.
                    _addr_outcome = await monitor.implementer.address_review(
                        monitor,
                        target,
                    )
                    if not _addr_outcome.ok:
                        logger.warning(
                            "pr_review #%d: address dispatch falhou (%s) — "
                            "em_andamento; reaper/reconcile retomam",
                            target.number,
                            (_addr_outcome.error or "")[:160],
                        )
                    await monitor.forge.clear_batch_label("pr", target.number)
                    return
                if _sha_guard_fired:
                    _block_reason = (
                        f"review pediu mudança mas o HEAD (`{_current_sha_now[:8]}`) "
                        f"não mudou após {MAX_ADDRESS_ATTEMPTS} tentativa(s) de "
                        "auto-correção — o worker não conseguiu aplicar o fix; "
                        "humano: corrija manualmente ou faça checkout da PR (#520), "
                        "depois remova ~workflow:bloqueada"
                    )
                else:
                    _block_reason = (
                        "duas tentativas de review/merge sem progresso (diff idêntico)"
                    )
                await monitor.forge.clear_batch_label("pr", target.number)
                await _block_pr(
                    monitor,
                    target.number,
                    target.title,
                    target.url,
                    _block_reason,
                )
                return
            # HEAD mudou (ou primeira review): o worker pushou um fix com sucesso —
            # reseta a janela de auto-correção (Fix #8) e grava o SHA atual para o
            # próximo tick comparar (Fix A). Se o worker fizer push de um fix, o
            # HEAD muda e o SHA-guard não dispara.
            monitor._resume_tracker.reset_address_attempt(target.number)
            _sha_to_record = getattr(target, "head_sha", "") or ""
            if _sha_to_record:
                monitor._resume_tracker.set_reviewed_sha(target.number, _sha_to_record)
            # Release the batch lock so the next tick can re-claim; keep em_andamento.
            await monitor.forge.clear_batch_label("pr", target.number)
            logger.info(
                "pr_review #%d incompleto — em_andamento (será retomada)", target.number
            )
            return

        # Issue #309 fase 3.5 — Bug B fix: proof-of-work check antes de marcar
        # CONCLUDED no caminho legacy (resume desligado). Sem evidência (comment
        # do bot, review formal, merge, novo commit) NÃO marca concluida —
        # libera batch pra reaper retomar (impede review-theatre silencioso
        # observado no R2 da PR #344 onde labels alternaram em 5s sem qualquer
        # ação real do worker).
        bot_login = await _resolve_bot_login(monitor)
        has_proof = await _assert_review_proof_of_work(
            monitor.forge,
            "pr",
            target.number,
            bot_login,
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
                monitor,
                f"could not transition PR #{target.number} to concluida",
                exc,
            )
        await monitor.forge.clear_batch_label("pr", target.number)
        monitor._stats.prs_reviewed += 1
        await monitor.notifier.pr_reviewed(
            target.number, target.title, target.url, merged=False
        )
    finally:
        monitor._resume_in_flight.discard(target.number)


async def review_one_open_pr(monitor: "PipelineMonitor") -> None:
    try:
        prs = await monitor.forge.list_open_prs(limit=50)
    except GhCommandError as exc:
        await _record_forge_error(
            monitor,
            "could not list PRs (forge error)",
            exc,
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

    # --- Pre-processing: invalidate ~review:concluida on PRs with new commits
    #     (issue #351 — invalidate-on-new-commit). Runs BEFORE candidate
    #     selection; the freshly invalidated PR will be picked up on the NEXT
    #     tick because _candidate() checks pr.labels on the same in-memory
    #     snapshot (which still carries REVIEW_CONCLUDED until the next fetch).
    for pr in prs:
        if (
            REVIEW_CONCLUDED in pr.labels
            and WORKFLOW_BLOCKED not in pr.labels
            and not pr.is_draft
            and pr.batch_id is None
            and monitor._owns_pr_branch(pr.head_ref, pr_number=pr.number)
        ):
            await _handle_review_concluded_invalidation(monitor, pr)

    def _candidate(pr) -> bool:
        if (
            pr.is_draft
            or REVIEW_CONCLUDED in pr.labels
            or WORKFLOW_BLOCKED in pr.labels
        ):
            return False
        if not monitor._owns_pr_branch(pr.head_ref, pr_number=pr.number):
            return False
        if REVIEW_IN_PROGRESS in pr.labels:
            # In-progress: only resumable when resume is enabled, cadence ok,
            # and not currently batch-locked by another monitor's live attempt.
            return (
                resume_enabled
                and pr.batch_id is None
                and pr.number not in monitor._resume_in_flight
                and monitor._resume_tracker.cadence_ok(
                    pr.number, now, monitor.config.resume_interval
                )
            )
        # Fresh: unclaimed PR awaiting first review.
        return pr.batch_id is None

    # Sort by priority so the most urgent PR is reviewed first.
    # Decisão #46 — skip PRs em janela de pausa por backoff de auth.
    target = None
    for pr in sort_by_priority(prs):
        if not _candidate(pr):
            continue
        if is_target_auth_paused(monitor, "pr", pr.number):
            _key = _auth_target_key("pr", pr.number)
            _paused_until = monitor._paused_until_ts.get(_key, 0.0)
            _rem = max(0, int(_paused_until - _monotonic()))
            log_auth_skip(
                target=_key,
                until_iso=format_iso_utc(now_utc() + timedelta(seconds=_rem)),
                remaining_s=_rem,
            )
            continue
        target = pr
        break
    if target is None:
        return
    # Defensive guard (Mistério #4): if the head branch no longer exists on
    # the remote (force-deleted, squash-merged with branch removal, etc.),
    # there is nothing to review/merge — block the PR so it does not churn
    # the pipeline forever. The human removes ``~workflow:bloqueada`` after
    # restoring the branch (or closes the PR by hand).
    if target.head_ref:
        try:
            branch_alive = await monitor.forge.branch_exists(target.head_ref)
        except Exception as exc:  # noqa: BLE001 — fail-open on API hiccup
            logger.debug(
                "branch_exists check failed for PR #%d (%s); proceeding",
                target.number,
                exc,
            )
            branch_alive = True
        if not branch_alive:
            logger.warning(
                "PR #%d has orphan head_ref=%r (branch deleted on remote); "
                "marking %s",
                target.number,
                target.head_ref,
                WORKFLOW_BLOCKED,
            )
            await _block_pr(
                monitor,
                target.number,
                target.title,
                target.url,
                f"branch `{target.head_ref}` foi removida do remote — "
                "restaure a branch ou feche a PR manualmente",
            )
            monitor._stats.errors += 1
            return
    # CI-gate determinístico (issue #85 Phase 2): NÃO despachar a review
    # enquanto o CI do repo-alvo ainda está rodando. O tick é de ~60s; sem este
    # gate a review seria redispachada a cada tick durante os minutos do CI,
    # cada dispatch abrindo uma sessão claude que só veria "CI pending" e
    # encerraria — custo + churn de label, sem progresso. ``pending`` = CI em
    # execução → skip este tick (NÃO consome tentativa); o próximo tick recheca.
    # ``passing``/``failing``/``none`` seguem o fluxo normal e o brief decide
    # (verde → merge, vermelho → corrige até verde, sem-CI → suíte in-pod).
    # Best-effort: falha de API no check NÃO bloqueia o dispatch (fail-open).
    try:
        _ci_status = await monitor.forge.get_ci_status(target.number)
    except Exception as exc:  # noqa: BLE001 — fail-open: CI check não trava review
        logger.debug(
            "get_ci_status falhou para PR #%d (%s); prossegue sem gate",
            target.number,
            exc,
        )
        _ci_status = "none"
    if _ci_status == "pending":
        logger.info(
            "pr_review #%d: CI em execução (pending) — skip dispatch neste tick "
            "(aguarda CI terminar; reconcile no próximo)",
            target.number,
        )
        return
    is_resume = REVIEW_IN_PROGRESS in target.labels
    # FIX #6 (Decisão #33): monitor único (shard_count==1) NÃO deve claimar
    # ~batch: — gera add/remove do label a cada tick sem necessidade, pois
    # ~review:em_andamento já é o lock durável. Espelha _critique_one_issue
    # (stages.py ~linha 977) que já aplicava este guard.
    multi = monitor.identity.shard_count > 1
    if multi:
        batch = await monitor.forge.claim_with_batch("pr", target.number)
        if batch is None:
            return
    # Tag ownership so other monitors can identify who claimed this PR —
    # mirrors the identical pattern in stage 1 for issues.
    await monitor.forge.add_labels(
        "pr", target.number, [monitor.identity.ownership_label()]
    )
    if is_resume:
        state = monitor._resume_tracker.get(target.number)
        # Attempt ceiling for review/merge — per-stage max_retries (issue #391).
        _review_max_attempts = resolve_stage_max_retries("pr_review")
        if state.attempt >= _review_max_attempts:
            await monitor.forge.clear_batch_label("pr", target.number)
            await _block_pr(
                monitor,
                target.number,
                target.title,
                target.url,
                f"teto de tentativas atingido ({state.attempt}/"
                f"{_review_max_attempts}) sem mergear",
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

    # FRESH (issue #373): dispatch fire-and-forget. ``em_andamento`` é o lock
    # durável; o ``~batch:`` é transitório (libera já). O veredito é processado
    # por :func:`reconcile_review_prs` no tick seguinte via ground-truth (PR
    # merged?) + resume-info. NÃO bloqueia o tick.
    if not is_resume:
        outcome = await monitor.implementer.review(monitor, target, resume=False)
        await monitor.forge.clear_batch_label("pr", target.number)
        if not outcome.ok:
            logger.warning(
                "pr_review #%d: fresh dispatch falhou (%s) — em_andamento; "
                "reaper/reconcile retomam",
                target.number,
                (outcome.error or "")[:160],
            )
            return
        logger.info(
            "pr_review #%d dispatched fire-and-forget (task_id=%s) — reconcile "
            "no próximo tick",
            target.number,
            getattr(outcome, "task_id", "") or "",
        )
        return

    # RESUME: roda em BACKGROUND (não bloqueia o loop do monitor). O gate de
    # cadência (record_dispatch acima) + _resume_in_flight impedem re-dispatch
    # concorrente da mesma PR; o lease do worker é o backstop.
    monitor._resume_in_flight.add(target.number)
    monitor.spawn_background(_resume_review_one_pr(monitor, target, resume_enabled))
    return


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
                kind,
                number,
                bot_login,
                since_ts=since_ts,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "proof_of_work check: forge.has_bot_activity_since raised: %s — "
            "assuming true (fail-open)",
            exc,
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
            logger.warning(
                "post_merge_callback failed for PR #%d: %s", target.number, exc
            )


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
        logger.warning(
            "stage 4: could not post follow-up report on PR #%s: %s", pr_number, exc
        )

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
                pr.number,
                exc,
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
    """Scan ~review:em_andamento, ~workflow:em_implementacao e
    ~workflow:em_revisao com idade > ``config.reaper_stale_seconds`` sem
    progresso e libera (próximo tick re-claim via resume). Best-effort: catch
    + log nas operações de label.

    Mecânica:
    1. Lista PRs abertas e issues abertas com label terminal-stale.
    2. Pra cada uma, lê ``label_applied_at`` da label terminal.
    3. Se idade > threshold:
       - Lê ``current_attempt`` das labels ~attempt:N (default 0).
       - Se ``attempt + 1 >= reaper_max_attempts``: marca ~workflow:bloqueada
         + ~retry:exhausted (não retorna pra fila — humano decide).
       - Senão: remove ~review:em_andamento (ou ~workflow:em_implementacao
         ou ~workflow:em_revisao), remove batch_label e ownership, adiciona
         ~attempt:(N+1), recoloca label inicial (~review:pendente,
         ~workflow:nova ou ~workflow:revisada).

    Não toca em PRs/issues sem dispatch do nosso monitor (ownership label) —
    apenas escopa às próprias.

    NOTA: ``em_arquitetura`` e ``em_refinamento`` são AMBÍGUOS — podem ser
    estado de DESCANSO entre passes (a issue aguarda o próximo tick, SEM
    dispatch em voo) OU lock de um refino fire-and-forget travado (issue #373,
    COM ledger entry + task_id). O reaper cobre dois casos:
    1. Com ledger entry com task_id: ramo com-ledger (TTL = reaper_stale_seconds).
    2. Sem ledger entry / ledger ausente: ramo sem-ledger (TTL =
       reaper_arch_hard_seconds, 2h default, muito maior que poll_interval 60s)
       — um item sadio em descanso é re-selecionado em ~1-2 ticks e grava
       ledger entry, permanecendo abaixo do hard-ceiling. Permanecer 2h sem
       ledger = zumbi real.
    ``em_revisao`` (crítica fire-and-forget) é sempre lock transitório.

    AC8 (#427): cada ramo auto-pula quando seu TTL é <= 0 (sem early-return
    global) para que desligar um TTL não silencia os demais ramos.
    """
    threshold = monitor.config.reaper_stale_seconds
    max_attempts = monitor.config.reaper_max_attempts
    arch_hard_threshold = monitor.config.reaper_arch_hard_seconds
    now_ts = int(time.time())
    own_label = monitor.identity.ownership_label()

    # PRs com ~review:em_andamento (stuck no review).
    # AC8 (#427): pula o ramo quando seu TTL específico é 0 (não early-return global).
    if threshold > 0:
        try:
            prs = await monitor.forge.list_open_prs()
        except GhCommandError as exc:
            await _record_forge_error(monitor, "reaper: list_open_prs failed", exc)
            return
        for pr in sort_by_priority(prs):
            if REVIEW_IN_PROGRESS not in pr.labels:
                continue
            # Só re-claim PRs deste monitor (ownership).
            if own_label not in pr.labels:
                continue
            applied_at = await monitor.forge.label_applied_at(
                "pr",
                pr.number,
                REVIEW_IN_PROGRESS,
            )
            if applied_at is None:
                continue  # forge sem suporte ou label sem timestamp
            age = now_ts - applied_at
            if age < threshold:
                continue
            await _reap_one(
                monitor,
                kind="pr",
                number=pr.number,
                labels=pr.labels,
                from_label=REVIEW_IN_PROGRESS,
                to_label=REVIEW_PENDING,
                max_attempts=max_attempts,
                age_seconds=age,
                description=f"PR #{pr.number} review stuck há {age // 60}min",
                url=pr.url,
            )

    # Issues com ~workflow:em_implementacao (stuck no implement).
    # AC8 (#427): mesma política — pula ramo quando threshold <= 0.
    if threshold > 0:
        try:
            impl_issues = await monitor.forge.list_issues_with_label(
                WORKFLOW_IMPLEMENTING,
            )
        except GhCommandError as exc:
            await _record_forge_error(
                monitor,
                "reaper: list_issues_with_label failed",
                exc,
            )
            return
        for issue in sort_by_priority(impl_issues):
            if own_label not in issue.labels:
                continue
            applied_at = await monitor.forge.label_applied_at(
                "issue",
                issue.number,
                WORKFLOW_IMPLEMENTING,
            )
            if applied_at is None:
                continue
            age = now_ts - applied_at
            if age < threshold:
                continue
            await _reap_one(
                monitor,
                kind="issue",
                number=issue.number,
                labels=issue.labels,
                from_label=WORKFLOW_IMPLEMENTING,
                to_label=WORKFLOW_REVIEWED,
                max_attempts=max_attempts,
                age_seconds=age,
                description=f"issue #{issue.number} implement stuck há {age // 60}min",
                url=issue.url,
            )

    # Issues com ~workflow:em_revisao (crítica de escopo interrompida por restart
    # de pod — lock transitório que nenhum stage reseleciona).
    # AC8 (#427): mesma política — pula ramo quando threshold <= 0.
    if threshold > 0:
        try:
            reviewing_issues = await monitor.forge.list_issues_with_label(
                WORKFLOW_REVIEWING,
            )
        except GhCommandError as exc:
            await _record_forge_error(
                monitor,
                "reaper: list_issues_with_label(em_revisao) failed",
                exc,
            )
            return
        for issue in sort_by_priority(reviewing_issues):
            if own_label not in issue.labels:
                continue
            applied_at = await monitor.forge.label_applied_at(
                "issue",
                issue.number,
                WORKFLOW_REVIEWING,
            )
            if applied_at is None:
                continue
            age = now_ts - applied_at
            if age < threshold:
                continue
            await _reap_one(
                monitor,
                kind="issue",
                number=issue.number,
                labels=issue.labels,
                from_label=WORKFLOW_REVIEWING,
                to_label=WORKFLOW_NEW,
                max_attempts=max_attempts,
                age_seconds=age,
                description=f"issue #{issue.number} em_revisao stuck há {age // 60}min",
                url=issue.url,
            )

    # Issues com ~workflow:em_refinamento / ~workflow:em_arquitetura travadas por
    # um refino fire-and-forget (issue #373). AMBÍGUOS: só reapa quando há ledger
    # entry com task_id (= dispatch em voo travado), distinguindo do descanso
    # entre passes (sem entry → o refino o reseleciona no próximo tick).
    ledger = getattr(monitor.implementer, "_ledger", None)
    if ledger is not None:
        for refine_state in (WORKFLOW_REFINING, WORKFLOW_ARCHITECTURE):
            try:
                refine_issues = await monitor.forge.list_issues_with_label(refine_state)
            except GhCommandError as exc:
                await _record_forge_error(
                    monitor,
                    f"reaper: list_issues_with_label({refine_state}) failed",
                    exc,
                )
                continue
            for issue in sort_by_priority(refine_issues):
                if own_label not in issue.labels:
                    continue
                # Só lock de dispatch em voo (ledger entry com task_id).
                entry = ledger.get(DispatchLedger.key_for_issue(issue.number))
                if not entry or not entry.get("task_id"):
                    continue
                applied_at = await monitor.forge.label_applied_at(
                    "issue",
                    issue.number,
                    refine_state,
                )
                if applied_at is None:
                    continue
                age = now_ts - applied_at
                if age < threshold:
                    continue
                await _reap_one(
                    monitor,
                    kind="issue",
                    number=issue.number,
                    labels=issue.labels,
                    from_label=refine_state,
                    to_label=WORKFLOW_NEW,
                    max_attempts=max_attempts,
                    age_seconds=age,
                    description=f"issue #{issue.number} {refine_state} stuck há {age // 60}min",
                    url=issue.url,
                )
                # Lock liberado → o dispatch em voo morreu; limpa o ledger pra
                # não consultar resume-info de uma task abandonada.
                ledger.clear(DispatchLedger.key_for_issue(issue.number))

    # ----------------------------------------------------------------------- #
    # Ramo sem-ledger: em_arquitetura / em_refinamento SEM task_id no ledger  #
    # (AC1 + AC2 — issue #427)                                                #
    # ----------------------------------------------------------------------- #
    # Cobre dois sub-casos:
    #   A) ledger is None  — modo sem worker (sem DispatchLedger disponível).
    #   B) ledger is not None mas a entry está ausente ou sem task_id —
    #      dispatch morreu antes de gravar no ledger (TOCTOU, restart, etc.).
    #
    # Um item sadio em "descanso entre passes" é re-selecionado em ~1-2
    # poll_interval (60s) e a re-seleção grava ledger entry com task_id
    # (stages.py:1270-1278), promovendo-o ao ramo com-ledger. Permanecer
    # arch_hard_threshold (default 2h) SEM task_id ⇒ zumbi real.
    #
    # AC8 (#427): pula ramo inteiro quando arch_hard_threshold <= 0.
    if arch_hard_threshold > 0:
        for refine_state in (WORKFLOW_REFINING, WORKFLOW_ARCHITECTURE):
            try:
                refine_issues_no_ledger = await monitor.forge.list_issues_with_label(
                    refine_state,
                )
            except GhCommandError as exc:
                await _record_forge_error(
                    monitor,
                    f"reaper(no-ledger): list_issues_with_label({refine_state}) failed",
                    exc,
                )
                continue
            for issue in sort_by_priority(refine_issues_no_ledger):
                if own_label not in issue.labels:
                    continue
                # Pula se o ramo com-ledger já tratou esta issue neste tick
                # (ledger entry com task_id ⇒ o ramo com-ledger acima é o dono).
                if ledger is not None:
                    entry = ledger.get(DispatchLedger.key_for_issue(issue.number))
                    if entry and entry.get("task_id"):
                        continue  # item sadio com dispatch em voo — não é zumbi
                applied_at = await monitor.forge.label_applied_at(
                    "issue",
                    issue.number,
                    refine_state,
                )
                if applied_at is None:
                    continue
                age = now_ts - applied_at
                if age < arch_hard_threshold:
                    continue
                # AC2: heurística de roteamento. Se a issue já passou por ao
                # menos um pass de refino (current_refine_attempt > 0), volta
                # para ~workflow:revisada (o contexto de refino está gravado nos
                # labels ~refine:N — o próximo tick retoma do ponto). Caso
                # contrário volta para ~workflow:nova (primeira tentativa).
                if (
                    refine_state == WORKFLOW_ARCHITECTURE
                    and current_refine_attempt_from_labels(issue.labels) > 0
                ):
                    to_label = WORKFLOW_REVIEWED
                else:
                    to_label = WORKFLOW_NEW
                await _reap_one(
                    monitor,
                    kind="issue",
                    number=issue.number,
                    labels=issue.labels,
                    from_label=refine_state,
                    to_label=to_label,
                    max_attempts=max_attempts,
                    age_seconds=age,
                    description=(
                        f"issue #{issue.number} {refine_state} zumbi "
                        f"(sem task_id há {age // 60}min, hard-TTL={arch_hard_threshold // 60}min)"
                    ),
                    url=issue.url,
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
    url: str,
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
                "reaper #%d: remove_labels failed: %s",
                number,
                exc,
            )
        try:
            await monitor.forge.add_labels(
                kind,
                number,
                [WORKFLOW_BLOCKED, make_attempt_label(next_attempt)],
            )
        except GhCommandError as exc:
            logger.warning(
                "reaper #%d: add bloqueada failed: %s",
                number,
                exc,
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
            kind,
            number,
            next_attempt,
            age_seconds,
        )
        log_reaper_block(
            target_kind=kind,
            target=number,
            attempts=next_attempt,
            cap=max_attempts,
            reason=description,
        )
        try:
            await monitor.notifier.reaper_blocked(
                number,
                url,
                kind=kind,
                attempt=next_attempt,
                max_attempts=max_attempts,
                age_seconds=age_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort DM
            logger.warning("reaper #%d: discord notify failed: %s", number, exc)
        return

    # Libera: remove labels stale, adiciona ~attempt:(N+1) + label de retorno.
    # If remove fails, skip add: applying to_label while from_label persists
    # would leave the issue wearing two ~workflow: state labels simultaneously,
    # violating the single-state invariant. Let the reaper retry next tick.
    try:
        await monitor.forge.remove_labels(kind, number, to_remove)
    except GhCommandError as exc:
        logger.warning(
            "reaper #%d: remove_labels failed: %s — skipping add_labels to preserve "
            "label-state invariant; will retry next tick",
            number,
            exc,
        )
        return
    try:
        await monitor.forge.add_labels(
            kind,
            number,
            [to_label, make_attempt_label(next_attempt)],
        )
    except GhCommandError as exc:
        logger.warning("reaper #%d: add_labels failed: %s", number, exc)
    logger.info(
        "reaper RELEASED %s #%d to %s (attempt %d/%d, age=%ds)",
        kind,
        number,
        to_label,
        next_attempt,
        max_attempts,
        age_seconds,
    )
    log_reaper_unblock(
        target_kind=kind,
        target=number,
        attempts=next_attempt,
        reason=description,
        last_activity_s=age_seconds,
    )


async def reconcile_closed_issues(monitor: "PipelineMonitor") -> None:
    """Run terminal GC on issues in ~workflow:em_pr that GitHub closed (issue #590).

    When a PR is merged and it references an issue (Closes #N), GitHub
    automatically closes the issue. This reconcile pass detects those
    closed issues and calls run_terminal_gc best-effort, which strips
    transient pipeline labels and applies ~workflow:concluida.

    Best-effort: any failure is logged but does NOT abort the tick.
    Idempotent: run_terminal_gc returns 'noop' on already-clean issues.
    """
    try:
        issues = await monitor.forge.list_issues_with_label(WORKFLOW_PR, limit=50)
    except (
        Exception
    ) as exc:  # noqa: BLE001 — best-effort; do not count toward forge_errors
        logger.warning(
            "reconcile_closed_issues: could not list em_pr issues: %s",
            exc,
        )
        return
    for issue in issues:
        try:
            current = await monitor.forge.get_issue(issue.number)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "reconcile_closed_issues: get_issue #%d failed: %s",
                issue.number,
                exc,
            )
            continue
        if current is None or current.state != "closed":
            continue
        try:
            result = await run_terminal_gc(
                monitor.forge, "issue", issue.number, "closed"
            )
            logger.debug(
                "reconcile_closed_issues: GC %s for closed issue #%d",
                result,
                issue.number,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "reconcile_closed_issues: GC failed for issue #%d: %s",
                issue.number,
                exc,
            )
