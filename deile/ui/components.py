"""Reusable UI Components for DEILE

This module contains concrete implementations of UI components
following the Component pattern for modularity and reusability.
"""

from typing import Optional, Dict, Any, List
import time
from contextlib import contextmanager

from .base import UIComponent, UIRenderer, UIStatus, UIMessage, MessageType


class StatusDisplay(UIComponent):
    """Component for displaying status messages with spinners and progress"""
    
    def __init__(self, renderer: UIRenderer):
        super().__init__(renderer)
        self._current_status: Optional[UIStatus] = None
    
    def show_loading(self, message: str, details: Optional[str] = None) -> None:
        """Show loading status with spinner"""
        status = UIStatus(
            message=message,
            is_loading=True,
            details=details
        )
        self._current_status = status
        self.renderer.render_status(status)
    
    def show_progress(self, message: str, progress: float, details: Optional[str] = None) -> None:
        """Show progress with percentage"""
        status = UIStatus(
            message=message,
            is_loading=False,
            progress=max(0.0, min(1.0, progress)),  # Clamp to 0-1
            details=details
        )
        self._current_status = status
        self.renderer.render_status(status)
    
    def hide(self) -> None:
        """Hide current status"""
        self._current_status = None
    
    def render(self, status: UIStatus) -> None:
        """Render status directly"""
        self._current_status = status
        self.renderer.render_status(status)


class ProgressIndicator(UIComponent):
    """Component for showing progress through multiple steps"""
    
    def __init__(self, renderer: UIRenderer):
        super().__init__(renderer)
        self.steps: List[str] = []
        self.current_step = 0
        self.total_steps = 0
    
    def start(self, steps: List[str]) -> None:
        """Start progress with list of steps"""
        self.steps = steps
        self.total_steps = len(steps)
        self.current_step = 0
        self._render_progress()
    
    def next_step(self, custom_message: Optional[str] = None) -> None:
        """Move to next step"""
        if self.current_step < self.total_steps:
            self.current_step += 1
            self._render_progress(custom_message)
    
    def complete(self) -> None:
        """Mark all steps as complete"""
        self.current_step = self.total_steps
        self._render_progress("Completed!")
    
    def render(self) -> None:
        """Render current progress state"""
        self._render_progress()
    
    def _render_progress(self, custom_message: Optional[str] = None) -> None:
        """Internal method to render progress"""
        if self.total_steps == 0:
            return
        
        progress = self.current_step / self.total_steps
        
        if custom_message:
            message = custom_message
        elif self.current_step < self.total_steps:
            message = f"Step {self.current_step + 1}/{self.total_steps}: {self.steps[self.current_step]}"
        else:
            message = "All steps completed"
        
        status = UIStatus(
            message=message,
            is_loading=self.current_step < self.total_steps,
            progress=progress
        )
        self.renderer.render_status(status)


class ResponseRenderer(UIComponent):
    """Component for rendering AI responses with rich formatting"""
    
    def __init__(self, renderer: UIRenderer):
        super().__init__(renderer)
    
    def render_response(
        self, 
        content: str, 
        metadata: Optional[Dict[str, Any]] = None,
        show_metadata: bool = True
    ) -> None:
        """Render AI response with metadata"""
        # Render separator before response
        self.renderer.render_separator("DEILE")
        
        # Render main content
        message = UIMessage(
            content=content,
            message_type=MessageType.ASSISTANT,
            metadata=metadata or {},
            timestamp=time.time()
        )
        self.renderer.render_message(message)
        
        # Render metadata if requested
        if show_metadata and metadata:
            self._render_metadata(metadata)
        
        # Render separator after response
        self.renderer.render_separator()
    
    def render_streaming_response(self, content_chunks: List[str]) -> None:
        """Render streaming response (for future streaming support)"""
        self.renderer.render_separator("DEILE")
        
        full_content = "".join(content_chunks)
        message = UIMessage(
            content=full_content,
            message_type=MessageType.ASSISTANT,
            timestamp=time.time()
        )
        self.renderer.render_message(message)
        
        self.renderer.render_separator()
    
    def render(self, content: str, **kwargs) -> None:
        """Main render method"""
        self.render_response(content, **kwargs)
    
    def _render_metadata(self, metadata: Dict[str, Any]) -> None:
        """Render metadata information"""
        if not metadata:
            return
        
        # Show execution time if available
        if "execution_time" in metadata:
            exec_time = metadata["execution_time"]
            time_msg = UIMessage(
                content=f"⏱️ Response time: {exec_time:.2f}s",
                message_type=MessageType.SYSTEM,
                timestamp=time.time()
            )
            self.renderer.render_message(time_msg)
        
        # Show tool results count if available
        if "tool_results_count" in metadata and metadata["tool_results_count"] > 0:
            tools_msg = UIMessage(
                content=f"🔧 Tools executed: {metadata['tool_results_count']}",
                message_type=MessageType.SYSTEM,
                timestamp=time.time()
            )
            self.renderer.render_message(tools_msg)


