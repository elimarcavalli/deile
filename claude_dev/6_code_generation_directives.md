## ⚙️ Code Generation Directives - CURRENT SYSTEM STANDARDS

### Snippet picker — go straight to the right section

| You are creating / editing… | Use section |
|---|---|
| New file in `deile/tools/**/*.py` | **Tool Development Standards** |
| New file in `deile/commands/**/*.py` | **Command Implementation Pattern** |
| New file in `deile/parsers/**/*.py` | **Parser Development Guidelines** |
| Storing / retrieving state across turns or sessions | **Memory System Integration** |
| Permission check, audit log, input sanitization | **Security Implementation Requirements** |
| Adding or changing intent patterns / regex | **Intent Analysis Integration** |
| New exception class or non-trivial `except` block | **Error Handling Best Practices** |
| New `test_*.py` or pytest marker | **Testing Requirements** |
| New config key, env var, or settings field | **Configuration Management** |
| Repeated, long-running, or pooled operation | **Performance Optimization Patterns** |
| Any code emitting log lines | **Logging Standards** |
| Any function with I/O | **Async-First Development** (always) + the row above that matches |

If multiple rows match, apply each. If your file matches none and you are still writing inside `deile/`, default to: **Async-First** + **Registry Pattern** + the snippet for the nearest analog. Each snippet section ends with a ❌ line listing the most common deviation — read it before copying the snippet.

---

### Async-First Development (Critical Requirement)
- **Always Async**: Every function that performs I/O MUST be async
- **Await Properly**: Never forget to await async operations
- **No Blocking**: Never use blocking I/O in async contexts
- **Concurrent Execution**: Use asyncio.gather() for parallel operations
- **Resource Management**: Async context managers for cleanup

### Registry Pattern Implementation
- **Auto-Discovery**: Tools must support automatic discovery
- **Registration**: Use decorators or explicit registration
- **Validation**: Validate all registered components with Pydantic
- **Type Safety**: Full type hints for all registry methods
- **Hot-Reload**: Support dynamic reloading of components

### Tool Development Standards
```python
class CustomTool(BaseTool):
    name = "tool_name"
    description = "Clear description for LLM"
    category = "tool_category"
    security_level = SecurityLevel.MEDIUM
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            properties={
                "param": {"type": "string", "description": "Parameter description"}
            },
            required=["param"]
        )
    
    async def execute(self, context: ToolContext) -> ToolResult:
        try:
            # Implementation with proper error handling
            result = await self._perform_operation(context.args)
            return ToolResult(success=True, data=result)
        except Exception as e:
            return ToolResult(success=False, error=str(e))
```

❌ Never: hard-code parameter validation inside `execute()` (use `get_schema()`); return raw data or raise out of `execute()` (always wrap in `ToolResult`); omit `security_level`; perform sync I/O inside `execute()`; instantiate the tool manually instead of registering it.

### Command Implementation Pattern
```python
class CustomCommand(SlashCommand):
    name = "command"
    description = "Command description"
    aliases = ["cmd", "c"]
    
    async def execute(self, context: CommandContext) -> CommandResult:
        # Validate arguments
        if not self._validate_args(context.args):
            return CommandResult(
                success=False,
                message="Invalid arguments",
                display_type=DisplayType.ERROR
            )
        
        # Execute with proper async pattern
        try:
            result = await self._process_command(context)
            return CommandResult(
                success=True,
                data=result,
                display_type=DisplayType.RICH
            )
        except Exception as e:
            logger.error(f"Command failed: {e}")
            return CommandResult(
                success=False,
                error=str(e),
                display_type=DisplayType.ERROR
            )
```

❌ Never: print directly to stdout (use `CommandResult.display_type`); duplicate tool logic inside a command — call the tool through the registry; skip `aliases` if the command has a natural shorthand; mutate global state instead of returning data via `CommandResult`.

### Parser Development Guidelines
```python
class CustomParser(BaseParser):
    name = "custom_parser"
    priority = 50  # 0-100, higher runs first
    
    async def can_parse(self, text: str, context: ParseContext) -> bool:
        # Determine if this parser should handle the input
        return self._matches_pattern(text)
    
    async def parse(self, text: str, context: ParseContext) -> ParseResult:
        try:
            parsed_data = await self._extract_data(text)
            return ParseResult(
                success=True,
                parsed_type="custom_type",
                data=parsed_data
            )
        except Exception as e:
            return ParseResult(
                success=False,
                error=str(e)
            )
```

❌ Never: do heavy work inside `can_parse()` (it runs for every parser on every input — keep it a fast pattern check); set `priority` arbitrarily without checking neighbors; return `ParseResult` with success and no `data`; let exceptions escape `parse()`.

### Memory System Integration

The canonical entry point is `MemoryManager` in `deile/memory/memory_manager.py`. Each layer has its own module under `deile/memory/`. Method names below match the real public API; **always confirm exact parameter shapes against the source file** — this snippet is a routing guide, not a frozen signature spec.

```python
# Top-level convenience (covers most cases)
await memory_manager.store_interaction(...)

# Working Memory  (deile/memory/working_memory.py)
await memory_manager.working_memory.store(...)
await memory_manager.working_memory.store_interaction(...)

# Episodic Memory  (deile/memory/episodic_memory.py)
await memory_manager.episodic_memory.store_episode(...)

# Semantic Memory  (deile/memory/semantic_memory.py)
await memory_manager.semantic_memory.store_knowledge(knowledge_dict)
await memory_manager.semantic_memory.store_correction(interaction_id, correction_data)

# Procedural Memory  (deile/memory/procedural_memory.py)
patterns = await memory_manager.procedural_memory.get_relevant_patterns(query)
```

