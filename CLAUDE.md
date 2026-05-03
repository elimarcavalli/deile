# DEILE — Claude Code Context

## Knowledge base — START HERE

The authoritative project knowledge lives in `docs/system_design/`. The single index/table-of-contents is `docs/system_design/00-VISAO-GERAL.md` — open it first to navigate. The three documents auto-loaded into your context via the `@`-imports below are the minimum set you should always have on hand:

- `docs/system_design/00-VISAO-GERAL.md` — pillars index, single source of truth for counts, decisions table.
- `docs/system_design/03-PRINCIPIOS-ARQUITETURAIS.md` — non-negotiable rules with a fast trigger index.
- `docs/system_design/12-PADROES-CODIGO.md` — concrete templates for tools, commands, parsers, memory, security, tests.

@docs/system_design/00-VISAO-GERAL.md
@docs/system_design/03-PRINCIPIOS-ARQUITETURAIS.md
@docs/system_design/12-PADROES-CODIGO.md

The remaining pillar docs are **read-on-demand**. Open them with the `Read` tool only when the situation demands; never preemptively.

## Mandatory protocol (run before every non-trivial turn)

Before the first `Write`, `Edit`, or mutating `Bash` of each turn:

1. Classify the **action** you are about to perform → consult the trigger index in `03-PRINCIPIOS-ARQUITETURAIS.md`.
2. Classify the **target file path(s)** → match against the subpackage map in `02-ARQUITETURA.md`.
3. Match the **user's keywords** → architecture / scope / capability terms point you to the relevant pillar (see `00-VISAO-GERAL.md`).
4. **Take the union** of pillars implied by the three checks above. `Read` every unread document in that union **before** the first mutation.
5. If the scope grows mid-task, **stop and re-run the protocol** with the expanded scope.

Exemptions: typos, whitespace, single-line cosmetic edits, renaming a strictly-local variable, non-architectural read-only questions, running tests/lint/formatters, editing `.env` or lockfiles, editing `CLAUDE.md` or files under `docs/system_design/`. When uncertain, the action is **not** exempt — run the protocol.

## Pillar map

| # | Pillar | Document |
|---|---|---|
| 0 | Index / counts / decisions table | `docs/system_design/00-VISAO-GERAL.md` |
| 1 | Capabilities | `docs/system_design/01-CAPACIDADES.md` |
| 2 | Architecture | `docs/system_design/02-ARQUITETURA.md` |
| 3 | Architectural principles | `docs/system_design/03-PRINCIPIOS-ARQUITETURAIS.md` |
| 4 | Component model (registries) | `docs/system_design/04-MODELO-COMPONENTES.md` |
| 5 | Execution flow | `docs/system_design/05-FLUXO-EXECUCAO.md` |
| 6 | Memory (4 layers) | `docs/system_design/06-MEMORIA.md` |
| 7 | LLM integrations | `docs/system_design/07-INTEGRACOES-LLM.md` |
| 8 | Security | `docs/system_design/08-SEGURANCA.md` |
| 9 | Configuration | `docs/system_design/09-CONFIGURACAO.md` |
| 10 | Diagrams | `docs/system_design/10-DIAGRAMAS.md` |
| 11 | Development workflow | `docs/system_design/11-WORKFLOW-DESENVOLVIMENTO.md` |
| 12 | Code patterns | `docs/system_design/12-PADROES-CODIGO.md` |
| 13 | Documentation template | `docs/system_design/13-PADRAO-DOCUMENTACAO.md` |
| — | Decision records | `docs/system_design/DECISOES.md` |

## Operational quick reference

Entry point: `python3 deile.py` (CLI shell in `DeileAgentCLI`; all logic lives in the `deile/` package).

| Task | Command |
|---|---|
| Run agent | `python3 deile.py` |
| Run tests | `python3 -m pytest deile/tests/ -q 2>&1 \| tail -5` — shows only the final summary line; add `-v` only when debugging a specific failure |
| Single test | `python3 -m pytest deile/tests/path/to/test_x.py -v` |
| Coverage | auto-runs with `pytest`; fails under 80% (`--cov-fail-under=80`) |
| Lint | `ruff check deile/` |
| Imports | `isort --check-only deile/` |
| Complexity | `radon cc deile/ -a` |

## Gotchas (not in the system design)

- **At least one provider API key is required at startup** — the agent exits if none are set. Configure any of: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, or `GOOGLE_API_KEY` in `.env` (loaded via `python-dotenv`). The `bootstrap_providers()` function in `deile/core/models/bootstrap.py` handles conditional registration.
- **`deile-bot` lives in a separate repo** (`elimarcavalli/deile-bot`). The `deile_bot/` directory you may see locally is that repo nested as a working tree (its own `.git`); this `deile` repo no longer ships the daemon. To enable the proactive messaging tools (`messaging.discord_*`), install the thin client extra: `pip install deile[bot]` and configure `DEILE_BOT_ENDPOINT` + `DEILE_BOT_AUTH_TOKEN` (see `.env.example`). Tools auto-register only when both the client is installed and both env vars are set.
- **If you're touching messaging tools**, open [`08-SEGURANCA.md`](docs/system_design/08-SEGURANCA.md) **before** writing code — DM and role-mention tools are gated by `ApprovalSystem` by design and changes to that gate are non-trivial.
- **Two `config/` directories**: `./config/` (runtime YAML/JSON) vs `./deile/config/` (package code + `settings.py` + YAML configs like `intent_patterns.yaml`). Don't conflate.
- **`deile/tests/` mixes two kinds of tests**:
  - *Pytest tests* (`test_*.py`) — collected automatically by `pytest`.
  - *Standalone scripts* (`*_test.py`, `smoke_test_*.py`, `proactive_final_test.py`) — run manually via `python deile/tests/<name>.py`. Pytest sees no `Test*` class / `test_*` function in them and silently skips, so they coexist safely. Use `python deile/tests/all.py` to run every standalone script in sequence; pass `--filter <substring>` to narrow the set.
- **`pytest.ini` uses `--strict-markers`** — register new markers there before using.
- **`asyncio_mode = auto`** — async tests don't need `@pytest.mark.asyncio`.
- **Settings is a singleton** — use `from deile.config.settings import get_settings`, never instantiate `Settings()` directly.
- **Personas are MD-driven** — instructions live in `deile/personas/instructions/*.md`; edit those to change behavior, no code change needed.
- **`.gitignore` has `*claude*`** — `CLAUDE.md` is explicitly negated (`!CLAUDE.md`). Don't remove that negation.

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
