## 🔄 Mandatory Operational Workflow

You MUST follow this sequence of operations for every task. This workflow ensures architectural integrity, security, and maintainability.

### Phase 1: Intent Analysis & Understanding
- Parse the user's request through the Intent Analyzer to identify the underlying goal
- If intent confidence is below threshold, ask clarifying questions until clear understanding is achieved
- Analyze whether this requires a single tool execution or multi-step workflow orchestration
- Consider security implications and required permissions for the intended operations
- State your understanding of the intent and proposed approach for validation

### Phase 2: Architectural Design & Planning
Before any implementation, present a comprehensive architectural plan including:
- **Component Analysis**: Identify affected modules (tools, parsers, commands, personas)
- **Dependency Mapping**: New dependencies and integration points
- **Interface Contracts**: New or modified interfaces with type definitions
- **Registry Updates**: Tools, commands, or parsers to be registered
- **Memory Impact**: How this affects working, episodic, semantic, or procedural memory
- **Security Assessment**: Required permissions and audit considerations
- **Performance Analysis**: Async patterns, caching needs, and resource usage
- **Test Strategy**: Unit, integration, and security test requirements

**Wait for user approval before proceeding to implementation.**

### Phase 3: Implementation Following Clean Architecture
- Structure code following hexagonal architecture with clear layer separation
- Implement using async/await patterns for all I/O operations
- Use Pydantic models for all data validation and type safety
- Follow the Registry Pattern for any extensible components
- Apply SOLID principles rigorously, especially Single Responsibility
- Implement comprehensive error handling with specific exception types
- Add appropriate logging for debugging and audit purposes
- Ensure all security validations are in place

### Phase 4: Testing & Validation
Before presenting the implementation, perform thorough validation:
- **Type Safety**: Verify all Pydantic models and type hints are correct
- **Async Patterns**: Ensure proper async/await usage without blocking
- **Error Scenarios**: Test edge cases, null inputs, and failure conditions
- **Security Checks**: Validate permission requirements and input sanitization
- **Memory Management**: Check for proper resource cleanup and disposal
- **Performance**: Verify no blocking operations in async contexts
- **Integration Points**: Ensure compatibility with existing registries
- **Documentation**: Confirm all public APIs have proper docstrings

Refine the implementation based on this review.

### Phase 5: Delivery & Testing Instructions
- Present the final, validated implementation with clear file paths
- Provide specific test commands using pytest with example assertions
- Include integration test scenarios demonstrating the feature
- Supply example usage through the CLI interface
- Document any new configuration requirements
- Conclude with: **"Please test the implementation using the provided test cases. Once validated, I will generate comprehensive technical documentation. Ready to proceed with testing?"**

### Phase 6: Documentation Generation
- **DO NOT** generate documentation until the user confirms successful testing
- Once confirmed, request a concise feature title from the user
- Generate comprehensive documentation following the Documentation Directives
- Include architecture decisions, implementation details, and usage examples
- Propose filename: `docs/YYMMDD_HHMM_FEATURE_TITLE.md`
- Present the complete documentation content

### Phase 7: Integration Checklist
After documentation approval, provide an integration checklist:
- [ ] Update relevant registries (tool_registry, command_registry, parser_registry)
- [ ] Add configuration entries if needed
- [ ] Update intent patterns if applicable
- [ ] Extend test suites with new test cases
- [ ] Update README.md if adding user-facing features
- [ ] Consider persona instruction updates if behavior changes
- [ ] Verify hot-reload functionality works correctly
- [ ] Check audit logging captures new operations
