"""Integration layer between personas and DEILE's unified memory system"""

from typing import Dict, Any, List, Optional
from datetime import datetime
import logging

# Import only what we need for testing without causing circular imports
try:
    from ...memory.memory_manager import MemoryManager
except ImportError:
    MemoryManager = None

try:
    from ...memory.working_memory import WorkingMemoryEntry
except ImportError:
    WorkingMemoryEntry = None

logger = logging.getLogger(__name__)


class PersonaMemoryLayer:
    """Integration layer between personas and DEILE's unified memory system"""

    def __init__(self, memory_manager: MemoryManager, persona_id: str):
        self.memory_manager = memory_manager
        self.persona_id = persona_id
        self.logger = logger

    async def store_persona_state(self, state: Dict[str, Any]) -> None:
        """Store persona state in DEILE's semantic memory"""
        await self.memory_manager.semantic_memory.store_concept(
            concept=f"persona:{self.persona_id}:state",
            data={
                'state': state,
                'timestamp': datetime.now().isoformat(),
                'persona_id': self.persona_id
            },
            metadata={'type': 'persona_state', 'persona_id': self.persona_id}
        )
        self.logger.debug(f"Stored persona state for {self.persona_id}")

    async def get_persona_state(self) -> Dict[str, Any]:
        """Retrieve persona state from DEILE's semantic memory"""
        result = await self.memory_manager.semantic_memory.get_concept(
            f"persona:{self.persona_id}:state"
        )
        return result.get('state', {}) if result else {}

    async def store_conversation_context(
        self,
        session_id: str,
        context: Dict[str, Any]
    ) -> None:
        """Store conversation context in DEILE's episodic memory"""
        await self.memory_manager.episodic_memory.record_event(
            event_type="persona_conversation",
            session_id=session_id,
            details={
                'persona_id': self.persona_id,
                'context': context,
                'timestamp': datetime.now().isoformat()
            },
            metadata={'persona_id': self.persona_id}
        )

    async def get_conversation_history(
        self,
        session_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get conversation history from DEILE's episodic memory"""
        events = await self.memory_manager.episodic_memory.get_session_events(
            session_id=session_id,
            event_type="persona_conversation",
            limit=limit
        )

        return [
            event for event in events
            if event.get('details', {}).get('persona_id') == self.persona_id
        ]

    async def store_persona_preference(self, key: str, value: Any) -> None:
        """Store persona preference in working memory for quick access"""
        await self.memory_manager.working_memory.set(
            key=f"persona:{self.persona_id}:pref:{key}",
            value=value,
            ttl=3600  # 1 hour TTL
        )

    async def get_persona_preference(self, key: str, default: Any = None) -> Any:
        """Get persona preference from working memory"""
        result = await self.memory_manager.working_memory.get(
            f"persona:{self.persona_id}:pref:{key}"
        )
        return result if result is not None else default

    async def learn_pattern(
        self,
        pattern_type: str,
        pattern_data: Dict[str, Any],
        success_rate: float = 1.0
    ) -> None:
        """Learn pattern using DEILE's procedural memory"""
        await self.memory_manager.procedural_memory.learn_pattern(
            pattern_type=f"persona:{self.persona_id}:{pattern_type}",
            context=pattern_data,
            success_metrics={'success_rate': success_rate}
        )

    async def get_learned_patterns(self, pattern_type: str) -> List[Dict[str, Any]]:
        """Get learned patterns from DEILE's procedural memory"""
        return await self.memory_manager.procedural_memory.get_patterns(
            pattern_type=f"persona:{self.persona_id}:{pattern_type}"
        )

    async def cleanup_persona_memory(self) -> None:
        """Cleanup persona-specific memory data"""
        # Cleanup working memory
        await self.memory_manager.working_memory.remove_pattern(
            f"persona:{self.persona_id}:*"
        )

        # Mark semantic concepts for cleanup
        await self.memory_manager.semantic_memory.mark_for_cleanup(
            pattern=f"persona:{self.persona_id}:*"
        )

        self.logger.info(f"Cleaned up memory for persona {self.persona_id}")