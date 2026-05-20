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
The LLM sometimes retries `dispatch_deile_task` 2-3x when the first
result looks "empty" or "wrong" (e.g. worker missing `ping`), causing
duplicate workers to spawn for the same user message. This module
maintains a class-level cache keyed by ``channel_id`` with a 30s
cooldown: any 2nd attempt within that window returns an idempotency
error to the LLM with a clear message. The LLM then reports the error
to the user instead of looping. Cooldown is short enough that genuinely
new requests on the same channel resume normally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from .base import (SecurityLevel, Tool, ToolCategory, ToolContext, ToolResult,
                   ToolSchema)

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_S = 600.0
_DISPATCH_PATH = "/v1/dispatch"
_ALLOWED_ENDPOINT_SCHEMES = ("http", "https")
_PERSONA_ALLOWLIST = ("developer", "architect", "debugger")
# RFC 6750: token68 = 1*( ALPHA / DIGIT / "-" / "." / "_" / "~" / "+" / "/" )
# followed by optional "=" padding. We keep the validation strict: anything
# outside this set (notably CR, LF, NUL, space) would let a malicious value
# inject extra headers into the Authorization line ("header smuggling").
_BEARER_TOKEN_RE = re.compile(r"^[A-Za-z0-9\-._~+/]+=*$")


class _DispatchConfigError(ValueError):
    """Raised when a dispatch-time config value (endpoint/token) is invalid."""


def _worker_endpoint() -> str:
    """Return the worker control-plane endpoint URL.

    Validates the scheme against an explicit allowlist (``http``/``https``).
    Schemes like ``file://`` or ``gopher://`` may load local resources or
    exhibit version-dependent behavior under httpx; rejecting them at config
    time prevents the worker from being aimed at a non-HTTP target.
    """
    raw = os.environ.get(
        "DEILE_WORKER_ENDPOINT",
        "http://deile-worker.deile.svc.cluster.local:8766",
    )
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in _ALLOWED_ENDPOINT_SCHEMES:
        raise _DispatchConfigError(
            f"DEILE_WORKER_ENDPOINT scheme {parsed.scheme!r} is not allowed "
            f"(allowed: {_ALLOWED_ENDPOINT_SCHEMES})"
        )
    if not parsed.netloc:
        raise _DispatchConfigError(
            "DEILE_WORKER_ENDPOINT is missing host (netloc)"
        )
    return raw


def _validate_bearer_token(value: str) -> str:
    """Reject control chars (CR/LF/NUL/space/...) in the bearer token.

    Embedding ``\\r\\n`` in the token would let an attacker append fake HTTP
    headers to the ``Authorization`` line (header smuggling). We enforce the
    RFC 6750 ``token68`` grammar — anything outside that set is rejected.
    """
    if not _BEARER_TOKEN_RE.match(value):
        raise _DispatchConfigError(
            "worker bearer token contains characters outside RFC 6750 token68 "
            "(possible CR/LF/NUL — refusing to send to avoid header smuggling)"
        )
    return value


def _worker_token() -> str:
    """Read the worker bearer token. Tolerant of both bot and worker layouts.

    Order of resolution:
      1. env var DEILE_WORKER_BEARER_TOKEN (set by wrapper before bootstrap)
      2. file /run/secrets/bot/worker/AUTH_TOKEN  (bot pod, real K8s mount)
      3. file /run/secrets/worker/AUTH_TOKEN      (worker pod itself)
      4. file /run/secrets/bot/WORKER_BEARER_TOKEN (legacy fallback)

    Whichever source is used, the resolved value is validated against
    :func:`_validate_bearer_token` before being returned.
    """
    val = os.environ.get("DEILE_WORKER_BEARER_TOKEN", "").strip()
    if val:
        return _validate_bearer_token(val)
    for path in (
        "/run/secrets/bot/worker/AUTH_TOKEN",
        "/run/secrets/worker/AUTH_TOKEN",
        "/run/secrets/bot/WORKER_BEARER_TOKEN",
    ):
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return _validate_bearer_token(v)
        except OSError:
            continue
    return ""


def _bot_context(context: ToolContext) -> Dict[str, object]:
    """Return the ``bot_context`` dict from session data (``{}`` when absent)."""
    return context.session_data.get("bot_context") or {}


