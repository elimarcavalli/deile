"""Empirical test of persona rule 8 (anti-hallucination in explanations).

Invokes DEILE programmatically (bypassing the TUI) and probes:
- T1 (cenário S1): "explain how _resolve_project_path works in detail"
       Pass = DEILE calls read_file on file_tools.py upfront, cites file:line,
       no fabricated narrative.
- T2 (cenário S4): "now summarize that in 5 short sentences"
       Pass = DEILE re-cites source OR re-reads, doesn't just rehash poisoned
       history.

Prints captured response text + tool call list + duration. Output is the
artifact for human evaluation in the parent conversation.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Ensure project root is importable
# Path: deile/tests/might/persona-rule-8/test_rule8.py
# parents[0]=persona-rule-8, [1]=might, [2]=tests, [3]=deile, [4]=project root
PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from deile.config.manager import ConfigManager  # noqa: E402
from deile.core.agent import DeileAgent  # noqa: E402
from deile.core.models.bootstrap import bootstrap_providers  # noqa: E402
from deile.core.models.router import get_model_router  # noqa: E402


def _format_tool_calls(tool_results) -> str:
    """Render the tool call list compactly."""
    if not tool_results:
        return "  (none)"
    lines = []
    for i, tr in enumerate(tool_results, 1):
        name = tr.metadata.get("function_name", "?")
        status = tr.status.value if hasattr(tr.status, "value") else str(tr.status)
        # Show key arg if available
        msg = (tr.message or "").splitlines()[0] if tr.message else ""
        lines.append(f"  [{i}] {name} status={status}")
        if msg:
            lines.append(f"      msg: {msg[:120]}")
    return "\n".join(lines)


async def run_turn(
    agent: DeileAgent, session_id: str, label: str, user_input: str
) -> None:
    print("=" * 100)
    print(f"=== {label}")
    print(f"USER: {user_input}")
    print("=" * 100)

    t0 = time.time()
    response = await agent.process_input(user_input, session_id=session_id)
    dt = time.time() - t0

    print(f"\n--- TOOL CALLS ({len(response.tool_results)}) ---")
    print(_format_tool_calls(response.tool_results))

    print("\n--- RESPONSE TEXT ---")
    print(response.content)

    print("\n--- METADATA ---")
    print(f"  duration: {dt:.2f}s")
    print(f"  model_used: {response.metadata.get('model_used', '?')}")
    print(
        f"  status: {response.status.value if hasattr(response.status, 'value') else response.status}"
    )
    print()


async def main():
    print("Bootstrapping providers (mirroring deile.py CLI flow)...")
    config_manager = ConfigManager()
    config_manager.load_config()
    router = get_model_router()
    registered = bootstrap_providers(router=router)
    print(f"Providers registered: {registered}")

    agent = DeileAgent(config_manager=config_manager, model_router=router)
    if hasattr(agent, "initialize"):
        await agent.initialize()

    session_id = "rule8-test"
    session = agent._get_or_create_session(session_id)
    session.context_data["forced_model"] = "deepseek:deepseek-v4-pro"
    print(f"Forced model: {session.context_data['forced_model']}\n")

    # T1 — S1 probe: ask for detailed explanation. Should trigger read_file.
    await run_turn(
        agent,
        session_id,
        "T1 — S1 (read+fabricate narrative)",
        "explica como funciona a função `_resolve_project_path` do DEILE em detalhes — "
        "incluindo a estrutura geral, os edge cases que ela trata, e o que acontece quando "
        "o path não pode ser resolvido. Quero entender de verdade como o algoritmo funciona.",
    )

    # T2 — S4 probe: summary of the previous answer. Should re-cite source, not rehash.
    await run_turn(
        agent,
        session_id,
        "T2 — S4 (summarize previous explanation)",
        "agora resume tudo isso em no máximo 5 frases curtas",
    )


if __name__ == "__main__":
    asyncio.run(main())
