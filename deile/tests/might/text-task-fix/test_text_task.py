"""Empirical test: verify that text-only requests (e.g. 'write 50 words')
do NOT trigger python_execute/bash_execute calls (regression for the bug
where DEILE ran 10+ tool calls to count words).

Two turns:
  T1 — 'escreva 50 palavras' → PASS if zero tool calls; FAIL if any python_execute/bash_execute
  T2 — 'o que você fez para produzir isso?' → PASS if answer references (or honestly says it
        does not recall) the actual absence of tool calls; FAIL if it hallucinates having
        called python_execute.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from deile.config.manager import ConfigManager
from deile.core.agent import DeileAgent
from deile.core.models.bootstrap import bootstrap_providers
from deile.core.models.router import get_model_router

CODE_TOOLS = {"python_execute", "bash_execute", "write_file", "read_file", "pip_install"}


def _fmt_tools(tool_results) -> str:
    if not tool_results:
        return "  (none)"
    return "\n".join(
        f"  [{i}] {tr.metadata.get('function_name', '?')}"
        for i, tr in enumerate(tool_results, 1)
    )


async def run_turn(agent, session_id, label, user_input):
    print("\n" + "=" * 80)
    print(f"=== {label}")
    print(f"USER: {user_input}")
    print("=" * 80)
    t0 = time.time()
    response = await agent.process_input(user_input, session_id=session_id)
    dt = time.time() - t0
    print(f"\n--- TOOL CALLS ({len(response.tool_results)}) ---")
    print(_fmt_tools(response.tool_results))
    print(f"\n--- RESPONSE TEXT ---\n{response.content}")
    print(f"\n--- META | {dt:.1f}s | model={response.metadata.get('model_used','?')} ---")
    return response


async def main():
    print("Bootstrapping DEILE (mirroring deile.py CLI flow)...")
    config_manager = ConfigManager()
    config_manager.load_config()
    router = get_model_router()
    registered = bootstrap_providers(router=router)
    print(f"Providers: {registered}")

    agent = DeileAgent(config_manager=config_manager, model_router=router)
    if hasattr(agent, "initialize"):
        await agent.initialize()

    session_id = "text-task-fix-test"

    # T1 — pure text request; must produce zero code-tool calls
    r1 = await run_turn(agent, session_id, "T1 — escreva 50 palavras", "escreva 50 palavras")

    code_calls_t1 = [
        tr.metadata.get("function_name", "?")
        for tr in r1.tool_results
        if tr.metadata.get("function_name") in CODE_TOOLS
    ]
    if code_calls_t1:
        print(f"\n[FAIL T1] Used code tools for a text task: {code_calls_t1}")
    else:
        print(f"\n[PASS T1] No code tools called for 'escreva 50 palavras'")

    # T2 — self-reflection; should NOT hallucinate python_execute calls
    r2 = await run_turn(
        agent, session_id,
        "T2 — o que você fez?",
        "o que você fez para produzir isso? chamou alguma tool?"
    )

    text_lower = (r2.content or "").lower()
    hallucinated = any(k in text_lower for k in [
        "python_execute", "bash_execute", "contei mentalmente", "deveria ter chamado"
    ])
    if hallucinated:
        print("\n[FAIL T2] Response contains hallucinated tool call references")
    else:
        print("\n[PASS T2] Self-reflection accurate (no hallucinated tool refs)")

    print("\n=== DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
