"""Empirical EVOLVE harness — Tier 4 of issue #149.

Three smoke-test scenarios that drive DEILE programmatically through an
``/EVOLVE``-style audit and assert structural properties of the transcript.
The point is **regression of run-2 catastrophe** (loop-break on
sandbox-rejected paths, no fallback to ``bash_execute``) and validation of
the run-3 quality bar (devil's advocate, post-creation review).

These scripts cost real LLM tokens. They are NOT collected by ``pytest``
(no ``test_*`` function with ``Test*`` class), and are run manually via::

    python deile/tests/might/evolve/test_evolve_harness.py
    # or single scenario:
    python deile/tests/might/evolve/test_evolve_harness.py --only 02

Token budget: ≤4 messages per scenario × 3 scenarios. Use a cheap model
(``--model deepseek:deepseek-v4-pro`` or omit to let the router pick).

Requires at least one provider key in ``.env`` at the project root —
otherwise bootstrap registers 0 providers and the script aborts cleanly.

Scenarios
---------

01_standalone
    Plain repo with ``.github/ISSUE_TEMPLATE/intent.md`` of its own and a
    Python file containing a TODO. DEILE should detect the template, run
    devil's advocate before deciding to file an issue, and emit the
    ``criada+revisada`` post-creation review marker (Fase 7b of the
    skill). DRY-RUN: DEILE is told NOT to actually invoke
    ``gh issue create`` — we only need the workflow markers in the
    transcript.

02_subproject
    Subproject (no templates) inside a parent repo (with templates).
    DEILE should detect that ``list_files(.github/ISSUE_TEMPLATE/)``
    returns nothing in the subproject, then **switch tool family** to
    ``bash_execute`` per REGRA #13 to inspect the parent repo. Asserts
    that the transcript names the subproject as a label and that the
    subproject's name appears as the rastreabilidade label.

03_no_templates
    Orphan repo with NO templates anywhere. DEILE should abort with a
    clear message listing the paths it inspected — never call
    ``gh issue create``.

Each scenario passes if every required keyword is present and every
forbidden keyword is absent. Failure prints the missing/spurious set so
the operator can inspect the captured transcript.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

# Project root is .../deile/, four parents up from this file.
PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from deile.config.manager import ConfigManager  # noqa: E402
from deile.core.agent import DeileAgent  # noqa: E402
from deile.core.models.bootstrap import bootstrap_providers  # noqa: E402
from deile.core.models.router import get_model_router  # noqa: E402

# Cap on tool calls per scenario — keeps token spend bounded even if the
# model goes off-script. Mirrors the issue's "≤4 messages per scenario"
# budget guideline.
MAX_TOOL_CALLS_PER_SCENARIO = 25


# ---------------------------------------------------------------------------
# Common scenario plumbing
# ---------------------------------------------------------------------------


def _evolve_skill_text() -> str:
    """Inline EVOLVE skill content as system-style preface for DEILE.

    DEILE doesn't natively expose ``/EVOLVE`` — this function loads the
    versioned skill (``docs/skills/EVOLVE.md``) and returns it verbatim so
    every scenario gives DEILE the same instructions a Claude Code
    ``/EVOLVE`` invocation would.
    """
    skill_path = PROJECT_ROOT / "docs" / "skills" / "EVOLVE.md"
    if not skill_path.exists():
        raise FileNotFoundError(
            f"Expected EVOLVE skill at {skill_path}; harness needs the "
            "Tier-3 deliverable from issue #149 to be present."
        )
    return skill_path.read_text(encoding="utf-8")


def _dry_run_addendum() -> str:
    """Hard instruction appended after the skill text — keeps the harness
    non-destructive (no real ``gh issue create`` calls)."""
    return (
        "\n\n---\n\n"
        "# DRY RUN — HARNESS MODE\n\n"
        "Você está rodando dentro de um harness de teste. **NÃO** execute "
        "comandos que mudem estado externo: nada de `gh issue create`, "
        "nada de `git push`, nada de `git commit`. Para cada comando que "
        "MUDARIA estado, imprima literalmente:\n\n"
        "    [DRY RUN PLAN] <comando que executaria>\n\n"
        "Em seguida, **continue** o workflow como se o comando tivesse "
        "tido sucesso (incluindo a verificação `gh issue view <N>`, "
        "também impressa como `[DRY RUN PLAN]`). Marque o status final "
        "como `criada+revisada` apenas se você teria executado **ambos** "
        "o create e o view.\n\n"
        "Para comandos read-only (`bash_execute(command='ls ...')`, "
        "`list_files`, `read_file`), execute normalmente."
    )


def _setup_standalone(tmp: Path) -> None:
    """Scenario 01 — standalone repo with own templates."""
    (tmp / ".github" / "ISSUE_TEMPLATE").mkdir(parents=True)
    (tmp / ".github" / "ISSUE_TEMPLATE" / "intent.md").write_text(
        "---\nname: Intent\nlabels: intent\n---\n## Resumo\n\n## Motivação\n",
        encoding="utf-8",
    )
    (tmp / "src").mkdir()
    (tmp / "src" / "main.py").write_text(
        "# TODO: implement payment retry logic\n"
        "def process_payment(order):\n"
        "    pass\n",
        encoding="utf-8",
    )


def _setup_subproject(tmp: Path) -> Path:
    """Scenario 02 — subproject without templates inside a parent that has them.

    Returns the subproject path (where DEILE will operate from)."""
    parent = tmp / "parent_repo"
    sub = parent / "deile_bot"
    (parent / ".github" / "ISSUE_TEMPLATE").mkdir(parents=True)
    (parent / ".github" / "ISSUE_TEMPLATE" / "intent.md").write_text(
        "---\nname: Intent\nlabels: intent\n---\n## Resumo\n",
        encoding="utf-8",
    )
    sub.mkdir()
    (sub / "src").mkdir()
    (sub / "src" / "bot.py").write_text(
        "# TODO: register AdminCog and EventsCog at startup\n"
        "class Bot:\n"
        "    def start(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    return sub


def _setup_no_templates(tmp: Path) -> None:
    """Scenario 03 — orphan repo with no ISSUE_TEMPLATE anywhere reachable."""
    (tmp / "src").mkdir()
    (tmp / "src" / "lib.py").write_text("def f(): pass\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Bootstrap + transcript capture
# ---------------------------------------------------------------------------


_AGENT_CACHE: Dict[str, DeileAgent] = {}


async def _bootstrap_agent() -> DeileAgent:
    """Bootstrap once per process — DEILE singletons (router, providers) are
    expensive to spin up and identical across scenarios."""
    if "agent" in _AGENT_CACHE:
        return _AGENT_CACHE["agent"]

    cm = ConfigManager()
    cm.load_config()
    router = get_model_router()
    registered = bootstrap_providers(router=router)
    if not registered:
        raise RuntimeError(
            "No LLM providers registered — set at least one of "
            "ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY / "
            "GOOGLE_API_KEY in .env at the project root."
        )
    agent = DeileAgent(config_manager=cm, model_router=router)
    if hasattr(agent, "initialize"):
        await agent.initialize()
    _AGENT_CACHE["agent"] = agent
    return agent


def _format_tool_calls(tool_results) -> str:
    if not tool_results:
        return "  (none)"
    lines = []
    for i, tr in enumerate(tool_results, 1):
        name = tr.metadata.get("function_name", "?")
        status = tr.status.value if hasattr(tr.status, "value") else str(tr.status)
        msg = (tr.message or "").splitlines()[0] if tr.message else ""
        lines.append(f"  [{i}] {name} status={status}")
        if msg:
            lines.append(f"      msg: {msg[:120]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Assertion model
# ---------------------------------------------------------------------------


class ScenarioResult:
    """Captured outcome of a scenario run.

    ``transcript`` is the concatenation of the assistant text response and
    every tool message — the harness asserts against this combined string
    (case-insensitive) so a marker in either place counts as present.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.transcript: str = ""
        self.tool_calls: List[str] = []
        self.duration_s: float = 0.0
        self.errors: List[str] = []

    @property
    def passed(self) -> bool:
        return not self.errors

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)


