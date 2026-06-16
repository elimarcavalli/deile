"""End-to-end smoke test for write_file autonomy.

Reproduces the scenario where the user asks the agent to alter an existing
file. With the autonomous default, the agent must rewrite the file in a single
turn, without asking for confirmation.

Verifies:
  * the agent did NOT ask any "yes/no" type question
  * the file content actually changed on disk
  * the new content matches what the agent claims it wrote

Usage:
    python scripts/smoke_test_write_autonomy.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


CONFIRMATION_PATTERNS = [
    r"\bsim/n[ãa]o\b",
    r"\b(você|voce)\s+(me\s+)?autoriza\b",
    r"\bposso\s+(prosseguir|sobrescrever)\b",
    r"\bconfirma(r|s|m)?\?",
    r"\bdeseja\s+(que\s+eu|continuar|prosseguir)\b",
]


def _looks_like_confirmation_request(text: str) -> str | None:
    lowered = text.lower()
    for pat in CONFIRMATION_PATTERNS:
        if re.search(pat, lowered):
            return pat
    return None


async def main() -> int:
    from deile.config.manager import ConfigManager
    from deile.config.settings import get_settings
    from deile.core.agent import DeileAgent
    from deile.core.models.gemini_provider import GeminiProvider
    from deile.core.models.router import get_model_router
    from deile.parsers.registry import get_parser_registry
    from deile.tools.registry import get_tool_registry

    if not os.getenv("GOOGLE_API_KEY"):
        print("FATAL: GOOGLE_API_KEY not set", file=sys.stderr)
        return 2

    target = PROJECT_ROOT / "ola.py"
    pre_existed = target.exists()
    original = target.read_text(encoding="utf-8") if pre_existed else None
    if pre_existed:
        print(f"[setup] ola.py exists, original: {original!r}")
    else:
        print("[setup] ola.py missing — will ask the agent to create it")

    try:
        settings = get_settings()
        settings.working_directory = PROJECT_ROOT

        config = ConfigManager()
        config.load_config()

        router = get_model_router()
        if not router.providers:
            router.register_provider(GeminiProvider(), priority=1)

        agent = DeileAgent(
            model_router=router,
            tool_registry=get_tool_registry(),
            parser_registry=get_parser_registry(),
            config_manager=config,
        )
        await agent.initialize()

        session = agent.create_session(
            session_id="autonomy_smoke", working_directory=PROJECT_ROOT
        )

        verb = "altere" if pre_existed else "crie"
        prompt = (
            f"{verb} o ola.py pra imprimir a string exata "
            "'AUTONOMY_PROOF_42' (sem aspas) e nada mais"
        )
        print("\n" + "=" * 70)
        print(f"[user] {prompt}")
        response = await agent.process_input(prompt, session_id=session.session_id)
        text = response.content or ""
        print(f"[deile] {text}")
        tool_calls = [
            (r.metadata or {}).get("function_name", "?") for r in response.tool_results
        ]
        print(f"[tools] {tool_calls}")

        on_disk = target.read_text(encoding="utf-8")
        print(f"\n[verify] file on disk now: {on_disk!r}")

        problems: list[str] = []

        confirmation = _looks_like_confirmation_request(text)
        if confirmation:
            problems.append(
                f"agent asked for confirmation (matched pattern: {confirmation!r})"
            )

        if "write_file" not in tool_calls:
            problems.append(
                f"agent did not call write_file in this turn (called: {tool_calls})"
            )

        if "AUTONOMY_PROOF_42" not in on_disk:
            problems.append(
                "file on disk does not contain the requested marker "
                "'AUTONOMY_PROOF_42'"
            )

        if pre_existed and on_disk == original:
            problems.append("file on disk is unchanged from before")

        print("\n" + "=" * 70)
        if problems:
            print("FAIL:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1

        print("PASS: agent rewrote ola.py in one turn, no confirmation asked")
        return 0
    finally:
        # Idempotent cleanup: put the workspace back to whatever state it was
        # in before the test ran (preserve original content, or remove the file
        # entirely if the agent had to create it from scratch).
        if pre_existed:
            target.write_text(original, encoding="utf-8")
            print("[cleanup] restored ola.py to original content")
        elif target.exists():
            target.unlink()
            print("[cleanup] removed ola.py created by the agent")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
