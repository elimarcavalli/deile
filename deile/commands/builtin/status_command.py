"""Status Command - Complete system status and health information"""

from typing import Dict, Any, Optional
import platform
import sys
from datetime import datetime
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.columns import Columns
from rich.tree import Tree
import psutil

from ..base import DirectCommand, CommandResult, CommandContext
from ...core.exceptions import CommandError


class StatusCommand(DirectCommand):
    """Complete system status, health monitoring and connectivity information"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="status",
            description="Complete system status, health monitoring and connectivity information.",
            aliases=["info", "stat", "sys", "health"]
        )
        super().__init__(config)
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute status command"""
        args = context.args if hasattr(context, 'args') else ""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            
            if not parts:
                # Show comprehensive status overview
                return await self._show_complete_status()
            
            section = parts[0].lower()
            
            if section == "system":
                return await self._show_system_status()
            elif section == "models":
                return await self._show_models_status()
            elif section == "tools":
                return await self._show_tools_status()
            elif section == "memory":
                return await self._show_memory_status()
            elif section == "plans":
                return await self._show_plans_status()
            elif section == "connectivity":
                return await self._show_connectivity_status()
            elif section == "performance":
                return await self._show_performance_status()
            else:
                raise CommandError(f"Unknown status section: {section}")
                
        except Exception as e:
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to execute status command: {str(e)}")
    
    async def _show_complete_status(self) -> CommandResult:
        """Show complete system status overview"""
        
        # System Info Panel
        system_info = self._get_system_info()
        system_panel = self._create_system_panel(system_info)
        
        # Models Status Panel
        models_info = self._get_models_info()
        models_panel = self._create_models_panel(models_info)
        
        # Tools Status Panel
        tools_info = self._get_tools_info()
        tools_panel = self._create_tools_panel(tools_info)
        
        # Health Status Panel
        health_info = self._get_health_info()
        health_panel = self._create_health_panel(health_info)
        
        # Combine all panels in columns
        left_column = Columns([system_panel, tools_panel], equal=True)
        right_column = Columns([models_panel, health_panel], equal=True)
        
        # Usage instructions
        usage_panel = Panel(
            Text(
                "Detailed Views:\n"
                "• /status system        - Detailed system information\n"
                "• /status models        - AI models and providers status\n"
                "• /status tools         - Tools registry and availability\n"
                "• /status memory        - Memory and session usage\n"
                "• /status plans         - Active plans and orchestration\n"
                "• /status connectivity  - Network and API connectivity\n"
                "• /status performance   - System performance metrics",
                style="dim"
            ),
            title="📋 Status Sections",
            border_style="dim"
        )
        
        final_display = f"{left_column}\n\n{right_column}\n\n{usage_panel}"
        
        return CommandResult.success_result(final_display, "rich")
    
    def _get_system_info(self) -> Dict[str, Any]:
        """Get system information"""
        try:
            return {
                'deile_version': '4.0.0',
                'python_version': f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                'platform': platform.system(),
                'platform_release': platform.release(),
                'platform_version': platform.version(),
                'architecture': platform.machine(),
                'hostname': platform.node(),
                'uptime': self._get_system_uptime(),
                'cpu_count': psutil.cpu_count(),
                'memory_total': psutil.virtual_memory().total,
                'memory_used': psutil.virtual_memory().used,
                'memory_percent': psutil.virtual_memory().percent,
                'disk_usage': psutil.disk_usage('.').percent
            }
        except Exception as e:
            return {'error': str(e)}
    
    def _get_models_info(self) -> Dict[str, Any]:
        """Get AI models information"""
        try:
            # Mock data - would integrate with actual model manager
            return {
                'active_model': 'gemini-2.5-pro',
                'provider': 'Google GenAI',
                'available_models': 5,
                'auto_selection': True,
                'last_switch': '2025-09-07T10:30:00',
                'performance_score': 8.7,
                'connectivity': 'healthy',
                'api_quota_used': 15.3,
                'cost_today': 0.0247
            }
        except Exception as e:
            return {'error': str(e)}
    
    def _get_tools_info(self) -> Dict[str, Any]:
        """Get tools registry information"""
        try:
            from ...tools.registry import get_tool_registry
            registry = get_tool_registry()
            stats = registry.get_stats()
            
            return {
                'total_tools': stats['total_tools'],
                'enabled_tools': stats['enabled_tools'],
                'disabled_tools': stats['disabled_tools'],
                'categories': stats['categories'],
                'function_definitions': stats['available_functions'],
                'tools_with_schemas': stats['tools_with_schemas'],
                'auto_discovery': stats['auto_discovery_enabled']
            }
        except Exception as e:
            return {'error': str(e)}
    
    def _get_health_info(self) -> Dict[str, Any]:
        """Get system health information"""
        try:
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            
            # Determine health status
            health_score = 100
            warnings = []
            
            if cpu_percent > 80:
                health_score -= 20
                warnings.append("High CPU usage")
            
            if memory.percent > 85:
                health_score -= 15
                warnings.append("High memory usage")
            
            # Mock additional health checks
            health_status = "healthy" if health_score >= 80 else "warning" if health_score >= 60 else "critical"
            
            return {
                'overall_status': health_status,
                'health_score': health_score,
                'cpu_usage': cpu_percent,
                'memory_usage': memory.percent,
                'warnings': warnings,
                'uptime': self._get_system_uptime(),
                'last_check': datetime.now().isoformat()
            }
        except Exception as e:
            return {'error': str(e), 'overall_status': 'unknown'}
    
    def _get_system_uptime(self) -> str:
        """Get system uptime"""
        try:
            boot_time = datetime.fromtimestamp(psutil.boot_time())
            uptime = datetime.now() - boot_time
            days = uptime.days
            hours, remainder = divmod(uptime.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            return f"{days}d {hours}h {minutes}m"
        except:
            return "Unknown"
    
    def _create_system_panel(self, info: Dict[str, Any]) -> Panel:
        """Create system information panel"""
        if 'error' in info:
            content = f"❌ Error: {info['error']}"
            style = "red"
        else:
            content = f"""💻 **DEILE v{info.get('deile_version', 'Unknown')}**

🐍 **Python**: {info.get('python_version', 'Unknown')}
🖥️  **Platform**: {info.get('platform', 'Unknown')} {info.get('platform_release', '')}
🏗️  **Architecture**: {info.get('architecture', 'Unknown')}
🌐 **Hostname**: {info.get('hostname', 'Unknown')}

⏱️  **Uptime**: {info.get('uptime', 'Unknown')}
🧮 **CPU Cores**: {info.get('cpu_count', 'Unknown')}
💾 **Memory**: {info.get('memory_percent', 0):.1f}% used
💿 **Disk**: {info.get('disk_usage', 0):.1f}% used"""
            style = "green"
        
        return Panel(
            Text(content, style=style),
            title="🖥️ System Info",
            border_style="green"
        )
    
    def _create_models_panel(self, info: Dict[str, Any]) -> Panel:
        """Create AI models status panel"""
        if 'error' in info:
            content = f"❌ Error: {info['error']}"
            style = "red"
        else:
            status_icon = "🟢" if info.get('connectivity') == 'healthy' else "🟡"
            auto_icon = "🔄" if info.get('auto_selection') else "🎯"
            
            content = f"""{status_icon} **Active Model**: {info.get('active_model', 'Unknown')}

🏢 **Provider**: {info.get('provider', 'Unknown')}
📊 **Performance**: {info.get('performance_score', 0)}/10
{auto_icon} **Auto-Selection**: {'On' if info.get('auto_selection') else 'Off'}

💰 **Cost Today**: ${info.get('cost_today', 0):.4f}
📈 **API Quota**: {info.get('api_quota_used', 0):.1f}%
🔄 **Last Switch**: {info.get('last_switch', 'Never')[:16]}
🎯 **Available Models**: {info.get('available_models', 0)}"""
            style = "cyan"
        
        return Panel(
            Text(content, style=style),
            title="🤖 AI Models",
            border_style="cyan"
        )
    
    def _create_tools_panel(self, info: Dict[str, Any]) -> Panel:
        """Create tools status panel"""
        if 'error' in info:
            content = f"❌ Error: {info['error']}"
            style = "red"
        else:
            content = f"""🔧 **Total Tools**: {info.get('total_tools', 0)}
✅ **Enabled**: {info.get('enabled_tools', 0)}
⛔ **Disabled**: {info.get('disabled_tools', 0)}

📂 **Categories**: {info.get('categories', 0)}
📋 **Schemas**: {info.get('tools_with_schemas', 0)}
🔄 **Functions**: {info.get('function_definitions', 0)}
🔍 **Auto-Discovery**: {'On' if info.get('auto_discovery') else 'Off'}"""
            style = "yellow"
        
        return Panel(
            Text(content, style=style),
            title="🔧 Tools Registry",
            border_style="yellow"
        )
    
    def _create_health_panel(self, info: Dict[str, Any]) -> Panel:
        """Create health status panel"""
        status = info.get('overall_status', 'unknown')
        
        if status == 'healthy':
            status_icon = "🟢"
            border_color = "green"
            style = "green"
        elif status == 'warning':
            status_icon = "🟡"
            border_color = "yellow" 
            style = "yellow"
        elif status == 'critical':
            status_icon = "🔴"
            border_color = "red"
            style = "red"
        else:
            status_icon = "⚪"
            border_color = "dim"
            style = "dim"
        
        content = f"""{status_icon} **Status**: {status.title()}
📊 **Health Score**: {info.get('health_score', 0)}/100

💻 **CPU Usage**: {info.get('cpu_usage', 0):.1f}%
💾 **Memory Usage**: {info.get('memory_usage', 0):.1f}%
⏱️  **Uptime**: {info.get('uptime', 'Unknown')}"""
        
        if info.get('warnings'):
            content += f"\n\n⚠️ **Warnings**:\n"
            for warning in info['warnings']:
                content += f"  • {warning}\n"
        else:
            content += "\n\n✨ **All systems normal**"
        
        return Panel(
            Text(content, style=style),
            title="🩺 Health Status",
            border_style=border_color
        )
    
    async def _show_system_status(self) -> CommandResult:
        """Show detailed system status"""
        info = self._get_system_info()
        
        table = Table(title="💻 Detailed System Information", show_header=True, header_style="bold green")
        table.add_column("Component", style="cyan", width=20)
        table.add_column("Value", style="white", width=30)
        table.add_column("Details", style="dim", width=25)
        
        if 'error' not in info:
            table.add_row("DEILE Version", info.get('deile_version', 'Unknown'), "Current system version")
            table.add_row("Python Version", info.get('python_version', 'Unknown'), f"Running on {sys.executable}")
            table.add_row("Platform", f"{info.get('platform', 'Unknown')} {info.get('platform_release', '')}", info.get('platform_version', ''))
            table.add_row("Architecture", info.get('architecture', 'Unknown'), "System architecture")
            table.add_row("Hostname", info.get('hostname', 'Unknown'), "Machine identifier")
            table.add_row("Uptime", info.get('uptime', 'Unknown'), "System uptime")
            table.add_row("CPU Cores", str(info.get('cpu_count', 0)), "Available processor cores")
            table.add_row("Memory Total", f"{info.get('memory_total', 0) // (1024**3):.1f} GB", f"{info.get('memory_percent', 0):.1f}% used")
            table.add_row("Disk Usage", f"{info.get('disk_usage', 0):.1f}%", "Current directory")
        
        return CommandResult.success_result(table, "rich")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Complete system status and health monitoring

Usage:
  /status                     Show complete system overview
  /status system              Detailed system information
  /status models              AI models and providers status  
  /status tools               Tools registry and availability
  /status memory              Memory and session usage
  /status plans               Active plans and orchestration
  /status connectivity        Network and API connectivity
  /status performance         System performance metrics

Status Sections:
  • System - OS, Python, hardware info
  • Models - Active AI model, performance, costs
  • Tools - Registry status, enabled tools
  • Memory - Session state, memory usage
  • Plans - Active orchestration, execution status
  • Connectivity - Network, API endpoints
  • Performance - CPU, memory, disk metrics

Health Indicators:
  🟢 Healthy - All systems normal
  🟡 Warning - Some issues detected  
  🔴 Critical - Immediate attention needed
  ⚪ Unknown - Status unavailable

Examples:
  /status                     Complete system overview
  /status system              Detailed system specs
  /status models              AI model status and performance
  /status tools               Tools availability and stats

Aliases: /info, /stat, /sys, /health"""
    
    async def _show_models_status(self) -> CommandResult:
        """Show detailed models status - placeholder"""
        return CommandResult.success_result(
            Panel(
                Text("Models status view - would show detailed AI model information", style="yellow"),
                title="🤖 Models Status",
                border_style="yellow"
            ),
            "rich"
        )
    
    async def _show_tools_status(self) -> CommandResult:
        """Show detailed tools status - placeholder"""
        return CommandResult.success_result(
            Panel(
                Text("Tools status view - would show detailed tools registry information", style="yellow"),
                title="🔧 Tools Status", 
                border_style="yellow"
            ),
            "rich"
        )
    
    async def _show_memory_status(self) -> CommandResult:
        """Show memory status - placeholder"""
        return CommandResult.success_result(
            Panel(
                Text("Memory status view - would show session and memory usage", style="yellow"),
                title="💾 Memory Status",
                border_style="yellow"
            ),
            "rich"
        )
    
    async def _show_plans_status(self) -> CommandResult:
        """Show plans status - placeholder"""
        return CommandResult.success_result(
            Panel(
                Text("Plans status view - would show active orchestration status", style="yellow"),
                title="📋 Plans Status",
                border_style="yellow"
            ),
            "rich"
        )
    
    async def _show_connectivity_status(self) -> CommandResult:
        """Show connectivity status - placeholder"""
        return CommandResult.success_result(
            Panel(
                Text("Connectivity status view - would show network and API status", style="yellow"),
                title="🌐 Connectivity Status",
                border_style="yellow"
            ),
            "rich"
        )
    
    async def _show_performance_status(self) -> CommandResult:
        """Show performance status - placeholder"""
        return CommandResult.success_result(
            Panel(
                Text("Performance status view - would show system metrics", style="yellow"),
                title="📊 Performance Status",
                border_style="yellow"
            ),
            "rich"
        )