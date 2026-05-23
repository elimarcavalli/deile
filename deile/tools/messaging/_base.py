"""MessagingTool — common base for all `messaging.discord_*` tools.

Each subclass declares:
- `tool_name`: short identifier (e.g. `discord_send_message`)
- `description`: human-readable summary used by the LLM
- `security_level`: SecurityLevel.HIGH triggers ApprovalSystem; MODERATE skips it
- `parameters` / `required`: JSON-schema for the function call
- `_perform(facade, args)`: async impl that calls the BotClientFacade

The base class:
1. Guards against missing/disabled integration with a typed
   ToolResult.error_result(code="BOT_INTEGRATION_DISABLED").
2. Calls `PermissionManager.check_permission(...)` and returns
   PERMISSION_DENIED on refusal.
3. For HIGH-risk ops, requests approval via ApprovalSystem; returns
   APPROVAL_REQUIRED on auto-deny / timeout, APPROVAL_GRANTED-flow on success.
4. Wraps `_perform()` in a try/except that maps BotClientError subclasses
   to typed ToolResult error codes (BOT_AUTH, BOT_TIMEOUT, BOT_RATE_LIMITED,
   BOT_NOT_READY, BOT_UPSTREAM, BOT_UNREACHABLE).
5. Emits AuditEvent(TOOL_EXECUTION) for every invocation, with text
   redacted to a SHA256 hash (never the raw text).
"""

from __future__ import annotations

import abc
import logging
from typing import Any, Dict, List, Optional

from ...integrations.bot import (BOT_CLIENT_AVAILABLE, BotClientFacade,
                                 get_bot_client)
from ...security.audit_logger import (AuditEventType, SeverityLevel,
                                      get_audit_logger)
from .._hash_utils import sha8 as _sha8
from ..base import (SecurityLevel, Tool, ToolCategory, ToolContext, ToolResult,
                    ToolSchema)

logger = logging.getLogger(__name__)


def _resolve_facade(context: ToolContext) -> BotClientFacade:
    """Pull facade from session_data when injected by tests, else use the singleton."""
    candidate = context.session_data.get("bot_client_facade") if context else None
    if candidate is not None:
        return candidate
    return get_bot_client()


def _resolve_permission_manager(context: ToolContext):
    return context.session_data.get("permission_manager")


def _resolve_approval_system(context: ToolContext):
    explicit = context.session_data.get("approval_system")
    if explicit is not None:
        return explicit
    try:
        from ...orchestration.approval_system import get_approval_system

        return get_approval_system()
    except Exception:  # pragma: no cover
        return None


def _resolve_audit_logger(context: ToolContext):
    explicit = context.session_data.get("audit_logger")
    if explicit is not None:
        return explicit
    return get_audit_logger()


def _trusted_operator_mode() -> bool:
    """Opt-in: the human operator running this CLI session waives the
    interactive approval prompt for messaging tools. The decision is
    still audited (severity WARNING + event APPROVAL_GRANTED) so the
    waiver appears in the audit trail.

    Set ``approval.auto: true`` in ``~/.deile/settings.json`` to enable.
    Off by default; tests never see this turned on."""
    from deile.config.settings import get_settings

    return get_settings().bot_approval_auto


