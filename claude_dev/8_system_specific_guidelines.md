## 🚀 System-Specific Guidelines for DEILE Development

**Scope of this doc**: it is auto-loaded into every conversation, so it must stay small and contain only DEILE-specific invariants that you need *every* turn. Anything that fits one of the buckets below does **not** belong here:

- Generic SE checklists (deployment readiness, monitoring, code quality) → out of scope.
- Full code snippets (tool/command/parser templates, security/error/memory examples) → live in doc **6** (read-on-demand).
- Project gotchas already covered in `CLAUDE.md` (settings singleton, `GOOGLE_API_KEY`, two `config/` directories, SQL responsibility, persona MD format) → do not duplicate.
- Workflow phases and integration checklists → live in doc **5**.

---

### Async/Await compliance (always-on)
- Every I/O operation MUST be `async`. Inside an `async def`: never `time.sleep` (use `asyncio.sleep`), never blocking `open()`, never sync `requests`, never any sync DB driver.
- Use `asyncio.gather()` for concurrent independent I/O.
- Use `async with` for resources that must be released.
- A missing `await` is a silent failure — when reviewing your own code, scan for awaitables that are not awaited.

### Registry integration (always-on)
DEILE uses the Registry Pattern for tools, commands, parsers, and personas. Anything in those categories must be discoverable. Two ways:

```python
# Decorator-based (preferred for built-ins)
@register_tool
class CustomTool(BaseTool):
    ...

# Explicit registration (for plugins, factories, conditional registration)
registry = get_tool_registry()
registry.register(CustomTool())
```

Where things live:
- Tools → `deile/tools/*.py` (no `builtin/` subfolder; tools are siblings of `registry.py` and `base.py`).
- Commands → `deile/commands/builtin/*.py`.
- Parsers → `deile/parsers/`.
- Persona instructions → `deile/personas/instructions/*.md` (Markdown, no Python change needed).

Full schema/execute/anti-pattern templates: doc **6**.

### Gemini API integration (DEILE-specific)
Function declarations must be generated from each tool's schema:

```python
def get_function_declaration(self) -> FunctionDeclaration:
    return FunctionDeclaration(
        name=self.name,
        description=self.description,
        parameters=self.get_schema().to_gemini_schema(),
    )
```

When sending a file alongside a prompt, upload it via `genai.upload_file(path)` and pass the resulting object as part of the message:

```python
file_obj = genai.upload_file(path)
response = await chat.send_message([prompt_text, file_obj])
```

The active model is configured in `deile/config/manager.py` and `deile/config/settings.py`. Read those for the current default; never hard-code a model id in tool, command, or parser code.

### Memory layers — pick the right one
Four layers, picked by purpose, not by convenience. Method names and signatures live in `deile/memory/*.py` — open the layer's source before calling, signatures evolve. Doc **6** has the canonical call shapes.

| Layer | Module | Purpose | Lifetime |
|---|---|---|---|
| **Working** | `working_memory.py` | Short-lived transient state inside one task/turn | TTL (seconds–minutes) |
| **Episodic** | `episodic_memory.py` | Session-scoped event log (what happened, when) | Session |
| **Semantic** | `semantic_memory.py` | Facts and knowledge that persist across sessions | Persistent |
| **Procedural** | `procedural_memory.py` | Learned patterns / skills extracted from prior runs | Persistent, evolves |

`MemoryManager` (`deile/memory/memory_manager.py`) exposes both per-layer access (`memory_manager.working_memory.<method>`) and a top-level `store_interaction(...)` convenience for the common path.

❌ Never: stash cross-turn state in module globals or class attributes; store secrets/PII in any layer; write to memory without `await`; invent method names — open the layer's source file first.
