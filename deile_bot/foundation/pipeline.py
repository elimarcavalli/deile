"""IngressPipeline + EgressPipeline.

Ingress: adapter -> identity -> permissions -> rate_limit -> store -> intent ->
         persona -> capability_catalog -> bridge.invoke -> egress.

Egress: render markup -> split -> send -> persist -> audit; on failure -> DLQ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from tenacity import (AsyncRetrying, retry_if_exception_type,
                      stop_after_attempt, wait_exponential)

from deile_bot.foundation.agent_bridge import (AgentBridge, AgentInvocation,
                                               AgentResponse)
from deile_bot.foundation.agent_meta import AgentMetaProvider
from deile_bot.foundation.audit import AuditEventType, BotAuditLogger
from deile_bot.foundation.capabilities import CapabilityCatalog
from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.dlq import DeadLetterQueue
from deile_bot.foundation.envelope import Channel, MessageEnvelope
from deile_bot.foundation.event_bus import BotEventBus, BotEventType
from deile_bot.foundation.exceptions import (AgentInvocationError,
                                             ProviderError, RateLimited)
from deile_bot.foundation.identity import IdentityResolver
from deile_bot.foundation.intent import IntentClassifier
from deile_bot.foundation.logging import get_logger
from deile_bot.foundation.metrics import MetricsCollector, timer
from deile_bot.foundation.output_formatter import OutputFormatter
from deile_bot.foundation.permissions import Action, PermissionGate
from deile_bot.foundation.persona_selector import PersonaSelector
from deile_bot.foundation.rate_limit import RateLimiter
from deile_bot.foundation.settings import get_bot_settings


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_seconds: float = 0.2
    max_seconds: float = 5.0


class EgressPipeline:
    """Render → split → send via adapter → persist → audit; DLQ on fatal fail."""

    def __init__(
        self,
        formatters: Dict[str, OutputFormatter],
        rate_limit: RateLimiter,
        store: ConversationStore,
        audit: BotAuditLogger,
        event_bus: BotEventBus,
        metrics: MetricsCollector,
        dlq: DeadLetterQueue,
        retry_policy: Optional[RetryPolicy] = None,
        default_formatter: Optional[OutputFormatter] = None,
    ):
        self._formatters = formatters
        self._rate_limit = rate_limit
        self._store = store
        self._audit = audit
        self._event_bus = event_bus
        self._metrics = metrics
        self._dlq = dlq
        self._policy = retry_policy or RetryPolicy()
        self._logger = get_logger("egress")
        self._default_formatter = default_formatter

    def _formatter_for(self, provider: str) -> OutputFormatter:
        f = self._formatters.get(provider)
        if f is not None:
            return f
        if self._default_formatter is not None:
            return self._default_formatter
        from deile_bot.foundation.output_formatter import PlainTextFormatter

        return PlainTextFormatter()

    async def send_response(
        self,
        adapter,
        env: MessageEnvelope,
        response: AgentResponse,
        persona: str,
        *,
        target_bot_user_id: Optional[str] = None,
    ) -> None:
        formatter = self._formatter_for(adapter.name)
        rendered = formatter.render(response.markup) if response.markup else response.text
        chunks = formatter.split(rendered) or [rendered]
        last_msg_id: Optional[str] = env.message_id
        target_id = target_bot_user_id or env.author.bot_user_id
        for i, chunk in enumerate(chunks):
            try:
                msg_id = await self._send_with_retry(
                    adapter, env.channel, chunk, reply_to=last_msg_id if i == 0 else None
                )
                await self._store.record_outbound(
                    provider=adapter.name,
                    channel=env.channel,
                    provider_message_id=msg_id,
                    bot_user_id=target_id,
                    text=chunk,
                    reply_to=last_msg_id,
                    sent_at=datetime.now(timezone.utc),
                )
                self._metrics.inc(
                    "bot_outbound_total", {"provider": adapter.name, "status": "ok"}
                )
                self._metrics.observe(
                    "bot_outbound_chars",
                    {"provider": adapter.name},
                    float(len(chunk)),
                )
                await self._audit.log(
                    AuditEventType.OUTBOUND_SENT,
                    user=env.author,
                    channel=env.channel,
                    message_id=msg_id,
                    payload={"persona": persona, "chars": len(chunk), "chunk_index": i},
                )
                await self._event_bus.publish(
                    BotEventType.OUTBOUND_SENT,
                    {"provider": adapter.name, "channel": env.channel.provider_channel_id},
                )
                last_msg_id = msg_id
            except Exception as e:  # noqa: BLE001
                self._metrics.inc(
                    "bot_outbound_total", {"provider": adapter.name, "status": "fail"}
                )
                await self._audit.log(
                    AuditEventType.OUTBOUND_FAILED,
                    user=env.author,
                    channel=env.channel,
                    payload={"error": str(e)[:200]},
                )
                await self._dlq.enqueue(
                    adapter.name,
                    {
                        "channel_id": env.channel.provider_channel_id,
                        "channel_provider": env.channel.provider,
                        "channel_scope": env.channel.scope.value,
                        "channel_name": env.channel.name,
                        "channel_parent_id": env.channel.parent_channel_id,
                        "text": chunk,
                        "reply_to": last_msg_id,
                        "bot_user_id": target_id,
                    },
                    error=f"{type(e).__name__}: {e}",
                    attempts=self._policy.max_attempts,
                )
                self._metrics.inc(
                    "bot_dlq_enqueued_total", {"provider": adapter.name}
                )
                await self._event_bus.publish(
                    BotEventType.DLQ_ENQUEUED, {"provider": adapter.name}
                )
                return

    async def _send_with_retry(
        self,
        adapter,
        channel: Channel,
        text: str,
        reply_to: Optional[str],
    ) -> str:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._policy.max_attempts),
            wait=wait_exponential(
                multiplier=self._policy.base_seconds, max=self._policy.max_seconds
            ),
            retry=retry_if_exception_type(ProviderError),
            reraise=True,
        ):
            with attempt:
                return await adapter.send_message(channel, text, reply_to=reply_to)
        raise ProviderError("retry exhausted")  # pragma: no cover

    async def send_fallback(
        self,
        adapter,
        channel: Channel,
        reply_to: Optional[str],
        reason: str,
    ) -> None:
        msg = f"⚠️ não foi possível processar agora ({reason})."
        try:
            await adapter.send_message(channel, msg, reply_to=reply_to)
        except Exception:
            self._logger.warning("fallback send also failed", exc_info=True)


class IngressPipeline:
    """Single entry point: adapter delivers an envelope; pipeline routes it."""

    def __init__(
        self,
        identity: IdentityResolver,
        permissions: PermissionGate,
        rate_limit: RateLimiter,
        store: ConversationStore,
        intent: IntentClassifier,
        bridge: AgentBridge,
        capability_catalog: CapabilityCatalog,
        persona_selector: PersonaSelector,
        audit: BotAuditLogger,
        event_bus: BotEventBus,
        metrics: MetricsCollector,
        egress: EgressPipeline,
        agent_meta: AgentMetaProvider,
    ):
        self.identity = identity
        self.permissions = permissions
        self.rate_limit = rate_limit
        self.store = store
        self.intent = intent
        self.bridge = bridge
        self.capability_catalog = capability_catalog
        self.persona_selector = persona_selector
        self.audit = audit
        self.event_bus = event_bus
        self.metrics = metrics
        self.egress = egress
        self.agent_meta = agent_meta
        self._logger = get_logger("ingress")

    async def handle(self, env: MessageEnvelope, adapter) -> None:
        provider = env.channel.provider
        scope = env.channel.scope.value
        # 1. inbound audit + metric
        self.metrics.inc("bot_inbound_total", {"provider": provider, "scope": scope})
        await self.audit.log(
            AuditEventType.INBOUND_RECEIVED,
            user=env.author,
            channel=env.channel,
            message_id=env.message_id,
            payload={"text_chars": len(env.text)},
        )
        # 2. resolve identity (refresh display_name)
        user = await self.identity.resolve(
            env.author.provider,
            env.author.provider_user_id,
            env.author.display_name,
            is_bot=env.author.is_bot,
        )
        # 3. permission gate
        decision = await self.permissions.check(
            user, Action.INVOKE_AGENT, scope=env.channel.scope
        )
        if not decision.allowed:
            self.metrics.inc(
                "bot_permission_denied_total",
                {"provider": provider, "action": "INVOKE_AGENT"},
            )
            await self.audit.log(
                AuditEventType.PERMISSION_DENIED,
                user=user,
                channel=env.channel,
                message_id=env.message_id,
                payload={"reason": decision.reason},
            )
            await self.event_bus.publish(
                BotEventType.PERMISSION_DENIED,
                {"provider": provider, "reason": decision.reason},
            )
            return
        # 4. rate limit
        try:
            await self.rate_limit.acquire_inbound(user)
        except RateLimited as e:
            reason = e.context.get("reason", "unknown")
            self.metrics.inc(
                "bot_rate_limited_total", {"provider": provider, "reason": reason}
            )
            await self.audit.log(
                AuditEventType.RATE_LIMITED,
                user=user,
                channel=env.channel,
                payload={"reason": reason},
            )
            return
        # 5. persist (use resolved user.bot_user_id, not env.author's stale one)
        await self.store.upsert_user(user)
        await self.store.upsert_channel(env.channel)
        await self.store.record_inbound(env, bot_user_id_override=user.bot_user_id)
        # 6. recent history
        history = await self.store.get_recent_messages(provider, env.channel, limit=40)
        # 7. intent classifier
        decision_intent = await self.intent.decide(env, history, adapter.self_user_id)
        self.metrics.inc(
            "bot_should_respond_total",
            {
                "provider": provider,
                "decision": "true" if decision_intent.should_respond else "false",
                "classifier": type(self.intent).__name__,
            },
        )
        await self.audit.log(
            AuditEventType.SHOULD_RESPOND_DECIDED,
            user=user,
            channel=env.channel,
            message_id=env.message_id,
            payload={
                "decision": decision_intent.should_respond,
                "reason": decision_intent.reason,
            },
        )
        if not decision_intent.should_respond:
            return
        # 8. owner / persona
        is_owner = await self.permissions.is_owner(user)
        persona = await self.persona_selector.resolve(env, user, is_owner)
        # 9. capability snapshot + extra prompt
        snap = await self.capability_catalog.snapshot(adapter, self.agent_meta)
        extra = self.capability_catalog.render_for_system_prompt(snap)
        bot_settings = get_bot_settings().foundation
        # 10. agent invoke
        inv = AgentInvocation(
            bot_user_id=user.bot_user_id,
            persona=persona,
            forced_model=bot_settings.forced_model,
            inbound_text=env.text,
            default_model=bot_settings.default_model,
            inbound_attachments=env.attachments,
            history=history,
            capabilities=snap,
            extra_system_prompt=extra,
            bot_context={
                "provider": provider,
                "channel_scope": scope,
                "channel_id": env.channel.provider_channel_id,
                "channel_name": env.channel.name,
                "is_owner": is_owner,
                "persona": persona,
            },
            timeout_seconds=bot_settings.agent_invocation_timeout_seconds,
        )
        self.metrics.inc(
            "bot_agent_invocations_total",
            {"provider": provider, "persona": persona, "status": "started"},
        )
        await self.audit.log(
            AuditEventType.AGENT_INVOKED,
            user=user,
            channel=env.channel,
            message_id=env.message_id,
            payload={"persona": persona, "extra_chars": len(extra)},
        )
        # Show typing indicator while the agent processes (best-effort, never raises).
        try:
            await adapter.send_typing(env.channel)
        except Exception:
            self._logger.debug("send_typing ignored", exc_info=True)
        try:
            with timer(self.metrics, "bot_agent_invocation_seconds", {"provider": provider}):
                response = await self.bridge.invoke(inv)
        except AgentInvocationError as e:
            self.metrics.inc(
                "bot_agent_invocations_total",
                {"provider": provider, "persona": persona, "status": "fail"},
            )
            await self.audit.log(
                AuditEventType.AGENT_FAILED,
                user=user,
                channel=env.channel,
                message_id=env.message_id,
                payload={"error": str(e)[:200], "type": type(e).__name__},
            )
            await self.egress.send_fallback(
                adapter, env.channel, env.message_id, reason="agent_failed"
            )
            return
        await self.audit.log(
            AuditEventType.AGENT_RESPONDED,
            user=user,
            channel=env.channel,
            message_id=env.message_id,
            payload={
                "elapsed_ms": response.elapsed_ms,
                "model": response.model_used,
                "chars": len(str(response.text)),
            },
        )
        self.metrics.inc(
            "bot_agent_invocations_total",
            {"provider": provider, "persona": persona, "status": "ok"},
        )
        # 11. egress
        await self.egress.send_response(
            adapter, env, response, persona, target_bot_user_id=user.bot_user_id
        )
