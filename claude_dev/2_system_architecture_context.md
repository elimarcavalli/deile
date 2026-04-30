## 🏗️ System Architecture Context

### Current Technology Stack
```
┌─────────────────────────────────────────────────────────────────┐
│                         CLI INTERFACE                           │
│  Rich Terminal UI + Autocompletion + Themes + Status Bars      │
└─────────────────────┬───────────────────────────────────────────┘
                      │ Command & Parser Pipeline
┌─────────────────────▼───────────────────────────────────────────┐
│                    DEILE AGENT CORE                            │
│  Mediator Pattern + Session Management + Intent Analysis       │
└─────────────────────┬───────────────────────────────────────────┘
                      │ Component Orchestration
┌─────────────────────▼───────────────────────────────────────────┐
│                    SERVICE LAYER                               │
│  Tool Registry + Command Registry + Parser Registry            │
│  Workflow Executor + Plan Manager + Memory Manager             │
└─────────────────────┬───────────────────────────────────────────┘
                      │ Model & External APIs
┌─────────────────────▼───────────────────────────────────────────┐
│                 INTEGRATION LAYER                              │
│  Google Gemini API + Function Calling + File Support          │
│  SQLite Storage + Security System + Audit Logging             │
└─────────────────────────────────────────────────────────────────┘

              ┌─────────────────────────────────────────┐
              │       EXTENSION ECOSYSTEM               │
              │  • Built-in Tools  (deile/tools/*.py)   │
              │  • Slash Commands  (deile/commands/)    │
              │  • Dynamic Personas                     │
              │  • Custom Parsers                       │
              └─────────────────────────────────────────┘
```

### Core Technology Stack
| Component | Technology | Version/Module | Purpose |
|-----------|------------|----------------|----------|
| **Language** | Python | 3.9+ | Primary development language with async support |
| **LLM Provider** | Google Gemini | see `deile/config/manager.py` & `deile/config/settings.py` for current default | Advanced language model with function calling |
| **CLI Framework** | Rich | Latest | Terminal UI with themes and components |
| **Storage** | SQLite | Built-in | Task persistence and memory storage |
| **Configuration** | YAML/JSON | PyYAML/json | Configuration and pattern management |
| **Async I/O** | aiofiles | Latest | Non-blocking file operations |
| **Validation** | Pydantic | v2+ | Data validation and schema enforcement |
| **Testing** | Pytest | Latest | Comprehensive test framework |

### 🧠 Core Components Architecture (organized by subpackage)

> Authoritative source is the directory listing of `deile/`. The sections below describe responsibilities; do not rely on file-level line counts here — they rot quickly and the code is the source of truth.

#### Agent System (`deile/core/`)
- **DeileAgent** (`agent.py`) - Central orchestrator with Mediator Pattern implementation
- **ContextManager** (`context_manager.py`) - Conversation context and state management
- **IntentAnalyzer** (`intent_analyzer.py`) - Advanced pattern-driven intent detection
- **IntentMetrics** (`intent_metrics.py`) - Performance tracking for intent analysis
- **ModelRouter** (`models/router.py`) - Intelligent provider selection with fallback

#### Tool System (`deile/tools/`)
- **ToolRegistry** (`registry.py`) - Auto-discovery with function calling generation
- **BaseTool** (`base.py`) - Tool interface with security levels
- Built-in tools live as siblings inside `deile/tools/` (e.g. `file_tools.py`, `bash_tools.py`, `search_tools.py`, `git_tools.py`, `http_tools.py`, `tokenizer_tool.py`, …). Run `ls deile/tools/*.py` for the current set — there is no `deile/tools/builtin/` folder.

#### Orchestration System (`deile/orchestration/`)
- **PlanManager** (`plan_manager.py`) - Autonomous workflow generation
- **WorkflowExecutor** (`workflow_executor.py`) - Step execution with rollback
- **TaskManager** (`task_manager.py`) - Base task lifecycle management
- **SQLiteTaskManager** (`sqlite_task_manager.py`) - Task persistence layer

#### Command System (`deile/commands/`)
- **CommandRegistry** (`registry.py`) - Auto-discovery of slash commands
- **SlashCommand** (`base.py`) - Command interface and execution pattern
- **Built-in Commands** (`builtin/`) - help, plan, run, debug, config, status, etc.

#### Parser System (`deile/parsers/`)
- **ParserRegistry** (`registry.py`) - Dynamic parser discovery
- **CommandParser** - Slash command processing
- **FileParser** - File reference analysis
- **DiffParser** - Patch and diff handling
- **IntelligentFileParser** - Context-aware parsing

