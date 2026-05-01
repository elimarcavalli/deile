# DEILE — Claude Code Context

## Knowledge base — START HERE

The authoritative project knowledge lives in `claude_dev/`. Three files are auto-loaded into your context via the `@`-imports below:

- `claude_dev/0_deile-agent.md` — decision tree mapping each situation to one of the eight docs.
- `claude_dev/1_agent_persona.md` — your role.
- `claude_dev/8_system_specific_guidelines.md` — async, registry, Gemini, memory specifics.

@claude_dev/0_deile-agent.md
@claude_dev/1_agent_persona.md
@claude_dev/8_system_specific_guidelines.md

Docs 2–7 are **read-on-demand**. Open them with the `Read` tool only when a trigger in `0_deile-agent.md` fires; do not preemptively read them.

## Mandatory protocol (run before every non-trivial turn)

`claude_dev/0_deile-agent.md` defines a **three-table decision protocol** (Action × Path × Keyword). Before the first `Write`, `Edit`, or mutating `Bash` of each turn:

1. Classify the action, the target file paths, and the user's keywords against the three tables in doc 0.
2. Take the **union** of docs the tables point to.
3. `Read` every unread doc in that union — **before** the first mutation, never after.
4. If scope grows mid-task, **stop and re-run the protocol** with the expanded scope.

Exemptions are listed in doc 0 (typos, whitespace, read-only operations, lockfiles). When uncertain, the action is **not** exempt — run the protocol.

## Operational quick reference

Entry point: `python3 deile.py` (CLI shell in `DeileAgentCLI`; all logic lives in the `deile/` package).

| Task | Command |
|---|---|
| Run agent | `python3 deile.py` |
| Run tests | `pytest` (config in `pytest.ini`, testpaths = `deile/tests/`) |
| Single test | `pytest deile/tests/path/to/test_x.py -v` |
| Coverage | auto-runs with `pytest`; fails under 80% (`--cov-fail-under=80`) |
| Lint | `ruff check deile/` |
| Imports | `isort --check-only deile/` |
| Complexity | `radon cc deile/ -a` |

## Gotchas (not in claude_dev)

- **At least one provider API key is required at startup** — the agent exits if none are set. Configure any of: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, or `GOOGLE_API_KEY` in `.env` (loaded via `python-dotenv`). The `bootstrap_providers()` function in `deile/core/models/bootstrap.py` handles conditional registration.
- **Two `config/` directories**: `./config/` (runtime YAML/JSON) vs `./deile/config/` (package code + `settings.py` + YAML configs like `intent_patterns.yaml`). Don't conflate.
- **`deile/tests/` mixes two kinds of tests**:
  - *Pytest tests* (`test_*.py`) — collected automatically by `pytest`.
  - *Standalone scripts* (`*_test.py`, `smoke_test_*.py`, `proactive_final_test.py`) — run manually via `python deile/tests/<name>.py`. Pytest sees no `Test*` class / `test_*` function in them and silently skips, so they coexist safely. Use `python deile/tests/all.py` to run every standalone script in sequence; pass `--filter <substring>` to narrow the set.
- **`pytest.ini` uses `--strict-markers`** — register new markers there before using.
- **`asyncio_mode = auto`** — async tests don't need `@pytest.mark.asyncio`.
- **Settings is a singleton** — use `from deile.config.settings import get_settings`, never instantiate `Settings()` directly.
- **Personas are MD-driven** — instructions live in `deile/personas/instructions/*.md`; edit those to change behavior, no code change needed.
- **`.gitignore` has `*claude*`** — `CLAUDE.md` and `claude_dev/` are explicitly negated (`!CLAUDE.md`, `!claude_dev/`). Don't remove those negations.

## Running DEILE for empirical testing

You are authorized to invoke `python3 deile.py` (or call the agent programmatically) to test behavior changes — persona rules, gates, tooling — against the real LLM. The user has approved modest token spend for this. Two distinct conventions for **where files go**, do not conflate:

| Folder | Owner | Purpose |
|---|---|---|
| `test-your-might/<nickname>/` | **DEILE writes here** | Sandbox for artifacts DEILE creates *during interactive intelligence-tests* the user runs against him (e.g. the calc-package test, the fib.py test). When the user prompts DEILE to "create a program in tmp/X/...", instruct DEILE to scope under `test-your-might/<nickname>/` so the project root stays clean. |
| `deile/tests/might/<nickname>/` | **You write here** | YOUR test scripts that make real LLM API requests (like `test_rule8.py`). Live alongside `deile/tests/` but isolated under `might/` because they cost real tokens and aren't part of the standard `pytest` suite. |
| `deile/tests/` (rest) | **You write here** | Regular pytest tests — no API calls, no token spend. |

Constraints when running:

- **Keep the budget proportional to the question** — a smoke test is 1–4 messages, not a 20-message marathon. The user covered ~38 requests ≈ $0.13; aim well below that per ad-hoc test.
- **Same DEILE process across multi-turn probes** so conversation history persists (e.g. probing S4 "summarize what you just said" requires history continuity).
- **To bootstrap programmatically**, mirror what `deile.py` (the CLI) does — `ConfigManager().load_config()` + `bootstrap_providers(router=get_model_router())`. Calling `bootstrap_providers()` alone registers 0 providers because the router is the singleton DeileAgent reads from.
- **Capture output, strip ANSI, report verbatim** what DEILE actually said + which tools it actually called. Don't paraphrase — that's exactly the kind of fabrication rule 8 was added to prevent.
- **If DEILE asks for an interactive confirmation you cannot answer**, kill the process and surface that as a finding rather than guessing.

## SQL / database operations

All SQL scripts are the human operator's responsibility to run. If a DB error appears during a task, **stop and tell the operator which script to execute** — do not attempt to run migrations or schema changes yourself.
