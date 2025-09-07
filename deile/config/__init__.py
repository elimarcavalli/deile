"""Sistema de configuração do DEILE"""

from .settings import Settings, get_settings
from .manager import (
    ConfigManager, 
    DeileConfig, 
    GeminiConfig, 
    SystemConfig,
    UIConfig,
    AgentConfig,
    CommandConfig,
    get_config_manager
)

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