# DEILE — Claude Code Context

## Knowledge base — START HERE

The authoritative project knowledge lives in `claude_dev/`. Three files are auto-loaded into your context via the `@`-imports below:

- `claude_dev/0_deile-agent.md` — decision tree mapping each situation to one of the eight docs.
- `claude_dev/1_agent_persona.md` — your role.
- `claude_dev/8_system_specific_guidelines.md` — async, registry, Gemini, memory specifics.

@claude_dev/0_deile-agent.md
@claude_dev/1_agent_persona.md
@claude_dev/8_system_specific_guidelines.md

Docs 2–7 are **read-on-demand**. Open them with the `Read` tool only when the trigger described in `0_deile-agent.md` fires; do not preemptively read them.

## Minimum reading rule (mandatory)

Before any non-trivial change (new class/function, refactor, feature, bugfix that touches more than one file), open the docs whose triggers apply per `0_deile-agent.md`. Typo fixes and one-line tweaks are exempt.

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

- **`GOOGLE_API_KEY` is required at startup** — agent exits if missing. Loaded from `.env` via `python-dotenv`.
- **Two `config/` directories**: `./config/` (runtime YAML/JSON) vs `./deile/config/` (package code + `settings.py` + YAML configs like `intent_patterns.yaml`). Don't conflate.
- **`deile/tests/` mixes two kinds of tests**:
  - *Pytest tests* (`test_*.py`) — collected automatically by `pytest`.
  - *Standalone scripts* (`*_test.py`, `smoke_test_*.py`, `proactive_final_test.py`) — run manually via `python deile/tests/<name>.py`. Pytest sees no `Test*` class / `test_*` function in them and silently skips, so they coexist safely. Use `python deile/tests/all.py` to run every standalone script in sequence; pass `--filter <substring>` to narrow the set.
- **`pytest.ini` uses `--strict-markers`** — register new markers there before using.
- **`asyncio_mode = auto`** — async tests don't need `@pytest.mark.asyncio`.
- **Settings is a singleton** — use `from deile.config.settings import get_settings`, never instantiate `Settings()` directly.
- **Personas are MD-driven** — instructions live in `deile/personas/instructions/*.md`; edit those to change behavior, no code change needed.
- **`.gitignore` has `*claude*`** — `CLAUDE.md` and `claude_dev/` are explicitly negated (`!CLAUDE.md`, `!claude_dev/`). Don't remove those negations.

## SQL / database operations

All SQL scripts are the human operator's responsibility to run. If a DB error appears during a task, **stop and tell the operator which script to execute** — do not attempt to run migrations or schema changes yourself.
