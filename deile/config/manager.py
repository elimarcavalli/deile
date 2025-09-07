"""Gerenciador central de configurações do DEILE"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Union
from pathlib import Path
import yaml
import json
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class FunctionCallingMode(Enum):
    """Modos de function calling"""
    AUTO = "AUTO"
    ANY = "ANY"
    NONE = "NONE"


@dataclass
class GeminiConfig:
    """Configurações específicas do modelo Gemini"""
    model_name: str = "gemini-1.5-pro-latest"
    
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
        "max_output_tokens": 8192,
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
        if max_tokens > 8192 or max_tokens <= 0:
            errors.append("max_output_tokens deve estar entre 1 e 8192")
        
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


class ConfigManager:
    """Gerenciador central de configurações"""
    
    def __init__(self, config_dir: Union[str, Path] = None):
        if config_dir is None:
            config_dir = Path("deile/config")
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        
        self._config: Optional[DeileConfig] = None
        self._config_files = {
            "api_config": self.config_dir / "api_config.yaml",
            "system_config": self.config_dir / "system_config.yaml", 
            "commands": self.config_dir / "commands.yaml"
        }
    
    def get_config(self) -> DeileConfig:
        """Obtém configuração atual (carrega se necessário)"""
        if self._config is None:
            self._config = self.load_config()
        return self._config
    
    def load_config(self) -> DeileConfig:
        """Carrega configurações de arquivos YAML"""
        try:
            # Carrega configuração da API
            api_config = self._load_yaml("api_config")
            
            # Carrega configuração do sistema
            system_config = self._load_yaml("system_config")
            
            # Carrega comandos
            commands_config = self._load_yaml("commands")
            
            # Constrói configuração completa
            config = DeileConfig()
            
            # Aplica configurações da API
            if api_config and "gemini" in api_config:
                config.gemini = GeminiConfig(**api_config["gemini"])
            
            # Aplica configurações do sistema
            if system_config:
                if "system" in system_config:
                    config.system = SystemConfig(**system_config["system"])
                if "ui" in system_config:
                    config.ui = UIConfig(**system_config["ui"])
                if "agent" in system_config:
                    config.agent = AgentConfig(**system_config["agent"])
            
            # Aplica comandos
            if commands_config and "commands" in commands_config:
                for cmd_name, cmd_data in commands_config["commands"].items():
                    config.commands[cmd_name] = CommandConfig(
                        name=cmd_name,
                        **cmd_data
                    )
            
            # Valida configuração
            errors = config.validate()
            if errors:
                logger.warning(f"Configuration validation errors: {errors}")
            
            logger.info("Configuration loaded successfully")
            return config
            
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")
            logger.info("Using default configuration")
            return self._create_default_config()
    
    def save_config(self, config: Optional[DeileConfig] = None) -> bool:
        """Salva configurações em arquivos YAML"""
        if config is None:
            config = self.get_config()
        
        try:
            # Salva configuração da API
            api_data = {
                "gemini": {
                    "model_name": config.gemini.model_name,
                    "tool_config": config.gemini.tool_config,
                    "generation_config": config.gemini.generation_config,
                    "safety_settings": config.gemini.safety_settings
                }
            }
            self._save_yaml("api_config", api_data)
            
            # Salva configuração do sistema
            system_data = {
                "system": {
                    "debug_mode": config.system.debug_mode,
                    "log_level": config.system.log_level,
                    "log_requests": config.system.log_requests,
                    "log_responses": config.system.log_responses,
                    "session_timeout": config.system.session_timeout,
                    "auto_save_sessions": config.system.auto_save_sessions
                },
                "ui": {
                    "theme": config.ui.theme,
                    "show_timestamps": config.ui.show_timestamps,
                    "auto_complete": config.ui.auto_complete,
                    "emoji_support": config.ui.emoji_support,
                    "rich_formatting": config.ui.rich_formatting
                },
                "agent": {
                    "max_context_tokens": config.agent.max_context_tokens,
                    "context_optimization": config.agent.context_optimization,
                    "auto_discover_tools": config.agent.auto_discover_tools,
                    "auto_discover_parsers": config.agent.auto_discover_parsers,
                    "rag_enabled": config.agent.rag_enabled
                }
            }
            self._save_yaml("system_config", system_data)
            
            # Salva comandos
            commands_data = {
                "commands": {}
            }
            for cmd_name, cmd_config in config.commands.items():
                commands_data["commands"][cmd_name] = {
                    "description": cmd_config.description,
                    "prompt_template": cmd_config.prompt_template,
                    "action": cmd_config.action,
                    "aliases": cmd_config.aliases,
                    "enabled": cmd_config.enabled
                }
            self._save_yaml("commands", commands_data)
            
            logger.info("Configuration saved successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error saving configuration: {e}")
            return False
    
    def update_debug_mode(self, enabled: bool) -> None:
        """Atualiza modo debug e salva configuração"""
        config = self.get_config()
        config.system.debug_mode = enabled
        config.system.log_requests = enabled
        config.system.log_responses = enabled
        config.system.log_level = "DEBUG" if enabled else "INFO"
        
        self.save_config(config)
        self._config = config  # Atualiza cache
        
        logger.info(f"Debug mode {'enabled' if enabled else 'disabled'}")
    
    def update_gemini_config(self, **kwargs) -> None:
        """Atualiza configuração do Gemini"""
        config = self.get_config()
        
        # Atualiza generation_config
        if "generation_config" in kwargs:
            config.gemini.generation_config.update(kwargs["generation_config"])
        
        # Atualiza tool_config
        if "tool_config" in kwargs:
            config.gemini.tool_config.update(kwargs["tool_config"])
        
        # Atualiza outras configurações
        for key, value in kwargs.items():
            if hasattr(config.gemini, key):
                setattr(config.gemini, key, value)
        
        self.save_config(config)
        self._config = config
    
    def reload_config(self) -> None:
        """Recarrega configuração dos arquivos"""
        self._config = None
        self.get_config()
    
    def create_default_configs(self) -> None:
        """Cria arquivos de configuração padrão se não existem"""
        default_config = self._create_default_config()
        
        for config_file in self._config_files.values():
            if not config_file.exists():
                logger.info(f"Creating default config file: {config_file}")
        
        self.save_config(default_config)
    
    def _load_yaml(self, config_name: str) -> Optional[Dict[str, Any]]:
        """Carrega arquivo YAML específico"""
        file_path = self._config_files.get(config_name)
        if not file_path or not file_path.exists():
            return None
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Error loading {config_name}: {e}")
            return None
    
    def _save_yaml(self, config_name: str, data: Dict[str, Any]) -> None:
        """Salva dados em arquivo YAML"""
        file_path = self._config_files.get(config_name)
        if not file_path:
            return
        
        with open(file_path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, indent=2)
    
    def _create_default_config(self) -> DeileConfig:
        """Cria configuração padrão"""
        config = DeileConfig()
        
        # Comandos padrão
        default_commands = {
            "help": CommandConfig(
                name="help",
                description="Lista comandos disponíveis e exemplos de uso",
                action="show_help"
            ),
            "exit": CommandConfig(
                name="exit",
                description="Sair do DEILE Agent",
                action="exit_application",
                aliases=["quit", "bye"]
            ),
            "status": CommandConfig(
                name="status", 
                description="Mostra versão, modelo ativo, conectividade e diagnóstico",
                action="show_system_status",
                aliases=["info"]
            ),
            "model": CommandConfig(
                name="model",
                description="Trocar ou selecionar modelo de IA",
                prompt_template="Por favor, liste os modelos disponíveis e permita que eu escolha qual usar. Configuração atual: {current_model}. Se argumentos foram fornecidos ({args}), use-os para selecionar o modelo.",
                action="manage_models",
                aliases=["ai", "llm"]
            ),
            "clear": CommandConfig(
                name="clear",
                description="Limpar histórico da conversa e tela",
                action="clear_session",
                aliases=["cls", "clean"]
            ),
            "bash": CommandConfig(
                name="bash",
                description="Executar comando bash no sistema",
                prompt_template="Execute o seguinte comando bash de forma segura: {args}. Mostre o resultado da execução incluindo stdout, stderr e código de saída. Se houver erro, explique o que pode ter causado.",
                action="execute_bash",
                aliases=["sh", "cmd", "run", "$"]
            ),
            "debug": CommandConfig(
                name="debug",
                description="Toggle do modo debug (logs detalhados + request/response files)",
                action="toggle_debug_mode",
                aliases=["dbg", "verbose"]
            ),
            "config": CommandConfig(
                name="config",
                description="Mostrar configurações atuais do sistema",
                action="show_config",
                aliases=["settings", "cfg"]
            )
        }
        
        config.commands = default_commands
        return config
    
    def get_command_config(self, command_name: str) -> Optional[CommandConfig]:
        """Obtém configuração de um comando específico"""
        config = self.get_config()
        
        # Busca por nome direto
        if command_name in config.commands:
            return config.commands[command_name]
        
        # Busca por alias
        for cmd_config in config.commands.values():
            if command_name in cmd_config.aliases:
                return cmd_config
        
        return None
    
    def get_enabled_commands(self) -> List[CommandConfig]:
        """Retorna lista de comandos habilitados"""
        config = self.get_config()
        return [cmd for cmd in config.commands.values() if cmd.enabled]


# Singleton instance
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """Retorna instância singleton do ConfigManager"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
        _config_manager.create_default_configs()  # Cria configs padrão se necessário
    return _config_manager