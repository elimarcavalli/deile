"""IntentClassifier — should the bot respond to this inbound?

Four implementations:
- HeuristicIntentClassifier
- LLMIntentClassifier
- AlwaysRespondToAddressed
- AlwaysRespond
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Protocol

from deile_bot.foundation.conversation_store import StoredMessage
from deile_bot.foundation.envelope import MessageEnvelope
from deile_bot.foundation.settings import FoundationSettings


class IntentDecision(NamedTuple):
    should_respond: bool
    reason: str


class IntentClassifier(Protocol):
    async def decide(
        self,
        env: MessageEnvelope,
        history: List[StoredMessage],
        self_user_id: str,
    ) -> IntentDecision: ...


def _has_command_prefix(text: str, prefix: str = "d!") -> bool:
    return bool(text) and text.lstrip().startswith(prefix)


class HeuristicIntentClassifier:
    """Cheap rules; no LLM call."""

    def __init__(self, *, min_chars: int = 4, command_prefix: str = "d!"):
        self.min_chars = min_chars
        self.command_prefix = command_prefix

    async def decide(
        self,
        env: MessageEnvelope,
        history: List[StoredMessage],
        self_user_id: str,
    ) -> IntentDecision:
        if env.has_force_respond:
            return IntentDecision(True, "force_respond")
        if env.is_dm:
            return IntentDecision(True, "dm")
        if env.mentions_self(self_user_id):
            return IntentDecision(True, "mention")
        if env.reply is not None and env.reply.replied_author.provider_user_id == self_user_id:
            return IntentDecision(True, "reply_to_bot")
        if _has_command_prefix(env.text, self.command_prefix):
            return IntentDecision(False, "command_prefix")
        if len(env.text.strip()) < self.min_chars:
            return IntentDecision(False, "too_short")
        return IntentDecision(False, "noise")


class LLMIntentClassifier:
    """LLM-backed should-respond decision (kept simple; opt-in)."""

    def __init__(
        self,
        model: str = "deepseek:deepseek-chat",
        max_tokens: int = 10,
        temperature: float = 0.3,
        invoker: Optional[object] = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.invoker = invoker  # opt-in; tests pass mocks

    async def decide(
        self,
        env: MessageEnvelope,
        history: List[StoredMessage],
        self_user_id: str,
    ) -> IntentDecision:
        if env.has_force_respond:
            return IntentDecision(True, "force_respond")
        if env.is_dm or env.mentions_self(self_user_id):
            return IntentDecision(True, "addressed")
        if self.invoker is None:
            return IntentDecision(False, "no_llm_configured")
        try:
            verdict = await self.invoker.ask(env.text)  # type: ignore[attr-defined]
            return IntentDecision(bool(verdict), "llm")
        except Exception:
            return IntentDecision(False, "llm_error")


class AlwaysRespondToAddressed:
    async def decide(
        self,
        env: MessageEnvelope,
        history: List[StoredMessage],
        self_user_id: str,
    ) -> IntentDecision:
        if env.has_force_respond:
            return IntentDecision(True, "force_respond")
        if env.is_dm:
            return IntentDecision(True, "dm")
        if env.mentions_self(self_user_id):
            return IntentDecision(True, "mention")
        if env.reply is not None and env.reply.replied_author.provider_user_id == self_user_id:
            return IntentDecision(True, "reply_to_bot")
        return IntentDecision(False, "not_addressed")


class AlwaysRespond:
    async def decide(
        self,
        env: MessageEnvelope,
        history: List[StoredMessage],
        self_user_id: str,
    ) -> IntentDecision:
        return IntentDecision(True, "always")


def build_intent_classifier(settings: FoundationSettings) -> IntentClassifier:
    name = settings.intent_classifier
    if name == "heuristic":
        return HeuristicIntentClassifier()
    if name == "llm":
        return LLMIntentClassifier()
    if name == "always_respond_to_addressed":
        return AlwaysRespondToAddressed()
    if name == "always_respond":
        return AlwaysRespond()
    raise ValueError(f"unknown intent_classifier: {name}")
