"""Registry para gerenciamento de comandos slash"""

from typing import Dict, List, Optional, Type, Any, Callable
import asyncio
import inspect
import importlib
import logging
from collections import defaultdict

from .base import SlashCommand, CommandContext, CommandResult
from ..config.manager import CommandConfig


logger = logging.getLogger(__name__)


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
        
        # Actions (funções que implementam comandos)
        self._actions: Dict[str, Callable] = {}
        
        # Estatísticas
        self._registration_count = 0
        self._execution_count = 0
    
    def register_command(self, command: SlashCommand) -> None:
        """Registra comando no registry"""
        if not isinstance(command, SlashCommand):
            raise TypeError(f"Expected SlashCommand, got {type(command)}")
        
        command_name = command.name
        
        if command_name in self._commands:
            logger.warning(f"Command '{command_name}' already registered, replacing")
        
        # Registra comando
        self._commands[command_name] = command
        
        # Registra aliases
        for alias in command.aliases:
            if alias in self._aliases:
                logger.warning(f"Alias '{alias}' already exists, overwriting")
            self._aliases[alias] = command_name
        
        # Categorização automática
        category = getattr(command, 'category', 'general')
        self._categories[category].append(command)
        
        self._registration_count += 1
        logger.debug(f"Registered command: /{command_name}")
    
    def register_action(self, name: str, action: Callable) -> None:
        """Registra função de ação"""
        self._actions[name] = action
        logger.debug(f"Registered action: {name}")
    
    def get_command(self, command_name: str) -> Optional[SlashCommand]:
        """Obtém comando pelo nome ou alias"""
        # Tenta nome direto
        if command_name in self._commands:
            return self._commands[command_name]
        
        # Tenta alias
        if command_name in self._aliases:
            real_name = self._aliases[command_name]
            return self._commands.get(real_name)
        
        return None
    
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
            if cmd.name.startswith(partial_lower):
                suggestions.append({
                    "name": cmd.name,
                    "display": f"/{cmd.name}",
                    "description": cmd.description,
                    "type": "command"
                })
        
        # Busca em aliases
        for alias, real_name in self._aliases.items():
            if alias.startswith(partial_lower):
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
                
                # Cria comando baseado na configuração
                if cmd_config.prompt_template:
                    # Comando LLM
                    command = self._create_llm_command(cmd_config)
                else:
                    # Comando direto
                    command = self._create_direct_command(cmd_config)
                
                if command:
                    self.register_command(command)
                    loaded_count += 1
            
            logger.info(f"Loaded {loaded_count} commands from configuration")
            return loaded_count
            
        except Exception as e:
            logger.error(f"Error loading commands from config: {e}")
            return 0
    
    def auto_discover_builtin_commands(self) -> int:
        """Descobre comandos builtin automaticamente"""
        try:
            discovered = 0
            
            # Lista de módulos builtin para descobrir
            builtin_modules = [
                'deile.commands.builtin.help_command',
                'deile.commands.builtin.debug_command',
                'deile.commands.builtin.clear_command',
                'deile.commands.builtin.status_command',
                'deile.commands.builtin.config_command',
                'deile.commands.builtin.context_command',
                'deile.commands.builtin.cost_command',
                'deile.commands.builtin.tools_command',
                'deile.commands.builtin.model_command',
                'deile.commands.builtin.export_command',
                'deile.commands.builtin.stop_command',
                'deile.commands.builtin.diff_command',
                'deile.commands.builtin.patch_command',
                'deile.commands.builtin.apply_command',
                'deile.commands.builtin.memory_command',
                'deile.commands.builtin.logs_command',
                'deile.commands.builtin.permissions_command',
                'deile.commands.builtin.sandbox_command'
            ]
            
            for module_name in builtin_modules:
                try:
                    discovered += self._discover_in_module(module_name)
                except ImportError:
                    logger.debug(f"Module {module_name} not found for auto-discovery")
                except Exception as e:
                    logger.warning(f"Error discovering in {module_name}: {e}")
            
            return discovered
            
        except Exception as e:
            logger.error(f"Auto-discovery failed: {e}")
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
    
    def _create_direct_command(self, config: CommandConfig) -> Optional[SlashCommand]:
        """Cria comando direto baseado na configuração"""
        from .base import DirectCommand
        
        # Procura action correspondente
        action_func = self._actions.get(config.action)
        if not action_func:
            logger.warning(f"Action '{config.action}' not found for command '{config.name}'")
            return None
        
        class ConfigDirectCommand(DirectCommand):
            def __init__(self, cmd_config, action):
                super().__init__(cmd_config)
                self.action_func = action
            
            async def execute(self, context: CommandContext) -> CommandResult:
                try:
                    # Executa action
                    if asyncio.iscoroutinefunction(self.action_func):
                        return await self.action_func(context.args, context)
                    else:
                        return self.action_func(context.args, context)
                except Exception as e:
                    return CommandResult.error_result(
                        f"Error in action '{config.action}': {str(e)}",
                        error=e
                    )
        
        return ConfigDirectCommand(config, action_func)
    
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
        self._actions.clear()
        logger.info("Cleared all commands from registry")
    
    def __len__(self) -> int:
        return len(self._commands)
    
    def __contains__(self, command_name: str) -> bool:
        return command_name in self._commands or command_name in self._aliases
    
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


class CommandRegistry:
    """Simple static command registry for backward compatibility with builtin commands"""
    
    _commands: Dict[str, Any] = {}
    
    @classmethod
    def register(cls, name: str, command_class: Type) -> None:
        """Register a command class with a name"""
        cls._commands[name] = command_class
        logger.debug(f"Registered command class: {name}")
    
    @classmethod
    def get_command_class(cls, name: str) -> Optional[Type]:
        """Get a command class by name"""
        return cls._commands.get(name)
    
    @classmethod
    def get_all_command_names(cls) -> List[str]:
        """Get all registered command names"""
        return list(cls._commands.keys())
    
    @classmethod
    def clear(cls) -> None:
        """Clear all registered commands"""
        cls._commands.clear()