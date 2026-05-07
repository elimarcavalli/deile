"""Empirical test: verify that the max_tokens bump (8192→16384) removes truncation.

Sends a prompt that requires ≥800 words, captures full text, counts words,
reports whether the response completed naturally or was cut off.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from deile.config.manager import ConfigManager
from deile.core.agent import DeileAgent
from deile.core.models.bootstrap import bootstrap_providers
from deile.core.models.router import get_model_router


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


async def main() -> None:
    config_manager = ConfigManager()
    config_manager.load_config()
    router = get_model_router()
    registered = bootstrap_providers(router=router)
    print(f"Providers registered: {registered}")

    agent = DeileAgent(config_manager=config_manager, model_router=router)
    if hasattr(agent, "initialize"):
        await agent.initialize()

    session_id = "long-response-test"
    session = agent._get_or_create_session(session_id)
    # Use deepseek as fallback (anthropic credits unavailable)
    session.context_data["forced_model"] = "deepseek:deepseek-v4-pro"
    print(f"Forced model: {session.context_data['forced_model']}\n")

    prompt = (
        "Explique em detalhe (mínimo 800 palavras) o pattern Registry usado em DEILE. "
        "Cite arquivos por path e linha. Não pare antes de 800 palavras."
    )

    print(f"PROMPT: {prompt}\n")
    print("=" * 80)

    t0 = time.time()
    response = await agent.process_input(prompt, session_id=session_id)
    elapsed = time.time() - t0

    content = strip_ansi(response.content or "")
    word_count = len(content.split())
    char_count = len(content)
    approx_tokens = char_count // 4

    print(content)
    print("\n" + "=" * 80)
    print(f"ELAPSED: {elapsed:.1f}s")
    print(f"MODEL USED: {response.metadata.get('model_used', '?')}")
    print(f"WORD COUNT: {word_count}")
    print(f"CHAR COUNT: {char_count}")
    print(f"APPROX TOKENS: {approx_tokens}")
    print(f"RESULT: {'PASS (>=800 words)' if word_count >= 800 else 'FAIL (<800 words)'}")

    tail = content.rstrip()[-100:]
    ends_cleanly = bool(tail) and tail[-1] in ".!?\n" or tail.endswith("```") or tail.endswith("---")
    print(f"ENDS CLEANLY: {ends_cleanly}")
    print(f"TAIL: ...{repr(tail[-60:])}")


if __name__ == "__main__":
    asyncio.run(main())