def _assert_present(result: ScenarioResult, needles: List[str], why: str) -> None:
    haystack = result.transcript.lower()
    missing = [n for n in needles if n.lower() not in haystack]
    if missing:
        result.add_error(f"{why} — missing: {missing}")


def _assert_absent(result: ScenarioResult, needles: List[str], why: str) -> None:
    haystack = result.transcript.lower()
    present = [n for n in needles if n.lower() in haystack]
    if present:
        result.add_error(f"{why} — found forbidden: {present}")


def _assert_no_loop_break(result: ScenarioResult) -> None:
    # ``[loop-break:`` is what ``format_loop_break_message`` prefixes when
    # the guard trips. Its presence in any of the three scenarios is a
    # direct regression of the run-2 failure mode.
    if "[loop-break:" in result.transcript or "loop-break" in result.transcript.lower():
        result.add_error("loop_guard tripped during scenario (run-2 regression)")


# ---------------------------------------------------------------------------
# Scenario runners
# ---------------------------------------------------------------------------


async def _run_scenario(
    agent: DeileAgent,
    name: str,
    cwd: Path,
    user_prompt: str,
    forced_model: Optional[str],
) -> ScenarioResult:
    result = ScenarioResult(name)
    session_id = f"evolve-harness-{name}-{int(time.time())}"

    # Create the session anchored at the scenario's tmpdir so file_tools
    # treat that dir as the project root.
    session = agent.create_session(session_id, working_directory=cwd)
    if forced_model:
        session.context_data["forced_model"] = forced_model

    skill = _evolve_skill_text() + _dry_run_addendum()
    full_prompt = (
        f"# /EVOLVE skill (inlined for harness)\n\n{skill}\n\n"
        f"---\n\n# Operador\n\n{user_prompt}"
    )

    print("=" * 100)
    print(f"=== Scenario {name} — cwd={cwd}")
    print("=" * 100)

    t0 = time.time()
    try:
        response = await agent.process_input(full_prompt, session_id=session_id)
    except Exception as exc:
        result.duration_s = time.time() - t0
        result.add_error(f"agent.process_input raised {type(exc).__name__}: {exc}")
        result.transcript = f"<exception: {exc}>"
        return result
    result.duration_s = time.time() - t0

    # Combine assistant text + every tool message into one searchable
    # transcript.  We do this rather than asserting against ``response.content``
    # alone because some markers (e.g. the ``[loop-break:`` prefix) are
    # emitted by tool results rather than by the model.
    parts = [response.content or ""]
    for tr in response.tool_results:
        result.tool_calls.append(tr.metadata.get("function_name", "?"))
        if tr.message:
            parts.append(tr.message)
    result.transcript = "\n".join(parts)

    print(f"\n--- TOOL CALLS ({len(response.tool_results)}) ---")
    print(_format_tool_calls(response.tool_results))
    if len(result.tool_calls) > MAX_TOOL_CALLS_PER_SCENARIO:
        result.add_error(
            f"tool-call budget exceeded: {len(result.tool_calls)} > "
            f"{MAX_TOOL_CALLS_PER_SCENARIO}"
        )

    print("\n--- RESPONSE TEXT (truncated) ---")
    print((response.content or "")[:3000])
    print(f"\n--- DURATION: {result.duration_s:.2f}s ---")

    return result


