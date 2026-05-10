"""Ações para comandos slash do DEILE"""

import asyncio
import logging
import sys
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from deile.config.settings import get_settings

from ..tools.base import ToolContext
from ..tools.execution_tools import EnhancedExecutionTool
from .base import CommandContext, CommandResult

logger = logging.getLogger(__name__)


class CommandActions:
    """Container para todas as ações de comandos slash"""
    
    def __init__(self, agent=None, ui_manager=None, config_manager=None):
        self.agent = agent
        self.ui_manager = ui_manager
        self.config_manager = config_manager
        self.settings = get_settings()
        self.console = Console()
        
        # Tools que podem ser usadas pelas ações
        self.execution_tool = EnhancedExecutionTool()
    
    async def show_help(self, args: str, context: CommandContext) -> CommandResult:
        """Ação para /help - mostra ajuda dos comandos"""
        try:
            from .registry import get_command_registry
            registry = get_command_registry(self.config_manager)
            
            if args.strip():
                # Ajuda específica para um comando - INCLUI aliases
                command = registry.get_command(args.strip())
                if command:
                    help_content = await command.get_help()
                    
                    # Adiciona informação de aliases se existirem
                    aliases_info = ""
                    if hasattr(command, 'aliases') and command.aliases:
                        aliases_str = ", ".join([f"/{alias}" for alias in command.aliases])
                        aliases_info = f"\n\n**Aliases:** {aliases_str}"
                    elif hasattr(command.config, 'aliases') and command.config.aliases:
                        aliases_str = ", ".join([f"/{alias}" for alias in command.config.aliases])
                        aliases_info = f"\n\n**Aliases:** {aliases_str}"
                    
                    # Combina help original com aliases
                    full_help = help_content + aliases_info
                    
                    panel = Panel(
                        full_help,
                        title=f"[bold cyan]Help: /{command.name}[/bold cyan]",
                        border_style="cyan"
                    )
                    return CommandResult.success_result(panel, "rich")
                else:
                    return CommandResult.error_result(f"Command '/{args.strip()}' not found")
            
            # Help geral - lista todos os comandos (SEM aliases)
            table = Table(title="📚 DEILE Commands (Main Names Only)", box=box.ROUNDED)
            table.add_column("Command", style="cyan", width=15)
            table.add_column("Description", style="white", width=40)
            table.add_column("Type", style="yellow", width=10)
            
            for command in registry.get_enabled_commands():
                cmd_type = "LLM" if command.has_prompt_template else "Direct"
                table.add_row(
                    f"/{command.name}",
                    command.description,
                    cmd_type
                )
            
            # Adiciona informações extras
            footer_text = Text()
            footer_text.append("\n💡 ", style="yellow")
            footer_text.append("Use '/help <comando>' para ajuda específica e aliases\n", style="dim")
            footer_text.append("📝 ", style="blue")
            footer_text.append("Digite '@' para autocompletar arquivos\n", style="dim")
            footer_text.append("🔧 ", style="green") 
            footer_text.append("Digite '/' para ver comandos disponíveis\n", style="dim")
            footer_text.append("🏷️ ", style="magenta")
            footer_text.append("Apenas nomes principais mostrados (aliases via /help <cmd>)", style="dim")
            
            # Combina table e footer em um painel
            from rich.console import Group
            content_group = Group(table, footer_text)
            help_panel = Panel(
                content_group,
                title="[bold cyan]DEILE Commands[/bold cyan]",
                border_style="cyan"
            )
            
            return CommandResult.success_result(
                help_panel, 
                "rich",
                total_commands=len(registry.get_enabled_commands())
            )
            
        except Exception as e:
            logger.error(f"Error in show_help: {e}")
            return CommandResult.error_result(f"Error showing help: {str(e)}", error=e)
    
    async def exit_application(self, args: str, context: CommandContext) -> CommandResult:
        """Ação para /exit - sair do aplicativo"""
        try:
            # Mostra mensagem de despedida
            goodbye_panel = Panel(
                Text("👋 Obrigado por usar o DEILE!\n\nSessão encerrada com sucesso.", justify="center"),
                title="[bold blue]Goodbye[/bold blue]",
                border_style="blue"
            )
            
            # Agenda saída após mostrar mensagem
            async def delayed_exit():
                await asyncio.sleep(1)
                sys.exit(0)
            
            asyncio.create_task(delayed_exit())
            
            return CommandResult.success_result(
                goodbye_panel, 
                "rich",
                exit_requested=True
            )
            
        except Exception as e:
            logger.error(f"Error in exit_application: {e}")
            return CommandResult.error_result(f"Error exiting: {str(e)}", error=e)
    
    async def show_system_status(self, args: str, context: CommandContext) -> CommandResult:
        """Ação para /status - mostra status do sistema"""
        try:
            # Coleta informações do sistema
            status_data = {}
            
            # Informações básicas
            status_data["version"] = "4.0.0"
            status_data["python_version"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            status_data["working_directory"] = self.settings.working_directory #str(Path.cwd())
            
            # Status do agente (se disponível)
            if self.agent:
                try:
                    agent_stats = await self.agent.get_stats()
                    status_data.update(agent_stats)
                except Exception:
                    status_data["agent_status"] = "unavailable"
            
            # Status da configuração
            if self.config_manager:
                config = self.config_manager.get_config()
                status_data["debug_mode"] = config.system.debug_mode
                status_data["model_name"] = config.gemini.model_name
                status_data["temperature"] = config.gemini.generation_config.get("temperature", "unknown")
            
            # Conectividade (teste básico)
            try:
                import socket
                socket.create_connection(("8.8.8.8", 53), timeout=3)
                status_data["connectivity"] = "✅ Online"
            except Exception:
                status_data["connectivity"] = "❌ Offline"
            
            # Cria tabela rica
            table = Table(title="🔍 DEILE System Status", box=box.ROUNDED)
            table.add_column("Metric", style="cyan", width=20)
            table.add_column("Value", style="green", width=60)
            
            for key, value in status_data.items():
                # Formata chaves bonitas
                display_key = key.replace('_', ' ').title()
                table.add_row(display_key, str(value))
            
            return CommandResult.success_result(
                table, 
                "rich",
                status_data=status_data
            )
            
        except Exception as e:
            logger.error(f"Error in show_system_status: {e}")
            return CommandResult.error_result(f"Error getting status: {str(e)}", error=e)
    
    async def clear_session(self, args: str, context: CommandContext) -> CommandResult:
        """Ação para /clear - limpa sessão e tela, /cls reset para reset completo"""
        try:
            is_reset = args.strip().lower() == "reset"
            
            if is_reset:
                # RESET COMPLETO da sessão
                # Limpa histórico da sessão
                if context.session:
                    if hasattr(context.session, 'conversation_history'):
                        context.session.conversation_history.clear()
                    if hasattr(context.session, 'context_data'):
                        context.session.context_data.clear()
                    if hasattr(context.session, 'memory'):
                        context.session.memory.clear()
                    if hasattr(context.session, 'tokens'):
                        context.session.tokens = 0
                    if hasattr(context.session, 'cost'):
                        context.session.cost = 0.0
                
                # Limpa planos ativos se disponível
                try:
                    from ..orchestration.plan_manager import get_plan_manager
                    await get_plan_manager().clear_active_state()
                except Exception:
                    pass  # Se não conseguir acessar plan manager, continua

                # Limpa logs de auditoria em memória
                try:
                    from ..security.audit_logger import get_audit_logger
                    get_audit_logger().clear_events()
                except Exception:
                    pass

                # Limpa tela
                if self.ui_manager:
                    self.ui_manager.console.clear()
                    if hasattr(self.ui_manager, 'show_welcome'):
                        self.ui_manager.show_welcome()
                else:
                    print("\033c", end="", flush=True)
                
                # Mensagem de reset completo
                reset_panel = Panel(
                    Text("🔄 **RESET COMPLETO REALIZADO**\n\n"
                         "✅ Histórico de conversa limpo\n"
                         "✅ Dados de contexto removidos\n"
                         "✅ Memória de sessão resetada\n"
                         "✅ Contadores de tokens zerados\n"
                         "✅ Planos ativos parados\n"
                         "✅ Logs de auditoria em memória limpos\n"
                         "✅ Tela limpa\n\n"
                         "🚀 **Sessão completamente reiniciada!**\n"
                         "Digite '/' para ver comandos disponíveis.", 
                         justify="left"),
                    title="[bold green]Session Reset[/bold green]",
                    border_style="green"
                )
                return CommandResult.success_result(reset_panel, "rich", session_reset=True)
                
            else:
                # Clear normal (apenas histórico e tela)
                if context.session:
                    if hasattr(context.session, 'conversation_history'):
                        context.session.conversation_history.clear()
                    if hasattr(context.session, 'context_data'):
                        context.session.context_data.clear()
                
                # Limpa tela via UI manager
                if self.ui_manager:
                    self.ui_manager.console.clear()
                    # Reexibe welcome se disponível
                    if hasattr(self.ui_manager, 'show_welcome'):
                        self.ui_manager.show_welcome()
                else:
                    print("\033c", end="", flush=True)
            
            # Mensagem de confirmação
            success_panel = Panel(
                Text("✨ Sessão limpa com sucesso!\n\n• Histórico de conversa removido\n• Tela reinicializada\n• Cache de contexto limpo", justify="center"),
                title="[bold green]Session Cleared[/bold green]",
                border_style="green"
            )
            
            return CommandResult.success_result(
                success_panel, 
                "rich",
                session_cleared=True
            )
            
        except Exception as e:
            logger.error(f"Error in clear_session: {e}")
            return CommandResult.error_result(f"Error clearing session: {str(e)}", error=e)
    
    async def toggle_debug_mode(self, args: str, context: CommandContext) -> CommandResult:
        """Ação para /debug - toggle modo debug"""
        try:
            if not self.config_manager:
                return CommandResult.error_result("Configuration manager not available")
            
            current_config = self.config_manager.get_config()
            current_debug = current_config.system.debug_mode
            
            # Toggle debug mode
            new_debug_state = not current_debug
            self.config_manager.update_debug_mode(new_debug_state)
            
            # Atualiza logging level em runtime
            if new_debug_state:
                logging.getLogger().setLevel(logging.DEBUG)
                
                # Cria diretório de debug
                debug_dir = Path("logs/debug")
                debug_dir.mkdir(parents=True, exist_ok=True)
                
                panel = Panel(
                    Text.from_markup(
                        "[green]✅ Debug Mode ATIVADO[/green]\n\n"
                        "📝 Logs detalhados: [cyan]logs/deile.log[/cyan]\n"
                        "📥 Request logs: [cyan]logs/debug/request_*.json[/cyan]\n" 
                        "📤 Response logs: [cyan]logs/debug/response_*.json[/cyan]\n"
                        "🔍 Debug info: [cyan]logs/debug/debug_*.json[/cyan]\n\n"
                        "[dim]Use '/debug' novamente para desativar[/dim]"
                    ),
                    title="🐛 Debug System",
                    border_style="green"
                )
            else:
                logging.getLogger().setLevel(logging.INFO)
                
                panel = Panel(
                    Text.from_markup(
                        "[yellow]⚠️ Debug Mode DESATIVADO[/yellow]\n\n"
                        "📝 Apenas logs essenciais serão mantidos\n"
                        "🗑️ Logs de request/response pausados\n\n"
                        "[dim]Use '/debug' novamente para reativar[/dim]"
                    ),
                    title="🐛 Debug System", 
                    border_style="yellow"
                )
            
            return CommandResult.success_result(
                panel, 
                "rich",
                debug_mode=new_debug_state,
                previous_state=current_debug
            )
            
        except Exception as e:
            logger.error(f"Error in toggle_debug_mode: {e}")
            return CommandResult.error_result(f"Error toggling debug: {str(e)}", error=e)
    
    async def execute_bash(self, args: str, context: CommandContext) -> CommandResult:
        """Ação para /bash - executa comando bash"""
        try:
            if not args.strip():
                return CommandResult.error_result(
                    "Nenhum comando bash fornecido.\nUso: /bash <comando>"
                )
            
            # Usa ExecutionTool existente
            tool_context = ToolContext(
                user_input=f"execute: {args}",
                parsed_args={
                    "command": args.strip(),
                    "timeout": 30,
                    "allow_dangerous": False  # Segurança por padrão
                },
                working_directory=context.working_directory
            )
            
            # Executa o comando
            result = self.execution_tool.execute_sync(tool_context)
            
            if result.is_success:
                # Formata output com Rich
                output_text = str(result.data) if result.data else "No output"
                
                panel = Panel(
                    Text(f"$ {args}\n\n{output_text}", style="white"),
                    title="🖥️ Bash Output",
                    border_style="green" if result.is_success else "red"
                )
                
                return CommandResult.success_result(
                    panel, 
                    "rich",
                    command=args,
                    exit_code=0,
                    output=output_text,
                    execution_time=result.execution_time
                )
            else:
                # Erro na execução
                error_panel = Panel(
                    Text(f"$ {args}\n\nError: {result.message}", style="red"),
                    title="❌ Bash Error",
                    border_style="red"
                )
                
                return CommandResult.error_result(
                    error_panel,
                    error=result.error,
                    command=args
                )
            
        except Exception as e:
            logger.error(f"Error in execute_bash: {e}")
            return CommandResult.error_result(f"Error executing bash: {str(e)}", error=e)
    
    async def show_config(self, args: str, context: CommandContext) -> CommandResult:
        """Ação para /config - mostra configurações atuais"""
        try:
            if not self.config_manager:
                return CommandResult.error_result("Configuration manager not available")
            
            config = self.config_manager.get_config()
            
            # Cria tabelas para diferentes seções
            tables = []
            
            # Configuração do Sistema
            system_table = Table(title="🔧 System Configuration", box=box.ROUNDED)
            system_table.add_column("Setting", style="cyan")
            system_table.add_column("Value", style="green")
            
            system_table.add_row("Debug Mode", "✅ Enabled" if config.system.debug_mode else "❌ Disabled")
            system_table.add_row("Log Level", config.system.log_level)
            system_table.add_row("Log Requests", "✅ Yes" if config.system.log_requests else "❌ No")
            system_table.add_row("Log Responses", "✅ Yes" if config.system.log_responses else "❌ No")
            
            tables.append(system_table)
            
            # Configuração do Gemini
            gemini_table = Table(title="🤖 Gemini Configuration", box=box.ROUNDED)
            gemini_table.add_column("Parameter", style="cyan")
            gemini_table.add_column("Value", style="green")
            
            gemini_table.add_row("Model", config.gemini.model_name)
            gemini_table.add_row("Temperature", str(config.gemini.generation_config.get("temperature", "N/A")))
            gemini_table.add_row("Max Output Tokens", str(config.gemini.generation_config.get("max_output_tokens", "N/A")))
            gemini_table.add_row("Top K", str(config.gemini.generation_config.get("top_k", "N/A")))
            gemini_table.add_row("Function Calling", config.gemini.tool_config.get("function_calling_config", {}).get("mode", "N/A"))
            
            tables.append(gemini_table)
            
            # Configuração de Comandos
            commands_table = Table(title="⚡ Commands Status", box=box.ROUNDED)
            commands_table.add_column("Command", style="cyan")
            commands_table.add_column("Status", style="green")
            commands_table.add_column("Type", style="yellow")
            
            for cmd_name, cmd_config in config.commands.items():
                status = "✅ Enabled" if cmd_config.enabled else "❌ Disabled"
                cmd_type = "LLM" if cmd_config.prompt_template else "Direct"
                commands_table.add_row(f"/{cmd_name}", status, cmd_type)
            
            tables.append(commands_table)
            
            from rich.console import Group

            config_panel = Panel(
                Group(*tables),
                title="[bold cyan]DEILE Configuration[/bold cyan]",
                border_style="cyan",
            )
            return CommandResult.success_result(
                config_panel,
                "rich",
                config_sections=["system", "gemini", "commands"],
            )
            
        except Exception as e:
            logger.error(f"Error in show_config: {e}")
            return CommandResult.error_result(f"Error showing config: {str(e)}", error=e)