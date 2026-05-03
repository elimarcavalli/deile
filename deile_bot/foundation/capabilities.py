"""ProviderCapabilities flags + CapabilitySnapshot DTO + CapabilityCatalog."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, FrozenSet, List, Mapping, Optional

from deile_bot.foundation.envelope import AttachmentKind

if TYPE_CHECKING:  # pragma: no cover
    from deile_bot.foundation.agent_meta import AgentMetaProvider, ToolMeta
    from deile_bot.foundation.output_formatter import OutputFormatter


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    can_edit_message: bool
    can_react: bool
    can_send_dm: bool
    can_threads: bool
    can_polls: bool
    can_inline_keyboards: bool
    can_slash_commands: bool
    can_voice_messages: bool
    can_send_typing: bool
    can_fetch_user_profile: bool
    has_conversation_window: bool
    max_message_chars: int
    max_attachments_per_message: int
    supported_attachment_kinds: FrozenSet[AttachmentKind]


@dataclass(frozen=True)
class CapabilitySnapshot:
    provider: str
    provider_capabilities: ProviderCapabilities
    cogs_or_handlers: List[str] = field(default_factory=list)
    agent_tools: List["ToolMeta"] = field(default_factory=list)
    agent_models: List[str] = field(default_factory=list)
    persona_default: str = "developer"
    bot_settings_summary: Mapping[str, Any] = field(default_factory=dict)


class CapabilityCatalog:
    """Snapshots provider+agent capabilities and renders them for prompts/UI."""

    def __init__(self, settings: Optional[Any] = None):
        self.settings = settings

    async def snapshot(
        self,
        adapter: Any,
        agent_meta_provider: "AgentMetaProvider",
        cogs_or_handlers: Optional[List[str]] = None,
    ) -> CapabilitySnapshot:
        tools = await agent_meta_provider.list_tools()
        models = await agent_meta_provider.list_models()
        personas = await agent_meta_provider.list_personas()
        persona_default = personas[0] if personas else "developer"
        return CapabilitySnapshot(
            provider=adapter.name,
            provider_capabilities=adapter.capabilities,
            cogs_or_handlers=cogs_or_handlers or [],
            agent_tools=list(tools),
            agent_models=list(models),
            persona_default=persona_default,
            bot_settings_summary={},
        )

    def render_for_system_prompt(self, snap: CapabilitySnapshot) -> str:
        """Render snapshot as a `<bot_capabilities>` block for the LLM system prompt."""
        lines: List[str] = ["<bot_capabilities>"]
        lines.append(f"provider: {snap.provider}")
        cap = snap.provider_capabilities
        flags = [
            ("can_edit_message", cap.can_edit_message),
            ("can_react", cap.can_react),
            ("can_send_dm", cap.can_send_dm),
            ("can_threads", cap.can_threads),
            ("can_slash_commands", cap.can_slash_commands),
            ("has_conversation_window", cap.has_conversation_window),
        ]
        lines.append("flags: " + ", ".join(f"{k}={v}" for k, v in flags))
        lines.append(f"max_message_chars: {cap.max_message_chars}")
        if snap.cogs_or_handlers:
            lines.append("handlers: " + ", ".join(snap.cogs_or_handlers))
        if snap.agent_tools:
            lines.append("tools:")
            for tool in snap.agent_tools:
                desc = (tool.description or "")[:80]
                lines.append(f"  - {tool.name}: {desc}")
        if snap.agent_models:
            lines.append("models: " + ", ".join(snap.agent_models))
        lines.append(f"persona: {snap.persona_default}")
        lines.append("</bot_capabilities>")
        return "\n".join(lines)

    def render_for_user(
        self,
        snap: CapabilitySnapshot,
        formatter: "OutputFormatter",
    ) -> str:
        """Render snapshot for human consumption via /capabilities."""
        from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind

        spans = [
            MarkupSpan(SpanKind.HEADING, "DEILE — Capabilities", meta={"level": 2}),
            MarkupSpan(SpanKind.LINE_BREAK, "\n"),
            MarkupSpan(SpanKind.PLAIN, f"Provider: {snap.provider}\n"),
            MarkupSpan(SpanKind.PLAIN, f"Persona ativa: {snap.persona_default}\n"),
        ]
        if snap.agent_tools:
            spans.append(MarkupSpan(SpanKind.HEADING, "Tools", meta={"level": 3}))
            for tool in snap.agent_tools[:30]:
                spans.append(
                    MarkupSpan(
                        SpanKind.BULLET,
                        f"{tool.name} — {(tool.description or '')[:60]}\n",
                    )
                )
        if snap.agent_models:
            spans.append(MarkupSpan(SpanKind.HEADING, "Models", meta={"level": 3}))
            for model in snap.agent_models:
                spans.append(MarkupSpan(SpanKind.BULLET, f"{model}\n"))
        return formatter.render(MarkupAST(spans))