#### UI System (`deile/ui/`)
- **ConsoleUIManager** (`console_ui.py`) - Main UI orchestrator
- **DisplayManager** (`display_manager.py`) - Rich output formatting
- **AutocompleteManager** (`autocomplete.py`) - Hybrid autocompletion
- **ThemeManager** (`themes.py`) - Configurable color themes

#### Personas System (`deile/personas/`)
- **BaseAutonomousPersona** (`base.py`) - Persona contract and lifecycle
- **PersonaLoader** (`loader.py`) - Markdown-based instruction loading
- **PersonaManager** (`manager.py`) - Hot-reload and capability management
- **Instructions** (`instructions/*.md`) - Markdown persona definitions (e.g. `developer.md`, `fallback.md`)

#### Memory System (`deile/memory/`)
- **MemoryManager** (memory_manager.py) - Multi-layer coordination
- **WorkingMemory** - Active context cache
- **EpisodicMemory** - Session history tracking
- **SemanticMemory** - Structured knowledge with embeddings
- **ProceduralMemory** - Learned patterns and skills

#### Security System (`deile/security/`)
- **PermissionManager** (permissions.py) - Rule-based access control
- **AuditLogger** (audit_logger.py) - Comprehensive operation logging
- **SecurityValidator** - Input validation and sanitization
- **SandboxExecutor** - Isolated execution environment

#### Configuration System (`deile/config/`)
- **ConfigManager** (`manager.py`) - YAML/JSON configuration management
- **Settings** (`settings.py`) - Global settings singleton (`get_settings()`); never instantiate `Settings()` directly
- **IntentPatterns** (`intent_patterns.yaml`) - Regex/keyword catalog for intent detection
- **EnvironmentLoader** - Environment variable management

#### Supporting subpackages
- **Events** (`deile/events/`) - Event bus / observer wiring used for hot-reload and progress emission
- **Infrastructure** (`deile/infrastructure/`) - Adapters for external integrations (the hexagonal "right side")
- **Storage** (`deile/storage/`) - Persistence primitives (SQLite, file system) shared by orchestration and memory
- **Plugins** (`deile/plugins/`) - Third-party extension surface
- **Evolution** (`deile/evolution/`) - Long-term learning / feedback-loop machinery

### Key Dependencies & Integration Points
- **google-generativeai**: Gemini API integration with function calling
- **pydantic**: Data validation and schema enforcement
- **rich**: Terminal UI components and formatting
- **aiofiles**: Asynchronous file operations
- **watchdog**: File system monitoring for hot-reload
- **psutil**: System resource monitoring
- **chardet**: Automatic encoding detection

### Pydantic Models & Data Structures
- **ToolSchema** - Tool parameter validation
- **ToolContext** - Execution context data
- **ToolResult** - Standardized tool responses
- **CommandContext** - Command execution context
- **CommandResult** - Command execution results
- **IntentPattern** - Intent detection patterns
- **WorkflowStep** - Orchestration step definition
- **TaskState** - Task execution state
- **PersonaConfig** - Persona configuration
- **MemoryEntry** - Memory storage structure
- **SecurityRule** - Permission rule definition
- **AuditEvent** - Audit log entry

### Design Patterns Implementation

#### Mediator Pattern (DeileAgent)
```python
class DeileAgent:
    def __init__(self):
        self.tool_registry = get_tool_registry()
        self.parser_registry = get_parser_registry()
        self.command_registry = get_command_registry()
        self.orchestrator = WorkflowExecutor()
        self.memory = MemoryManager()
        # Agent mediates all component interactions
```

#### Registry Pattern (Dynamic Discovery)
```python
class ToolRegistry:
    def auto_discover(self):
        # Automatic tool discovery in modules
        # Dynamic loading with validation
        # Function calling generation for Gemini
```

#### Observer Pattern (Hot-Reload)
```python
class PersonaConfigHandler(FileSystemEventHandler):
    def on_modified(self, event):
        # Auto-reload persona instructions
        # Invalidate caches
        # Notify dependent components
```

#### Strategy Pattern (Model Selection)
```python
class ModelRouter:
    def select_provider(self, context, session):
        # Context-aware provider selection
        # Fallback strategies
        # Load balancing logic
```

#### Command Pattern (Slash Commands)
```python
class SlashCommand(ABC):
    @abstractmethod
    async def execute(self, context: CommandContext) -> CommandResult:
        # Encapsulated command execution
        # Validation and error handling
```