async def scenario_01_standalone(agent: DeileAgent, forced_model: Optional[str]) -> ScenarioResult:
    with tempfile.TemporaryDirectory(prefix="evolve-01-") as raw:
        tmp = Path(raw).resolve()
        _setup_standalone(tmp)
        prompt = (
            "Audit this repo for gaps. Use the EVOLVE skill above. "
            "Find at most 1 gap, do devil's advocate on it, then file an issue "
            "(DRY RUN) and verify it (DRY RUN). Report status as "
            "criada+revisada in the final table."
        )
        r = await _run_scenario(agent, "01_standalone", tmp, prompt, forced_model)

    _assert_no_loop_break(r)
    _assert_present(
        r,
        ["devil", "criada+revisada"],
        "01: skill should run devil's advocate and emit post-creation review marker",
    )
    return r


async def scenario_02_subproject(agent: DeileAgent, forced_model: Optional[str]) -> ScenarioResult:
    with tempfile.TemporaryDirectory(prefix="evolve-02-") as raw:
        tmp = Path(raw).resolve()
        sub = _setup_subproject(tmp)
        prompt = (
            "Audit this repo for gaps. Use the EVOLVE skill above. "
            "There are no ISSUE_TEMPLATE files in the current directory — "
            "follow Fase 0 of the skill to find them in the parent repo. "
            "When you do, label any issues you would file with `deile-bot` "
            "for traceability. DRY RUN — do not actually create issues."
        )
        r = await _run_scenario(agent, "02_subproject", sub, prompt, forced_model)

    _assert_no_loop_break(r)
    _assert_present(
        r,
        ["bash_execute", "deile-bot"],
        "02: REGRA #13 — file_tools rejection must trigger bash_execute fallback "
        "and the subproject label must be applied",
    )
    return r