class MessagingTool(Tool, abc.ABC):
    """Base for Discord/messaging tools that go through the deilebot daemon."""

    tool_name: str = "messaging.unknown"
    description_text: str = ""
    parameters: Dict[str, Any] = {}
    required_params: List[str] = []
    security_level: SecurityLevel = SecurityLevel.MODERATE
    require_approval: bool = False
    approval_risk: str = "moderate"

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name=self.tool_name,
                description=self.description_text,
                parameters={
                    "type": "object",
                    "properties": self.parameters,
                },
                required=list(self.required_params),
                security_level=self.security_level,
                category=ToolCategory.MESSAGING,
            )
        )

    # ---- Tool ABC plumbing --------------------------------------------------

    @property
    def name(self) -> str:
        return self.tool_name

    @property
    def description(self) -> str:
        return self.description_text

    @property
    def category(self) -> str:
        return ToolCategory.MESSAGING.value

    # ---- Subclass hook ------------------------------------------------------

    @abc.abstractmethod
    async def _perform(self, facade: BotClientFacade, args: Dict[str, Any]) -> Dict[str, Any]:
        """Run the actual control-plane call and return a JSON-serializable dict."""

    def _build_audit_payload(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Subclasses override to add op-specific fields. Must NOT include
        free-form text — only ids and hashes.

        IDs (channel_id, user_id, role_id, message_id) are platform-public
        snowflakes and go in plaintext — they're how operators correlate
        an audit row with the Discord side. Free-form payload (text,
        emoji which can be ``<:secret:1234>`` for custom emojis) is
        hashed via SHA8 to keep secrets out of the audit log while still
        letting an operator confirm "the same bytes were sent twice".
        """
        payload: Dict[str, Any] = {"tool": self.tool_name}
        if isinstance(args, dict):
            text = args.get("text")
            if text:
                payload["text_hash"] = _sha8(text)
            emoji = args.get("emoji")
            if emoji:
                payload["emoji_hash"] = _sha8(str(emoji))
            for key in ("channel_id", "user_id", "bot_user_id", "role_id", "message_id"):
                if args.get(key) is not None:
                    payload[key] = args[key]
        return payload

    # ---- execute() ---------------------------------------------------------

    async def execute(self, context: ToolContext) -> ToolResult:
        args = dict(context.parsed_args or {})
        facade = _resolve_facade(context)
        audit = _resolve_audit_logger(context)
        audit_payload = self._build_audit_payload(args)

        # 0. integration availability ----------------------------------------
        if not facade.is_available:
            reason = (
                "deilebot not installed"
                if not BOT_CLIENT_AVAILABLE
                else "DEILE_BOT_ENDPOINT/AUTH_TOKEN not configured"
            )
            self._emit_audit(audit, "denied", {**audit_payload, "reason": reason},
                             severity=SeverityLevel.WARNING)
            return ToolResult.error_result(
                f"messaging integration disabled: {reason}",
                error_code="BOT_INTEGRATION_DISABLED",
            )

        # 1. permission check -------------------------------------------------
        pm = _resolve_permission_manager(context)
        if pm is not None:
            allowed = self._check_permission(pm, args)
            if not allowed:
                self._emit_audit(
                    audit,
                    "denied",
                    {**audit_payload, "reason": "permission_denied"},
                    severity=SeverityLevel.WARNING,
                    event_type=AuditEventType.PERMISSION_DENIED,
                )
                return ToolResult.error_result(
                    f"permission denied for {self.tool_name}",
                    error_code="PERMISSION_DENIED",
                )

        # 2. approval gate (HIGH-risk only) -----------------------------------
        if self.require_approval:
            if not _trusted_operator_mode():
                approval_outcome = await self._maybe_request_approval(context, args, audit, audit_payload)
                if approval_outcome is not None:
                    return approval_outcome
            else:
                # Trusted-operator opt-in (env DEILE_BOT_APPROVAL_AUTO=1):
                # the human running the CLI explicitly waived the approval
                # prompt for this session. Audit it so the decision is traceable.
                self._emit_audit(
                    audit,
                    "approved",
                    {**audit_payload, "approval": "auto:trusted_operator"},
                    severity=SeverityLevel.WARNING,
                    event_type=AuditEventType.APPROVAL_GRANTED,
                )

        # 3. perform ----------------------------------------------------------
        try:
            data = await self._perform(facade, args)
        except Exception as exc:
            err = self._map_exception(exc, args)
            self._emit_audit(
                audit,
                "failed",
                {**audit_payload, "error_code": err.metadata.get("error_code", "UNKNOWN")},
                severity=SeverityLevel.ERROR,
            )
            return err

        self._emit_audit(audit, "success", audit_payload, severity=SeverityLevel.INFO)
        return ToolResult.success_result(
            data=data,
            message=self._success_message(data, args),
        )

    # ---- helpers ------------------------------------------------------------

    def _check_permission(self, pm, args: Dict[str, Any]) -> bool:
        # Resource string is "messaging:<op>:<channel|user|role>".
        scope = (
            args.get("channel_id")
            or args.get("user_id")
            or args.get("bot_user_id")
            or args.get("role_id")
            or "*"
        )
        resource = f"messaging:{self.tool_name}:{scope}"
        try:
            return bool(
                pm.check_permission(
                    tool_name=self.tool_name,
                    resource=resource,
                    action="execute",
                    context={"security_level": self.security_level.value},
                )
            )
        except Exception:  # pragma: no cover
            logger.exception("permission check raised")
            return False

    async def _maybe_request_approval(
        self,
        context: ToolContext,
        args: Dict[str, Any],
        audit,
        audit_payload: Dict[str, Any],
    ) -> Optional[ToolResult]:
        approval = _resolve_approval_system(context)
        if approval is None:  # pragma: no cover
            return None
        try:
            request_id = await approval.request_approval(
                step_id=context.session_data.get("step_id", "ad-hoc"),
                plan_id=context.session_data.get("plan_id", "interactive"),
                tool_name=self.tool_name,
                operation=self.tool_name,
                risk_level=self.approval_risk,
                description=f"Outbound messaging via {self.tool_name}",
                consequences=[
                    "Sends a message visible to other users on Discord",
                    "Cannot be unsent automatically",
                ],
                rollback_available=False,
                timeout=context.session_data.get("approval_timeout", 60.0),
                context=audit_payload,
            )
        except Exception:  # pragma: no cover
            logger.exception("approval request raised")
            return ToolResult.error_result(
                f"approval system unavailable for {self.tool_name}",
                error_code="APPROVAL_UNAVAILABLE",
            )

        try:
            granted = await approval.wait_for_approval(request_id)
        except Exception:  # pragma: no cover
            logger.exception("approval wait raised")
            granted = False

        if not granted:
            self._emit_audit(
                audit,
                "denied",
                {**audit_payload, "approval_request_id": request_id},
                severity=SeverityLevel.WARNING,
                event_type=AuditEventType.APPROVAL_DENIED,
            )
            return ToolResult.error_result(
                f"approval not granted for {self.tool_name}",
                error_code="APPROVAL_REQUIRED",
            )
        self._emit_audit(
            audit,
            "approved",
            {**audit_payload, "approval_request_id": request_id},
            severity=SeverityLevel.INFO,
            event_type=AuditEventType.APPROVAL_GRANTED,
        )
        return None

    def _map_exception(
        self, exc: Exception, args: Optional[Dict[str, Any]] = None
    ) -> ToolResult:
        from ...integrations.bot.client import (BotClientAuthError,
                                                BotClientNotReady,
                                                BotClientRateLimited,
                                                BotClientTimeoutError,
                                                BotClientUpstreamError)

        # Log full traceback at ERROR level for diagnostics.
        logger.error("%s: erro mapeado — %s", self.tool_name, type(exc).__name__,
                     exc_info=exc)

        tool = self.tool_name
        args = args or {}
        channel_id = args.get("channel_id", "")

        # ── BotClientAuthError ──────────────────────────────────────────
        if isinstance(exc, BotClientAuthError):
            msg = (
                f"{tool}: token de autenticação do deilebot rejeitado. "
                f"O daemon deilebot não aceitou o token configurado. "
                f"Verifique DEILE_BOT_AUTH_TOKEN."
            )
            return ToolResult.error_result(
                msg,
                error=exc,
                error_code="BOT_AUTH_ERROR",
                metadata={
                    "error_details": {
                        "error_code": "BOT_AUTH_ERROR",
                        "suggestion": "Verifique DEILE_BOT_AUTH_TOKEN",
                        "recoverable": False,
                    }
                },
            )

        # ── BotClientRateLimited ────────────────────────────────────────
        if isinstance(exc, BotClientRateLimited):
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                suggestion = f"Aguarde {retry_after}s antes de reenviar."
            else:
                suggestion = "Aguarde antes de reenviar."
            msg = (
                f"{tool}: rate-limited pela API do Discord. "
                f"O daemon deilebot foi limitado pela API do Discord. "
                f"{suggestion}"
            )
            details: Dict[str, Any] = {
                "error_code": "BOT_RATE_LIMITED",
                "suggestion": suggestion,
                "recoverable": True,
            }
            if retry_after is not None:
                details["retry_after"] = retry_after
            return ToolResult.error_result(
                msg,
                error=exc,
                error_code="BOT_RATE_LIMITED",
                metadata={"error_details": details},
            )

        # ── BotClientNotReady ───────────────────────────────────────────
        if isinstance(exc, BotClientNotReady):
            msg = (
                f"{tool}: deilebot não está pronto. "
                f"O daemon pode estar iniciando ou indisponível. "
                f"Tente novamente em alguns segundos."
            )
            return ToolResult.error_result(
                msg,
                error=exc,
                error_code="BOT_NOT_READY",
                metadata={
                    "error_details": {
                        "error_code": "BOT_NOT_READY",
                        "suggestion": "Tente novamente em alguns segundos",
                        "recoverable": True,
                    }
                },
            )

        # ── BotClientTimeoutError ───────────────────────────────────────
        if isinstance(exc, BotClientTimeoutError):
            msg = (
                f"{tool}: timeout ao enviar requisição. "
                f"O daemon deilebot não respondeu a tempo. "
                f"Verifique se o serviço deilebot está saudável e tente novamente."
            )
            return ToolResult.error_result(
                msg,
                error=exc,
                error_code="BOT_TIMEOUT",
                metadata={
                    "error_details": {
                        "error_code": "BOT_TIMEOUT",
                        "suggestion": "Verifique se o serviço deilebot está saudável",
                        "recoverable": True,
                    }
                },
            )

        # ── BotClientUpstreamError ──────────────────────────────────────
        if isinstance(exc, BotClientUpstreamError):
            if channel_id:
                prefix = f"{tool}: falha ao postar no canal {channel_id}."
            else:
                prefix = f"{tool}: falha ao comunicar com o Discord."
            msg = (
                f"{prefix} "
                f"O daemon deilebot recebeu erro da API do Discord "
                f"(possível instabilidade temporária ou payload inválido). "
                f"Tente novamente em alguns segundos."
            )
            return ToolResult.error_result(
                msg,
                error=exc,
                error_code="BOT_UPSTREAM",
                metadata={
                    "error_details": {
                        "error_code": "BOT_UPSTREAM",
                        "suggestion": "Tente novamente em alguns segundos",
                        "recoverable": True,
                    }
                },
            )

        # ── Unknown error → BOT_UNREACHABLE ────────────────────────────
        return ToolResult.error_result(
            f"{tool}: falha inesperada ({type(exc).__name__}). "
            f"Erro não classificado na comunicação com o deilebot. "
            f"Verifique os logs do daemon para diagnóstico.",
            error=exc,
            error_code="BOT_UNREACHABLE",
            metadata={
                "error_details": {
                    "error_code": "BOT_UNREACHABLE",
                    "suggestion": "Verifique os logs do daemon para diagnóstico",
                    "recoverable": False,
                }
            },
        )

    def _success_message(self, data: Dict[str, Any], args: Dict[str, Any]) -> str:
        return f"{self.tool_name} ok"

    def _emit_audit(
        self,
        audit,
        outcome: str,
        payload: Dict[str, Any],
        *,
        severity: SeverityLevel,
        event_type: AuditEventType = AuditEventType.TOOL_EXECUTION,
    ) -> None:
        if audit is None:
            return
        try:
            audit.log_event(
                event_type=event_type,
                severity=severity,
                actor=self.tool_name,
                resource=payload.get("channel_id") or payload.get("user_id") or payload.get("role_id") or "*",
                action="execute",
                result=outcome,
                details=dict(payload),
                tool_name=self.tool_name,
            )
        except Exception:  # pragma: no cover
            logger.exception("audit emission failed")
