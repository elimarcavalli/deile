"""Mimic the daemon's in-process agent flow and ask it to send a DM.

Failure-mode probe for the agent_failed timeout the user saw:
this script bootstraps the agent the same way `deilebot/cli.py`
does, then calls `agent.process_input(...)` with the same
bot_context/persona/extra_system_prompt the bot would inject.

If the agent picks the right `discord_*` tool the first try, this
returns in seconds. If it flails (the symptom we're fixing), we
see exactly where it goes wrong.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from dotenv import load_dotenv


async def main() -> int:
    load_dotenv()
    print("DEILE_BOT_ENDPOINT:", os.environ.get("DEILE_BOT_ENDPOINT"))
    print("DEILE_BOT_AUTH_TOKEN set:", bool(os.environ.get("DEILE_BOT_AUTH_TOKEN")))

    from deile.config.manager import ConfigManager
    from deile.core.agent import DeileAgent
    from deile.core.models.bootstrap import bootstrap_providers
    from deile.core.models.router import get_model_router

    ConfigManager().load_config()
    mr = get_model_router()
    bootstrap_providers(router=mr)

    agent = DeileAgent(model_router=mr)
    await agent.initialize()

    # Confirm the messaging tools are visible to the agent.
    from deile.tools.registry import get_tool_registry
    reg = get_tool_registry()
    discord_tools = [t.name for t in reg.list_all() if t.name.startswith("discord_")]
    print("messaging tools:", discord_tools)
    if not discord_tools:
        print("ABORT: agent has no discord_* tools registered")
        return 2

    # Same bot_context the pipeline injects when it's a DM.
    bot_context = {
        "provider": "discord",
        "channel_scope": "DM",
        "channel_id": "1499608051114836128",  # elimar's DM channel
        "channel_name": None,
        "is_owner": True,
        "persona": "discord_developer",
    }

    # Persona is already attached to the agent via agent.initialize().
    pm = agent.persona_manager

    # Construct the same kind of inbound the user sent on Discord.
    inbound = (
        "envia uma DM via tool discord_send_dm pra user_id 1475913578648436909 "
        "(elimar.ciss) com o texto 'probe ok — agente esta usando a tool certa'. "
        "Apos enviar, responda APENAS com o message_id retornado."
    )

    sid = "probe_session_001"
    if hasattr(agent, "get_or_create_session"):
        await agent.get_or_create_session(sid, persisted=False)
    else:
        try:
            agent.create_session(sid)
        except Exception:
            pass

    # Read the bot persona instructions verbatim (matches what the bot
    # injects via extra_system_prompt + the persona switch).
    if pm is not None and hasattr(pm, "switch_persona"):
        try:
            await pm.switch_persona("discord_developer")
            print("persona switched to discord_developer")
        except Exception as e:
            print("persona switch failed:", e)
    else:
        print("(persona manager not available — proceeding with default)")

    start = time.monotonic()
    try:
        response = await asyncio.wait_for(
            agent.process_input(inbound, session_id=sid, bot_context=bot_context),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        print(f"TIMEOUT after {time.monotonic()-start:.1f}s")
        return 1

    elapsed = time.monotonic() - start
    text = getattr(response, "content", str(response))
    if isinstance(text, list):
        text = " ".join(str(p) for p in text)
    print(f"\n--- agent reply ({elapsed:.1f}s) ---")
    print(text[:500])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
