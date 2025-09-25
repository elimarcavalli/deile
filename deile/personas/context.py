"""Unified persona context using DEILE's memory system"""

from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

from .memory.integration import PersonaMemoryLayer

# Import with fallback for testing
try:
    from ..memory.memory_manager import MemoryManager
except ImportError:
    MemoryManager = None


@dataclass
class PersonaContext:
    """Unified persona context using DEILE's memory system"""
    persona_id: str
    session_id: str
    memory_layer: PersonaMemoryLayer
    current_state: Dict[str, Any] = field(default_factory=dict)
    preferences: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    async def create(
        cls,
        persona_id: str,
        session_id: str,
        memory_manager: MemoryManager
    ) -> 'PersonaContext':
        """Create persona context with memory integration"""
        memory_layer = PersonaMemoryLayer(memory_manager, persona_id)

        # Load existing state and preferences
        current_state = await memory_layer.get_persona_state()
        preferences = {}

        # Load common preferences
        for pref_key in ['communication_style', 'preferred_tools', 'behavior_mode']:
            pref_value = await memory_layer.get_persona_preference(pref_key)
            if pref_value is not None:
                preferences[pref_key] = pref_value

        return cls(
            persona_id=persona_id,
            session_id=session_id,
            memory_layer=memory_layer,
            current_state=current_state,
            preferences=preferences
        )

    async def save_state(self) -> None:
        """Save current state to memory"""
        await self.memory_layer.store_persona_state(self.current_state)

        # Save preferences
        for key, value in self.preferences.items():
            await self.memory_layer.store_persona_preference(key, value)

    async def update_from_interaction(self, interaction_data: Dict[str, Any]) -> None:
        """Update context based on interaction"""
        # Store interaction in conversation context
        await self.memory_layer.store_conversation_context(
            self.session_id,
            interaction_data
        )

        # Update current state
        self.current_state.update({
            'last_interaction': datetime.now().isoformat(),
            'interaction_count': self.current_state.get('interaction_count', 0) + 1
        })

        await self.save_state()