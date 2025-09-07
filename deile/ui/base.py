"""Base UI interfaces and abstractions for DEILE

Following Clean Architecture principles, this module defines the contracts
that any UI implementation must adhere to, ensuring modularity and testability.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Iterator
from dataclasses import dataclass
from enum import Enum


class UITheme(Enum):
    """Available UI themes"""
    DEFAULT = "default"
    DARK = "dark"
    LIGHT = "light"
    CYBERPUNK = "cyberpunk"


class MessageType(Enum):
    """Types of messages to display"""
    INFO = "info"
    SUCCESS = "success" 
    WARNING = "warning"
    ERROR = "error"
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass
class UIMessage:
    """Structured message for UI display"""
    content: str
    message_type: MessageType
    metadata: Dict[str, Any] = None
    timestamp: Optional[float] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass 
class UIStatus:
    """Status information for display"""
    message: str
    is_loading: bool = False
    progress: Optional[float] = None  # 0.0 to 1.0
    details: Optional[str] = None


class UIRenderer(ABC):
    """Abstract base class for UI renderers
    
    This interface allows different rendering strategies (console, web, etc.)
    to be plugged into the system without changing the core logic.
    """
    
    @abstractmethod
    def render_message(self, message: UIMessage) -> None:
        """Render a single message"""
        pass
    
    @abstractmethod
    def render_status(self, status: UIStatus) -> None:
        """Render status information"""
        pass
    
    @abstractmethod
    def render_header(self) -> None:
        """Render application header"""
        pass
    
    @abstractmethod
    def render_separator(self, title: Optional[str] = None) -> None:
        """Render a visual separator"""
        pass
    
    @abstractmethod
    def clear_screen(self) -> None:
        """Clear the display"""
        pass


class UIInputHandler(ABC):
    """Abstract base class for input handling
    
    Separates input concerns from rendering concerns for better testability.
    """
    
    @abstractmethod
    def get_user_input(self, prompt: str = "You > ") -> str:
        """Get input from user"""
        pass
    
    @abstractmethod
    def get_confirmation(self, message: str, default: bool = False) -> bool:
        """Get yes/no confirmation from user"""
        pass
    
    @abstractmethod
    def setup_autocompletion(self, completions: List[str]) -> None:
        """Setup autocompletion with given options"""
        pass


class UIManager(ABC):
    """Main UI manager interface
    
    This is the primary interface that the Agent will interact with.
    It orchestrates rendering and input handling components.
    """
    
    def __init__(self, theme: UITheme = UITheme.DEFAULT):
        self.theme = theme
        self.renderer: Optional[UIRenderer] = None
        self.input_handler: Optional[UIInputHandler] = None
    
    @abstractmethod
    def initialize(self) -> None:
        """Initialize the UI components"""
        pass
    
    @abstractmethod 
    def show_welcome(self) -> None:
        """Display welcome screen"""
        pass
    
    @abstractmethod
    def display_message(self, message: UIMessage) -> None:
        """Display a message to the user"""
        pass
    
    @abstractmethod
    def display_response(self, content: str, metadata: Optional[Dict] = None) -> None:
        """Display agent response with formatting"""
        pass
    
    @abstractmethod
    def display_status(self, status: UIStatus) -> None:
        """Display status information"""
        pass
    
    @abstractmethod
    def get_user_input(self, prompt: str = "You > ") -> str:
        """Get input from user with prompt"""
        pass
    
    @abstractmethod
    def confirm_action(self, message: str, default: bool = False) -> bool:
        """Get confirmation for an action"""
        pass
    
    @abstractmethod
    def setup_file_completion(self, file_paths: List[str]) -> None:
        """Setup file path autocompletion"""
        pass
    
    @abstractmethod
    def display_error(self, error: str, details: Optional[str] = None) -> None:
        """Display error message"""
        pass
    
    @abstractmethod
    def display_success(self, message: str) -> None:
        """Display success message"""
        pass
    
    @abstractmethod
    def display_stats(self, stats: Dict[str, Any]) -> None:
        """Display system statistics"""
        pass
    
    @abstractmethod
    def cleanup(self) -> None:
        """Cleanup UI resources"""
        pass


class UIComponent(ABC):
    """Base class for reusable UI components"""
    
    def __init__(self, renderer: UIRenderer):
        self.renderer = renderer
    
    @abstractmethod
    def render(self, *args, **kwargs) -> None:
        """Render this component"""
        pass


class StreamingRenderer(ABC):
    """Interface for streaming text rendering
    
    Used for displaying real-time responses from AI models.
    """
    
    @abstractmethod
    def start_stream(self, prefix: str = "") -> None:
        """Start streaming display"""
        pass
    
    @abstractmethod
    def stream_chunk(self, chunk: str) -> None:
        """Display a chunk of streaming text"""
        pass
    
    @abstractmethod  
    def end_stream(self, suffix: str = "") -> None:
        """End streaming display"""
        pass