def _build_dispatch_payload(
    *,
    brief: str,
    channel_id: str,
    persona: str,
    wait: bool,
    user_message_id: object,
    context: ToolContext,
) -> Dict[str, object]:
    """Assemble the JSON body POSTed to the worker control plane.

    Attachments are forwarded from ``bot_context`` so the worker can call
    vision tools without re-downloading from the (expiring) Discord CDN.
    """
    payload: Dict[str, object] = {
        "brief": brief,
        "channel_id": channel_id,
        "persona": persona,
        "wait_for_result": wait,
    }
    if user_message_id:
        payload["user_message_id"] = str(user_message_id)
    atts = _bot_context(context).get("attachments")
    if atts:
        payload["attachments"] = atts
    return payload


async def _post_dispatch(
    *,
    endpoint: str,
    payload: Dict[str, object],
    token: str,
    wait: bool,
) -> Tuple[Optional[dict], Optional[ToolResult]]:
    """POST the dispatch payload to the worker control plane.

    Returns ``(data, None)`` on a successful response, or
    ``(None, error_result)`` on any transport, decoding or HTTP-status
    failure. ``token`` is a secret — it is never logged or echoed.
    """
    try:
        import httpx
    except ImportError:
        return None, ToolResult.error_result(
            "httpx is not installed in this image", error_code="INTERNAL_ERROR"
        )

    timeout = _DEFAULT_TIMEOUT_S + 60 if wait else 30
    async with httpx.AsyncClient(timeout=timeout) as cli:
        try:
            resp = await cli.post(
                endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException as exc:
            return None, ToolResult.error_result(
                f"worker timeout after {timeout}s", error=exc,
                error_code="WORKER_TIMEOUT",
            )
        except httpx.HTTPError as exc:
            return None, ToolResult.error_result(
                f"worker unreachable: {type(exc).__name__}", error=exc,
                error_code="WORKER_UNREACHABLE",
            )

    try:
        data = resp.json()
    except json.JSONDecodeError:
        return None, ToolResult.error_result(
            f"worker returned non-JSON (status={resp.status_code})",
            error_code="WORKER_BAD_RESPONSE",
        )

    if resp.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else {}
        code = (err or {}).get("code") or "WORKER_ERROR"
        msg = (err or {}).get("message") or f"HTTP {resp.status_code}"
        return None, ToolResult.error_result(msg, error_code=code)

    return data, None


def _summarize_worker_response(data: object) -> str:
    """Build the compact one-line summary handed back to the bot LLM.

    The user already sees the rich status message edited live by the
    worker, so this stays terse on purpose — do NOT echo the full output.
    """
    if not isinstance(data, dict):
        return ""
    if data.get("ok") is True:
        files = data.get("files") or []
        elapsed = data.get("elapsed_s") or 0
        return (
            f"worker concluiu em {float(elapsed):.1f}s — "
            f"{len(files)} arquivo(s): " + ", ".join(files[:5])
        )
    if data.get("ok") is False:
        return (
            f"worker FALHOU: {str(data.get('summary') or data.get('error'))[:300]}"
        )
    return (
        f"worker dispatch aceito (task_id={data.get('task_id')}); "
        "use wait_for_result=true para acompanhar."
    )


class DispatchDeileTaskTool(Tool):
    """Delegate a code task to a deile-worker Pod and stream UX to Discord."""

    # Class-level cooldown registry — keyed by channel_id, value is the
    # monotonic timestamp of the LAST dispatch. Used to block the LLM
    # from rajaring the worker when the first attempt comes back vazio
    # or with an error it's tempted to "retry".
    _LAST_DISPATCH: Dict[str, float] = {}
    _DISPATCH_COOLDOWN_S = 30.0

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

    def __init__(self) -> None:
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
                            "enum": list(_PERSONA_ALLOWLIST),
                            "default": "developer",
                            "description": (
                                "Optional persona for the worker DEILE "
                                "(default: 'developer'). Choose 'architect' for design-"
                                "heavy work, 'debugger' for bug hunting, 'developer' "
                                "for normal coding."
                            ),
                        },
                        "wait_for_result": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "When true, block until the worker finishes "
                                "(timeout ~10min). When false (default), returns "
                                "immediately with the task_id so the LLM can keep "
                                "talking; UX continues via the worker editing the "
                                "status message in background."
                            ),
                        },
                    },
                },
                required=["brief", "channel_id"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.OTHER,
                max_execution_time=int(_DEFAULT_TIMEOUT_S) + 30,
            )
        )

    async def execute(self, context: ToolContext) -> ToolResult:
        try:
            args = dict(context.parsed_args or {})
            brief = str(args.get("brief", "")).strip()
            channel_id = str(args.get("channel_id", "")).strip()
            # Auto-fill from bot_context if the LLM forgot — this enables
            # the worker's 🔧/✅ reaction UX without depending on persona
            # discipline. ``_build_dispatch_payload`` ``str()``-ifies and
            # drops falsy values, so a single ``or`` covers both fallbacks.
            user_message_id = (
                args.get("user_message_id")
                or _bot_context(context).get("user_message_id")
            )
            persona = args.get("persona") or "developer"
            # Defense-in-depth: JSON-Schema enum already blocks values from the
            # LLM, but this runtime check covers internal callers that bypass
            # schema validation (e.g. programmatic invocations of execute()).
            if persona not in _PERSONA_ALLOWLIST:
                return ToolResult.error_result(
                    f"persona {persona!r} is not allowed "
                    f"(allowed: {_PERSONA_ALLOWLIST})",
                    error_code="BAD_REQUEST",
                )
            wait = bool(args.get("wait_for_result", False))

            if not brief:
                return ToolResult.error_result(
                    "brief is required", error_code="BAD_REQUEST"
                )
            if not channel_id:
                # Fall back to bot_context if the LLM forgot.
                channel_id = str(_bot_context(context).get("channel_id") or "").strip()
                if not channel_id:
                    return ToolResult.error_result(
                        "channel_id is required (and not in bot_context)",
                        error_code="BAD_REQUEST",
                    )

            # Anti-loop guard: refuse a 2nd dispatch within COOLDOWN_S on
            # the same channel. Worker spawning is expensive AND the user
            # sees duplicate status messages — both bad UX. The check
            # happens HERE (before config validation) so a recent dispatch
            # short-circuits without touching env vars/secret files; the
            # RECORD happens after config is validated so an invalid
            # endpoint/token doesn't burn the cooldown on the channel.
            now = time.monotonic()
            last = self._LAST_DISPATCH.get(channel_id)
            elapsed = (now - last) if last is not None else None
            if elapsed is not None and elapsed < self._DISPATCH_COOLDOWN_S:
                remaining = self._DISPATCH_COOLDOWN_S - elapsed
                return ToolResult.error_result(
                    f"dispatch já feito há {elapsed:.0f}s nesse canal; "
                    f"aguarde {remaining:.0f}s e relate ao usuário em vez de retentar. "
                    f"Se a 1ª chamada falhou (ex: 'ping' não existe no worker), "
                    f"explique isso ao usuário — NÃO chame dispatch_deile_task de novo "
                    f"esperando resultado diferente.",
                    error_code="DISPATCH_COOLDOWN",
                )

            endpoint = _worker_endpoint().rstrip("/") + _DISPATCH_PATH
            # _worker_token() may read secret files from disk — keep that
            # blocking I/O off the event loop. `token` is a secret: it must
            # never be interpolated into log or error messages.
            token = await asyncio.to_thread(_worker_token)
            if not token:
                return ToolResult.error_result(
                    "WORKER_BEARER_TOKEN not configured in this Pod",
                    error_code="WORKER_AUTH_MISSING",
                )
            # Record AFTER all config validation passed (endpoint + token +
            # persona). If validation throws/returns above, the cooldown is
            # NOT burned — corrupted config wouldn't otherwise block the
            # channel for 30s without a real dispatch attempt.
            self._LAST_DISPATCH[channel_id] = now

            payload = _build_dispatch_payload(
                brief=brief,
                channel_id=channel_id,
                persona=persona,
                wait=wait,
                user_message_id=user_message_id,
                context=context,
            )

            data, error = await _post_dispatch(
                endpoint=endpoint, payload=payload, token=token, wait=wait,
            )
            if error is not None:
                return error

            short_summary = _summarize_worker_response(data)

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
        except _DispatchConfigError as exc:
            # Config-time validation failure (invalid endpoint scheme/netloc,
            # or bearer token outside RFC 6750 token68). Distinct from
            # WORKER_AUTH_MISSING (token absent) — here the value is PRESENT
            # but rejected by validation. Don't leak the raw value: ``exc``
            # carries a sanitized message that names the symptom, not the
            # token.
            logger.warning("dispatch_deile_task config rejected: %s", exc)
            return ToolResult.error_result(
                f"worker config invalid: {exc}",
                error=exc,
                error_code="WORKER_CONFIG_INVALID",
            )
        except Exception as exc:  # noqa: BLE001 — top-level guard required by Tool contract
            logger.exception("dispatch_deile_task failed unexpectedly")
            return ToolResult.error_result(
                f"unexpected error: {exc}", error=exc, error_code="INTERNAL_ERROR"
            )
