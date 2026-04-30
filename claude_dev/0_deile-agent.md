# DEILE knowledge-base index

This file is the **decision tree** that maps each situation to one of the eight `claude_dev/` docs. Files 0, 1, and 8 are auto-loaded by `CLAUDE.md`; the rest are **read-on-demand** — open them with the `Read` tool **before** starting work whose trigger fires, never preemptively.

| Doc | Path | Read when |
|---|---|---|
| Knowledge-base index | `claude_dev/0_deile-agent.md` | Auto-loaded — this file. |
| Agent Persona | `claude_dev/1_agent_persona.md` | Auto-loaded — defines your role. |
| System Architecture | `claude_dev/2_system_architecture_context.md` | Need component, technology, or overall architecture details (e.g. "where does the parser sit?", "which registry owns X?"). |
| Brief Project Doc | `claude_dev/3_brief_project_documentation.md` | Need full project scope/capabilities — broad design questions, scoping conversations, or onboarding context. |
| Core Architectural Principles | `claude_dev/4_core_architectural_principles.md` | Creating a new class, function, or module — non-negotiable rules live here. |
| Mandatory Operational Workflow | `claude_dev/5_mandatory_operational_workflow.md` | Implementing an improvement or feature — describes the required steps. |
| Code Generation Directives | `claude_dev/6_code_generation_directives.md` | Writing code (new tool, command, parser, pattern). |
| Documentation Directives | `claude_dev/7_documentation_directives.md` | Generating, updating, or restructuring documentation. |
| System-Specific Guidelines | `claude_dev/8_system_specific_guidelines.md` | Auto-loaded — async/registry/Gemini/memory specifics. |

All paths use forward slashes (`/`).
