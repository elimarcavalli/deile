## 🛏️ Core Architectural Principles (Non-negotiable) - CURRENT IMPLEMENTATION

### How to use this doc

Below is the full principles catalog. Use the **trigger index** below to jump to the section that fires for the situation in front of you. If a row matches what you are about to write, read the linked section before the first Write/Edit.

### Triggers & anti-patterns index

| Code situation (what you are about to write) | Apply section | ❌ Never |
|---|---|---|
| New class extending `BaseTool`, `SlashCommand`, or `BaseParser` | Registry Pattern Excellence + Tool System Design | Manual instantiation without going through the registry; missing `@register_*` decorator or explicit `registry.register(...)` |
| Function that performs file/network/DB I/O | Asynchronous by Design | `time.sleep`, blocking `open()`/`requests` in async path, missing `await`, sync call inside `async def` |
| New third-party dependency wired into `deile/core/` or `deile/orchestration/` | Clean Architecture Implementation | Importing the lib directly inside core/orchestration — route it through an adapter in `deile/infrastructure/` |
| Code that reads a user-supplied path, command, or query | Security-First Development | Skipping permission check, raw input into shell/SQL, missing audit log entry |
| State that must survive across turns or sessions | Memory System Architecture | Module-global dicts as cache; pick a layer (working / episodic / semantic / procedural) |
| `try: … except Exception:` block | Error Handling Philosophy | Silent except, generic catch without re-raise/log, swallowing `asyncio.CancelledError` |
| New config key being read | Configuration Management | Reading `os.environ` or YAML directly — go through the `settings.py` singleton |
| Component meant to be plugged in / discovered | Extensibility Principles + Registry Pattern | `if isinstance(...)` chains branching on plugin types instead of dispatching via the registry |
| Public method on a new module | Documentation Standards | Public symbol without docstring or type hints |
| Multi-step operation that can fail partway | Orchestration Principles | No rollback strategy / no progress emission |

If multiple rows match, apply all matching sections. If your situation is not in the table but you are writing inside `deile/`, default to: **Asynchronous by Design** + **Registry Pattern** + the section nearest your subpackage's responsibility.

---

### Agent Autonomy (Primary Principle)
- **Intent-Driven Execution**: All operations begin with intent analysis to understand user goals
- **Workflow Orchestration**: Complex tasks automatically decomposed into executable steps
- **Self-Directed Learning**: Pattern extraction and reuse from successful operations
- **Approval-Based Safety**: Risk assessment with user approval for sensitive operations
- **Continuous Adaptation**: Real-time adjustment based on execution feedback

### Clean Architecture Implementation
- **Hexagonal Architecture**: Core domain isolated from external dependencies
- **Dependency Inversion**: Abstractions define contracts, implementations depend on abstractions
- **Layer Separation**: Clear boundaries between UI, Application, Domain, and Infrastructure
- **Port & Adapters**: External integrations through well-defined interfaces
- **Domain-Driven Design**: Business logic encapsulated in domain entities

### Registry Pattern Excellence
- **Dynamic Discovery**: Automatic component discovery at runtime
- **Plugin Architecture**: Extensible without modifying core code
- **Type Safety**: Pydantic validation for all registered components
- **Hot-Reload Support**: Components updated without restart
- **Capability Negotiation**: Components declare and validate capabilities

### Asynchronous by Design
- **Full Async/Await**: Every I/O operation is non-blocking
- **Event-Driven Architecture**: Observer pattern for real-time updates
- **Concurrent Execution**: Parallel processing where dependencies allow
- **Resource Pooling**: Connection and thread pool management
- **Graceful Degradation**: System remains responsive under load

### Memory System Architecture
- **Multi-Layer Design**: Working, Episodic, Semantic, Procedural layers
- **Consolidation Engine**: Background optimization and compression
- **Pattern Recognition**: Automatic extraction of reusable patterns
- **Context Retrieval**: Parallel search across all memory types
- **Performance Optimization**: Caching and indexing for fast access

### Security-First Development
- **Permission System**: Fine-grained access control with inheritance
- **Audit Logging**: Complete operation tracking with immutability
- **Input Validation**: Sanitization at all system boundaries
- **Sandboxed Execution**: Dangerous operations in isolated environments
- **Principle of Least Privilege**: Minimal permissions by default

### Tool System Design Principles
- **Single Responsibility**: Each tool has one well-defined purpose
- **Schema-Driven**: JSON Schema validation for all parameters
- **Error Recovery**: Comprehensive error handling with retry logic
- **Security Levels**: Tools classified by risk and required permissions
- **Function Calling**: Automatic generation for LLM integration

### Orchestration Principles
- **Task Decomposition**: Complex goals broken into atomic steps
- **Dependency Management**: Automatic ordering based on dependencies
- **State Management**: Persistent task state with SQLite
- **Rollback Capability**: Automatic rollback on failure
- **Progress Tracking**: Real-time visibility into execution

### UI/UX Excellence
- **Rich Terminal Experience**: Beautiful CLI with themes and components
- **Progressive Disclosure**: Information revealed as needed
- **Contextual Help**: Intelligent assistance based on current state
- **Error Messaging**: Clear, actionable error messages
- **Responsive Design**: UI remains interactive during operations

### Testing & Quality Principles
- **Test-Driven Development**: Tests written before implementation
- **Comprehensive Coverage**: minimum gate enforced by `pytest.ini` (`--cov-fail-under`); aim well above the floor
- **Integration Testing**: Full system integration tests
- **Security Testing**: Dedicated security test suite
- **Performance Testing**: Load and stress testing

### Configuration Management
- **Environment Separation**: Dev, staging, prod configurations
- **Schema Validation**: All configurations validated at load
- **Hot-Reload**: Configuration changes without restart
- **Secure Secrets**: API keys and sensitive data properly managed
- **Version Control**: Configuration versioning and migration

### Error Handling Philosophy
- **Fail-Fast**: Early detection and reporting of issues
- **Graceful Recovery**: Automatic recovery where possible
- **User Notification**: Clear communication of issues
- **Diagnostic Information**: Sufficient context for debugging
- **Retry Logic**: Exponential backoff for transient failures

### Performance Optimization
- **Lazy Loading**: Resources loaded only when needed
- **Caching Strategy**: Multi-level caching with invalidation
- **Connection Pooling**: Efficient resource utilization
- **Background Processing**: Long operations off main thread
- **Memory Management**: Proper cleanup and garbage collection

### Documentation Standards
- **Code as Documentation**: Self-documenting code with clear naming
- **Inline Comments**: Complex logic explained inline
- **API Documentation**: Complete docstrings for public APIs
- **Architecture Docs**: High-level design documentation maintained
- **Usage Examples**: Practical examples for all features

### Extensibility Principles
- **Open/Closed Principle**: Open for extension, closed for modification
- **Interface Segregation**: Small, focused interfaces
- **Composition over Inheritance**: Prefer composition patterns
- **Event-Driven Extensions**: Hooks for extending behavior
- **Plugin Ecosystem**: Support for third-party extensions
