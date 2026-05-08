"""Empirical test of persona rule 8 across all four providers (cheapest model each).

Models (provider:model_id):
    anthropic:claude-haiku-4-5
    openai:gpt-5.4-mini
    deepseek:deepseek-v4-flash
    gemini:gemini-2.5-flash-lite

Probe: T1 (cenario S1) — "explain how _resolve_project_path works in detail".
Pass criteria for rule 8:
    1. At least one read_file call BEFORE composing the explanation.
    2. Response cites file:line ranges.
    3. No fabricated narrative substituting for source.

Each model runs in an isolated session_id with forced_model. Output of every
turn is written to a per-model .log file in this directory.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Path: deile/tests/might/persona-rule-8-multi/test_rule8_multi.py
# parents[0]=persona-rule-8-multi, [1]=might, [2]=tests, [3]=deile, [4]=project root
PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from deile.config.manager import ConfigManager  # noqa: E402
from deile.core.agent import DeileAgent  # noqa: E402
from deile.core.models.bootstrap import bootstrap_providers  # noqa: E402
from deile.core.models.router import get_model_router  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent

MODELS = [
    "anthropic:claude-haiku-4-5",
    "openai:gpt-5.4-mini",
    "deepseek:deepseek-v4-flash",
    "gemini:gemini-2.5-flash-lite",
]

PROMPT_T1 = (
    "explica como funciona a função `_resolve_project_path` do DEILE em detalhes — "
    "incluindo a estrutura geral, os edge cases que ela trata, e o que acontece quando "
    "o path não pode ser resolvido. Quero entender de verdade como o algoritmo funciona."
)


def _format_tool_calls(tool_results) -> str:
    if not tool_results:
        return "  (none)"
    lines = []
    for i, tr in enumerate(tool_results, 1):
        name = tr.metadata.get("function_name", "?") if tr.metadata else "?"
        status = tr.status.value if hasattr(tr.status, "value") else str(tr.status)
        msg = (tr.message or "").splitlines()[0] if tr.message else ""
        lines.append(f"  [{i}] {name} status={status}")
        if msg:
            lines.append(f"      msg: {msg[:160]}")
    return "\n".join(lines)


async def run_for_model(agent: DeileAgent, model_key: str) -> dict:
    """Run T1 against `model_key` in an isolated session. Returns summary dict."""
    safe_key = model_key.replace(":", "__").replace("/", "_")
    log_path = THIS_DIR / f"run-{safe_key}.log"
    session_id = f"rule8-multi-{safe_key}"
    session = agent._get_or_create_session(session_id)
    session.context_data["forced_model"] = model_key

    header = (
        "=" * 100 + "\n"
        f"=== MODEL: {model_key}\n"
        f"=== SESSION: {session_id}\n"
        f"=== USER: {PROMPT_T1}\n"
        + "=" * 100 + "\n"
    )

    t0 = time.time()
    try:
        response = await agent.process_input(PROMPT_T1, session_id=session_id)
        dt = time.time() - t0
        tool_calls_block = _format_tool_calls(response.tool_results)
        n_tools = len(response.tool_results)
        n_read_file = sum(
            1 for tr in response.tool_results
            if (tr.metadata or {}).get("function_name") == "read_file"
        )
        body = (
            f"\n--- TOOL CALLS ({n_tools}) ---\n{tool_calls_block}\n\n"
            f"--- RESPONSE TEXT ---\n{response.content}\n\n"
            f"--- METADATA ---\n"
            f"  duration: {dt:.2f}s\n"
            f"  model_used: {response.metadata.get('model_used', '?') if response.metadata else '?'}\n"
            f"  status: {response.status.value if hasattr(response.status, 'value') else response.status}\n"
            f"  read_file_count: {n_read_file}\n"
        )
        log_path.write_text(header + body)
        print(f"[{model_key}] OK — duration={dt:.2f}s, tools={n_tools}, read_file={n_read_file} → {log_path.name}")
        return {
            "model": model_key,
            "ok": True,
            "duration": dt,
            "tools": n_tools,
            "read_file_count": n_read_file,
            "log": str(log_path),
        }
    except Exception as exc:
        dt = time.time() - t0
        body = f"\n--- EXCEPTION after {dt:.2f}s ---\n{type(exc).__name__}: {exc}\n"
        log_path.write_text(header + body)
        print(f"[{model_key}] FAIL — {type(exc).__name__}: {exc}")
        return {"model": model_key, "ok": False, "error": f"{type(exc).__name__}: {exc}", "log": str(log_path)}


async def main():
    print("Bootstrapping providers...")
    config_manager = ConfigManager()
    config_manager.load_config()
    router = get_model_router()
    registered = bootstrap_providers(router=router)
    print(f"Providers registered: {registered}\n")

    agent = DeileAgent(config_manager=config_manager, model_router=router)
    if hasattr(agent, "initialize"):
        await agent.initialize()

    summary = []
    for model_key in MODELS:
        print(f"\n>>> Running {model_key} ...")
        result = await run_for_model(agent, model_key)
        summary.append(result)

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    for r in summary:
        if r["ok"]:
            print(f"  {r['model']:42s}  duration={r['duration']:6.2f}s  tools={r['tools']:2d}  read_file={r['read_file_count']}  log={Path(r['log']).name}")
        else:
            print(f"  {r['model']:42s}  FAILED  {r['error']}")


if __name__ == "__main__":
    asyncio.run(main())
