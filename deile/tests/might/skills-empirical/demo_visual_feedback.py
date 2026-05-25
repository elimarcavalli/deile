"""Capture the EXACT runtime experience around skill loading + auto-trigger.

Two captures:

1. Boot summary log (logger.info from agent._auto_discover_components).
2. STAGE events the streaming renderer would show on screen when a skill
   auto-triggers — emitted by agent.process_input_stream after
   build_context detects active skills.

Output is plain text (no ANSI) so it can be pasted verbatim into chat.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

# Bridge agent logs (deile.*) to stderr so the boot summary surfaces.
log_buffer = io.StringIO()
log_handler = logging.StreamHandler(log_buffer)
log_handler.setLevel(logging.INFO)
log_handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
for name in ("deile.core.agent", "deile.skills.bootstrap", "deile.skills.config",
             "deile.commands.skill_loader", "deile.core.context_manager"):
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.addHandler(log_handler)
    lg.propagate = True

from deile.config.manager import ConfigManager  # noqa: E402
from deile.core.agent import DeileAgent  # noqa: E402
from deile.core.models.bootstrap import bootstrap_providers  # noqa: E402
from deile.core.models.router import get_model_router  # noqa: E402
from deile.core.models.stream_events import StreamEventType  # noqa: E402

MODEL_KEY = "deepseek:deepseek-v4-flash"


def banner(s: str) -> None:
    print("\n" + "═" * 72)
    print(s)
    print("═" * 72)


async def main():
    banner("BOOT — what shows in the launch log")

    cm = ConfigManager()
    cm.load_config()
    router = get_model_router()
    bootstrap_providers(router=router)

    log_buffer.seek(0); log_buffer.truncate(0)
    agent = DeileAgent(model_router=router, config_manager=cm)
    await agent.initialize()

    boot_log = log_buffer.getvalue()
    for line in boot_log.splitlines():
        if "skill" in line.lower():
            print(f"  {line}")

    banner("PER-TURN — STAGE events the spinner shows")
    print("Prompt: 'Estou revisando @deile/__version__.py' (file ref triggers `python` skill via *.py glob)")
    print()

    session = agent.create_session(
        session_id="skill-feedback-demo",
        working_directory=PROJECT_ROOT,
    )
    session.context_data["forced_model"] = MODEL_KEY

    log_buffer.seek(0); log_buffer.truncate(0)

    stages: list[tuple[int, str]] = []
    text_chars = 0
    n_tool_results = 0
    async for ev in agent.process_input_stream(
        user_input="Estou revisando @deile/__version__.py — me lembre a regra do projeto sobre CancelledError em 1 linha.",
        session_id=session.session_id,
    ):
        if ev.type is StreamEventType.STAGE:
            stages.append((ev.iteration or 0, ev.stage or ""))
        elif ev.type is StreamEventType.TEXT_DELTA and ev.text:
            text_chars += len(ev.text)
        elif ev.type is StreamEventType.TOOL_RESULT:
            n_tool_results += 1

    print("ORDERED LIST OF STAGE EVENTS:")
    for i, (it, label) in enumerate(stages, 1):
        # The stage message template already prefixes the 🧩 emoji — no
        # second one here. Just use a column marker so the skill stage
        # row is visually scannable in the list.
        marker = "🧩" if "Skill ativa" in label else " ·"
        print(f"  {i:2d}. iter={it} {marker} {label}")

    print()
    print(f"ALSO IN LOG OUTPUT this turn:")
    for line in log_buffer.getvalue().splitlines():
        if "skill" in line.lower():
            print(f"  {line}")

    print()
    print(f"(LLM response: {text_chars} chars · tool_results: {n_tool_results})")

    try:
        await agent.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
