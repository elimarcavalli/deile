"""dispatch_deile_task — bot-side tool that delegates work to the deile-worker Pod.

The Discord bot's embedded agent has only `messaging.*` tools enabled
(by design — Discord input is untrusted). When the user asks for code
work ("create a fib.py with cache", "fix the bug in foo.py"), the bot
calls THIS tool instead of trying to do the work itself.

The tool POSTs to the deile-worker control plane, which:
  1. posts a stub status message in the user's channel,
  2. reacts on the user's message with 🔧,
  3. runs DEILE in-process inside an isolated workspace,
  4. edits the status message live with progress,
  5. edits a final summary + reacts ✅/❌.

The bot's LLM only receives a tiny summary back so it doesn't have to
re-narrate everything — the user already sees the rich status message.

Anti-loop guard
---------------
The LLM sometimes retries ``dispatch_deile_task`` 2-3x when the first
result looks "empty" or "wrong" (e.g. worker missing ``ping``), causing
duplicate workers to spawn for the same user message. This module
maintains a class-level cache keyed by ``channel_id`` with a 30s
cooldown: any 2nd attempt within that window returns an idempotency
error to the LLM with a clear message. The LLM then reports the error
to the user instead of looping. Cooldown is short enough that genuinely
new requests on the same channel resume normally.

The cooldown is recorded ONLY when we are about to actually issue the
HTTP request — pre-network validation failures (missing brief, missing
channel_id, payload validation, missing token, missing httpx) do NOT
consume the cooldown slot, so the LLM can retry with corrected input.

Transport layer
---------------
HTTP transport, endpoint resolution, secret-file reads, bearer-token
sanitization, payload assembly (``build_dispatch_payload``) and the
LLM-facing summary (``summarize_dispatch_response``) all live in
:mod:`deile.infrastructure.deile_worker_client` (hexagonal — pilar
03 §2). This module owns only the bot-facing LLM tool surface plus the
anti-loop guard; wire-format and response shaping are delegated to the
adapter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Dict, Optional

from deile.infrastructure.deile_worker_client import (
    MAX_DISPATCH_BUDGET_S, DeileWorkerClient, WorkerDispatchError,
    build_dispatch_payload, summarize_dispatch_response,
    validate_dispatch_payload)

from ._dispatch_cooldown import is_in_cooldown, prune_expired, record_dispatch
from .base import (SecurityLevel, Tool, ToolCategory, ToolContext, ToolResult,
                   ToolSchema)

logger = logging.getLogger(__name__)


def _bot_context(context: ToolContext) -> Dict[str, object]:
    """Return the ``bot_context`` dict from session data (``{}`` when absent)."""
    return context.session_data.get("bot_context") or {}


# Pre-network errors that roll back the cooldown — no HTTP call was issued,
# so the channel slot is freed for the user to retry after fixing input.
_ROLLBACK_ERROR_CODES = frozenset(
    {
        "WORKER_AUTH_MISSING",
        "WORKER_AUTH_MALFORMED",
        "WORKER_TRANSPORT_MISSING",
        "BAD_REQUEST",
    }
)


class DispatchDeileTaskTool(Tool):
    """Delegate a code task to a deile-worker Pod and stream UX to Discord."""

    # Class-level cooldown registry — keyed by channel_id, value is the
    # monotonic timestamp of the LAST dispatch. Used to block the LLM
    # from hammering the worker when the first attempt comes back empty
    # or with an error it's tempted to "retry".
    _LAST_DISPATCH: Dict[str, float] = {}
    _DISPATCH_COOLDOWN_S = 30.0
    # Periodic cleanup: entries older than 5×COOLDOWN are dropped on
    # next dispatch attempt. Bounds memory under sustained traffic
    # without needing a background task.
    _CLEANUP_FACTOR = 5
    # Per-channel lock so the cooldown check + the cooldown write are
    # atomic — two coroutines on the same ``channel_id`` cannot both
    # observe ``last=None`` and both spawn a worker. Distinct channels
    # never contend. ``asyncio.Lock`` binds to the running loop on first
    # acquire, so the defaultdict materialising locks eagerly is safe.
    _CHANNEL_LOCKS: "Dict[str, asyncio.Lock]" = defaultdict(asyncio.Lock)

    @property
    def name(self) -> str:
        return "dispatch_deile_task"

    @property
    def description(self) -> str:
        return (
            "Delegate a real coding task to the isolated deile-worker pod. "
            "Use whenever the user's request requires creating/editing files, "
            "running shell/Python, installing packages, running tests, "
            "exploring code, or any actual development work. "
            "The worker has its own filesystem, full toolset, and runs in a sandbox; "
            "it posts a live status message in the channel and edits it with progress. "
            "You only get back a tiny summary — do NOT re-narrate, the user already saw "
            "the live progress. Just confirm with one short line."
        )

    @property
    def category(self) -> str:
        return ToolCategory.OTHER.value

    def __init__(
        self, worker_client: Optional[DeileWorkerClient] = None
    ) -> None:
        # Constructor injection: tests pass a stub client; production
        # falls back to the default stateless adapter.
        self._worker_client = worker_client or DeileWorkerClient()
        super().__init__(
            schema=ToolSchema(
                name=self.name,
                description=self.description,
                parameters={
                    "type": "object",
                    "properties": {
                        "brief": {
                            "type": "string",
                            "description": (
                                "Verbatim or lightly-rephrased description of what the "
                                "user wants done. Pass it as PT-BR / EN as the user "
                                "wrote it. Max ~4000 chars."
                            ),
                        },
                        "channel_id": {
                            "type": "string",
                            "description": (
                                "Discord channel_id from bot_context. The worker posts "
                                "a live status message in this channel."
                            ),
                        },
                        "user_message_id": {
                            "type": "string",
                            "description": (
                                "Discord message_id of the user's prompt. ALWAYS pass "
                                "bot_context.user_message_id here — it's always present "
                                "in DM/group/thread inbound. The worker reacts 🔧/✅ on it."
                            ),
                        },
                        "persona": {
                            "type": "string",
                            "description": (
                                "Optional persona for the worker DEILE "
                                "(default: 'developer'). Choose 'architect' for design-"
                                "heavy work, 'debugger' for bug hunting, 'developer' "
                                "for normal coding."
                            ),
                        },
                        "wait_for_result": {
                            "type": "boolean",
                            "description": (
                                "When true (default), block until the worker finishes "
                                "(timeout ~10min). When false, returns immediately with "
                                "the task_id so the LLM can keep talking; UX continues "
                                "via the worker editing the status message in background."
                            ),
                        },
                    },
                },
                required=["brief", "channel_id"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.OTHER,
                max_execution_time=int(MAX_DISPATCH_BUDGET_S),
            )
        )

    @classmethod
    def _prune_expired_dispatch_entries(cls, now: float) -> None:
        """Drop ``_LAST_DISPATCH`` entries older than ``COOLDOWN × FACTOR``,
        and the matching ``_CHANNEL_LOCKS`` entries when those locks are
        not currently held — bounds memory on both class-level dicts
        without racing against a coroutine that owns its lock.
        """
        cutoff = cls._DISPATCH_COOLDOWN_S * cls._CLEANUP_FACTOR
        prune_expired(cls._LAST_DISPATCH, cutoff, now)
        # Drop locks for channels no longer tracked AND not currently
        # held. The ``lock.locked()`` check is essential: another
        # coroutine may still own its lock while we prune.
        orphan_locks = [
            cid
            for cid, lock in cls._CHANNEL_LOCKS.items()
            if cid not in cls._LAST_DISPATCH and not lock.locked()
        ]
        for cid in orphan_locks:
            cls._CHANNEL_LOCKS.pop(cid, None)

    async def execute(self, context: ToolContext) -> ToolResult:
        # TODO(deferred — decisão #29): this tool bridges untrusted Discord
        # input → privileged remote execution (full DEILE toolset in an
        # isolated worker) but currently has no ``PermissionManager`` gate
        # and emits no ``AuditEvent``. See
        # ``docs/system_design/DECISOES.md`` (Decisão #29) for the full
        # rationale: the gate requires a new resource-string convention,
        # a corresponding ``config/permissions.yaml`` rule, and pillar 08
        # expansion — each a separate design decision, deferred to keep
        # PR #233 scoped to the hexagonal transport extraction. Follow-up
        # issue must cover the ``dispatch:<channel_id>`` permission rule
        # plus three audit emissions (pending / success / failed with
        # SHA8(brief), channel_id, user_message_id, persona, task_id,
        # error_code). Compensating controls in the meantime: bot
        # embedded-agent whitelist (decisão #28), NetworkPolicy
        # default-deny (decisão #27), and the 30s cooldown below.
        try:
            args = dict(context.parsed_args or {})
            bot_ctx = _bot_context(context)
            brief = str(args.get("brief", "")).strip()
            channel_id = str(args.get("channel_id", "")).strip()
            # Auto-fill from bot_context if the LLM forgot — this enables
            # the worker's 🔧/✅ reaction UX without depending on persona
            # discipline. ``build_dispatch_payload`` ``str()``-ifies and
            # drops falsy values, so a single ``or`` covers both fallbacks.
            user_message_id = (
                args.get("user_message_id") or bot_ctx.get("user_message_id")
            )
            persona = args.get("persona") or "developer"
            wait = bool(args.get("wait_for_result", True))

            if not brief:
                return ToolResult.error_result(
                    "brief is required", error_code="BAD_REQUEST"
                )
            if not channel_id:
                # Fall back to bot_context if the LLM forgot.
                channel_id = str(bot_ctx.get("channel_id") or "").strip()
                if not channel_id:
                    return ToolResult.error_result(
                        "channel_id is required (and not in bot_context)",
                        error_code="BAD_REQUEST",
                    )

            payload = build_dispatch_payload(
                brief=brief,
                channel_id=channel_id,
                persona=persona,
                wait=wait,
                user_message_id=user_message_id,
                attachments=bot_ctx.get("attachments"),
                # Forward recent channel history so the worker can resolve
                # follow-ups ("agora adiciona um teste pra aquele arquivo").
                # The ingress pipeline injects bot_context.recent_history on
                # the bot-mediated path; the /deile passthrough builds its own
                # ToolContext without it, keeping that path one-shot.
                history=bot_ctx.get("recent_history"),
            )

            # Validate payload BEFORE recording the cooldown — a payload
            # rejection is a programming error, not a worker invocation,
            # and should not consume the channel's cooldown slot. The
            # validation rule lives in the adapter (single source of truth);
            # here we only translate its WorkerDispatchError into a ToolResult.
            try:
                validate_dispatch_payload(payload)
            except WorkerDispatchError as exc:
                return ToolResult.error_result(
                    exc.message, error=exc, error_code=exc.error_code
                )

            # Anti-loop guard: refuse a 2nd dispatch within COOLDOWN_S on
            # the same channel. Worker spawning is expensive AND the user
            # sees duplicate status messages — both bad UX. The per-channel
            # lock makes the check+write atomic: two coroutines arriving on
            # the same channel before the first finishes cannot both observe
            # ``last=None`` and both spawn a worker.
            async with self._CHANNEL_LOCKS[channel_id]:
                now = time.monotonic()
                self._prune_expired_dispatch_entries(now)
                if is_in_cooldown(
                    self._LAST_DISPATCH, channel_id,
                    self._DISPATCH_COOLDOWN_S, now,
                ):
                    last = self._LAST_DISPATCH[channel_id]
                    remaining = self._DISPATCH_COOLDOWN_S - (now - last)
                    return ToolResult.error_result(
                        f"dispatch já feito há {now - last:.0f}s nesse canal; "
                        f"aguarde {remaining:.0f}s e relate ao usuário em vez de retentar. "
                        f"Se a 1ª chamada falhou (ex: 'ping' não existe no worker), "
                        f"explique isso ao usuário — NÃO chame dispatch_deile_task de novo "
                        f"esperando resultado diferente.",
                        error_code="DISPATCH_COOLDOWN",
                    )
                # Record BEFORE the HTTP call (still inside the lock) so any
                # concurrent retry observes the timestamp. If the client
                # later raises a pre-network failure (auth/transport
                # missing), the timestamp is ROLLED BACK below.
                record_dispatch(self._LAST_DISPATCH, channel_id, now)

            try:
                data = await self._worker_client.dispatch(payload, wait=wait)
            except WorkerDispatchError as exc:
                if exc.error_code in _ROLLBACK_ERROR_CODES:
                    # Roll back: no HTTP request was ever issued.
                    self._LAST_DISPATCH.pop(channel_id, None)
                return ToolResult.error_result(
                    exc.message, error=exc, error_code=exc.error_code
                )

            short_summary = summarize_dispatch_response(data)

            return ToolResult.success_result(
                data={
                    "task_id": data.get("task_id"),
                    "ok": data.get("ok"),
                    "elapsed_s": data.get("elapsed_s"),
                    "files": data.get("files", []),
                    "summary_for_llm": short_summary,
                },
                message=short_summary or "dispatch ok",
            )
        except Exception as exc:  # noqa: BLE001 — top-level guard required by Tool contract
            logger.exception("dispatch_deile_task failed unexpectedly")
            return ToolResult.error_result(
                f"unexpected error: {exc}", error=exc, error_code="INTERNAL_ERROR"
            )
