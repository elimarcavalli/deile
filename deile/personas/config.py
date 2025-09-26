"""
Unified Persona Configuration Models for DEILE v5.0 ULTRA
========================================================

This module provides persona configuration models that integrate with DEILE's
unified ConfigManager system, eliminating duplicate configuration systems.

All persona configuration is managed through the central ConfigManager,
ensuring consistency, unified hot-reload, and centralized validation.

Author: DEILE Team
Version: 5.0.0 ULTRA
"""

from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from enum import Enum
import logging

try:
    from ..core.exceptions import ValidationError, DEILEError
    from ..config.manager import ConfigManager, get_config_manager
except ImportError:
    # Fallback if modules don't exist
    class ValidationError(Exception):
        pass
    class DEILEError(Exception):
        pass

    def get_config_manager():
        from ..config.manager import get_config_manager as _get_config_manager
        return _get_config_manager()

logger = logging.getLogger(__name__)


class CommunicationStyle(Enum):
    """Available communication styles for personas"""
    TECHNICAL = "technical"
    EDUCATIONAL = "educational"
    COLLABORATIVE = "collaborative"
    ANALYTICAL = "analytical"
    STRATEGIC = "strategic"
    CREATIVE = "creative"


class VerbosityLevel(Enum):
    """Verbosity levels for persona responses"""
    MINIMAL = "minimal"
    FOCUSED = "focused"
    DETAILED = "detailed"
    COMPREHENSIVE = "comprehensive"


@dataclass
class ModelPreferences:
    """Model-specific preferences for persona"""
    temperature: float = 0.7
    max_tokens: int = 6000
    top_p: float = 0.9
    top_k: Optional[int] = None
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
            'top_p': self.top_p,
            'top_k': self.top_k,
            'frequency_penalty': self.frequency_penalty,
            'presence_penalty': self.presence_penalty
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ModelPreferences':
        return cls(
            temperature=data.get('temperature', 0.7),
            max_tokens=data.get('max_tokens', 6000),
            top_p=data.get('top_p', 0.9),
            top_k=data.get('top_k'),
            frequency_penalty=data.get('frequency_penalty', 0.0),
            presence_penalty=data.get('presence_penalty', 0.0)
        )


@dataclass
class BehaviorSettings:
    """Behavior settings for persona"""
    verbosity_level: VerbosityLevel = VerbosityLevel.DETAILED
    code_explanation: bool = True
    suggest_improvements: bool = True
    step_by_step: bool = False
    ask_clarifying_questions: bool = False
    focus_on_patterns: bool = False
    include_trade_offs: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'verbosity_level': self.verbosity_level.value,
            'code_explanation': self.code_explanation,
            'suggest_improvements': self.suggest_improvements,
            'step_by_step': self.step_by_step,
            'ask_clarifying_questions': self.ask_clarifying_questions,
            'focus_on_patterns': self.focus_on_patterns,
            'include_trade_offs': self.include_trade_offs
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BehaviorSettings':
        return cls(
            verbosity_level=VerbosityLevel(data.get('verbosity_level', 'detailed')),
            code_explanation=data.get('code_explanation', True),
            suggest_improvements=data.get('suggest_improvements', True),
            step_by_step=data.get('step_by_step', False),
            ask_clarifying_questions=data.get('ask_clarifying_questions', False),
            focus_on_patterns=data.get('focus_on_patterns', False),
            include_trade_offs=data.get('include_trade_offs', False)
        )


