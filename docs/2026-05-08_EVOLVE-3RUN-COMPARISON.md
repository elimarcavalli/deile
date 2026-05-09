# 2026-05-08 — `/EVOLVE` 3-run comparison and root-cause analysis

> Companion document for issue [#149](https://github.com/elimarcavalli/deile/issues/149) and PR [#153](https://github.com/elimarcavalli/deile/pull/153).
> Captures the empirical evidence that motivated the Tier 1–5 calibration
> work and explains how each tier maps back to the observed failure modes.

## TL;DR

Three consecutive invocations of `/EVOLVE` against the same target
(`deile_bot/` subproject living inside the `deile/` parent repo) produced
drastically different outcomes:

| Run | Subjective quality | Result |
|---|---:|---|
| 1ª | **72%** | Operated on parent repo but missed subproject context. Found only dead-config gaps (the easiest category). No devil's advocate. No post-creation verification. |
| 2ª | **8%** | Catastrophic loop. Three consecutive `loop_guard` trips on `list_files(path='.github/ISSUE_TEMPLATE/')`. Zero issues created. User gave up. |
| 3ª | **90%** | Detected subproject. Operated on parent repo with `deile-bot` label for traceability. Devil's advocate on each finding. Post-creation `gh issue view` for every issue. Caught the AdminCog/EventsCog wiring gaps and BotEventBus dead enums — the highest-value findings of the three runs. |

The variance is **structural**, not stochastic. Five distinct failure
modes in DEILE's tooling, persona, and skill content combined to make
run-2 possible and made the run-1 → run-3 quality jump non-deterministic.
PR #153 closes those failure modes.

## The five root causes (run-2)

| # | Site | Failure | Observable |
|---|---|---|---|
| 1 | `deile/tools/file_tools.py` | Path sandbox silently mutilates absolute paths fed by the LLM and rewrites them to project-relative siblings that don't exist. | `list_files(path='/Users/x/.github/...')` returns `[]` instead of an explanation. |
| 2 | `deile/tools/file_tools.py` | `..` rejected without a useful fallback hint. | `LocalFileAccessViolation` raised without telling the model *what to use instead*. |
| 3 | `deile/personas/instructions/core/DEILE.md` | Persona instructed the LLM to "ignore paths outside the project" without supplying a fail-over to `bash_execute`. | LLM rephrased the same call instead of switching tool family. |
| 4 | `~/.claude/commands/EVOLVE.md` (skill) | Skill aborted when no `.github/ISSUE_TEMPLATE/` was found in the current directory — no monorepo / parent-repo fallback. | Skill terminated instead of operating on the parent repo. |
| 5 | `deile/core/loop_guard.py` | Loop-guard exit message asked the LLM to "rephrase" without nudging an alternative tool family. | LLM kept rephrasing the same broken call until the hard ceiling tripped. |

Together: the skill failed to find templates → LLM tried `list_files` →
sandbox silently rewrote the path → empty result → LLM rephrased
(persona told it to ignore paths outside the project) → guard tripped →
exit message said "rephrase" → LLM rephrased again → guard tripped
again → catastrophic dead end.

## Why run-1 (72%) was not enough either

The run-1 → run-3 quality jump is independent of the loop bug. Run-1
finished without a loop-break but produced a shallow audit: only
dead-config gaps (the easiest tier). Run-3 also found wiring gaps and
dead enums (the highest-value tier). The structural difference:

| Behavior | Run-1 | Run-3 |
|---|:---:|:---:|
| Devil's advocate per gap | ❌ | ✅ |
| Post-creation `gh issue view` per issue | ❌ | ✅ |
| Operate-on-parent label `deile-bot` | ❌ (no rastreabilidade) | ✅ |
| Quality self-check (warn on >80% surface findings) | ❌ | n/a (deep findings) |

The skill as written before #153 *suggested* devil's advocate but didn't
gate on it; some LLMs/contexts ran it, others skipped silently. The new
skill (Tier 3) makes devil's advocate (Fase 4) explicit and adds
mandatory post-creation review (Fase 7b) plus a quality self-check
(Fase 8) that emits a warning when the audit is dominated by surface
findings.

## How each tier maps to the failure modes

| Tier | What it changes | Closes failure mode |
|---|---|---|
| **1** — `bash_execute` hint consistency in `read_file` / `write_file` / `delete_file` | Every path-tool that rejects a path now embeds an explicit `bash_execute(command="…")` hint in the error message. | #1, #2 |
| **2a** — `AbortKind.HARD_STOP` | When the guard already aborted on hash *H* and the LLM rephrases to the *same* hash, the next abort is `HARD_STOP` — terminate the turn, do not let the model rephrase a third time. | #5 |
| **2b** — `error_signature` fast-trip in `record_result` | Two consecutive failures with the same opaque error signature short-circuit `NO_PROGRESS` (≥2 instead of ≥6). Catches the "near-miss rephrase" case where the args hash differs but the error is structurally identical. | #5 |
| **3** — EVOLVE skill rewrite (Fase 7b mandatory review, Fase 8 quality self-check) | Adds gates on post-creation verification and on shallow-audit detection. Run-1 quality is now mechanically detected. | run-1 → run-3 gap |
| **5** — Persona REGRA #13 (tool-selection resilience) | Explicit fallback table: `file_tools` → `bash_execute`; `http_*` → curl; `db_*` → CLI; `bash_execute` → alternative CLI. Anti-pattern call-out: switching arguments vs. switching family. | #3 |

Tier 4 (the empirical harness in `deile/tests/might/evolve/`) is what
makes future regressions visible: each of the three scenarios is a
direct golden against one of the failure modes (loop-break absence in
all three, REGRA #13 fallback in scenario 02, clean abort in scenario
03, devil's-advocate + post-review markers in scenario 01).

## Run transcripts (summary, not verbatim)

The full transcripts of each run are operator-private and not
checked into the repo (they include local paths, model identifiers, and
incidental file contents). What matters for regression purposes is the
behavior signatures captured by Tier 4's harness assertions. If a future
run drifts into run-1 or run-2 patterns, the harness will fail with a
named keyword missing or with `[loop-break:` present.

## Issues spawned from each run

- **Run-1** seeded #138 (enterprise.yaml dead-config) and #139 — both
  dead-config tier, illustrative of the surface-only quality.
- **Run-2** seeded no issues (catastrophic dead end).
- **Run-3** seeded #144 and #145 (AdminCog/EventsCog never registered),
  and #147 (BotEventBus dead enums). All three are wiring-gap or
  dead-enum tier — the highest-value categories.

The contrast in *output value* (run-1 surface vs. run-3 wiring) is the
empirical justification for keeping Tier 3's quality self-check in the
skill: 100% dead-config is a strong negative signal that the audit
didn't reach behavior-level analysis.

## Decision: keep this document, don't expand it

This file is a **frozen snapshot** of the empirical evidence behind
issue #149. It deliberately does **not** track future EVOLVE runs;
those belong in their own dated documents or in the Tier-4 harness's
log output. Treat it like an architectural decision record — refer to
it; don't keep editing it.
