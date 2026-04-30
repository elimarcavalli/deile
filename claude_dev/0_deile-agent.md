# DEILE knowledge-base index

This file is the **decision protocol**: before any non-trivial action, it tells you exactly which `claude_dev/` docs to open.

**Auto-loaded** (always in context): `0`, `1`, `8`.
**Read-on-demand** (open with the `Read` tool **before** the first Write/Edit/Bash-that-mutates): `2`, `3`, `4`, `5`, `6`, `7`. Never preemptive, never skipped when a trigger fires.

## The protocol — run this every turn before touching code

1. Classify the **action** you are about to perform → Table A.
2. Classify the **file path(s)** you are about to write/edit → Table B.
3. Scan the user's request for **keywords** → Table C.
4. **Take the union** of docs from all three tables. Open each unread doc with `Read` before the first mutating tool call.
5. If, partway through, scope grows (new file required, refactor surfaces, new symbol must exist) → **stop, re-run the protocol** with the new scope, and read newly-required docs before continuing.

## Doc reference

| Doc | Path | Covers |
|---|---|---|
| 0 | `claude_dev/0_deile-agent.md` | This protocol (auto-loaded). |
| 1 | `claude_dev/1_agent_persona.md` | Your role (auto-loaded). |
| 2 | `claude_dev/2_system_architecture_context.md` | Component map, tech stack, where each module sits. |
| 3 | `claude_dev/3_brief_project_documentation.md` | Project scope, capabilities, system overview. |
| 4 | `claude_dev/4_core_architectural_principles.md` | Non-negotiable rules: hexagonal, registry, async, security. |
| 5 | `claude_dev/5_mandatory_operational_workflow.md` | Operational workflow with scope gates (Trivial/Small/Medium/Large) and 7 phases — only some phases run per tier. |
| 6 | `claude_dev/6_code_generation_directives.md` | Concrete code patterns: tools, commands, parsers, personas. |
| 7 | `claude_dev/7_documentation_directives.md` | How to write/update docs (sections, structure, depth). |
| 8 | `claude_dev/8_system_specific_guidelines.md` | Async/registry/Gemini/memory specifics (auto-loaded). |

---

## Table A — Action triggers

| If you are about to… | Open |
|---|---|
| Create a new file under `deile/` (any subpackage) | **4, 6** |
| Add a new class, public function, or public method | **4, 6** |
| Refactor code that crosses ≥2 files | **4, 5** |
| Implement a feature (multi-step request, new capability) | **5, 4, 6** |
| Fix a bug touching ≥2 files, or a bug whose fix changes a public contract | **4, 5** |
| Register a new tool / command / parser / persona | **4, 6** (and **2** if uncertain where it plugs in) |
| Modify async/concurrency code, memory layers, or registry mechanics | **4, 6** (8 is already loaded — re-skim it) |
| Change permission rules, audit logging, or input validation | **4, 6** |
| Write or restructure feature documentation in `docs/`, root `README.md`, or `CHANGELOG.md` | **7** |
| Edit `CLAUDE.md` or any file in `claude_dev/` (meta-instructions for Claude itself) | *(no doc — these tune Claude's own behavior; doc 7's feature-doc template does not apply)* |
| Answer an architecture/scope question (read-only, but architectural) | **2** (and **3** if the question is system-wide) |

## Table B — Path triggers (most specific glob wins; union if multiple match)

| Path glob | Open |
|---|---|
| `deile/tools/**/*.py` | **4, 6** |
| `deile/commands/**/*.py` | **4, 6** |
| `deile/parsers/**/*.py` | **4, 6** |
| `deile/core/**/*.py`, `deile/orchestration/**/*.py` | **2, 4, 6** |
| `deile/memory/**/*.py` | **4, 6** (re-skim 8) |
| `deile/security/**/*.py` | **4, 6** |
| `deile/events/**/*.py`, `deile/infrastructure/**/*.py`, `deile/storage/**/*.py` | **4, 6** |
| `deile/evolution/**/*.py`, `deile/plugins/**/*.py` | **4, 6** |
| `deile/ui/**/*.py` | **4, 6** |
| `deile/personas/instructions/*.md` | *(no doc — persona content, not architecture)* |
| `deile/config/**/*.yaml`, `./config/**/*.yaml`, `./config/**/*.json` | **2** if change alters runtime behavior |
| `deile/tests/**/*.py` | *(no doc unless adding new test infra/markers — then **4, 6**)* |
| `docs/**/*.md`, root `README.md`, root `CHANGELOG.md` | **7** |
| `CLAUDE.md`, `claude_dev/*.md` | *(no doc — meta-instructions; treat as agent configuration, not feature docs)* |

## Table C — User-request keyword triggers (Portuguese & English)

| Keywords (substring match, case-insensitive) | Open |
|---|---|
| "architecture", "arquitetura", "where is", "onde fica", "which module", "qual módulo" | **2** |
| "scope", "escopo", "capabilities", "overview", "onboarding", "what can deile do" | **3** |
| "new tool", "novo tool", "new command", "novo comando", "new parser", "novo parser", "new persona", "nova persona" | **4, 6** |
| "feature", "implement", "implementar", "build", "construir", "add support for", "adicionar suporte" | **5, 4, 6** |
| "refactor", "refatorar", "redesign", "restructure", "reestruturar" | **4, 5** |
| "principle", "princípio", "non-negotiable", "rule", "regra arquitetural" | **4** |
| "document", "documentar", "write docs", "atualizar README", "update changelog" | **7** |
| "workflow", "fluxo de trabalho", "process", "phases", "etapas obrigatórias" | **5** |

---

## Exemptions (no docs needed)

- Typo fixes, whitespace, single-line cosmetic edits.
- Renaming a strictly-local variable inside one function.
- **Non-architectural** read-only questions (factual lookup, "where is X defined", explaining a small snippet). Architectural / system-wide questions still trigger doc **2** (and **3**) per Table A.
- Running tests, lint, formatters, or read-only `git` commands.
- Editing `.env`, lockfiles, or auto-generated artefacts.
- Editing `CLAUDE.md` or `claude_dev/*.md` (meta-instructions): exempt from docs 4–7 since you are tuning Claude's behavior, not writing DEILE code or feature docs.

If you are unsure whether an edit is exempt, **it is not**. Run the protocol.

## Self-check before the first Write/Edit

Ask yourself:
1. Did I run the protocol this turn?
2. Are all triggered docs already in my context (auto-loaded or read this turn)?
3. If no — `Read` them now, then proceed.

All paths use forward slashes (`/`).
