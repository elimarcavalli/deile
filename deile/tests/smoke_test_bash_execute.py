"""End-to-end smoke test for the bash_execute tool.

Asks the agent to run the ``ola.py`` that already exists at the repo root and
verifies the *actual* stdout from disk is what the agent surfaces back.

Usage:
    python scripts/smoke_test_bash_execute.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


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

    ola = PROJECT_ROOT / "ola.py"
    if not ola.exists():
        print(f"FATAL: {ola} not found — create it first", file=sys.stderr)
        return 2

    # Read the ground-truth stdout straight from disk. This is what we'll
    # require the agent to repeat back — that's the only way to prove it
    # actually executed instead of paraphrasing or hallucinating.
    expected_stdout = ola.read_text(encoding="utf-8").strip()
    # `print(...)` strips quotes, so derive expected output by literally
    # running the script ourselves (subprocess, not the agent).
    import subprocess
    proof = subprocess.run(
        ["python3", str(ola)], capture_output=True, text=True, check=True
    )
    expected_stdout = proof.stdout.strip()
    print(f"[setup] ola.py exists at {ola}")
    print(f"[setup] expected stdout (from real subprocess): {expected_stdout!r}")

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
        session_id="smoke_test", working_directory=PROJECT_ROOT
    )

    async def turn(label: str, prompt: str) -> str:
        print("\n" + "=" * 70)
        print(f"[user → {label}] {prompt}")
        response = await agent.process_input(prompt, session_id=session.session_id)
        text = response.content or ""
        print(f"[deile ← {label}] {text}")
        tools = [
            f"{(r.metadata or {}).get('function_name', '?')}={r.status.value}"
            for r in response.tool_results
        ]
        print(f"[tools] {tools}")
        return text

    result_text = await turn(
        "turn1",
        "execute o arquivo ola.py com python3 e me traga exatamente o que ele imprimiu",
    )

    print("\n" + "=" * 70)
    if expected_stdout and expected_stdout in result_text:
        print(f"PASS: agent surfaced the real stdout {expected_stdout!r}")
        return 0
    print(
        f"FAIL: agent reply did not contain the real stdout {expected_stdout!r}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