class FileListComponent(UIComponent):
    """Component for displaying file lists with syntax highlighting"""
    
    def __init__(self, renderer: UIRenderer):
        super().__init__(renderer)
    
    def render_file_list(
        self, 
        files: List[str], 
        title: str = "Project Files",
        max_display: int = 20
    ) -> None:
        """Render a list of files with proper formatting"""
        self.renderer.render_separator(title)
        
        displayed_files = files[:max_display]
        
        for i, file_path in enumerate(displayed_files, 1):
            # Add file icon based on extension
            icon = self._get_file_icon(file_path)
            content = f"{icon} {file_path}"
            
            message = UIMessage(
                content=content,
                message_type=MessageType.INFO,
                metadata={"file_path": file_path, "index": i}
            )
            self.renderer.render_message(message)
        
        # Show truncation message if necessary
        if len(files) > max_display:
            remaining = len(files) - max_display
            truncate_msg = UIMessage(
                content=f"... and {remaining} more files",
                message_type=MessageType.SYSTEM
            )
            self.renderer.render_message(truncate_msg)
        
        self.renderer.render_separator()
    
    def render(self, files: List[str], **kwargs) -> None:
        """Main render method"""
        self.render_file_list(files, **kwargs)
    
    def _get_file_icon(self, file_path: str) -> str:
        """Get appropriate icon for file type"""
        file_path = file_path.lower()
        
        if file_path.endswith('.py'):
            return '🐍'
        elif file_path.endswith(('.js', '.ts')):
            return '💛'
        elif file_path.endswith(('.html', '.htm')):
            return '🌐'
        elif file_path.endswith('.css'):
            return '🎨'
        elif file_path.endswith(('.md', '.txt')):
            return '📝'
        elif file_path.endswith(('.json', '.yaml', '.yml')):
            return '⚙️'
        elif file_path.endswith(('.jpg', '.png', '.gif')):
            return '🖼️'
        else:
            return '📄'


class ErrorDisplay(UIComponent):
    """Component for displaying errors with proper formatting and context"""
    
    def __init__(self, renderer: UIRenderer):
        super().__init__(renderer)
    
    def show_error(
        self, 
        error_message: str, 
        details: Optional[str] = None,
        suggestions: Optional[List[str]] = None
    ) -> None:
        """Show error with details and suggestions"""
        self.renderer.render_separator("ERROR")
        
        # Main error message
        error_msg = UIMessage(
            content=f"❌ {error_message}",
            message_type=MessageType.ERROR,
            timestamp=time.time()
        )
        self.renderer.render_message(error_msg)
        
        # Show details if provided
        if details:
            details_msg = UIMessage(
                content=f"Details: {details}",
                message_type=MessageType.ERROR,
                metadata={"details": True}
            )
            self.renderer.render_message(details_msg)
        
        # Show suggestions if provided
        if suggestions:
            suggest_msg = UIMessage(
                content="💡 Suggestions:",
                message_type=MessageType.INFO
            )
            self.renderer.render_message(suggest_msg)
            
            for i, suggestion in enumerate(suggestions, 1):
                suggestion_msg = UIMessage(
                    content=f"   {i}. {suggestion}",
                    message_type=MessageType.INFO
                )
                self.renderer.render_message(suggestion_msg)
        
        self.renderer.render_separator()
    
    def render(self, error_message: str, **kwargs) -> None:
        """Main render method"""
        self.show_error(error_message, **kwargs)