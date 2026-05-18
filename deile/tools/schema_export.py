"""Export de schemas de tools nos formatos dos providers LLM.

Extraído de :class:`~deile.tools.registry.ToolRegistry` (SRP): a tradução
de ``ToolSchema`` para os formatos Gemini/Anthropic/OpenAI depende apenas
de iterar as tools registradas — não do estado de registro/descoberta. O
registry expõe métodos finos que delegam para estas funções, no mesmo
padrão já adotado por ``schema_validation.py``.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterator, List, Mapping, Optional, Set

from .base import SecurityLevel, Tool

logger = logging.getLogger(__name__)

_SECURITY_HIERARCHY = {
    SecurityLevel.SAFE: 0,
    SecurityLevel.MODERATE: 1,
    SecurityLevel.DANGEROUS: 2,
}


def is_security_level_allowed(
    tool_level: SecurityLevel, max_level: SecurityLevel
) -> bool:
    """Verifica se o nível de segurança da tool está dentro do máximo permitido."""
    return _SECURITY_HIERARCHY[tool_level] <= _SECURITY_HIERARCHY[max_level]


def iter_authorized_tools(
    tools: Mapping[str, Tool],
    enabled: Set[str],
    authorized_only: bool,
    security_level: Optional[SecurityLevel],
) -> Iterator[Tool]:
    """Itera as tools que passam pelos filtros de autorização e segurança.

    Ponto único da lógica de filtragem compartilhada pelos exportadores
    por-provider.
    """
    for tool_name, tool in tools.items():
        if authorized_only and tool_name not in enabled:
            continue
        if security_level and tool.schema:
            if not is_security_level_allowed(
                tool.schema.security_level, security_level
            ):
                continue
        yield tool


def get_gemini_functions(
    tools: Mapping[str, Tool],
    enabled: Set[str],
    authorized_only: bool = True,
    security_level: Optional[SecurityLevel] = None,
) -> List:
    """Retorna tools no formato FunctionDeclaration para o Google GenAI SDK."""
    functions = []
    for tool in iter_authorized_tools(
        tools, enabled, authorized_only, security_level
    ):
        function_def = tool.get_function_definition()
        if function_def:
            functions.append(function_def)

    logger.debug(f"Generated {len(functions)} function definitions for Gemini API")
    return functions


def get_anthropic_tools(
    tools: Mapping[str, Tool],
    enabled: Set[str],
    authorized_only: bool = True,
    security_level: Optional[SecurityLevel] = None,
) -> List[Dict]:
    """Retorna tools no formato Anthropic tool_use."""
    return [
        tool.schema.to_anthropic_tool()
        for tool in iter_authorized_tools(
            tools, enabled, authorized_only, security_level
        )
        if tool.schema
    ]


def get_openai_functions(
    tools: Mapping[str, Tool],
    enabled: Set[str],
    authorized_only: bool = True,
    security_level: Optional[SecurityLevel] = None,
) -> List[Dict]:
    """Retorna tools no formato function_call da OpenAI / DeepSeek."""
    return [
        tool.schema.to_openai_function()
        for tool in iter_authorized_tools(
            tools, enabled, authorized_only, security_level
        )
        if tool.schema
    ]
