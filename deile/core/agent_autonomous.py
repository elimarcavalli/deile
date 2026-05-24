"""Autonomous file-resolution methods extracted from DeileAgent.

This mixin owns the autonomous-request methods
(``process_autonomous_request`` and its private helpers) that let DEILE
handle natural language file references like "read the readme" without
requiring exact filenames. It mirrors the split already applied to
streaming methods in :class:`AgentStreamingMixin`, so ``deile/core/agent.py``
stays focused on lifecycle and orchestration.

Methods here read state from ``self`` exactly as they did when they lived
on the concrete class — instance attributes (``self.proactive_analyzer``)
and sibling methods (``self._map_proactive_action_to_tool``,
``self._execute_proactive_tool``) are resolved via the MRO when
``DeileAgent(AgentStreamingMixin, AgentAutonomousMixin)`` is instantiated.
The mixin is not usable standalone.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .proactive_analyzer import ProactiveAction

if TYPE_CHECKING:
    from .agent import AgentSession
    from .proactive_analyzer import ProactiveIntent

logger = logging.getLogger(__name__)


class AgentAutonomousMixin:
    """Autonomous file-resolution methods of :class:`DeileAgent`."""

    async def process_autonomous_request(
        self, user_input: str, session: "AgentSession"
    ) -> Optional[str]:
        """
        Process autonomous requests with intelligent file resolution

        This is the main entry point for autonomous functionality that enables
        DEILE to handle natural language file references like "read the readme"
        without requiring exact filenames from the user.
        """
        if not self.proactive_analyzer:
            return None

        try:
            # Analyze if this requires autonomous processing
            intents = await self.proactive_analyzer.analyze_enhanced(user_input)

            if not intents:
                return None

            # Filter for autonomous-eligible intents
            autonomous_intents = [intent for intent in intents if intent.autonomous_eligible]

            if not autonomous_intents:
                return None

            logger.info(f"Found {len(autonomous_intents)} autonomous intent(s)")

            # Execute the highest priority autonomous intent
            highest_priority = max(autonomous_intents, key=lambda x: x.priority)

            return await self._execute_autonomous_intent(highest_priority, session)

        except Exception as e:
            logger.error(f"Error in autonomous processing: {e}")
            return None

    async def _execute_autonomous_intent(
        self, intent: "ProactiveIntent", session: "AgentSession"
    ) -> Optional[str]:
        """Execute an autonomous intent with intelligent error recovery"""
        try:
            if intent.action == ProactiveAction.READ_FILE and intent.resolved_file:
                return await self._autonomous_read_file(intent, session)

            elif intent.action == ProactiveAction.CHAIN_LIST_AND_READ:
                return await self._autonomous_chain_list_and_read(intent, session)

            elif intent.action == ProactiveAction.SUGGEST_ALTERNATIVES:
                return await self._autonomous_suggest_alternatives(intent, session)

            else:
                # Fallback to regular proactive execution
                tool_name = self._map_proactive_action_to_tool(intent.action)
                if tool_name:
                    return await self._execute_proactive_tool(tool_name, intent.target, session)

        except Exception as e:
            logger.error(f"Error executing autonomous intent {intent.action}: {e}")

            # Try alternative resolution if available
            if intent.chained_actions:
                for fallback_intent in intent.chained_actions:
                    result = await self._execute_autonomous_intent(fallback_intent, session)
                    if result:
                        return result

        return None

    async def _autonomous_read_file(
        self, intent: "ProactiveIntent", session: "AgentSession"
    ) -> Optional[str]:
        """Autonomously read a file using resolved file match"""
        if not intent.resolved_file:
            return None

        try:
            # Execute read_file tool with resolved path
            file_path = str(intent.resolved_file.path)
            result = await self._execute_proactive_tool("read_file", file_path, session)

            if result and intent.resolved_file.confidence < 1.0:
                # Add context about the resolution for transparency
                confidence_msg = f"\n\n*Autonomously resolved '{intent.target}' → '{intent.resolved_file.path.name}' (confidence: {intent.resolved_file.confidence:.1%})*"
                result = result + confidence_msg

            return result

        except Exception as e:
            logger.error(f"Error in autonomous read: {e}")
            return None

    async def _autonomous_suggest_alternatives(
        self, intent: "ProactiveIntent", session: "AgentSession"
    ) -> Optional[str]:
        """Provide intelligent alternatives when file resolution fails"""
        try:
            # Get file resolver instance
            from .file_resolver import get_file_resolver
            file_resolver = get_file_resolver(Path.cwd())

            # Get alternative suggestions
            suggestions = file_resolver.suggest_alternatives(intent.target, max_suggestions=5)

            if not suggestions:
                return f"❌ No files matching '{intent.target}' found in current directory."

            # Format suggestions nicely
            suggestion_text = f"🔍 Couldn't find exact match for '{intent.target}'. Here are some alternatives:\n\n"

            for i, match in enumerate(suggestions, 1):
                confidence = f"({match.confidence:.1%})" if match.confidence < 1.0 else ""
                suggestion_text += f"{i}. **{match.path.name}** {confidence}\n   └─ {match.reason}\n\n"

            suggestion_text += "💡 *Tip: Try being more specific, or ask me to read one of these files directly.*"

            return suggestion_text

        except Exception as e:
            logger.error(f"Error generating alternatives: {e}")
            return None

    async def _autonomous_chain_list_and_read(
        self, intent: "ProactiveIntent", session: "AgentSession"
    ) -> Optional[str]:
        """Chain list files → resolve → read operations autonomously"""
        try:
            # First, list files to help with resolution
            list_result = await self._execute_proactive_tool("list_files", ".", session)

            if not list_result:
                return None

            # Get file resolver and try to find the best match
            from .file_resolver import get_file_resolver
            file_resolver = get_file_resolver(Path.cwd())

            best_match = file_resolver.get_best_match(intent.target, min_confidence=0.7)

            if best_match:
                # Found a good match, read it
                read_result = await self._execute_proactive_tool("read_file", str(best_match.path), session)

                if read_result:
                    # Combine list + read results with resolution context
                    resolution_context = f"🎯 *Found and read '{best_match.path.name}' (confidence: {best_match.confidence:.1%})*\n\n"
                    return list_result + "\n\n" + resolution_context + read_result

            else:
                # No good match, provide alternatives
                alternatives = await self._autonomous_suggest_alternatives(intent, session)
                return list_result + "\n\n" + (alternatives or "❌ No matching files found.")

        except Exception as e:
            logger.error(f"Error in chain operation: {e}")
            return None
