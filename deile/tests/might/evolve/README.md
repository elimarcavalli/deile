# EVOLVE empirical harness — Tier 4 of issue #149

Smoke-test scripts that drive DEILE programmatically through three
golden ``/EVOLVE`` scenarios and assert structural properties of the
transcript. **These scripts make real LLM API calls.** They are not
collected by ``pytest`` and never run in the main suite.

## Why they live here

`deile/tests/might/<topic>/` is the convention for tests that cost real
tokens (mirrors `persona-rule-8/`). They guard regressions that *only*
manifest under real provider behavior — in this case, the run-2
catastrophic loop on sandbox-rejected paths and the run-3 quality bar
(devil's advocate, post-creation review). Both were captured in the
empirical comparison document at `docs/2026-05-08_EVOLVE-3RUN-COMPARISON.md`.

## Running

Prerequisites: at least one provider key in `.env` at the project root
(``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / ``DEEPSEEK_API_KEY`` /
``GOOGLE_API_KEY``). Run from the repo root:

```bash
# All three scenarios
python deile/tests/might/evolve/test_evolve_harness.py

# Single scenario
python deile/tests/might/evolve/test_evolve_harness.py --only 02

# Force a cheaper model
python deile/tests/might/evolve/test_evolve_harness.py --model deepseek:deepseek-v4-pro
# or via env var
DEILE_HARNESS_MODEL=deepseek:deepseek-v4-pro python deile/tests/might/evolve/test_evolve_harness.py
```

Exit code is `0` if every scenario passed its assertions, `1` otherwise.
The summary at the end lists each scenario's pass/fail and, on failure,
the specific keywords that were missing or spuriously present.

## Cost guideline

Each scenario uses a single user message; DEILE's tool-loop will execute
multiple tool calls in that turn. Hard cap of 25 tool calls per scenario
keeps the budget bounded even if the model goes off-script. With
DeepSeek as the forced model, a full three-scenario run costs roughly
the same as a few `/EVOLVE` invocations — well under one US-dollar.

The harness is **DRY-RUN**: the prompt explicitly instructs DEILE not to
execute any state-mutating command (`gh issue create`, `git push`, etc.)
and instead emit `[DRY RUN PLAN] <command>` markers. This makes it safe
to run even against a clean repo without polluting GitHub.

## What each scenario asserts

| # | Scenario | Required keywords | Forbidden / asserted-absent |
|---|---|---|---|
| 01 | Standalone repo with own `.github/ISSUE_TEMPLATE/` | `devil`, `criada+revisada` | `[loop-break:` |
| 02 | Subproject (no templates) inside parent (with templates) | `bash_execute`, `deile-bot` (label) | `[loop-break:` |
| 03 | Orphan repo with no templates anywhere | `abort` | `[loop-break:`, `criada+revisada` |

### Why these specific keywords

- **`devil`** — Fase 4 of the EVOLVE skill mandates devil's-advocate
  reasoning before filing each issue. Run-1 (72% quality) skipped it;
  run-3 (90%) ran it.
- **`criada+revisada`** — Fase 7b's mandatory post-creation review
  marker. Issue #149 explicitly added this gate so future runs don't
  silently skip the verification step.
- **`bash_execute`** — REGRA #13 (tool-selection resilience): when
  `file_tools` rejects a path, DEILE must switch tool family rather
  than rephrase. Subproject/parent navigation only works through
  `bash_execute` because `file_tools` cannot escape the working
  directory.
- **`deile-bot`** label — Fase 0 traceability rule: when operating on
  a parent repo from a subproject, every issue must carry the
  subproject's name as a label.
- **`abort`** in scenario 03 — Fase 0 mandates clean abort with a
  listing of paths inspected when no template directory is reachable.
- **`[loop-break:` absent** in every scenario — direct regression test
  for the run-2 failure mode (3 consecutive guard trips on the same
  `list_files` call). If the guard ever trips during these scenarios,
  the harness fails immediately.

## Updating

If a scenario fails because the *correct* behavior changed, update the
asserted keywords here — never weaken the assertions silently. The
keywords are the contract; the contract should evolve only with a clear
reason captured in `docs/2026-05-08_EVOLVE-3RUN-COMPARISON.md` or a
follow-up issue.
