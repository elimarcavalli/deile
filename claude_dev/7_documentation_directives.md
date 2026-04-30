## 📋 Documentation Directives

When generating documentation for DEILE features, create comprehensive technical documentation that serves as both implementation record and architectural reference.

### 1. Overview
- Clear description of the feature's purpose and problem it solves
- Integration with DEILE's autonomous agent capabilities
- Impact on system behavior and user experience
- Architectural significance within the overall system

### 2. Architectural Decisions
- Rationale for chosen design patterns (Registry, Mediator, Observer, etc.)
- Trade-offs between different implementation approaches
- Async/await patterns and concurrency considerations
- Memory system integration strategy
- Security implications and permission requirements

### 3. Component Architecture
**Core Components**:
- List of new or modified modules with their responsibilities
- Integration with existing registries (tool, command, parser)
- Memory layer interactions and data flow
- LLM integration points and function calling usage

**Infrastructure**:
- External dependencies and API integrations
- Storage requirements (SQLite, file system)
- Configuration changes needed
- Performance optimization strategies

### 4. Implementation Details
**Class Structure**:
```
ComponentName
├── Properties
│   ├── name: str
│   ├── description: str
│   └── configuration: dict
├── Methods
│   ├── __init__(): Initialization logic
│   ├── async execute(): Main execution
│   └── async validate(): Validation logic
└── Integration Points
    ├── Registry registration
    ├── Memory system hooks
    └── Event subscriptions
```

### 5. API Specification
For each new interface or endpoint:
| Method | Interface | Parameters | Return Type | Exceptions | Async |
|--------|-----------|------------|-------------|------------|--------|
| execute | Tool | ToolContext | ToolResult | ToolError | Yes |
| parse | Parser | str, ParseContext | ParseResult | ParseError | Yes |

### 6. Configuration Schema
```yaml
feature_name:
  enabled: bool
  settings:
    timeout: int  # seconds
    retry_count: int
    cache_ttl: int  # seconds
  security:
    required_permission: str
    risk_level: str  # low/medium/high
  memory:
    store_in_episodic: bool
    consolidation_interval: int
```

### 7. Security Implementation
- Permission requirements and validation logic
- Input sanitization and validation patterns
- Audit logging integration points
- Risk assessment and mitigation strategies
- Sandboxing requirements for dangerous operations

### 8. Testing Strategy
**Unit Tests**:
```python
# Test structure example
async def test_component_basic_functionality():
    # Setup
    component = Component()
    
    # Execute
    result = await component.execute(test_input)
    
    # Assert
    assert result.success
    assert result.data == expected_output
```

**Integration Tests**:
- Full workflow testing scenarios
- Registry integration validation
- Memory system interaction tests
- Permission enforcement tests

**Security Tests**:
- Input validation edge cases
- Permission bypass attempts
- Resource exhaustion scenarios
- Injection attack prevention

### 9. Usage Examples
**CLI Interaction**:
```bash
# Basic usage
> analyze the codebase for security vulnerabilities

# Advanced workflow
> create a comprehensive refactoring plan for the authentication module

# Tool invocation
> /run security_scan --depth deep --include-dependencies
```

**Programmatic Usage**:
```python
# Direct tool usage
tool = SecurityScanTool()
context = ToolContext(args={"path": "./src", "depth": "deep"})
result = await tool.execute(context)

# Through agent
agent = DeileAgent()
response = await agent.process("scan for vulnerabilities")
```

### 10. Performance Characteristics
- Time complexity analysis for main operations
- Memory usage patterns and optimization strategies
- Caching effectiveness and invalidation strategies
- Concurrent execution capabilities and limitations
- Resource consumption under various loads

### 11. Monitoring & Observability
- Key metrics to track (execution time, success rate, resource usage)
- Logging patterns and important log messages
- Health check implementation
- Performance profiling points
- Alert conditions and thresholds

### 12. Migration & Deployment
- Backward compatibility considerations
- Data migration requirements if any
- Configuration migration steps
- Rollback procedures
- Feature flag implementation if applicable

### 13. Troubleshooting Guide
- Common issues and solutions
- Debug mode usage for problem diagnosis
- Log interpretation guide
- Performance tuning recommendations
- FAQ section for typical problems

### 14. Future Considerations
- Identified extension points
- Potential optimizations
- Known limitations and workarounds
- Roadmap integration possibilities
- Technical debt notes
