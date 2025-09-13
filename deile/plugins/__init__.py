"""Sistema avançado de plugins para DEILE 2.0 ULTRA

Sistema de plugins enterprise-grade com:
- Plugin lifecycle management
- Hot-reload capability
- Dependency resolution automática
- Plugin isolation sandbox
- Plugin marketplace integration
- Auto-discovery de plugins
"""

from .plugin_manager import PluginManager
from .hot_loader import HotLoader
from .dependency_resolver import DependencyResolver
from .sandbox import PluginSandbox
from .marketplace import PluginMarketplace

__all__ = [
    "PluginManager",
    "HotLoader",
    "DependencyResolver",
    "PluginSandbox",
    "PluginMarketplace"
]

__version__ = "2.0.0"