@dataclass
class ToolPreferences:
    """Tool preferences for persona"""
    preferred_tools: List[str] = field(default_factory=list)
    avoid_tools: List[str] = field(default_factory=list)
    tool_timeout: int = 30

    def to_dict(self) -> Dict[str, Any]:
        return {
            'preferred_tools': self.preferred_tools,
            'avoid_tools': self.avoid_tools,
            'tool_timeout': self.tool_timeout
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ToolPreferences':
        return cls(
            preferred_tools=data.get('preferred_tools', []),
            avoid_tools=data.get('avoid_tools', []),
            tool_timeout=data.get('tool_timeout', 30)
        )


class PersonaConfig:
    """Unified persona configuration using DEILE's ConfigManager"""

    def __init__(
        self,
        persona_id: str,
        capabilities: List[str] = None,
        communication_style: CommunicationStyle = CommunicationStyle.TECHNICAL,
        model_preferences: ModelPreferences = None,
        behavior_settings: BehaviorSettings = None,
        tool_preferences: ToolPreferences = None,
        config_manager: ConfigManager = None
    ):
        self.persona_id = persona_id
        self.capabilities = capabilities or []
        self.communication_style = communication_style
        self.model_preferences = model_preferences or ModelPreferences()
        self.behavior_settings = behavior_settings or BehaviorSettings()
        self.tool_preferences = tool_preferences or ToolPreferences()

        # Use unified ConfigManager
        self.config_manager = config_manager or get_config_manager()
        self.logger = logger

    @classmethod
    async def load_from_config_manager(
        cls,
        persona_id: str,
        config_manager: ConfigManager = None
    ) -> 'PersonaConfig':
        """Load persona configuration from unified ConfigManager"""
        config_manager = config_manager or get_config_manager()

        try:
            # Load persona configuration
            persona_data = await config_manager.get_persona_config(persona_id)

            if not persona_data:
                raise ValidationError(f"Persona configuration not found: {persona_id}")

            return cls.from_dict(persona_id, persona_data, config_manager)

        except Exception as e:
            logger.error(f"Failed to load persona config for {persona_id}: {e}")
            raise

    @classmethod
    def from_dict(
        cls,
        persona_id: str,
        data: Dict[str, Any],
        config_manager: ConfigManager = None
    ) -> 'PersonaConfig':
        """Create PersonaConfig from dictionary with validation"""
        try:
            # Validate required fields
            cls._validate_persona_data(data)

            return cls(
                persona_id=persona_id,
                capabilities=data.get('capabilities', []),
                communication_style=CommunicationStyle(
                    data.get('communication_style', 'technical')
                ),
                model_preferences=ModelPreferences.from_dict(
                    data.get('model_preferences', {})
                ),
                behavior_settings=BehaviorSettings.from_dict(
                    data.get('behavior_settings', {})
                ),
                tool_preferences=ToolPreferences.from_dict(
                    data.get('tool_preferences', {})
                ),
                config_manager=config_manager
            )

        except Exception as e:
            logger.error(f"Failed to create PersonaConfig from data: {e}")
            raise ValidationError(f"Invalid persona configuration: {e}")

    @staticmethod
    def _validate_persona_data(data: Dict[str, Any]) -> None:
        """Validate persona configuration data"""
        if not isinstance(data, dict):
            raise ValidationError("Persona configuration must be a dictionary")

        # Validate capabilities
        capabilities = data.get('capabilities')
        if capabilities is not None and not isinstance(capabilities, list):
            raise ValidationError("capabilities must be a list")

        # Validate communication style
        comm_style = data.get('communication_style')
        if comm_style is not None:
            try:
                CommunicationStyle(comm_style)
            except ValueError:
                valid_styles = [style.value for style in CommunicationStyle]
                raise ValidationError(
                    f"Invalid communication_style '{comm_style}'. "
                    f"Valid options: {valid_styles}"
                )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage"""
        return {
            'capabilities': self.capabilities,
            'communication_style': self.communication_style.value,
            'model_preferences': self.model_preferences.to_dict(),
            'behavior_settings': self.behavior_settings.to_dict(),
            'tool_preferences': self.tool_preferences.to_dict()
        }

    async def save(self) -> None:
        """Save configuration using unified ConfigManager"""
        try:
            await self.config_manager.update_persona_config(
                self.persona_id,
                self.to_dict()
            )
            self.logger.info(f"Saved configuration for persona {self.persona_id}")

        except Exception as e:
            self.logger.error(f"Failed to save persona config: {e}")
            raise

    async def reload(self) -> None:
        """Reload configuration from unified ConfigManager"""
        try:
            updated_config = await self.config_manager.get_persona_config(self.persona_id)

            if updated_config:
                # Update current instance with new data
                updated_persona = self.from_dict(
                    self.persona_id, updated_config, self.config_manager
                )

                # Copy values to current instance
                self.capabilities = updated_persona.capabilities
                self.communication_style = updated_persona.communication_style
                self.model_preferences = updated_persona.model_preferences
                self.behavior_settings = updated_persona.behavior_settings
                self.tool_preferences = updated_persona.tool_preferences

                self.logger.debug(f"Reloaded configuration for persona {self.persona_id}")

        except Exception as e:
            self.logger.error(f"Failed to reload persona config: {e}")
            raise

    def get_model_config(self) -> Dict[str, Any]:
        """Get model configuration for API calls"""
        return {
            'temperature': self.model_preferences.temperature,
            'max_tokens': self.model_preferences.max_tokens,
            'top_p': self.model_preferences.top_p,
            'frequency_penalty': self.model_preferences.frequency_penalty,
            'presence_penalty': self.model_preferences.presence_penalty
        }

    def is_tool_preferred(self, tool_name: str) -> bool:
        """Check if tool is in preferred tools list"""
        return tool_name in self.tool_preferences.preferred_tools

    def is_tool_avoided(self, tool_name: str) -> bool:
        """Check if tool should be avoided"""
        return tool_name in self.tool_preferences.avoid_tools

    def has_capability(self, capability: str) -> bool:
        """Check if persona has specific capability"""
        return capability in self.capabilities

    def __str__(self) -> str:
        return f"PersonaConfig({self.persona_id}, {len(self.capabilities)} capabilities)"

    def __repr__(self) -> str:
        return f"PersonaConfig(id='{self.persona_id}', style={self.communication_style.value})"


# Alias for backward compatibility
# PersonaCapability alias - will be set after imports resolve
PersonaCapability = None  # Set in __init__.py


def validate_persona_config(config_data: Dict[str, Any]) -> List[str]:
    """Validate persona configuration and return list of errors"""
    errors = []

    try:
        PersonaConfig._validate_persona_data(config_data)
    except ValidationError as e:
        errors.append(str(e))
    except Exception as e:
        errors.append(f"Validation error: {e}")

    return errors


async def migrate_persona_config(
    old_config_data: Dict[str, Any],
    persona_id: str,
    config_manager: ConfigManager = None
) -> PersonaConfig:
    """Migrate old persona configuration to unified format"""
    config_manager = config_manager or get_config_manager()

    # Convert old format to new unified format
    unified_data = {
        'capabilities': old_config_data.get('capabilities', []),
        'communication_style': old_config_data.get('communication_style', 'technical'),
        'model_preferences': old_config_data.get('model_preferences', {}),
        'behavior_settings': old_config_data.get('behavior_settings', {}),
        'tool_preferences': old_config_data.get('tool_preferences', {})
    }

    # Create unified persona config
    persona_config = PersonaConfig.from_dict(persona_id, unified_data, config_manager)

    # Save to unified system
    await persona_config.save()

    logger.info(f"Migrated persona {persona_id} to unified configuration system")
    return persona_config