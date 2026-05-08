"""Focused diagnostic for gemini-2.5-flash-lite function calling.

Three phases:
    A) Same prompt as multi-test (DEILE flow) — baseline reproduction.
    B) Same DEILE flow, but with a more explicit prompt ordering tool use.
    C) Direct google-genai SDK call (bypassing DEILE) — does the raw model invoke tools
       at all when given a trivial FunctionDeclaration?

If A and B fail but C succeeds → DEILE config issue.
If A, B, C all fail → model is too weak / SDK incompatibility.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from deile.config.manager import ConfigManager  # noqa: E402
from deile.core.agent import DeileAgent  # noqa: E402
from deile.core.models.bootstrap import bootstrap_providers  # noqa: E402
from deile.core.models.router import get_model_router  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
MODEL_KEY = "gemini:gemini-2.5-flash-lite"
MODEL_ID = "gemini-2.5-flash-lite"


def _format_tool_calls(tool_results) -> str:
    if not tool_results:
        return "  (none)"
    lines = []
    for i, tr in enumerate(tool_results, 1):
        name = (tr.metadata or {}).get("function_name", "?")
        status = tr.status.value if hasattr(tr.status, "value") else str(tr.status)
        msg = (tr.message or "").splitlines()[0] if tr.message else ""
        lines.append(f"  [{i}] {name} status={status}")
        if msg:
            lines.append(f"      msg: {msg[:160]}")
    return "\n".join(lines)


async def phase_A_baseline(agent: DeileAgent, log_lines: list) -> None:
    """Reproduce the failing run with the original prompt."""
    log_lines.append("\n" + "=" * 100)
    log_lines.append("PHASE A — baseline (same prompt as multi-test)")
    log_lines.append("=" * 100)
    session_id = "diag-gemini-A"
    session = agent._get_or_create_session(session_id)
    session.context_data["forced_model"] = MODEL_KEY
    prompt = (
        "explica como funciona a função `_resolve_project_path` do DEILE em detalhes — "
        "incluindo a estrutura geral, os edge cases que ela trata, e o que acontece quando "
        "o path não pode ser resolvido. Quero entender de verdade como o algoritmo funciona."
    )
    t0 = time.time()
    response = await agent.process_input(prompt, session_id=session_id)
    dt = time.time() - t0
    log_lines.append(f"USER: {prompt}")
    log_lines.append(f"\n--- TOOL CALLS ({len(response.tool_results)}) ---")
    log_lines.append(_format_tool_calls(response.tool_results))
    log_lines.append(f"\n--- RESPONSE TEXT ---\n{response.content}")
    log_lines.append(f"\n--- METADATA ---\n  duration: {dt:.2f}s")
    log_lines.append(f"  model_used: {response.metadata.get('model_used', '?') if response.metadata else '?'}")


async def phase_B_explicit(agent: DeileAgent, log_lines: list) -> None:
    """Same flow but with a prompt that explicitly orders tool usage."""
    log_lines.append("\n" + "=" * 100)
    log_lines.append("PHASE B — explicit tool order")
    log_lines.append("=" * 100)
    session_id = "diag-gemini-B"
    session = agent._get_or_create_session(session_id)
    session.context_data["forced_model"] = MODEL_KEY
    prompt = (
        "USE A FERRAMENTA read_file PARA LER deile/tools/file_tools.py "
        "ANTES DE RESPONDER QUALQUER COISA. Depois explica brevemente a função "
        "`_resolve_project_path` citando file:linha."
    )
    t0 = time.time()
    response = await agent.process_input(prompt, session_id=session_id)
    dt = time.time() - t0
    log_lines.append(f"USER: {prompt}")
    log_lines.append(f"\n--- TOOL CALLS ({len(response.tool_results)}) ---")
    log_lines.append(_format_tool_calls(response.tool_results))
    log_lines.append(f"\n--- RESPONSE TEXT ---\n{response.content}")
    log_lines.append(f"\n--- METADATA ---\n  duration: {dt:.2f}s")


async def phase_C_raw_sdk(log_lines: list) -> None:
    """Direct google-genai call with one trivial FunctionDeclaration."""
    log_lines.append("\n" + "=" * 100)
    log_lines.append("PHASE C — raw google-genai SDK + 1 trivial tool")
    log_lines.append("=" * 100)

    from google import genai
    from google.genai import types
    from google.genai.types import FunctionDeclaration, HttpOptions, Tool

    api_key = os.getenv("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key, http_options=HttpOptions(api_version="v1beta"))

    fd = FunctionDeclaration(
        name="get_current_weather",
        description="Get the current weather for a city.",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
            },
            "required": ["city"],
        },
    )
    tools = [Tool(function_declarations=[fd])]

    config = types.GenerateContentConfig(
        tools=tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.1,
        max_output_tokens=512,
    )

    prompt = "What's the weather in Paris right now? Use the available tool."
    log_lines.append(f"USER: {prompt}")

    t0 = time.time()
    response = await client.aio.models.generate_content(
        model=MODEL_ID,
        contents=prompt,
        config=config,
    )
    dt = time.time() - t0

    candidates = getattr(response, "candidates", []) or []
    log_lines.append(f"  candidates: {len(candidates)}")
    if candidates:
        cand = candidates[0]
        log_lines.append(f"  finish_reason: {getattr(cand, 'finish_reason', '?')}")
        parts = getattr(getattr(cand, "content", None), "parts", []) or []
        log_lines.append(f"  parts: {len(parts)}")
        for i, p in enumerate(parts):
            fc = getattr(p, "function_call", None)
            txt = getattr(p, "text", None)
            if fc:
                log_lines.append(f"    [{i}] function_call name={fc.name} args={dict(fc.args or {})}")
            elif txt:
                log_lines.append(f"    [{i}] text: {txt[:200]}")
            else:
                log_lines.append(f"    [{i}] (other) {p}")

    log_lines.append(f"  duration: {dt:.2f}s")


async def phase_D_raw_sdk_no_persona(log_lines: list) -> None:
    """Like Phase C, but with a tool more relevant to the persona's domain."""
    log_lines.append("\n" + "=" * 100)
    log_lines.append("PHASE D — raw SDK + read_file-shaped tool, code-context prompt")
    log_lines.append("=" * 100)

    from google import genai
    from google.genai import types
    from google.genai.types import FunctionDeclaration, HttpOptions, Tool

    api_key = os.getenv("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key, http_options=HttpOptions(api_version="v1beta"))

    fd = FunctionDeclaration(
        name="read_file",
        description="Read a file from the project filesystem and return its content.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
            },
            "required": ["file_path"],
        },
    )
    tools = [Tool(function_declarations=[fd])]

    config = types.GenerateContentConfig(
        tools=tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.1,
        max_output_tokens=512,
    )

    prompt = (
        "Explain how the function `_resolve_project_path` works in detail. "
        "Read deile/tools/file_tools.py first using the read_file tool."
    )
    log_lines.append(f"USER: {prompt}")

    t0 = time.time()
    response = await client.aio.models.generate_content(
        model=MODEL_ID,
        contents=prompt,
        config=config,
    )
    dt = time.time() - t0

    candidates = getattr(response, "candidates", []) or []
    log_lines.append(f"  candidates: {len(candidates)}")
    if candidates:
        cand = candidates[0]
        log_lines.append(f"  finish_reason: {getattr(cand, 'finish_reason', '?')}")
        parts = getattr(getattr(cand, "content", None), "parts", []) or []
        log_lines.append(f"  parts: {len(parts)}")
        for i, p in enumerate(parts):
            fc = getattr(p, "function_call", None)
            txt = getattr(p, "text", None)
            if fc:
                log_lines.append(f"    [{i}] function_call name={fc.name} args={dict(fc.args or {})}")
            elif txt:
                log_lines.append(f"    [{i}] text: {txt[:200]}")

    log_lines.append(f"  duration: {dt:.2f}s")


async def main():
    print("Bootstrapping providers...")
    cm = ConfigManager()
    cm.load_config()
    router = get_model_router()
    bootstrap_providers(router=router)
    agent = DeileAgent(config_manager=cm, model_router=router)
    if hasattr(agent, "initialize"):
        await agent.initialize()

    log_lines: list[str] = []
    log_lines.append(f"Diagnostic for model: {MODEL_KEY}")

    await phase_A_baseline(agent, log_lines)
    await phase_B_explicit(agent, log_lines)
    await phase_C_raw_sdk(log_lines)
    await phase_D_raw_sdk_no_persona(log_lines)

    out = THIS_DIR / "diagnose-gemini-lite.log"
    out.write_text("\n".join(log_lines))
    print(f"\nWrote diagnostic log to {out.name}")

    print("\nQUICK SUMMARY:")
    print("\n".join(log_lines[-40:]))


if __name__ == "__main__":
    asyncio.run(main())
