"""Probe: agent reads an image attachment via vision_describe_image.

Mimics the Discord inbound flow:
- bot_context.attachments contains a Discord-CDN-style URL (we use a
  local stub server to avoid hitting Discord)
- the agent's text is "descreve essa imagem"
- the discord_developer persona instructs the agent to call
  vision_describe_image with the attachment URL

If the agent picks the right tool first try, this returns in seconds.
If it flails (the symptom we're fixing), we see the failure clearly.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import time

from aiohttp import web
from dotenv import load_dotenv


# 1x1 white PNG so the upstream Gemini call is cheap
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)


async def _start_image_server() -> tuple[str, web.AppRunner]:
    routes = web.RouteTableDef()

    @routes.get("/cdn/test.png")
    async def _img(_):
        return web.Response(body=PNG_BYTES, content_type="image/png")

    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app, handle_signals=False, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", runner


async def main() -> int:
    load_dotenv()

    image_base, image_runner = await _start_image_server()
    image_url = f"{image_base}/cdn/test.png"
    print("local image url:", image_url)

    from deile.config.manager import ConfigManager
    from deile.core.agent import DeileAgent
    from deile.core.models.bootstrap import bootstrap_providers
    from deile.core.models.router import get_model_router

    ConfigManager().load_config()
    mr = get_model_router()
    bootstrap_providers(router=mr)

    agent = DeileAgent(model_router=mr)
    await agent.initialize()

    from deile.tools.registry import get_tool_registry
    reg = get_tool_registry()
    has_vision = reg.get("vision_describe_image") is not None
    discord_tools = [t.name for t in reg.list_all() if t.name.startswith("discord_")]
    print("vision tool:", has_vision)
    print("discord tools:", len(discord_tools), "registered")

    if not has_vision:
        print("ABORT: vision tool not registered")
        await image_runner.cleanup()
        return 2

    bot_context = {
        "provider": "discord",
        "channel_scope": "DM",
        "channel_id": "1499608051114836128",
        "channel_name": None,
        "is_owner": True,
        "persona": "discord_developer",
        "attachments": [
            {
                "kind": "IMAGE",
                "url": image_url,
                "mime": "image/png",
                "filename": "test.png",
                "size_bytes": len(PNG_BYTES),
            }
        ],
    }

    sid = "probe_vision_001"
    if hasattr(agent, "get_or_create_session"):
        await agent.get_or_create_session(sid, persisted=False)

    if agent.persona_manager and hasattr(agent.persona_manager, "switch_persona"):
        try:
            await agent.persona_manager.switch_persona("discord_developer")
        except Exception as e:
            print("persona switch failed:", e)

    inbound = "descreve essa imagem"
    print("inbound:", inbound)

    # Build the same extra_system_prompt the bot pipeline injects, so the
    # LLM actually sees bot_context.attachments (not just the tools).
    ctx_lines = [
        "<bot_context>",
        f"provider: {bot_context['provider']}",
        f"channel_scope: {bot_context['channel_scope']}",
        f"channel_id: {bot_context['channel_id']}",
        f"is_owner: {bot_context['is_owner']}",
        f"persona: {bot_context['persona']}",
        "attachments:",
    ]
    for att in bot_context["attachments"]:
        ctx_lines.append(
            f"  - kind={att['kind']} mime={att['mime']} filename={att['filename']} "
            f"url={att['url']} size_bytes={att['size_bytes']}"
        )
    ctx_lines.append("</bot_context>")
    extra_prompt = "\n".join(ctx_lines)

    start = time.monotonic()
    try:
        response = await asyncio.wait_for(
            agent.process_input(
                inbound,
                session_id=sid,
                bot_context=bot_context,
                extra_system_prompt=extra_prompt,
            ),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        print(f"TIMEOUT after {time.monotonic()-start:.1f}s")
        await image_runner.cleanup()
        return 1

    elapsed = time.monotonic() - start
    text = getattr(response, "content", str(response))
    if isinstance(text, list):
        text = " ".join(str(p) for p in text)

    print(f"\n--- agent reply ({elapsed:.1f}s) ---")
    print(text[:800])
    await image_runner.cleanup()

    if "imagem" in text.lower() or "image" in text.lower():
        print("\nOK: agent referenced the image in its reply")
        return 0
    print("\nWARN: agent reply doesn't mention the image — manual inspection required")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