❌ Never: cache cross-turn state in module globals or class attributes — use the layer that fits (working = TTL'd transient, episodic = session events, semantic = facts/knowledge, procedural = learned skills); store secrets/PII in episodic memory; write to memory without `await`; invent method names — open the layer's module to confirm before calling.

❌ Never: cache cross-turn state in module globals or class attributes — use the appropriate layer; store secrets/PII in episodic memory; write to memory without `await`; pick a layer by convenience (working = TTL'd transient, episodic = session events, semantic = facts/knowledge, procedural = learned skills).

### Security Implementation Requirements
```python
# Permission Checking
async def check_permission(self, resource: str, action: str) -> bool:
    return await self.permission_manager.check(
        resource=resource,
        action=action,
        context=self.security_context
    )

# Audit Logging
async def log_operation(self, operation: str, details: dict):
    await self.audit_logger.log(
        AuditEvent(
            timestamp=datetime.now(),
            operation=operation,
            user=self.current_user,
            details=details,
            risk_level=self._assess_risk(operation)
        )
    )

# Input Sanitization
def sanitize_input(self, user_input: str) -> str:
    # Remove potentially dangerous characters
    sanitized = re.sub(r'[;&|`$()]', '', user_input)
    # Validate against whitelist patterns
    if not self._validate_pattern(sanitized):
        raise ValidationError("Invalid input pattern")
    return sanitized
```

❌ Never: trust user input to reach a shell, SQL, or filesystem call without sanitization; perform a privileged action without `check_permission()` first; log secrets or full request bodies; write your own audit format — use `AuditEvent` so events stay queryable.

### Intent Analysis Integration
```python
# Register Intent Pattern
intent_pattern = IntentPattern(
    pattern=r"create a new (\w+) for (\w+)",
    intent_type="creation",
    confidence_threshold=0.8,
    extractors={
        "entity_type": 1,
        "target": 2
    }
)
await self.intent_analyzer.register_pattern(intent_pattern)

# Analyze User Intent
intent_result = await self.intent_analyzer.analyze(user_message)
if intent_result.confidence > 0.7:
    workflow = await self.workflow_generator.create(intent_result)
    await self.workflow_executor.execute(workflow)
```

### Error Handling Best Practices
```python
class ToolExecutionError(DEILEError):
    """Raised when tool execution fails"""
    pass

class ValidationError(DEILEError):
    """Raised when validation fails"""
    pass

class PermissionError(DEILEError):
    """Raised when permission is denied"""
    pass

# Comprehensive Error Handling
try:
    result = await dangerous_operation()
except PermissionError as e:
    logger.warning(f"Permission denied: {e}")
    return ErrorResponse(code="PERMISSION_DENIED", message=str(e))
except ValidationError as e:
    logger.info(f"Validation failed: {e}")
    return ErrorResponse(code="VALIDATION_FAILED", message=str(e))
except Exception as e:
    logger.error(f"Unexpected error: {e}", exc_info=True)
    return ErrorResponse(code="INTERNAL_ERROR", message="An unexpected error occurred")
```

❌ Never: bare `except:` or silent `except Exception: pass`; catch `asyncio.CancelledError` without re-raising; return generic `Exception` strings to users — map to a typed `DEILEError` subclass with a code.

### Testing Requirements
```python
# Unit Test Example
@pytest.mark.asyncio
async def test_tool_execution():
    tool = CustomTool()
    context = ToolContext(args={"param": "value"})
    
    result = await tool.execute(context)
    
    assert result.success
    assert result.data == expected_data

# Integration Test Example
@pytest.mark.integration
async def test_workflow_execution():
    async with TestAgent() as agent:
        response = await agent.process("create a new feature")
        assert "workflow_executed" in response
        assert response["steps_completed"] == 5

# Security Test Example
@pytest.mark.security
async def test_permission_enforcement():
    with pytest.raises(PermissionError):
        await restricted_operation(user="guest")
```

### Configuration Management
```python
# Settings with Pydantic
class ToolSettings(BaseSettings):
    enabled: bool = True
    timeout: int = 30
    retry_count: int = 3
    security_level: SecurityLevel = SecurityLevel.MEDIUM
    
    class Config:
        env_prefix = "DEILE_TOOL_"
        case_sensitive = False

# Configuration Loading
settings = ToolSettings()
if settings.enabled:
    registry.register(CustomTool(settings=settings))
```

### Logging Standards
```python
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Structured Logging
logger.info(
    "Tool executed",
    extra={
        "tool": tool_name,
        "duration": execution_time,
        "success": result.success,
        "user": context.user
    }
)

# Debug Logging
logger.debug(f"Processing input: {input[:100]}...")  # Truncate long inputs

# Error Logging with Context
logger.error(
    "Operation failed",
    extra={"operation": op_name, "error": str(e)},
    exc_info=True
)
```

### Performance Optimization Patterns
```python
# Caching with TTL
@cached(ttl=300)
async def expensive_operation(param: str) -> Any:
    # This result will be cached for 5 minutes
    return await compute_expensive_result(param)

# Lazy Loading
class LazyResource:
    def __init__(self):
        self._resource = None
    
    async def get(self):
        if self._resource is None:
            self._resource = await self._load_resource()
        return self._resource

# Connection Pooling
class ConnectionPool:
    def __init__(self, size: int = 10):
        self._pool = asyncio.Queue(maxsize=size)
        self._semaphore = asyncio.Semaphore(size)
    
    async def acquire(self):
        async with self._semaphore:
            return await self._pool.get()
```
