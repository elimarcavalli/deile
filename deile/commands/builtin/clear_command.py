"""Clear Command for DEILE"""

import logging
import time
import uuid

from rich.panel import Panel
from rich.text import Text

from ...core.exceptions import CommandError
from .._sentinels import POST_SWITCH_ACTION_KEY, SWITCH_SESSION_KEY
from ..base import CommandContext, CommandResult, DirectCommand
from ._session_store import SessionHistoryStore
from ._shared import split_args, wrap_command_errors

logger = logging.getLogger(__name__)


class ClearCommand(DirectCommand):
    """Start a fresh conversation (and screen) while preserving the prior one."""

    cli_flag = "--clear"
    cli_help = "Start a new conversation, archive the current one, redraw the welcome screen."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="clear",
            description="Inicia uma nova conversa, arquivando a atual (para /resume) e redesenhando a tela.",
            aliases=["cls"],
        )
        super().__init__(config)

    @wrap_command_errors("clear")
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute clear command with enhanced reset functionality"""
        parts = split_args(context)

        if not parts:
            return await self._start_fresh_conversation(context)

        command = parts[0].lower()

        if command == "reset":
            # Complete session reset - SITUAÇÃO 7 SOLUTION
            force = "--force" in parts or "-f" in parts
            return await self._clear_reset(context, force)
        elif command == "history":
            return await self._clear_history_only(context)
        elif command == "screen":
            return await self._clear_screen_only(context)
        else:
            raise CommandError(f"Unknown clear option: {command}. Use: cls, cls reset, cls history, cls screen")

    async def _start_fresh_conversation(self, context: CommandContext) -> CommandResult:
        """Archive the current conversation and switch to a brand-new session.

        Mirrors the lifecycle that /fork and /rewind use (write
        ``_switch_session`` + ``_post_switch_action`` into the current
        session's ``context_data``), but creates an empty session and asks
        the CLI to redraw the welcome banner afterwards. Provider/persona/
        model config is owned by the agent, not the session — so it is
        preserved automatically.

        One-shot ``deile --clear`` runs without an interactive agent or
        session; in that path we degrade to the legacy screen-clear so the
        flag still exits 0.
        """
        agent = getattr(context, "agent", None)
        session = getattr(context, "session", None)
        if agent is None or session is None:
            return await self._clear_screen_only(context)

        # 1. Persist the soon-to-be-replaced conversation so /resume can find it.
        history = list(getattr(session, "conversation_history", []) or [])
        if history:
            try:
                name = session.context_data.get("conversation_name", "")
                SessionHistoryStore().save(session.session_id, history, name)
            except Exception as exc:
                logger.warning("Could not archive session before /clear: %s", exc)

        # 2. Spawn an empty session — agent.create_session normalizes the
        #    working_directory and registers it in agent._sessions so the
        #    CLI's get_session() finds it after the switch.
        new_sid = f"clear-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        try:
            agent.create_session(
                session_id=new_sid,
                working_directory=session.working_directory,
            )
        except Exception as exc:
            raise CommandError(f"Não foi possível criar nova sessão: {exc}")

        # 3. Hand the switch + redraw request to the CLI via the sentinels.
        session.context_data[SWITCH_SESSION_KEY] = new_sid
        session.context_data[POST_SWITCH_ACTION_KEY] = "welcome"

        return CommandResult.success_result(
            "",
            "text",
            suppress_response_display=True,
            new_session_id=new_sid,
            archived_session_id=session.session_id,
        )
    
    async def _clear_reset(self, context: CommandContext, force: bool = False) -> CommandResult:
        """Complete session reset - SOLVES SITUAÇÃO 7

        ``force`` is currently a no-op placeholder for a future interactive
        confirmation prompt; the parameter is kept to preserve the public
        ``/cls reset --force`` CLI surface.
        """
        del force  # interactive-confirm prompt not yet implemented (issue tracked separately)

        try:
            reset_steps: list[str] = []
            reset_steps.extend(self._reset_agent(context))
            reset_steps.extend(self._reset_plans())
            reset_steps.extend(self._reset_approvals())

            if hasattr(context, 'ui_manager') and context.ui_manager:
                context.ui_manager.clear_screen()
                reset_steps.append("✅ Screen display cleared")

            reset_steps.extend(self._reset_temp_files())
            reset_steps.extend(self._reset_session_id(context))
            
            # Create success report
            success_content = [
                "🎉 **SESSION RESET COMPLETE**",
                "",
                "**Operations Completed:**"
            ]
            
            for step in reset_steps:
                success_content.append(f"  {step}")
            
            success_content.extend([
                "",
                "**Session State:**",
                "• Fresh conversation context",
                "• Reset token counters", 
                "• Clear orchestration state",
                "• New session ID",
                "",
                "🚀 **Ready for fresh start!**"
            ])
            
            success_text = "\n".join(success_content)
            
            return CommandResult.success_result(
                Panel(
                    Text(success_text, style="green"),
                    title="🔄 Session Reset Complete", 
                    border_style="green",
                    padding=(1, 2)
                ),
                "rich"
            )
            
        except Exception as e:
            # Even if some steps failed, report what was accomplished
            error_content = [
                "⚠️ **PARTIAL RESET COMPLETED**",
                "",
                f"**Error:** {str(e)}",
                "",
                "**Completed Steps:**"
            ]
            
            for step in reset_steps:
                error_content.append(f"  {step}")
            
            error_content.extend([
                "",
                "Some components may still retain state.",
                "Try restarting the application for complete reset."
            ])
            
            error_text = "\n".join(error_content)
            
            return CommandResult.success_result(
                Panel(
                    Text(error_text, style="yellow"),
                    title="⚠️ Partial Reset",
                    border_style="yellow",
                    padding=(1, 2)
                ),
                "rich"
            )
    
    @staticmethod
    def _reset_agent(context: CommandContext) -> list[str]:
        """Steps 1-3: clear agent conversation/context/memory/token counters.

        Returns the list of human-readable step lines that were performed.
        Each call to a ``hasattr``-gated method is best-effort: missing
        methods simply skip the corresponding step.
        """
        steps: list[str] = []
        agent = getattr(context, 'agent', None)
        if not agent:
            return steps
        agent.clear_conversation_history()
        agent.clear_context()
        steps.append("✅ Conversation history cleared")
        steps.append("✅ Agent context cleared")
        if hasattr(agent, 'clear_session_memory'):
            agent.clear_session_memory()
            steps.append("✅ Session memory cleared")
        if hasattr(agent, 'reset_token_counters'):
            agent.reset_token_counters()
            steps.append("✅ Token counters reset")
        return steps

    @staticmethod
    def _reset_plans() -> list[str]:
        """Step 4: clear active plans/locks/stop-flags from PlanManager singleton."""
        try:
            from ...orchestration.plan_manager import get_plan_manager
            get_plan_manager().clear_active_state()
            return ["✅ Active plans cleared"]
        except Exception as exc:
            logger.warning("Could not clear orchestration state: %s", exc)
            return ["⚠️ Orchestration state partially cleared"]

    @staticmethod
    def _reset_approvals() -> list[str]:
        """Step 5: clear pending approval requests + futures."""
        try:
            from ...orchestration.approval_system import get_approval_system
            approval_system = get_approval_system()
            approval_system.pending_requests.clear()
            approval_system.request_futures.clear()
            return ["✅ Approval requests cleared"]
        except Exception as exc:
            logger.warning("Could not clear approval state: %s", exc)
            return ["⚠️ Approval state partially cleared"]

    @staticmethod
    def _reset_temp_files() -> list[str]:
        """Step 7: rm -rf well-known temp dirs (TEMP / CACHE / .deile_cache)."""
        steps: list[str] = []
        try:
            import shutil
            from pathlib import Path
            for temp_dir in ("TEMP", "CACHE", ".deile_cache"):
                temp_path = Path(temp_dir)
                if temp_path.exists():
                    shutil.rmtree(temp_path, ignore_errors=True)
                    steps.append(f"✅ Temporary directory {temp_dir} cleared")
        except Exception as exc:
            logger.warning("Could not clear temporary files: %s", exc)
            steps.append("⚠️ Temporary files partially cleared")
        return steps

    @staticmethod
    def _reset_session_id(context: CommandContext) -> list[str]:
        """Step 8: regenerate session UUID when present on the context."""
        if not hasattr(context, 'session_id'):
            return []
        import uuid
        context.session_id = str(uuid.uuid4())
        return ["✅ Session ID regenerated"]

    async def _clear_history_only(self, context: CommandContext) -> CommandResult:
        """Clear only conversation history"""
        
        try:
            if hasattr(context, 'agent') and context.agent:
                context.agent.clear_conversation_history()
                
            return CommandResult.success_result(
                Panel(
                    Text("✅ Conversation history cleared.\n\nContext, memory, and session state preserved.", 
                         style="green"),
                    title="📝 History Cleared",
                    border_style="green"
                ),
                "rich"
            )
            
        except Exception as e:
            raise CommandError(f"Failed to clear history: {str(e)}")
    
    async def _clear_screen_only(self, context: CommandContext) -> CommandResult:
        """Clear only screen display"""
        
        try:
            if hasattr(context, 'ui_manager') and context.ui_manager:
                context.ui_manager.clear_screen()
                
            return CommandResult.success_result(
                Panel(
                    Text("✅ Screen display cleared.\n\nHistory and session state preserved.", 
                         style="green"),
                    title="🖥️ Screen Cleared",
                    border_style="green"
                ),
                "rich"
            )
            
        except Exception as e:
            raise CommandError(f"Failed to clear screen: {str(e)}")
    
    def get_help(self) -> str:
        """Get detailed help for clear command"""
        return """Inicia uma nova conversa e redesenha a tela inicial.

Uso:
  /cls                  Arquiva a conversa atual e começa uma nova (default)
  /cls reset            Reset profundo (state + plans + approvals + temp)
  /cls history          Apenas limpa o histórico da sessão atual
  /cls screen           Apenas limpa a tela

Default (/cls sem argumentos):
  1. Salva a conversa atual em ~/.deile/sessions/ (visível em /resume)
  2. Cria uma sessão nova e vazia, com o mesmo working directory
  3. Redesenha o banner inicial (mesmo da entrada do DEILE)
  4. Modelo, persona e configs ficam preservados — eles são do agente,
     não da sessão

Subcomandos avançados (mantidos por compatibilidade):
  • /cls reset   - Limpa plans, approvals, temp; usa o session id atual
  • /cls history - Só limpa conversation_history (sem trocar sessão)
  • /cls screen  - Só limpa a tela

Comandos relacionados:
  • /resume - Recarrega uma conversa arquivada (mantém o id original)
  • /rewind - Cria um fork a partir de um ponto do histórico
  • /export - Backup explícito antes de qualquer reset"""