"""1-minute polling loop that drives the autonomous pipeline.

The :class:`PipelineMonitor` ticks every ``poll_interval_seconds`` (default 60s)
and, on each tick:

1. Checks for issues with label ``~workflow:nova`` *and no ~batch:* — claims
   the next one, transitions to ``~workflow:em_revisao``, asks DEILE to revise
   the body, then transitions to ``~workflow:revisada``.
2. Checks for issues with ``~workflow:revisada`` and no ``~workflow:em_pr`` —
   claims, sets up a worktree, invokes Claude Code one-shot to implement, and
   on success transitions the issue to ``~workflow:em_pr``.
3. Checks for open PRs without ``~review:concluida`` — claims, invokes Claude
   Code one-shot to review/correct/merge, then marks ``~review:concluida``.

Discord notifications (DiscordNotifier) fire at every transition.

This monitor is single-instance by design: locking via ``~batch:`` labels is a
best-effort coordination mechanism, not a true distributed lock.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional

from deile.orchestration.forge import ForgeClient, IssueRef, build_forge
from deile.orchestration.pipeline import stages
from deile.orchestration.pipeline._time_utils import now_utc, parse_iso_utc
from deile.orchestration.pipeline.actions import ACTIONS_BY_NAME
from deile.orchestration.pipeline.claude_dispatcher import ClaudeDispatcher
from deile.orchestration.pipeline.constants import (
    PIPELINE_STOP_TIMEOUT_SECONDS, pipeline_poll_interval_seconds)
# Import path preserved for callers that still type-hint ``GitHubClient`` —
# resolved through the shim so legacy attribute usage stays compatible.
from deile.orchestration.pipeline.github_client import \
    GitHubClient  # noqa: F401
from deile.orchestration.pipeline.identity import MonitorIdentity
from deile.orchestration.pipeline.implementer import (PipelineImplementer,
                                                      build_implementer,
                                                      is_claude_mode)
from deile.orchestration.pipeline.labels import (WORKFLOW_IMPLEMENTING,
                                                 WORKFLOW_NEW,
                                                 WORKFLOW_REVIEWED)
from deile.orchestration.pipeline.lockfile import LockHeldError
from deile.orchestration.pipeline.lockfile import acquire as acquire_lock
from deile.orchestration.pipeline.lockfile import release as release_lock
from deile.orchestration.pipeline.notifier import DiscordNotifier
from deile.orchestration.pipeline import pipeline_logger
from deile.orchestration.pipeline.resume_state import ResumeTracker
from deile.orchestration.pipeline.scheduler import PendingRun, ScheduleStore
from deile.orchestration.pipeline.stages import (_extract_pr_url,
                                                 _render_follow_up_report)
from deile.orchestration.pipeline.worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)

# Re-exported from ``stages`` for backwards compatibility: existing tests and
# callers import ``_extract_pr_url``/``_render_follow_up_report`` from this
# module. The canonical definitions now live in ``stages.py``.
__all__ = ["PipelineConfig", "PipelineMonitor", "_extract_pr_url",
           "_render_follow_up_report"]


@dataclass
class PipelineConfig:
    repo: str
    base_repo_path: Path
    poll_interval_seconds: int = pipeline_poll_interval_seconds()
    main_branch: str = "main"
    # ``branch_prefix`` is the *legacy* per-instance default. When an
    # ``identity`` is provided to :class:`PipelineMonitor`, the actual prefix
    # is derived from ``identity.branch_prefix("auto") + "/issue-"`` so two
    # monitors don't collide on branch names. ``branch_prefix`` here remains
    # the fallback for single-monitor (default identity) deployments.
    branch_prefix: str = "auto/issue-"
    notify_user_id: Optional[str] = None
    enable_review: bool = True
    enable_implement: bool = True
    enable_pr_review: bool = True
    enable_classify: bool = True
    enable_follow_ups: bool = True
    # Labels que disparam a auto-classificação. Convenção do projeto: o label
    # CANÔNICO é IDÊNTICO ao prefixo entre colchetes do título da issue
    # (``[FEATURE]`` → ``feature``, ``[BUG]`` → ``bug``, ``[INTENT]`` →
    # ``intent``, ``[REFACTOR]`` → ``refactor``), espelhando o ``labels:`` dos
    # templates em ``.github/ISSUE_TEMPLATE/``. ``security`` é aceito sem
    # template dedicado. ``enhancement`` entra como alias tolerado: é o label
    # padrão do GitHub para features e o que o template antigo aplicava — sem
    # ele, uma issue de feature criada com o label convencional ficava
    # invisível ao pipeline (foi exatamente o que travou a demo: a #247 nasceu
    # com ``enhancement`` e só andou quando virou ``intent``).
    classifiable_labels: frozenset = frozenset(
        {"intent", "bug", "refactor", "feature", "enhancement", "security"}
    )
    classify_skip_labels: frozenset = frozenset({"infra"})
    enable_pr_triage: bool = True
    enable_mention_handling: bool = True
    mention_handle: str = "@deile-one"
    # Default True: two simultaneous /pipeline start on the same host fail fast.
    use_pid_lock: bool = True
    # When True, Stage 3 reviews any non-draft PR regardless of head branch origin.
    enable_review_human_prs: bool = False
    # Limit catch-up to runs missed within the last N hours.  None = no limit (legacy).
    # Recommended: 1–2 hours so a long outage does not flood the queue.
    bootstrap_replay_window_hours: Optional[int] = 1
    # When True, cleanup_merged_branches() runs once on startup.
    enable_worktree_cleanup: bool = True
    # Which strategy implements/reviews the work (see ``implementer.py``):
    #   "claude"       → run ``claude -p`` in a local git worktree (legacy);
    #   "deile_worker" → dispatch to the deile-worker Pod over HTTP (DEILE-to-DEILE).
    # The dataclass default stays "claude" so hand-built configs (unit tests
    # that inject a mocked ``claude``) keep the legacy behaviour. The *product*
    # default is "deile_worker", resolved from settings in
    # ``build_default_pipeline_config`` — every real entry point (CLI autostart,
    # /pipeline tool/command, the deile-pipeline deployment) uses the worker.
    dispatch_mode: str = "claude"
    # Resume of partial work (issue #254). When ``enable_resume`` is True, the
    # monitor re-dispatches issues parked in ``~workflow:em_implementacao``
    # (continuable, NOT ``~workflow:bloqueada``) in RESUME mode instead of
    # leaving them parked forever. The three knobs mirror the settings:
    #   resume_interval     — min seconds between resume attempts (0 = immediate).
    #   resume_max_attempts — attempt ceiling per issue before the block flow.
    #   resume_budget       — accumulated-seconds ceiling (0 = no time ceiling).
    # The dataclass default is False so hand-built unit-test configs keep the
    # legacy "park forever" behaviour unless they opt in; the product default is
    # resolved from settings in ``build_default_pipeline_config``.
    enable_resume: bool = False
    resume_interval: int = 0
    resume_max_attempts: int = 10
    resume_budget: int = 0
    #: Dedicated tighter ceiling (#10) for the "agent finished without opening a
    #: PR" failure path — usually irrecoverable (LLM gave up on the task
    #: structure), so re-trying 10x is pure waste. After this many "incompleto
    #: sem PR" parks the issue is auto-blocked. #283 hit 50+ before the operator
    #: intervened manually; default 3 catches it before $10 burns.
    incomplete_no_pr_max: int = 3
    # Refinement gate + parallel decomposition (issue #257). ``refine_max_attempts``
    # caps how many refinement passes a poor-scoped issue gets before it is blocked
    # and returned to its author. ``max_parallel`` caps how many implementations the
    # implement stage dispatches CONCURRENTLY per tick (asyncio.gather to the
    # deile-worker Service; needs >=2 worker replicas to actually run in parallel).
    refine_max_attempts: int = 5
    max_parallel: int = 2
    # Reaper de claim órfão (issue #309 fase 3.5): PRs/issues com label
    # ~review:em_andamento ou ~workflow:em_implementacao há mais de
    # ``reaper_stale_seconds`` sem progresso são re-claimed (com
    # ``~attempt:N`` incrementado) para próximo tick retomar via resume.
    # Default 45min: > timeout máximo do claude-worker (2h) seria over-cauteloso;
    # 45min cobre o timeout antigo de 30min com folga. 0 desliga o reaper.
    reaper_stale_seconds: int = 45 * 60
    # Quando attempt >= reaper_max_attempts, o reaper bloqueia em vez de
    # liberar. Espelha resume_max_attempts mas separado pq são caminhos
    # distintos (resume = continuar trabalho parado; reaper = trabalho
    # presumido morto). Default 3 = 3 ciclos de 45min = 2h15m max stuck.
    reaper_max_attempts: int = 3
    # The refinement gate (critique → refine loop → decompose) is worker-only:
    # it dispatches type-specific personas (analyst/architect/debugger) to the
    # deile-worker. On the legacy Claude path it is OFF, so ``review`` keeps its
    # old no-op transition and refine/decompose no-op. Resolved from dispatch_mode
    # in ``build_default_pipeline_config`` (mirrors ``enable_resume``).
    enable_refinement_gate: bool = False


def _resolve_auto_max_parallel(namespace: str = "deile") -> Optional[int]:
    """Read the current ``claude-worker`` replica count via kubectl.

    Called when ``DEILE_PIPELINE_MAX_PARALLEL=auto`` is set so the pipeline
    derives its concurrency ceiling from the actual number of worker pods
    instead of a hardcoded value.  Returns ``None`` on any error (caller falls
    back to the built-in default of 2).
    """
    import shutil
    import subprocess

    kubectl = shutil.which("kubectl")
    if kubectl is None:
        logger.warning(
            "max_parallel=auto: kubectl not found on PATH; falling back to default"
        )
        return None
    try:
        proc = subprocess.run(
            [kubectl, "-n", namespace, "get",
             "deployment/claude-worker",
             "-o", "jsonpath={.spec.replicas}"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            logger.warning(
                "max_parallel=auto: kubectl exited %d; falling back to default. stderr=%r",
                proc.returncode, proc.stderr[:200],
            )
            return None
        raw = proc.stdout.strip()
        if not raw.isdigit():
            logger.warning(
                "max_parallel=auto: unexpected replicas value %r; falling back to default",
                raw,
            )
            return None
        replicas = int(raw)
        logger.info("max_parallel=auto: derived %d from claude-worker replicas", replicas)
        return replicas
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("max_parallel=auto: kubectl call failed (%s); falling back to default", exc)
        return None


def build_default_pipeline_config(*, use_pid_lock: bool = True) -> PipelineConfig:
    """Construct a :class:`PipelineConfig` from the default repo/path/settings.

    Centralizes the repo + base-path + notify-user resolution shared by the
    ``pipeline`` tool and the ``/pipeline`` slash command, so the two
    surfaces cannot drift on how a default config is assembled. ``use_pid_lock``
    lets the ``/pipeline start --no-pid-lock`` flag route through this helper
    too instead of hand-building its own config.

    When ``settings.pipeline_max_parallel == "auto"`` (set via
    ``DEILE_PIPELINE_MAX_PARALLEL=auto`` or ``pipeline.max_parallel: auto``
    in settings.json), ``max_parallel`` is derived from the current
    ``claude-worker`` replica count via ``kubectl``.  Falls back to 2 if
    kubectl is unavailable or the deployment cannot be read.
    """
    from deile.config.settings import get_settings
    from deile.orchestration.pipeline.constants import resolve_pipeline_repo
    from deile.tools._pipeline_paths import resolve_base_path

    settings = get_settings()
    dispatch_mode = (settings.pipeline_dispatch_mode or "deile_worker").strip().lower()

    # Resolve max_parallel: numeric setting → int directly; "auto" → kubectl.
    _mp_raw = settings.pipeline_max_parallel
    if _mp_raw == "auto":
        _derived = _resolve_auto_max_parallel()
        max_parallel = _derived if _derived is not None else 2
    else:
        max_parallel = int(_mp_raw)

    return PipelineConfig(
        repo=resolve_pipeline_repo(),
        base_repo_path=resolve_base_path(),
        notify_user_id=settings.pipeline_notify_user_id,
        use_pid_lock=use_pid_lock,
        dispatch_mode=dispatch_mode,
        # The deile_worker path implements/reviews inside the worker Pod; the
        # pipeline process has no local clone, so the on-startup worktree
        # cleanup would only emit warnings. Keep it for the claude path.
        enable_worktree_cleanup=is_claude_mode(dispatch_mode),
        # Resume of partial work (issue #254 + #309 fase 3.5).
        # Originalmente exclusivo do deile-worker (structured ground-truth via
        # ``resume_block``); fase 3.5 estendeu ao claude-worker via
        # ``DispatchLedger`` + ``--resume <session-id>`` no claude CLI.
        # Hoje o resume vale pra QUALQUER dispatch_mode (resolve em runtime
        # via DispatchLedger). Só o operator decide via setting.
        mention_handle=settings.forge_bot_login,
        enable_resume=bool(settings.pipeline_resume_enabled),
        resume_interval=int(settings.pipeline_resume_interval),
        resume_max_attempts=int(settings.pipeline_resume_max_attempts),
        resume_budget=int(settings.pipeline_resume_budget),
        refine_max_attempts=int(settings.pipeline_refine_max_attempts),
        max_parallel=max_parallel,
        enable_refinement_gate=(not is_claude_mode(dispatch_mode)),
    )


class _Stats:
    """Mutable stat bag for the pipeline monitor.

    ``forge_errors`` counts failures attributable to the forge CLI / REST API
    (previously ``gh_errors``). The old name remains accessible via a
    deprecated property for one release so existing dashboards / log scrapers
    keep working without change.
    """

    def __init__(self) -> None:
        self.ticks: int = 0
        self.issues_reviewed: int = 0
        self.issues_implemented: int = 0
        self.prs_reviewed: int = 0
        self.issues_classified: int = 0
        self.errors: int = 0
        # Contadores separados permitem distinguir falhas de CLI/REST da forge
        # de falhas do Claude (ex.: timeout, budget exceeded).
        self.forge_errors: int = 0
        self.claude_errors: int = 0
        self.catchup_runs: int = 0
        self.scheduled_runs: int = 0
        self.follow_ups_opened: int = 0
        self.follow_ups_skipped: int = 0
        # Incrementado quando uma ação agendada está desabilitada via enable_*.
        self.skipped_runs: int = 0
        self.prs_classified: int = 0
        self.mentions_processed: int = 0
        # Issues movidas para ~workflow:bloqueada pelo fluxo de bloqueio (#254).
        self.issues_blocked: int = 0
        # Dispatches de resume reenviados para implementações em pausa.
        self.resume_dispatches: int = 0

    @property
    def gh_errors(self) -> int:
        """Deprecated alias for :attr:`forge_errors`.

        .. deprecated::
            Use ``forge_errors`` directly. This alias will be removed in the
            next major release.
        """
        warnings.warn(
            "_Stats.gh_errors is deprecated; use forge_errors instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.forge_errors

    @gh_errors.setter
    def gh_errors(self, value: int) -> None:
        """Deprecated setter — redirects writes to :attr:`forge_errors`."""
        warnings.warn(
            "_Stats.gh_errors is deprecated; use forge_errors instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.forge_errors = value


class PipelineMonitor:
    """Async polling driver of the issue → PR → merge pipeline."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        forge: Optional[ForgeClient] = None,
        github: Optional[ForgeClient] = None,
        worktrees: Optional[WorktreeManager] = None,
        claude: Optional[ClaudeDispatcher] = None,
        notifier: Optional[DiscordNotifier] = None,
        review_callback: Optional[Callable[[IssueRef], Awaitable[str]]] = None,
        post_merge_callback: Optional[Callable[[int, str, str], Awaitable[None]]] = None,
        identity: Optional[MonitorIdentity] = None,
        schedule_store: Optional[ScheduleStore] = None,
        implementer: Optional[PipelineImplementer] = None,
    ) -> None:
        self.config = config
        self.identity = identity or MonitorIdentity.from_env()
        # ``forge`` is the canonical attribute (post-issue #297). ``github`` is
        # kept as a deprecated kwarg for legacy test code that passed a
        # ``GitHubClient`` by keyword; if both are given ``forge`` wins. When
        # neither is supplied the factory builds the right adapter from env +
        # ``config.repo`` — GitLab/self-hosted operators set ``DEILE_FORGE_KIND``
        # and (optionally) ``DEILE_<KIND>_HOST`` and the same code path serves
        # both forges.
        if forge is not None and github is not None and forge is not github:
            raise ValueError(
                "PipelineMonitor: pass only one of forge=/github= (github= is deprecated)"
            )
        self.forge: ForgeClient = forge or github or build_forge(project_path=config.repo)
        self.forge.on_label_change = lambda kind, num, rem, add: \
            pipeline_logger.log_label_change(target_kind=kind, target=num, removed=rem, added=add)
        # WorktreeManager validates that base_repo_path is a git repo at
        # construction. The deile_worker strategy never creates local
        # worktrees (the worker Pod owns its own clone) and runs where
        # base_repo_path is typically NOT a git repo — so only build the
        # manager for the claude strategy (or when one is injected).
        if worktrees is not None:
            self.worktrees = worktrees
        elif is_claude_mode(config.dispatch_mode):
            self.worktrees = WorktreeManager(
                config.base_repo_path,
                main_branch=config.main_branch,
                subdir=self.identity.worktree_subdir(),
            )
        else:
            self.worktrees = None
        self.claude = claude or ClaudeDispatcher()
        # Strategy that does the implement/review/mention work. When not
        # injected it is selected from ``config.dispatch_mode``: "claude"
        # uses ``self.claude`` + ``self.worktrees``; "deile_worker" dispatches
        # to the worker Pod over HTTP. Stage handlers delegate to it.
        self.implementer = implementer or build_implementer(config.dispatch_mode)
        self.notifier = notifier or DiscordNotifier(config.notify_user_id)
        self._review_cb = review_callback
        self._stats = _Stats()
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._held_lock: Optional[Path] = None
        self._post_merge_cb = post_merge_callback
        self._mention_cursor_path = Path(config.base_repo_path) / "data" / "mention_cursor.txt"
        self._mention_cursor: Optional[datetime] = None
        # Pipeline-side resume bookkeeping (issue #254): cadence timestamps,
        # last substantive fingerprint (progress guard) and attempt/budget as
        # reported by the worker. Instance state (like ``_mention_cursor``),
        # not agent memory — see ``resume_state.py``.
        self._resume_tracker = ResumeTracker()
        # Decisão #46 — backoff exponencial para ``WORKER_AUTH_EXPIRED``.
        # Tracking por target (``pr:N`` / ``issue:N``): contador de falhas
        # consecutivas e timestamp (monotonic) até quando o target está
        # pausado. Mantido como dict simples no monitor (estado de processo,
        # não agent memory) — qualquer restart do pipeline reseta o backoff,
        # que é exatamente o comportamento desejado (operador deve ter
        # arrumado o OAuth se reiniciou o pipeline).
        self._auth_failures_by_target: dict[str, int] = {}
        self._paused_until_ts: dict[str, float] = {}
        # Background tasks detached do tick (issue #445): o caminho de RESUME
        # de review roda aqui sem congelar o loop do monitor. ``_resume_in_flight``
        # guarda os números de PR cujo resume já está rodando (impede re-dispatch
        # concorrente da mesma PR num tick subsequente).
        self._bg_tasks: set = set()
        self._resume_in_flight: set = set()
        # Anti-loop (issue #418): números de issues promovidas a ``revisada`` no
        # TICK corrente (por convergência de refino OU crítica CLARO). O índice
        # de labels do GitHub tem eventual consistency, então uma issue promovida
        # neste tick ainda reaparece sob ``refinar``/``em_arquitetura`` na
        # listagem de ``refine_one_issue`` (mesmo tick) — sem este guard o
        # rehydrate a rebaixaria de volta, criando loop infinito. Limpo no início
        # de cada tick; consultado pelo candidate-filter do refino.
        self._refine_promoted_this_tick: set = set()
        # Schedule store — when present, schedule entries drive when each
        # action fires (instead of the fixed poll interval). On startup the
        # monitor first drains any catch-up queue (entries whose run time
        # has already passed), then enters the polling loop where every
        # tick re-checks for due entries. If no schedule file exists, the
        # monitor falls back to legacy "every action every poll" behaviour.
        self.schedule_store = schedule_store or ScheduleStore(
            config.base_repo_path, monitor_id=self.identity.monitor_id
        )

    def spawn_background(self, coro) -> None:
        """Roda *coro* detached (fire-and-forget interno) sem bloquear o tick.

        Mantém referência forte (evita GC) e loga exceção sem derrubar o monitor.
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)

        def _done(t: "asyncio.Task") -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error("background task falhou: %s", exc, exc_info=exc)

        task.add_done_callback(_done)

    # ------------------------------------------------------------------
    # Backwards-compat aliases (issue #297)
    # ------------------------------------------------------------------

    def __getattr__(self, name: str):
        """Resolve ``monitor.github`` to ``monitor.forge`` for legacy callers.

        ``__getattr__`` only fires when the attribute is NOT already set on
        the instance, so this never shadows a real attribute — it just
        catches the deprecated read path. No warning at every read: that
        would spam ``stages.py`` (which already migrated to ``monitor.forge``
        in this commit), but tests that grep the public attribute keep
        working without modification.
        """
        if name == "github":
            return self.forge
        raise AttributeError(name)

    # ------------------------------------------------------------------
    # identity-aware naming helpers
    # ------------------------------------------------------------------

    def branch_for_issue(self, issue_number: int) -> str:
        """Per-monitor branch name for stage 2 implementation."""
        if self.identity.is_default:
            return f"{self.config.branch_prefix}{issue_number}"
        # Per-monitor prefix overrides the legacy config.branch_prefix.
        return f"{self.identity.branch_prefix('auto')}/issue-{issue_number}"

    def _owns_pr_branch(self, head_ref: str, *, pr_number: int = 0) -> bool:
        """Return True if the PR's branch was opened by THIS monitor.

        Used to scope stage 3 to PRs the local monitor implemented. Default
        identity owns any branch starting with ``auto/issue-`` (legacy path).

        When ``config.enable_review_human_prs`` is True, this always
        returns True so stage 3 can review human-opened PRs too.
        """
        if self.config.enable_review_human_prs:
            return True
        if not head_ref:
            # Cross-repo PRs and GitHub API gaps arrive with empty head_ref.
            if pr_number:
                logger.warning(
                    "PR #%d has empty head_ref; skipping (cross-repo PR or GitHub API gap). "
                    "Set enable_review_human_prs=True to override.",
                    pr_number,
                )
            return False
        if self.identity.is_default:
            # Legacy: claim PRs whose branch matches the legacy prefix and has
            # no monitor segment.
            return head_ref.startswith("auto/issue-")
        return head_ref.startswith(f"{self.identity.branch_prefix('auto')}/")

    @property
    def stats(self) -> _Stats:
        return self._stats

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background polling loop.

        PID locking is enabled when:
        - ``config.use_pid_lock`` is explicitly set, **or**
        - the identity is non-default (any named monitor, any sharded deployment).

        The second condition is intentional: a non-default identity implies a
        multi-monitor deployment where two instances with the same ``monitor_id``
        on the same host are a guaranteed-bug — they would race on the same
        worktree sub-directory and schedule file. The lockfile is the last line
        of defence against operator error.
        """
        if self.is_running:
            return
        should_lock = self.config.use_pid_lock or not self.identity.is_default
        if should_lock:
            lock_path = Path(self.config.base_repo_path) / self.identity.lockfile_name()
            try:
                self._held_lock = acquire_lock(lock_path)
            except LockHeldError as exc:
                logger.error(
                    "another monitor with id=%s is already running (PID %d); refusing start",
                    self.identity.monitor_id, exc.holder_pid,
                )
                raise
        await self.forge.ensure_pipeline_labels()
        await self._catch_up_pending()
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_forever(), name=f"pipeline-monitor-{self.identity.monitor_id}"
        )

    async def _catch_up_pending(self) -> None:
        """On startup, drain any schedule entries whose time already passed."""
        # Opportunistic cleanup: remove on-disk worktrees for already-merged PRs.
        if self.config.enable_worktree_cleanup and self.worktrees is not None:
            try:
                merged_prs = await self.forge.list_recently_merged_prs(limit=100)
                merged_branches = [pr.head_ref for pr in merged_prs if pr.head_ref]
                # PR numbers are public metadata (already in the URL) so a
                # bounded sample is safe to log; head_refs leak branch
                # conventions and stay out.
                dropped_pr_numbers = sorted(
                    pr.number for pr in merged_prs if not pr.head_ref
                )
                if dropped_pr_numbers:
                    logger.debug(
                        "cleanup_merged_branches: dropped %d entries with empty "
                        "head_ref (PRs: %s)",
                        len(dropped_pr_numbers),
                        dropped_pr_numbers[:10],
                    )
                deleted = await self.worktrees.cleanup_merged_branches(merged_branches)
                if deleted:
                    logger.info("startup: cleaned up %d merged worktrees", deleted)
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                logger.warning("startup worktree cleanup failed: %s", exc)

        try:
            schedule = self.schedule_store.load()
        except Exception as exc:  # noqa: BLE001 — schedule errors must not block boot
            logger.warning("schedule load failed; skipping catch-up: %s", exc)
            return

        # GC completed oneshots so the YAML doesn't grow indefinitely.
        removed = schedule.gc_completed_oneshots()
        if removed:
            logger.info("startup: gc'd %d completed oneshots from schedule", removed)

        pending = schedule.compute_pending(
            replay_window_hours=self.config.bootstrap_replay_window_hours
        )
        if not pending:
            try:
                self.schedule_store.save(schedule)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not persist schedule after startup gc: %s", exc)
            return
        logger.info(
            "monitor %s catching up on %d missed runs",
            self.identity.monitor_id, len(pending),
        )
        for run in pending:
            await self._run_scheduled(run)
            schedule.mark_run(run)
            self._stats.catchup_runs += 1
        try:
            self.schedule_store.save(schedule)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist schedule after catch-up: %s", exc)

    async def _run_scheduled(self, run: PendingRun) -> None:
        """Execute a single scheduled action by name."""
        action_def = ACTIONS_BY_NAME.get(run.action)
        if action_def is None:
            logger.debug("scheduled action %s unknown; skipped", run.action)
            return

        # ``enable_attr`` is documented in :class:`ActionDef` as "must return
        # True" — use ``is not True`` so ``Optional[bool] = None`` and other
        # falsy-but-not-False values are treated as disabled too.
        if getattr(self.config, action_def.enable_attr) is not True:
            # Operator scheduled this action but disabled it in config — warn loudly.
            logger.warning(
                "scheduled action %r is disabled (%s is not True); "
                "skipping run at %s. Remove the schedule entry or re-enable the flag.",
                run.action, action_def.enable_attr, run.when.isoformat(),
            )
            self._stats.skipped_runs += 1
            return

        await getattr(self, action_def.method)()

    async def stop(self) -> None:
        self._stop_event.set()
        for t in list(self._bg_tasks):
            t.cancel()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=PIPELINE_STOP_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                self._task.cancel()
                # ``CancelledError`` é o resultado esperado de ``cancel()`` —
                # capturado e silenciado intencionalmente (não é uma falha).
                # Outras exceções do tick em curso são logadas (pilar 03 §6 —
                # ``except Exception: pass`` é proibido sem registro).
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001 — logged then suppressed
                    logger.warning("pipeline task raised during stop: %s", exc)
        if self._held_lock is not None:
            release_lock(self._held_lock)
            self._held_lock = None

    async def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                self._stats.errors += 1
                logger.exception("pipeline tick crashed: %s", exc)
                await self.notifier.error("monitor.tick", str(exc))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.config.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    def _this_monitor_owns(self, issue: IssueRef) -> bool:
        """Return True if this monitor should process the given issue."""
        if self.identity.is_default:
            return self.identity.owns(issue.title)
        return self.identity.ownership_label() in issue.labels

    # ------------------------------------------------------------------
    # one tick
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        import time as _time
        tick_started = _time.monotonic()
        self._stats.ticks += 1
        logger.debug("pipeline tick #%d", self._stats.ticks)

        # Issue #347 — publica state pro pipeline_status_server (best-effort).
        # ``_status_state`` é injetado pelo runner.py quando o server sobe.
        # Quando ausente (sem server, sem painel), no-op silencioso.
        _status_state = getattr(self, "_status_state", None)

        # Issue #309 fase 3.5 — reaper FIRST: scaneia ~review:em_andamento /
        # ~workflow:em_implementacao com idade > threshold sem progresso e
        # libera (próximo tick re-claim via resume). Best-effort: erros
        # no reaper NÃO derrubam o tick (catch + log).
        if self.config.reaper_stale_seconds > 0:
            try:
                await stages.reap_orphan_claims(self)
            except Exception as exc:  # noqa: BLE001
                logger.warning("reaper failed (non-fatal): %s", exc)

        # Snapshot stats for per-tick delta computation (issue #349).
        snap = self._stats_snapshot()

        # When a schedule file exists with at least one entry, the schedule
        # is authoritative: each tick runs only the actions whose cron
        # window opened since the previous tick. Without a schedule, fall
        # back to legacy "every action every tick" behaviour.
        #
        # If a stage is enabled in config but missing from the schedule's recurring
        # entries, we still run it legacy-style so an incomplete schedule doesn't
        # silently drop stages. Schedule entries override; gaps fall back to legacy.
        # Only-oneshot schedules are respected as-is (no recurring fallback).
        try:
            schedule = self.schedule_store.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning("schedule load failed on tick; falling back to legacy mode: %s", exc)
            schedule = None

        if schedule and (schedule.recurring or schedule.oneshot):
            pending = schedule.compute_pending()
            for run in pending:
                await self._run_scheduled(run)
                schedule.mark_run(run)
                self._stats.scheduled_runs += 1
            if pending:
                try:
                    self.schedule_store.save(schedule)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("could not persist schedule after tick: %s", exc)

            if schedule.recurring:
                scheduled_actions = {e.action for e in schedule.recurring if e.enabled}
                await self._dispatch_stages(skip=scheduled_actions)
            self._publish_status_state(_status_state, tick_started)
            await self._log_tick_summary(tick_started, snap)
            return

        await self._dispatch_stages()
        self._publish_status_state(_status_state, tick_started)
        await self._log_tick_summary(tick_started, snap)

    def _stats_snapshot(self) -> dict:
        """Return a shallow copy of per-tick-relevant counters for delta
        computation at end-of-tick (issue #349)."""
        s = self._stats
        return {
            "issues_classified": s.issues_classified,
            "prs_classified": s.prs_classified,
            "issues_reviewed": s.issues_reviewed,
            "issues_implemented": s.issues_implemented,
            "prs_reviewed": s.prs_reviewed,
        }

    def _publish_status_state(self, state, tick_started: float) -> None:
        """Best-effort: publica métricas + ledger snapshot pro pipeline_status_server.

        Issue #347. Chamado no fim de cada tick. Silenciosamente no-op quando
        ``state`` é None (sem server rodando).
        """
        if state is None:
            return
        import time as _time
        try:
            elapsed = _time.monotonic() - tick_started
            if hasattr(state, "record_tick"):
                state.record_tick(
                    duration_seconds=elapsed,
                    ticks_total=self._stats.ticks,
                    errors_total=self._stats.errors,
                )
            if hasattr(state, "set_ledger_snapshot"):
                try:
                    impl = getattr(self, "implementer", None)
                    ledger = getattr(impl, "_ledger", None) if impl else None
                    if ledger is not None and hasattr(ledger, "list_all"):
                        state.set_ledger_snapshot(ledger.list_all())
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("publish_status_state non-fatal: %s", exc)

    async def _log_tick_summary(self, tick_started: float, snap: dict) -> None:
        """Log a lightweight INFO summary at the end of each tick (issue #349).

        Computes per-tick deltas from a pre-stage :meth:`_stats_snapshot` so the
        operator can distinguish "idle" from "stuck" even when every stage is a
        no-op.  Backlog counts are best-effort (forge errors are swallowed).
        """
        import time as _time
        elapsed = _time.monotonic() - tick_started
        s = self._stats

        classified_n = (
            (s.issues_classified - snap["issues_classified"])
            + (s.prs_classified - snap["prs_classified"])
        )
        reviewed_n = s.issues_reviewed - snap["issues_reviewed"]
        implemented_n = s.issues_implemented - snap["issues_implemented"]
        dispatched_n = s.prs_reviewed - snap["prs_reviewed"]

        # Best-effort backlog: count issues in the active pipeline queue +
        # open PRs.  `-1` sentinel means "unavailable" (forge error).
        backlog_issues = -1
        backlog_prs = -1
        try:
            active_labels = (
                WORKFLOW_NEW, WORKFLOW_REVIEWED, WORKFLOW_IMPLEMENTING,
            )
            seen: set[int] = set()
            for label in active_labels:
                try:
                    items = await self.forge.list_issues_with_label(label, limit=200)
                    for item in items:
                        seen.add(item.number)
                except Exception as _inner_exc:  # noqa: BLE001
                    logger.debug(
                        "_log_tick_summary: list_issues(%s) failed: %s",
                        label, _inner_exc,
                    )
            backlog_issues = len(seen)
        except Exception as _outer_exc:  # noqa: BLE001
            logger.debug(
                "_log_tick_summary: backlog issues query failed: %s", _outer_exc,
            )
        try:
            prs = await self.forge.list_open_prs(limit=200)
            backlog_prs = len(prs)
        except Exception as _exc:  # noqa: BLE001
            logger.debug("_log_tick_summary: list_open_prs failed: %s", _exc)

        if backlog_issues >= 0 and backlog_prs >= 0:
            logger.info(
                "tick #%d done in %.2fs: classified=%d reviewed=%d "
                "implemented=%d dispatched=%d "
                "backlog={issues:%d prs:%d}",
                s.ticks, elapsed, classified_n, reviewed_n,
                implemented_n, dispatched_n, backlog_issues, backlog_prs,
            )
        else:
            logger.info(
                "tick #%d done in %.2fs: classified=%d reviewed=%d "
                "implemented=%d dispatched=%d backlog=unavailable",
                s.ticks, elapsed, classified_n, reviewed_n,
                implemented_n, dispatched_n,
            )

    async def _dispatch_stages(self, skip: set[str] | None = None) -> None:
        """Run the per-tick stage sequence.

        Stages whose key is in ``skip`` are bypassed because the scheduler
        has already run them in this tick. Stages without a schedule key
        (refinement_gate, resume, decompose, pr_triage, mention_handling)
        always run when their feature flag is enabled.

        ``skip=None`` is the legacy "every action every tick" mode used
        when there is no schedule file with recurring entries.
        """
        skip = skip or set()
        scheduled_mode = bool(skip)
        cfg = self.config
        # Anti-loop (issue #418): zera o set de "promovidas a revisada neste tick"
        # no começo do tick, antes do reconcile que o preenche e do refine que o lê.
        self._refine_promoted_this_tick.clear()

        async def _scheduled(enabled: bool, key: str, handler) -> None:
            """Run a schedulable stage unless the scheduler already covered it."""
            if not enabled or key in skip:
                return
            if scheduled_mode:
                logger.debug("%s not in schedule; running legacy fallback", key)
            await handler()

        # Issue #373: a crítica é fire-and-forget — reconcilia o veredito das
        # críticas em voo ANTES de despachar novas (libera capacidade no mesmo
        # tick, espelha o reconcile do implement).
        if cfg.enable_refinement_gate:
            await self._reconcile_critique_issues()
        await _scheduled(cfg.enable_classify, "classify", self._classify_new_issues)
        await _scheduled(cfg.enable_review, "review", self._review_one_new_issue)
        # Refinement loop (issue #257/#373): reconcilia o veredito dos refinos em
        # voo ANTES de despachar novos; o dispatch é fire-and-forget.
        if cfg.enable_refinement_gate:
            await self._reconcile_refine_issues()
            await self._refine_one_issue()
        # Resume parked, continuable work BEFORE claiming new issues
        # (issue #254) so a freshly-claimed issue is not re-dispatched in
        # the same tick; its first resume lands on the next tick.
        if cfg.enable_resume:
            await self._resume_in_progress_issues()
        # Issue #373: reconcile fire-and-forget implementing issues BEFORE
        # claiming new ones — completed issues free up capacity for new
        # dispatches in the same tick.
        if cfg.enable_implement:
            await self._reconcile_implementing_issues()
        # PR #380 follow-up (non-blocking review suggestion): fetch the
        # ``~workflow:revisada`` snapshot ONCE and ensure ownership ONCE, then
        # share it with both the implement and decompose stages — halving the
        # per-tick reviewed-list forge calls (each stage used to fetch + ensure
        # independently). The two stages target disjoint issue types (implement
        # → non-intent, decompose → intent), so a shared snapshot is safe.
        #
        # Two views preserve the exact prior pickup timing: implement gets the
        # PRE-ensure snapshot (it always filtered its own un-ensured fetch, so an
        # orphan code issue is adopted next tick); decompose gets the POST-ensure
        # snapshot (it used to re-fetch a fresh one carrying the label, so an
        # orphan intent is decomposed the same tick). On a forge error both are
        # None and each stage falls back to its own self-contained fetch.
        # ``implement`` only consumes the shared snapshot in legacy (non-schedule)
        # mode; when run via the scheduler it is invoked by name with no args and
        # fetches its own (unchanged).
        reviewed_pre = reviewed_post = None
        if (cfg.enable_implement and "implement" not in skip) or cfg.enable_refinement_gate:
            reviewed_pre, reviewed_post = await stages.fetch_reviewed_and_ensure_ownership(self)
        await _scheduled(
            cfg.enable_implement, "implement",
            lambda: self._implement_one_reviewed_issue(reviewed_pre),
        )
        # Decompose CLEAR intents into derived issues (issue #257).
        if cfg.enable_refinement_gate:
            await self._decompose_one_reviewed_intent(reviewed_post)
        # Issue #373: review fresh é fire-and-forget — reconcilia o veredito das
        # reviews em voo (por ground-truth: PR merged?) ANTES de despachar novas.
        if cfg.enable_pr_review and "pr_review" not in skip:
            await self._reconcile_review_prs()
        await _scheduled(cfg.enable_pr_review, "pr_review", self._review_one_open_pr)
        if cfg.enable_pr_triage:
            await self._classify_new_prs()
        if cfg.enable_mention_handling:
            await self._process_mentions()

    # ------------------------------------------------------------------
    # stage handlers — thin delegators to ``stages.py``
    #
    # The seven stage handlers below were extracted to ``stages.py`` as free
    # ``async def`` functions taking the monitor as first argument. The
    # methods here remain as thin delegators so existing tests and callers
    # that invoke them via the instance keep working unchanged.
    # ------------------------------------------------------------------

    async def _classify_new_issues(self) -> None:
        return await stages.classify_new_issues(self)

    async def _classify_new_prs(self) -> None:
        return await stages.classify_new_prs(self)

    async def _process_mentions(self) -> None:
        return await stages.process_mentions(self)

    async def _review_one_new_issue(self) -> None:
        return await stages.review_one_new_issue(self)

    async def _refine_one_issue(self) -> None:
        return await stages.refine_one_issue(self)

    async def _decompose_one_reviewed_intent(self, issues=None) -> None:
        return await stages.decompose_one_reviewed_intent(self, issues)

    async def _implement_one_reviewed_issue(self, issues=None) -> None:
        return await stages.implement_one_reviewed_issue(self, issues)

    async def _resume_in_progress_issues(self) -> None:
        return await stages.resume_in_progress_issues(self)

    async def _reconcile_implementing_issues(self) -> None:
        """Check ground truth for fire-and-forget implementing issues (issue #373)."""
        return await stages.reconcile_implementing_issues(self)

    async def _reconcile_critique_issues(self) -> None:
        """Process the verdict of fire-and-forget critiques (issue #373)."""
        return await stages.reconcile_critique_issues(self)

    async def _reconcile_refine_issues(self) -> None:
        """Process the verdict of fire-and-forget refines (issue #373)."""
        return await stages.reconcile_refine_issues(self)

    async def _reconcile_review_prs(self) -> None:
        """Process the verdict of fire-and-forget PR reviews (issue #373)."""
        return await stages.reconcile_review_prs(self)

    async def _review_one_open_pr(self) -> None:
        return await stages.review_one_open_pr(self)

    async def _stage4_follow_ups(self, pr_number: int, pr_title: str, pr_url: str) -> None:
        return await stages.stage4_follow_ups(self, pr_number, pr_title, pr_url)

    async def _standalone_follow_ups(self) -> None:
        return await stages.standalone_follow_ups(self)

    # ----- mention handling: cursor helpers ----------------------------------

    def _load_mention_cursor(self) -> datetime:
        try:
            if self._mention_cursor is not None:
                return self._mention_cursor
            if self._mention_cursor_path.exists():
                raw = self._mention_cursor_path.read_text().strip()
                return parse_iso_utc(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mention cursor load failed; using 30-min lookback: %s", exc)
        return now_utc().replace(second=0, microsecond=0) - timedelta(minutes=30)

    def _save_mention_cursor(self, ts: datetime) -> None:
        try:
            self._mention_cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self._mention_cursor_path.write_text(ts.isoformat())
            self._mention_cursor = ts
        except Exception as exc:  # noqa: BLE001
            logger.warning("mention cursor save failed: %s", exc)
