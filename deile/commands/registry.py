"""Registry para gerenciamento de comandos slash"""

import importlib
import inspect
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config.manager import CommandConfig
from .base import CommandContext, CommandResult, SlashCommand

logger = logging.getLogger(__name__)

_BUILTIN_PKG = "deile.commands.builtin"
_BUILTIN_DIR = Path(__file__).parent / "builtin"


class CommandRegistry:
    """Registry central para descoberta e gerenciamento de comandos slash

    Implementa o padrão Registry com auto-discovery configurável via YAML.
    """

    def __init__(self, config_manager=None):
        self.config_manager = config_manager

        # Storage interno
        self._commands: Dict[str, SlashCommand] = {}
        self._aliases: Dict[str, str] = {}  # alias -> command_name
        self._categories: Dict[str, List[SlashCommand]] = defaultdict(list)

        # Estatísticas
        self._registration_count = 0
        self._execution_count = 0
    
    def register_command(self, command: SlashCommand) -> None:
        """Registra comando no registry"""
        if not isinstance(command, SlashCommand):
            raise TypeError(f"Expected SlashCommand, got {type(command)}")

        command_name = command.name

        if command_name in self._commands:
            logger.warning("Command '%s' already registered, replacing", command_name)

        self._commands[command_name] = command

        for alias in command.aliases:
            if alias in self._aliases:
                logger.warning("Alias '%s' already exists, overwriting", alias)
            self._aliases[alias] = command_name

        category = getattr(command, "category", "general")
        self._categories[category].append(command)

        self._registration_count += 1
        logger.debug("Registered command: /%s", command_name)
    
    def unregister_command(self, name: str) -> bool:
        """Remove a command and its aliases from the registry. Returns True if removed."""
        if name not in self._commands:
            return False
        cmd = self._commands.pop(name)
        # Remove from aliases
        dead_aliases = [alias for alias, target in self._aliases.items() if target == name]
        for alias in dead_aliases:
            del self._aliases[alias]
        # Remove from categories
        category = getattr(cmd, "category", "general")
        cat_list = self._categories.get(category, [])
        try:
            cat_list.remove(cmd)
        except ValueError:
            pass
        logger.debug("Unregistered command: /%s", name)
        return True

    def get_command(self, command_name: str) -> Optional[SlashCommand]:

        """Obtém comando pelo nome ou alias (case-insensitive)."""
        # Nome exato
        if command_name in self._commands:
            return self._commands[command_name]

        # Alias exato
        if command_name in self._aliases:
            real_name = self._aliases[command_name]
            return self._commands.get(real_name)

        # Case-insensitive: nome canônico ou alias
        key = command_name.lower()
        for name, cmd in self._commands.items():
            if name.lower() == key:
                return cmd
        for alias, real_name in self._aliases.items():
            if alias.lower() == key:
                return self._commands.get(real_name)

        return None

    def has_command(self, command_name: str) -> bool:
        """True se *command_name* resolve para um comando (nome ou alias, qualquer casing)."""
        return self.get_command(command_name) is not None
    
    def get_enabled_commands(self) -> List[SlashCommand]:
        """Retorna apenas comandos habilitados"""
        return [cmd for cmd in self._commands.values() if cmd.enabled]
    
    def get_commands_by_category(self, category: str) -> List[SlashCommand]:
        """Retorna comandos por categoria"""
        return self._categories.get(category, [])
    
    def get_all_commands(self) -> List[SlashCommand]:
        """Retorna todos os comandos registrados"""
        return list(self._commands.values())
    
    def get_command_suggestions(self, partial: str) -> List[Dict[str, str]]:
        """Retorna sugestões de comandos para autocompletar"""
        suggestions = []
        partial_lower = partial.lower()
        
        # Busca em nomes de comando
        for cmd in self.get_enabled_commands():
            if cmd.name.lower().startswith(partial_lower):
                suggestions.append({
                    "name": cmd.name,
                    "display": f"/{cmd.name}",
                    "description": cmd.description,
                    "type": "command"
                })
        
        # Busca em aliases
        for alias, real_name in self._aliases.items():
            if alias.lower().startswith(partial_lower):
                cmd = self._commands[real_name]
                if cmd.enabled:
                    suggestions.append({
                        "name": alias,
                        "display": f"/{alias}",
                        "description": f"{cmd.description} (alias)",
                        "type": "alias"
                    })
        
        return suggestions
    
    async def execute_command(
        self, 
        command_name: str, 
        context: CommandContext
    ) -> CommandResult:
        """Executa comando pelo nome"""
        command = self.get_command(command_name)
        
        if not command:
            return CommandResult.error_result(
                f"Command '/{command_name}' not found",
                metadata={"available_commands": [cmd.name for cmd in self.get_enabled_commands()]}
            )
        
        if not command.enabled:
            return CommandResult.error_result(
                f"Command '/{command_name}' is disabled"
            )
        
        try:
            # Valida contexto
            if not await command.can_execute(context):
                return CommandResult.error_result(
                    f"Command '/{command_name}' cannot execute in current context"
                )
            
            # Valida argumentos
            validation_errors = await command.validate_args(context.args)
            if validation_errors:
                return CommandResult.error_result(
                    f"Invalid arguments: {', '.join(validation_errors)}"
                )
            
            # Executa comando
            import time
            start_time = time.time()
            
            result = await command.execute(context)
            
            execution_time = time.time() - start_time
            command._record_execution(execution_time)
            self._execution_count += 1
            
            # Adiciona metadados
            if result.execution_time is None:
                result.execution_time = execution_time
            result.metadata.update({
                "command_name": command_name,
                "execution_count": command.execution_count
            })
            
            return result
            
        except Exception as e:
            logger.error(f"Error executing command /{command_name}: {e}")
            return CommandResult.error_result(
                f"Error executing command: {str(e)}",
                error=e
            )
    
    def load_commands_from_config(self) -> int:
        """Carrega comandos da configuração YAML"""
        if not self.config_manager:
            return 0
        
        try:
            config = self.config_manager.get_config()
            loaded_count = 0
            
            for cmd_name, cmd_config in config.commands.items():
                if not cmd_config.enabled:
                    continue

                # Only LLM-template commands are config-driven; direct commands
                # are implemented as SlashCommand subclasses discovered via
                # auto_discover_builtin_commands().
                if not cmd_config.prompt_template:
                    continue

                command = self._create_llm_command(cmd_config)
                if command:
                    self.register_command(command)
                    loaded_count += 1
            
            logger.info(f"Loaded {loaded_count} commands from configuration")
            return loaded_count
            
        except Exception as e:
            logger.error(f"Error loading commands from config: {e}")
            return 0
    
    def auto_discover_builtin_commands(self) -> int:
        """Descobre comandos builtin automaticamente pelo filesystem.

        Itera ``deile/commands/builtin/*_command.py``, ignorando arquivos
        com prefixo ``_`` (helpers como ``_shared.py``, ``_status_collectors.py``).
        Substitui a lista hardcoded de 27 strings que drift-ava em relação ao
        filesystem (compact/skills/version/env commands estavam no disco mas
        não na lista pré-existente em ``builtin/__init__.py``).
        """
        try:
            discovered = 0
            for path in sorted(_BUILTIN_DIR.glob("*_command.py")):
                if path.stem.startswith("_"):
                    continue
                module_name = f"{_BUILTIN_PKG}.{path.stem}"
                try:
                    discovered += self._discover_in_module(module_name)
                except ImportError as exc:
                    logger.debug("Module %s not importable: %s", module_name, exc)
                except Exception as exc:
                    logger.warning("Error discovering in %s: %s", module_name, exc)
            return discovered
        except Exception as exc:
            logger.error("Auto-discovery failed: %s", exc)
            return 0
    
    def _discover_in_module(self, module_name: str) -> int:
        """Descobre comandos em módulo específico"""
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            return 0
        
        discovered = 0
        
        for name in dir(module):
            obj = getattr(module, name)
            if (
                inspect.isclass(obj) and
                issubclass(obj, SlashCommand) and
                obj != SlashCommand and
                not inspect.isabstract(obj)
            ):
                try:
                    # Instancia comando
                    command_instance = obj()
                    if command_instance.name not in self._commands:
                        self.register_command(command_instance)
                        discovered += 1
                except Exception as e:
                    logger.warning(f"Failed to instantiate command {name}: {e}")
        
        return discovered
    
    def _create_llm_command(self, config: CommandConfig) -> Optional[SlashCommand]:
        """Cria comando LLM baseado na configuração"""
        from .base import LLMCommand
        
        class ConfigLLMCommand(LLMCommand):
            def __init__(self, cmd_config):
                super().__init__(cmd_config)
            
            async def execute(self, context: CommandContext) -> CommandResult:
                prompt = self.get_prompt_for_llm(context.args)
                return CommandResult.success_result(
                    content=prompt,
                    content_type="llm_prompt",
                    command_name=self.name,
                    original_args=context.args
                )
        
        return ConfigLLMCommand(config)

    def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do registry"""
        return {
            "total_commands": len(self._commands),
            "enabled_commands": len(self.get_enabled_commands()),
            "total_aliases": len(self._aliases),
            "categories": len(self._categories),
            "registrations": self._registration_count,
            "total_executions": self._execution_count,
            "category_breakdown": {
                cat: len(cmds) for cat, cmds in self._categories.items()
            }
        }
    
    def clear(self) -> None:
        """Limpa todos os comandos registrados"""
        self._commands.clear()
        self._aliases.clear()
        self._categories.clear()
        logger.info("Cleared all commands from registry")
    
    def __len__(self) -> int:
        return len(self._commands)
    
    def __contains__(self, command_name: str) -> bool:
        return self.has_command(command_name)
    
    def __iter__(self):
        return iter(self._commands.values())


# Singleton instance
_command_registry: Optional[CommandRegistry] = None


def get_command_registry(config_manager=None) -> CommandRegistry:
    """Retorna instância singleton do CommandRegistry"""
    global _command_registry
    if _command_registry is None:
        _command_registry = CommandRegistry(config_manager)
    return _command_registry
