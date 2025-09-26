"""
Simple Model Command for DEILE - Easy model switching
==================================================

Simple command for switching between Gemini models easily.
"""

import logging
from rich.panel import Panel
from rich.text import Text

from ..base import DirectCommand, CommandResult, CommandContext
from ...config.manager import ConfigManager, CommandConfig

logger = logging.getLogger(__name__)


class ModelCommand(DirectCommand):
    """Simple model switching command"""

    def __init__(self):
        config = CommandConfig(
            name="model",
            description="Switch between available Gemini models",
            aliases=["models", "m"]
        )
        super().__init__(config)
        self.category = "ai"
        self.help_text = """
Model Command - Simple Model Switching

USAGE:
    /model [model_name]
    /model list
    /model current

AVAILABLE MODELS:
    gemini-2.5-flash-lite    # Cheapest - $0.10/$0.40 per 1M tokens
    gemini-2.5-flash         # Best price/performance balance
    gemini-2.5-pro           # Most capable for complex tasks

EXAMPLES:
    /model                           # Show current model
    /model list                      # List available models with prices
    /model current                   # Show current model details
    /model gemini-2.5-flash-lite    # Switch to cheapest model
    /model gemini-2.5-flash         # Switch to balanced model
    /model gemini-2.5-pro           # Switch to most capable model
"""

        # Available Gemini models with pricing info
        self.available_models = {
            "gemini-2.5-flash-lite": {
                "name": "Gemini 2.5 Flash-Lite",
                "description": "Most cost-efficient model",
                "input_cost": "$0.10/1M tokens",
                "output_cost": "$0.40/1M tokens",
                "best_for": "High-volume, cost-sensitive tasks"
            },
            "gemini-2.5-flash": {
                "name": "Gemini 2.5 Flash",
                "description": "Best price-performance balance",
                "input_cost": "$0.15/1M tokens",
                "output_cost": "$0.60/1M tokens",
                "best_for": "General-purpose tasks"
            },
            "gemini-2.5-pro": {
                "name": "Gemini 2.5 Pro",
                "description": "Most capable for complex reasoning",
                "input_cost": "$1.25/1M tokens",
                "output_cost": "$5.00/1M tokens",
                "best_for": "Complex analysis and reasoning"
            }
        }

    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute the model command"""
        try:
            args = context.args.strip().split() if context.args.strip() else []

            if not args:
                # Default behavior: show list of models
                return await self._list_models(context)

            action = args[0].lower()

            if action == "current":
                return await self._show_current_model(context)
            elif action == "list":
                return await self._list_models(context)
            elif action in self.available_models:
                return await self._switch_model(action, context)
            else:
                return CommandResult(
                success=False,
                content=Panel(Text(f"Unknown model '{action}'. Use '/model list' to see available models.", style="red"),
                             title="❌ Error", border_style="red")
            )

        except Exception as e:
            logger.error(f"ModelCommand execution error: {str(e)}")
            return CommandResult(
                success=False,
                content=Panel(Text(f"Command execution failed: {str(e)}", style="red"),
                             title="❌ Error", border_style="red")
            )

    async def _show_current_model(self, context: CommandContext) -> CommandResult:
        """Show current active model"""
        try:
            config_manager = ConfigManager()
            config = config_manager.load_config()
            current_model = config.gemini.model_name

            if current_model in self.available_models:
                model_info = self.available_models[current_model]

                info_text = (
                    f"🤖 **Current Model**: {model_info['name']}\n"
                    f"📝 **Model ID**: {current_model}\n"
                    f"💰 **Cost**: {model_info['input_cost']} input, {model_info['output_cost']} output\n"
                    f"🎯 **Best for**: {model_info['best_for']}\n\n"
                    f"💡 **Description**: {model_info['description']}"
                )
            else:
                info_text = (
                    f"🤖 **Current Model**: {current_model}\n"
                    f"⚠️ **Note**: This model is not in the standard list.\n"
                    f"Use '/model list' to see recommended models."
                )

            content = Panel(
                Text(info_text, style="green"),
                title="🤖 Current Model",
                border_style="green"
            )

            return CommandResult(
                success=True,
                content=content,
                metadata={'current_model': current_model}
            )

        except Exception as e:
            return CommandResult(
                success=False,
                content=Panel(Text(f"Failed to show current model: {str(e)}", style="red"),
                             title="❌ Error", border_style="red")
            )

    async def _list_models(self, context: CommandContext) -> CommandResult:
        """List all available models with pricing"""
        try:
            config_manager = ConfigManager()
            config = config_manager.load_config()
            current_model = config.gemini.model_name

            models_text = "💎 **Available Gemini Models**\n\n"

            for model_id, model_info in self.available_models.items():
                current_marker = " 🟢 **CURRENT**" if model_id == current_model else ""

                models_text += (
                    f"🤖 **{model_info['name']}**{current_marker}\n"
                    f"   ID: `{model_id}`\n"
                    f"   💰 Cost: {model_info['input_cost']} input, {model_info['output_cost']} output\n"
                    f"   🎯 Best for: {model_info['best_for']}\n"
                    f"   📝 {model_info['description']}\n\n"
                )

            models_text += (
                "💡 **How to switch**:\n"
                f"   `/model <model_id>`\n"
                f"   Example: `/model gemini-2.5-flash-lite`"
            )

            content = Panel(
                Text(models_text, style="cyan"),
                title="💎 Available Models",
                border_style="cyan"
            )

            return CommandResult(
                success=True,
                content=content,
                metadata={
                    'available_models': list(self.available_models.keys()),
                    'current_model': current_model
                }
            )

        except Exception as e:
            return CommandResult(
                success=False,
                content=Panel(Text(f"Failed to list models: {str(e)}", style="red"),
                             title="❌ Error", border_style="red")
            )

    async def _switch_model(self, model_name: str, context: CommandContext) -> CommandResult:
        """Switch to specified model"""
        try:
            config_manager = ConfigManager()

            # Update the config
            config_manager.update_gemini_config(model_name=model_name)

            model_info = self.available_models[model_name]

            switch_info = (
                f"✅ **Model Switch Successful**\n\n"
                f"🤖 **New Model**: {model_info['name']}\n"
                f"📝 **Model ID**: {model_name}\n"
                f"💰 **Cost**: {model_info['input_cost']} input, {model_info['output_cost']} output\n"
                f"🎯 **Best for**: {model_info['best_for']}\n\n"
                f"💡 The model has been updated in your configuration.\n"
                f"New conversations will use the selected model."
            )

            content = Panel(
                Text(switch_info, style="green"),
                title="✅ Model Switched",
                border_style="green"
            )

            return CommandResult(
                success=True,
                content=content,
                metadata={
                    'switched_to': model_name,
                    'model_info': model_info
                }
            )

        except Exception as e:
            return CommandResult(
                success=False,
                content=Panel(Text(f"Failed to switch to model '{model_name}': {str(e)}", style="red"),
                             title="❌ Error", border_style="red")
            )


# Register the model command
try:
    from deile.commands.registry import StaticCommandRegistry
    StaticCommandRegistry.register("model", ModelCommand)
    StaticCommandRegistry.register("models", ModelCommand)
    StaticCommandRegistry.register("m", ModelCommand)
except ImportError:
    # Fallback if registry not available
    pass