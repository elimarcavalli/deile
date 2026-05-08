"""Sistema avançado de plugins para DEILE 2.0 ULTRA

Sistema de plugins enterprise-grade com:
- Plugin lifecycle management
- Hot-reload capability
- Dependency resolution automática
- Plugin isolation skeleton (PluginSandbox does not isolate; see issue #54)
- Plugin marketplace integration
- Auto-discovery de plugins
"""

from .dependency_resolver import DependencyResolver
from .hot_loader import HotLoader
from .marketplace import PluginMarketplace
from .plugin_manager import PluginManager
from .sandbox import PluginSandbox

__all__ = [
    "PluginManager",
    "HotLoader",
    "DependencyResolver",
    "PluginSandbox",
    "PluginMarketplace"
]

__version__ = "2.0.0"