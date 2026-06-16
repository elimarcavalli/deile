"""Quick probe: dump the system prompt DEILE actually sends to the LLM.

Bootstraps ContextManager exactly as the agent would, runs build_context()
for one prompt, and prints what landed in ``system_instruction``. Confirms
whether the "Available Skills" catalog reaches the LLM.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from deile.core.context_manager import ContextManager  # noqa: E402
from deile.parsers.base import ParseResult, ParseStatus  # noqa: E402


async def main() -> None:
    cm = ContextManager()  # no persona_manager → fallback path; skills hook still runs

    parse_result = ParseResult(status=ParseStatus.SUCCESS, file_references=[])
    session = SimpleNamespace(
        conversation_history=[
            {
                "role": "user",
                "content": "Pergunta rápida sobre TypeScript: Record vs Map?",
            }
        ],
        context_data={},
    )

    ctx = await cm.build_context(
        user_input="Pergunta rápida sobre TypeScript: Record vs Map?",
        parse_result=parse_result,
        session=session,
    )

    sys_instr = ctx.get("system_instruction", "")
    print("=" * 80)
    print(f"system_instruction length: {len(sys_instr)} chars")
    print("=" * 80)

    # Look for the two markers we expect
    has_active = "## Active Skills" in sys_instr
    has_catalog = "## Available Skills" in sys_instr
    print(f"  '## Active Skills' present?    {has_active}")
    print(f"  '## Available Skills' present? {has_catalog}")
    print("=" * 80)

    # Print the last 2000 chars of the system prompt (where the skills block
    # would land) so we can verify.
    print("LAST 2000 CHARS OF SYSTEM PROMPT:")
    print("-" * 80)
    print(sys_instr[-2000:])
    print("-" * 80)


if __name__ == "__main__":
    asyncio.run(main())
