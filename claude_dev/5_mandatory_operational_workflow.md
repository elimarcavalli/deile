## 🔄 Mandatory Operational Workflow

This workflow has 7 phases. **Not every phase applies to every task** — first match the task to a tier in the **Scope gates** table below; that decides which phases run. The two strongest gates ("wait for user approval" in Phase 2, "do not generate docs until testing is confirmed" in Phase 6) only fire on the tiers marked.

### Scope gates — pick your lane first

| Tier | What it looks like | Phases that run | User-approval gate? | Doc-generation gate? |
|---|---|---|---|---|
| **Trivial** | Typo, whitespace, single-line tweak — already exempt per doc 0 | none | no | no |
| **Small** | Single-file fix; no new public symbol; no public-contract change | 1, 3, 4, 5 | no | no |
| **Medium** | New public symbol, single subpackage; or new test-only file | 1, 2 (brief), 3, 4, 5, 7 | only if user requests | no |
| **Large** | New tool / command / parser; ≥2 subpackages; new feature; cross-module refactor | **1–7 (full)** | **yes — wait before Phase 3** | **yes — wait for the user's testing confirmation** |

Rules:
- If you are unsure between two tiers, **pick the larger one**.
- If scope grows mid-task, **escalate the tier** and run the now-required phases retroactively before declaring the task done.
- Phases below are written for the Large tier. Each `### Phase N` heading is annotated with the minimum tier that triggers it; smaller tiers skip the rest.

### Phase 1: Intent Analysis & Understanding *(Small+)*
- Parse the user's request through the Intent Analyzer to identify the underlying goal
- If intent confidence is below threshold, ask clarifying questions until clear understanding is achieved
- Analyze whether this requires a single tool execution or multi-step workflow orchestration
- Consider security implications and required permissions for the intended operations
- State your understanding of the intent and proposed approach for validation

### Phase 2: Architectural Design & Planning *(Medium+ — brief at Medium, full plan + approval gate at Large)*
Before any implementation, present a comprehensive architectural plan including:
- **Component Analysis**: Identify affected modules (tools, parsers, commands, personas)
- **Dependency Mapping**: New dependencies and integration points
- **Interface Contracts**: New or modified interfaces with type definitions
- **Registry Updates**: Tools, commands, or parsers to be registered
- **Memory Impact**: How this affects working, episodic, semantic, or procedural memory
- **Security Assessment**: Required permissions and audit considerations
- **Performance Analysis**: Async patterns, caching needs, and resource usage
- **Test Strategy**: Unit, integration, and security test requirements

**Large tier only: wait for user approval before proceeding to implementation.** At Medium tier, present the brief plan and proceed unless the user explicitly asks to review.

### Phase 3: Implementation Following Clean Architecture *(Small+)*
- Structure code following hexagonal architecture with clear layer separation
- Implement using async/await patterns for all I/O operations
- Use Pydantic models for all data validation and type safety
- Follow the Registry Pattern for any extensible components
- Apply SOLID principles rigorously, especially Single Responsibility
- Implement comprehensive error handling with specific exception types
- Add appropriate logging for debugging and audit purposes
- Ensure all security validations are in place

### Phase 4: Testing & Validation *(Small+)*
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

### Phase 5: Delivery & Testing Instructions *(Small+)*
- Present the final, validated implementation with clear file paths
- Provide specific test commands using pytest with example assertions
- Include integration test scenarios demonstrating the feature (Medium+)
- Supply example usage through the CLI interface (Medium+)
- Document any new configuration requirements
- **At Large tier**, end the turn by asking the user to test, in the conversation language, and stating that documentation will be generated only after they confirm — do not proceed to Phase 6 unprompted.
- **At Small / Medium tiers**, simply state that the change is ready and stop; Phase 6 does not run.

### Phase 6: Documentation Generation *(Large only)*
- **DO NOT** generate documentation until the user confirms successful testing
- Once confirmed, request a concise feature title from the user
- Generate comprehensive documentation following the Documentation Directives
- Include architecture decisions, implementation details, and usage examples
- Propose filename: `docs/YYMMDD_HHMM_FEATURE_TITLE.md`
- Present the complete documentation content

### Phase 7: Integration Checklist *(Medium+)*
After implementation is complete (and, at Large tier, after documentation approval in Phase 6), provide an integration checklist:
- [ ] Update relevant registries (tool_registry, command_registry, parser_registry)
- [ ] Add configuration entries if needed
- [ ] Update intent patterns if applicable
- [ ] Extend test suites with new test cases
- [ ] Update README.md if adding user-facing features
- [ ] Consider persona instruction updates if behavior changes
- [ ] Verify hot-reload functionality works correctly
- [ ] Check audit logging captures new operations
