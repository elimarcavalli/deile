## 🚀 System-Specific Guidelines for DEILE Development

### Async/Await Compliance
- ALL I/O operations MUST use async/await patterns
- Never use blocking calls in async functions (no `time.sleep`, use `asyncio.sleep`)
- Use `asyncio.gather()` for concurrent operations
- Implement proper async context managers with `async with`
- Always await async operations - missing awaits cause silent failures

### Registry Integration Standards
```python
# Tool Registration Example
@register_tool
class CustomTool(BaseTool):
    """Tool must be in a module under deile/tools/"""
    pass

# Manual Registration
registry = get_tool_registry()
registry.register(CustomTool())

# Discovery Pattern
# Place file in deile/tools/builtin/ for auto-discovery
```

### Gemini API Integration
```python
# Function Calling Pattern
def get_function_declaration(self) -> FunctionDeclaration:
    return FunctionDeclaration(
        name=self.name,
        description=self.description,
        parameters=self.get_schema().to_gemini_schema()
    )

# File Handling with Gemini
if file_data:
    file_obj = genai.upload_file(path)
    message_parts = [text, file_obj]
    response = await chat.send_message(message_parts)
```

### Memory System Usage
```python
# Working Memory (Short-term)
await memory.working.set("key", value, ttl=300)

# Episodic Memory (Session History)  
await memory.episodic.record_event(
    event_type="tool_execution",
    details={"tool": tool_name, "result": result}
)

# Semantic Memory (Knowledge)
await memory.semantic.store_concept(
    concept="design_pattern",
    data={"name": "Registry", "usage": "..."}
)

# Procedural Memory (Skills)
await memory.procedural.learn_pattern(
    pattern_type="workflow",
    steps=workflow_steps
)
```

### Error Message Standards
```python
# User-Facing Errors
raise UserError(
    "Could not complete the operation",
    details="The file 'config.yaml' was not found",
    suggestion="Please ensure the file exists in the project root"
)

# System Errors (with context)
logger.error(
    "Database operation failed",
    extra={
        "operation": "task_update",
        "task_id": task_id,
        "error": str(e)
    },
    exc_info=True
)
```

### Testing Requirements
```bash
# Run specific test categories
pytest -m unit          # Unit tests only
pytest -m integration   # Integration tests
pytest -m security      # Security tests
pytest -m orchestration # Orchestration tests

# With coverage
pytest --cov=deile --cov-report=html

# Specific module testing
pytest tests/test_tools.py -v

# Performance testing
pytest tests/performance/ --benchmark-only
```

### Configuration Best Practices
```yaml
# config/settings.yaml
deile:
  agent:
    model: gemini-1.5-pro-latest
    temperature: 0.7
    max_tokens: 8000
  
  tools:
    enabled:
      - file_tools
      - bash_tools
      - search_tools
    security_level: medium
  
  memory:
    working_ttl: 300
    episodic_limit: 1000
    consolidation_interval: 3600
```

### Security Requirements
```python
# Permission Checking
if not await self.check_permission("file:write", path):
    raise PermissionError(f"Write access denied for {path}")

# Input Validation
validated_input = self.validate_and_sanitize(user_input)

# Audit Logging
await self.audit_log(
    "sensitive_operation",
    user=context.user,
    details={"action": "delete_file", "path": path}
)
```

### CLI Output Standards
```python
# Rich Console Output
from rich.console import Console
from rich.table import Table

console = Console()

# Success Message
console.print("[green]✓[/green] Operation completed successfully")

# Error Message
console.print("[red]✗[/red] Operation failed: {error}")

# Progress Indicator
with console.status("Processing..."):
    await long_running_operation()

# Data Table
table = Table(title="Results")
table.add_column("Name")
table.add_column("Status")
console.print(table)
```

### Persona Development Guidelines
```markdown
# personas/instructions/custom_persona.md
## Custom Persona

### Core Attributes
- **Role**: Specialized development assistant
- **Expertise**: Domain-specific knowledge
- **Style**: Professional and concise

### Capabilities
- Advanced code analysis
- Pattern recognition
- Automated refactoring

### Instructions
When analyzing code:
1. Identify patterns and anti-patterns
2. Suggest improvements with examples
3. Provide implementation steps
```

### Performance Optimization Checklist
- [ ] Use async/await for all I/O operations
- [ ] Implement caching where appropriate (working memory)
- [ ] Pool connections and resources
- [ ] Lazy load expensive resources
- [ ] Profile with `cProfile` for bottlenecks
- [ ] Use `asyncio.gather()` for parallel operations
- [ ] Implement circuit breakers for external APIs
- [ ] Add progress indicators for long operations

### Code Quality Standards
- [ ] 100% type hints for public APIs
- [ ] Docstrings for all public methods
- [ ] Comprehensive error handling
- [ ] Logging at appropriate levels
- [ ] Unit tests with >80% coverage
- [ ] Integration tests for workflows
- [ ] Security tests for sensitive operations
- [ ] Performance benchmarks for critical paths

### Deployment Readiness
- [ ] Environment variables documented
- [ ] Configuration schema validated
- [ ] Dependencies in requirements.txt
- [ ] README updated with new features
- [ ] Migration guide if breaking changes
- [ ] Performance impact assessed
- [ ] Security review completed
- [ ] Documentation generated

### Monitoring & Maintenance
- [ ] Metrics exposed for monitoring
- [ ] Health check endpoints implemented
- [ ] Logging follows structured format
- [ ] Error recovery mechanisms in place
- [ ] Resource cleanup verified
- [ ] Memory leaks checked
- [ ] Thread safety validated
- [ ] Graceful shutdown implemented

### Common Pitfalls to Avoid
- **Never** use synchronous I/O in async functions
- **Never** catch generic Exception without re-raising
- **Never** store sensitive data in logs
- **Never** skip input validation
- **Never** ignore async function warnings
- **Never** hardcode configuration values
- **Never** bypass the permission system
- **Never** forget to close resources

### Development Commands Reference
```bash
# Start DEILE
python3 deile.py

# Run with debug mode
python3 deile.py --debug

# Run specific tests
pytest tests/test_specific.py -v

# Check code quality
ruff check deile/
isort --check-only deile/
black --check deile/

# Generate coverage report
pytest --cov=deile --cov-report=html
open htmlcov/index.html

# Profile performance
python3 -m cProfile -o profile.stats deile.py
python3 -m pstats profile.stats

# Security scanning
bandit -r deile/
safety check

# Complexity analysis
radon cc deile/ -a
```

### Integration Points Checklist
When adding new features, ensure integration with:
- [ ] Tool Registry (for new tools)
- [ ] Command Registry (for new commands)
- [ ] Parser Registry (for new parsers)
- [ ] Intent Patterns (for new intents)
- [ ] Memory System (for state persistence)
- [ ] Permission System (for access control)
- [ ] Audit Logger (for sensitive operations)
- [ ] Configuration System (for settings)
- [ ] Test Suite (unit + integration)
- [ ] Documentation (technical + user)
