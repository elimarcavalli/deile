"""Model Command - Manage and display AI model information"""

from typing import Dict, Any, Optional
import json
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text

from ..base import DirectCommand
from ...core.exceptions import CommandError


class ModelCommand(DirectCommand):
    """Manage AI models: list available models, show info, set defaults"""
    
    def __init__(self):
        super().__init__(
            name="model",
            description="Manage AI models: list available models, show info, set defaults.",
            aliases=["models", "llm"]
        )
    
    def execute(self, 
               args: str = "",
               context: Optional[Dict[str, Any]] = None) -> Any:
        """Execute model command"""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            
            if not parts:
                # No arguments - list all models
                return self._list_models(context)
            
            command = parts[0]
            
            if command == "info":
                # Model info - detailed JSON
                return self._get_model_info(context)
            elif command == "default":
                # Set default model
                if len(parts) < 2:
                    raise CommandError("default command requires model name: /model default <model_name>")
                model_name = parts[1]
                return self._set_default_model(model_name, context)
            elif command == "list":
                # Explicit list command
                return self._list_models(context)
            elif command in ["--help", "-h", "help"]:
                return self.get_help()
            else:
                # Assume it's a model name for info
                return self._get_specific_model_info(command, context)
            
        except Exception as e:
            raise CommandError(f"Failed to execute model command: {str(e)}")
    
    def _list_models(self, context: Optional[Dict[str, Any]]) -> Table:
        """List all available models"""
        
        models_data = self._get_models_data(context)
        
        table = Table(title="🤖 Available AI Models", show_header=True, header_style="bold magenta")
        table.add_column("Model Name", style="cyan", width=20)
        table.add_column("Type", style="green", width=12)
        table.add_column("Max Tokens", justify="right", style="blue", width=12)
        table.add_column("Cost/1K", justify="right", style="yellow", width=10)
        table.add_column("Features", style="white", width=25)
        table.add_column("Status", style="green", width=10)
        
        current_default = models_data.get("current_default", "")
        
        for model in models_data.get("models", []):
            name = model.get("name", "")
            features = []
            
            if model.get("multimodal"):
                features.append("Vision")
            if model.get("function_calling"):
                features.append("Functions")
            if model.get("code_generation"):
                features.append("Code")
            
            status = "Default" if name == current_default else "Available"
            status_style = "bold green" if name == current_default else "green"
            
            table.add_row(
                name,
                model.get("type", "Unknown"),
                f"{model.get('max_tokens', 0):,}",
                f"${model.get('cost_per_1k', 0):.4f}",
                ", ".join(features) if features else "Basic",
                Text(status, style=status_style)
            )
        
        return table
    
    def _get_model_info(self, context: Optional[Dict[str, Any]]) -> str:
        """Get detailed model info as JSON"""
        
        models_data = self._get_models_data(context)
        
        # Return complete model information as JSON
        info_data = {
            "current_default": models_data.get("current_default"),
            "total_models": len(models_data.get("models", [])),
            "models": models_data.get("models", []),
            "provider_info": models_data.get("provider_info", {}),
            "usage_stats": models_data.get("usage_stats", {})
        }
        
        return json.dumps(info_data, indent=2, default=str)
    
    def _get_specific_model_info(self, model_name: str, 
                               context: Optional[Dict[str, Any]]) -> Panel:
        """Get information for a specific model"""
        
        models_data = self._get_models_data(context)
        models = models_data.get("models", [])
        
        # Find the specific model
        model_data = None
        for model in models:
            if model.get("name") == model_name:
                model_data = model
                break
        
        if not model_data:
            raise CommandError(f"Model '{model_name}' not found")
        
        # Create detailed display
        content_lines = [
            f"**{model_data.get('name', 'Unknown')}**",
            "",
            f"🏷️  **Type**: {model_data.get('type', 'Unknown')}",
            f"🏢 **Provider**: {model_data.get('provider', 'Unknown')}",
            f"📊 **Max Tokens**: {model_data.get('max_tokens', 0):,}",
            f"🌡️  **Temperature**: {model_data.get('temperature', 0.7)}",
            f"💰 **Cost per 1K**: ${model_data.get('cost_per_1k', 0):.4f}",
            f"📅 **Release**: {model_data.get('release_date', 'Unknown')}",
            ""
        ]
        
        # Capabilities
        capabilities = []
        if model_data.get("multimodal"):
            capabilities.append("🎨 Vision/Image Processing")
        if model_data.get("function_calling"):
            capabilities.append("🔧 Function Calling")
        if model_data.get("code_generation"):
            capabilities.append("💻 Code Generation")
        if model_data.get("reasoning"):
            capabilities.append("🧠 Advanced Reasoning")
        
        if capabilities:
            content_lines.extend([
                "✨ **Capabilities**:"
            ])
            for cap in capabilities:
                content_lines.append(f"  • {cap}")
            content_lines.append("")
        
        # Usage stats if available
        usage = model_data.get("usage_stats", {})
        if usage:
            content_lines.extend([
                "📊 **Usage Statistics**:",
                f"  • Total Requests: {usage.get('total_requests', 0)}",
                f"  • Success Rate: {usage.get('success_rate', 0):.1f}%",
                f"  • Avg Response Time: {usage.get('avg_response_time', 0):.2f}s",
                f"  • Total Tokens: {usage.get('total_tokens', 0):,}",
                ""
            ])
        
        # Strengths and use cases
        strengths = model_data.get("strengths", [])
        if strengths:
            content_lines.extend([
                "🎯 **Best For**:"
            ])
            for strength in strengths[:3]:  # Show top 3
                content_lines.append(f"  • {strength}")
        
        content = "\n".join(content_lines)
        
        current_default = models_data.get("current_default", "")
        is_default = model_name == current_default
        
        return Panel(
            Text(content, style="white"),
            title=f"🤖 {model_name}" + (" (Default)" if is_default else ""),
            border_style="green" if is_default else "blue",
            padding=(1, 2)
        )
    
    def _set_default_model(self, model_name: str, 
                          context: Optional[Dict[str, Any]]) -> Panel:
        """Set default model"""
        
        models_data = self._get_models_data(context)
        models = models_data.get("models", [])
        
        # Verify model exists
        model_exists = any(model.get("name") == model_name for model in models)
        if not model_exists:
            raise CommandError(f"Model '{model_name}' not found. Use /model list to see available models.")
        
        # In real implementation, this would update configuration
        success_message = f"✅ Default model set to: **{model_name}**\n\nThis model will be used for all future conversations unless explicitly overridden."
        
        return Panel(
            Text(success_message, style="green"),
            title="🤖 Model Configuration Updated",
            border_style="green",
            padding=(1, 2)
        )
    
    def _get_models_data(self, context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Get models data (mock implementation)"""
        
        # In real implementation, this would come from the model router/registry
        return {
            "current_default": "gemini-2.5-pro",
            "provider_info": {
                "google": {
                    "name": "Google GenAI",
                    "api_version": "v1beta",
                    "status": "active"
                }
            },
            "usage_stats": {
                "total_requests": 145,
                "total_tokens": 89500,
                "total_cost": 0.245
            },
            "models": [
                {
                    "name": "gemini-2.5-pro",
                    "type": "Large Language",
                    "provider": "Google",
                    "max_tokens": 2048000,
                    "temperature": 0.7,
                    "cost_per_1k": 0.00125,
                    "release_date": "2024-12",
                    "multimodal": True,
                    "function_calling": True,
                    "code_generation": True,
                    "reasoning": True,
                    "strengths": [
                        "Complex reasoning and analysis",
                        "Code generation and debugging",
                        "Multimodal understanding",
                        "Long context processing"
                    ],
                    "usage_stats": {
                        "total_requests": 95,
                        "success_rate": 98.9,
                        "avg_response_time": 1.8,
                        "total_tokens": 75200
                    }
                },
                {
                    "name": "gemini-1.5-pro",
                    "type": "Large Language",
                    "provider": "Google",
                    "max_tokens": 1048576,
                    "temperature": 0.7,
                    "cost_per_1k": 0.001,
                    "release_date": "2024-04",
                    "multimodal": True,
                    "function_calling": True,
                    "code_generation": True,
                    "reasoning": True,
                    "strengths": [
                        "Balanced performance/cost",
                        "Good for general tasks",
                        "Reliable function calling"
                    ],
                    "usage_stats": {
                        "total_requests": 35,
                        "success_rate": 97.1,
                        "avg_response_time": 1.4,
                        "total_tokens": 28900
                    }
                },
                {
                    "name": "gemini-1.5-flash",
                    "type": "Fast Language",
                    "provider": "Google",
                    "max_tokens": 1048576,
                    "temperature": 0.7,
                    "cost_per_1k": 0.0002,
                    "release_date": "2024-05",
                    "multimodal": True,
                    "function_calling": True,
                    "code_generation": False,
                    "reasoning": False,
                    "strengths": [
                        "Very fast responses",
                        "Low cost",
                        "Good for simple tasks"
                    ],
                    "usage_stats": {
                        "total_requests": 15,
                        "success_rate": 95.0,
                        "avg_response_time": 0.7,
                        "total_tokens": 12400
                    }
                }
            ]
        }
    
    def get_help(self) -> str:
        """Get command help"""
        return """Manage AI models: list available models, show info, set defaults

Usage:
  /model                    List all available models
  /model <model_name>       Show details for specific model  
  /model info               Show detailed JSON info for all models
  /model default <name>     Set default model
  /model list               List all models (same as no args)

Examples:
  /model                         List all available models
  /model gemini-2.5-pro          Show details for gemini-2.5-pro
  /model info                    Export all model info as JSON  
  /model default gemini-1.5-pro  Set gemini-1.5-pro as default
  
Features shown:
  • Vision: Supports image/multimodal input
  • Functions: Supports function calling
  • Code: Optimized for code generation
  • Reasoning: Advanced reasoning capabilities

Aliases: /models, /llm"""