async def scenario_03_no_templates(agent: DeileAgent, forced_model: Optional[str]) -> ScenarioResult:
    with tempfile.TemporaryDirectory(prefix="evolve-03-") as raw:
        tmp = Path(raw).resolve()
        _setup_no_templates(tmp)
        prompt = (
            "Audit this repo for gaps. Use the EVOLVE skill above. "
            "No ISSUE_TEMPLATE directory exists in the current dir, the "
            "parent dir, or the org-default `.github` repo. Per Fase 0, "
            "this means abort cleanly with a message listing every path "
            "you inspected. DRY RUN — do not file anything."
        )
        r = await _run_scenario(agent, "03_no_templates", tmp, prompt, forced_model)

    _assert_no_loop_break(r)
    # When templates are absent everywhere, the skill mandates abort. The
    # transcript should contain the abort word and should NOT claim an
    # issue was created.
    _assert_present(
        r,
        ["abort"],
        "03: skill must abort when no template path is reachable",
    )
    _assert_absent(
        r,
        ["criada+revisada"],
        "03: nothing was created, so no review marker should appear",
    )
    return r


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _print_summary(results: List[ScenarioResult]) -> int:
    """Returns process exit code: 0 if all passed, 1 otherwise."""
    print("\n" + "=" * 100)
    print("HARNESS SUMMARY")
    print("=" * 100)
    failed = 0
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name} — {r.duration_s:.2f}s, {len(r.tool_calls)} tool calls")
        if not r.passed:
            failed += 1
            for err in r.errors:
                print(f"      • {err}")
    print()
    if failed:
        print(f"{failed}/{len(results)} scenarios FAILED")
        return 1
    print(f"All {len(results)} scenarios PASSED")
    return 0


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--only",
        choices=["01", "02", "03"],
        help="Run a single scenario instead of all three.",
    )
    p.add_argument(
        "--model",
        default=os.environ.get("DEILE_HARNESS_MODEL"),
        help="Force a provider:model handle (e.g. deepseek:deepseek-v4-pro). "
        "Defaults to DEILE_HARNESS_MODEL env var or the router's choice.",
    )
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    print("Bootstrapping DEILE for EVOLVE harness...")
    agent = await _bootstrap_agent()
    print(f"Forced model: {args.model or '(router default)'}\n")

    runners = {
        "01": scenario_01_standalone,
        "02": scenario_02_subproject,
        "03": scenario_03_no_templates,
    }
    keys = [args.only] if args.only else ["01", "02", "03"]

    results: List[ScenarioResult] = []
    for key in keys:
        try:
            r = await runners[key](agent, args.model)
        except Exception as exc:
            r = ScenarioResult(key)
            r.add_error(f"runner crashed: {type(exc).__name__}: {exc}")
        results.append(r)

    return _print_summary(results)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
