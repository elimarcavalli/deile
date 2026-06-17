"""Modelos de dados do sistema de configuração do DEILE.

Este módulo contém exclusivamente os tipos de dados (enums e dataclasses)
usados pelo ConfigManager. Não possui dependências externas ao stdlib.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class FunctionCallingMode(Enum):
    """Modos de function calling"""
    AUTO = "AUTO"
    ANY = "ANY"
    NONE = "NONE"


@dataclass
class GeminiConfig:
    """Configurações específicas do modelo Gemini"""
    model_name: str = "gemini-2.5-flash-lite"

    # Tool configuration
    tool_config: Dict[str, Any] = field(default_factory=lambda: {
        "function_calling_config": {
            "mode": "AUTO"
        }
    })

    # Generation configuration
    generation_config: Dict[str, Any] = field(default_factory=lambda: {
        "temperature": 0.1,
        "top_k": 32,
        "top_p": 0.9,
        "max_output_tokens": 16384,
        "candidate_count": 1,
        "stop_sequences": []
    })

    # Safety settings
    safety_settings: List[Dict[str, Any]] = field(default_factory=lambda: [
        {
            "category": "HARM_CATEGORY_HARASSMENT",
            "threshold": "BLOCK_MEDIUM_AND_ABOVE"
        },
        {
            "category": "HARM_CATEGORY_HATE_SPEECH",
            "threshold": "BLOCK_MEDIUM_AND_ABOVE"
        },
        {
            "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "threshold": "BLOCK_MEDIUM_AND_ABOVE"
        },
        {
            "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
            "threshold": "BLOCK_MEDIUM_AND_ABOVE"
        }
    ])

    def validate(self) -> List[str]:
        """Valida configurações do Gemini"""
        errors = []

        # Valida temperature
        temp = self.generation_config.get("temperature", 0)
        if not (0 <= temp <= 2):
            errors.append("Temperature deve estar entre 0 e 2")

        # Valida max_output_tokens
        max_tokens = self.generation_config.get("max_output_tokens", 0)
        if max_tokens > 65536 or max_tokens <= 0:
            errors.append("max_output_tokens deve estar entre 1 e 65536")

        # Valida top_k
        top_k = self.generation_config.get("top_k", 1)
        if top_k <= 0 or top_k > 100:
            errors.append("top_k deve estar entre 1 e 100")

        # Valida function calling mode
        mode = self.tool_config.get("function_calling_config", {}).get("mode", "AUTO")
        valid_modes = [m.value for m in FunctionCallingMode]
        if mode not in valid_modes:
            errors.append(f"Function calling mode deve ser um de: {valid_modes}")

        return errors


@dataclass
class SystemConfig:
    """Configurações do sistema"""
    debug_mode: bool = False
    log_level: str = "INFO"
    log_requests: bool = False
    log_responses: bool = False
    session_timeout: int = 3600
    auto_save_sessions: bool = True


@dataclass
class UIConfig:
    """Configurações da interface do usuário"""
    theme: str = "default"
    show_timestamps: bool = True
    auto_complete: bool = True
    emoji_support: bool = True
    rich_formatting: bool = True


@dataclass
class AgentConfig:
    """Configurações do agente"""
    max_context_tokens: int = 8000
    context_optimization: bool = True
    auto_discover_tools: bool = True
    auto_discover_parsers: bool = True
    rag_enabled: bool = False


@dataclass
class CommandConfig:
    """Configuração de um comando slash"""
    name: str
    description: str
    prompt_template: Optional[str] = None
    action: str = ""
    aliases: List[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class DeileConfig:
    """Configuração completa do DEILE"""
    default_model: Optional[str] = None  # e.g. "deepseek:deepseek-v4-pro"; None = routing automático
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    system: SystemConfig = field(default_factory=SystemConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    commands: Dict[str, CommandConfig] = field(default_factory=dict)

    def validate(self) -> List[str]:
        """Valida toda a configuração"""
        errors = []

        # Valida configuração do Gemini
        errors.extend(self.gemini.validate())

        # Valida configurações do sistema
        if self.system.session_timeout <= 0:
            errors.append("session_timeout deve ser positivo")

        # Valida configurações do agente
        if self.agent.max_context_tokens <= 0:
            errors.append("max_context_tokens deve ser positivo")

        return errors
