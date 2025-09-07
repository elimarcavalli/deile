"""Componente para exibição de status"""

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from contextlib import contextmanager
from typing import Optional


class StatusDisplay:
    """Componente para exibir status de operações"""
    
    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()
    
    @contextmanager
    def show_progress(self, message: str):
        """Context manager para mostrar progresso com spinner"""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=True
        ) as progress:
            task = progress.add_task(message, total=None)
            try:
                yield progress
            finally:
                progress.stop()
    
    def show_status(self, message: str, status_type: str = "info") -> None:
        """Exibe status com emoji apropriado"""
        emoji_map = {
            "info": "ℹ️",
            "success": "✅", 
            "warning": "⚠️",
            "error": "❌",
            "processing": "🔄"
        }
        
        emoji = emoji_map.get(status_type, "ℹ️")
        self.console.print(f"{emoji} {message}")
    
    def show_loading(self, message: str = "Carregando...") -> None:
        """Exibe indicador de carregamento"""
        self.show_status(f"🔄 {message}", "processing")