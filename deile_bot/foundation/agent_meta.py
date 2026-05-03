"""AgentMetaProvider — introspect DEILE tools/models/personas for capability reports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, List, Literal


@dataclass(frozen=True)
class ToolMeta:
    name: str
    description: str = ""
    risk_level: Literal["safe", "low", "medium", "high"] = "safe"
    requires_bot_context: bool = False


class AgentMetaProvider(ABC):
    @abstractmethod
    async def list_tools(self) -> List[ToolMeta]: ...

    @abstractmethod
    async def list_models(self) -> List[str]: ...

    @abstractmethod
    async def list_personas(self) -> List[str]: ...


class DeileAgentMetaProvider(AgentMetaProvider):
    """Concrete provider that introspects a real DeileAgent."""

    def __init__(self, agent: Any):
        self._agent = agent

    async def list_tools(self) -> List[ToolMeta]:
        try:
            registry = getattr(self._agent, "tool_registry", None)
            if registry is None:
                return []
            tools = []
            for tool in registry.list_enabled() if hasattr(registry, "list_enabled") else []:
                tools.append(
                    ToolMeta(
                        name=getattr(tool, "name", str(tool)),
                        description=getattr(tool, "description", "") or "",
                        risk_level="safe",
                        requires_bot_context=False,
                    )
                )
            return tools
        except Exception:
            return []

    async def list_models(self) -> List[str]:
        try:
            router = getattr(self._agent, "model_router", None)
            if router is None:
                return []
            providers = getattr(router, "providers", {})
            out: List[str] = []
            for prov_name, prov in providers.items():
                models = getattr(prov, "available_models", None)
                if models is None and hasattr(prov, "list_models"):
                    try:
                        models = await prov.list_models()
                    except Exception:
                        models = None
                if models is None:
                    out.append(f"{prov_name}:default")
                else:
                    for m in models:
                        out.append(f"{prov_name}:{m}")
            return out
        except Exception:
            return []

    async def list_personas(self) -> List[str]:
        try:
            mgr = getattr(self._agent, "persona_manager", None)
            if mgr is None:
                return []
            if hasattr(mgr, "list_personas"):
                return list(mgr.list_personas())
            personas = getattr(mgr, "personas", None) or {}
            return list(personas.keys()) if isinstance(personas, dict) else []
        except Exception:
            return []
