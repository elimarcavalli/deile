"""Sistema de configuração do DEILE"""

from .manager import (AgentConfig, CommandConfig, ConfigManager, DeileConfig,
                      GeminiConfig, SystemConfig, UIConfig, get_config_manager)
from .settings import Settings, get_settings

__all__ = [
    # Sistema legado
    "Settings",
    "get_settings",
    # Novo sistema
    "ConfigManager", 
    "DeileConfig", 
    "GeminiConfig", 
    "SystemConfig",
    "UIConfig",
    "AgentConfig", 
    "CommandConfig",
    "get_config_manager"
]