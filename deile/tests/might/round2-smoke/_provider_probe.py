"""Probe minimo dos providers para descobrir quais respondem.

Faz 1 chamada de ~1 token em cada provedor configurado. Imprime
``OK``/``FAIL`` com o motivo. Custo total esperado: < $0.001.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from deile.config.manager import ConfigManager  # noqa: E402
from deile.core.models.bootstrap import bootstrap_providers  # noqa: E402
from deile.core.models.router import get_model_router  # noqa: E402

CANDIDATES = [
    "gemini:gemini-2.5-flash-lite",
    "deepseek:deepseek-v4-flash",
    "openai:gpt-5.4-mini",
    "anthropic:claude-haiku-4-5",
]


async def _ping(provider_key: str, provider) -> tuple[str, str]:
    from deile.core.models.base import ModelMessage
    try:
        result = await provider.generate(
            messages=[ModelMessage(role="user", content="hi")],
            max_tokens=5,
        )
        text = getattr(result, "content", None) or str(result)
        return (provider_key, f"OK (resp: {str(text)[:60]!r})")
    except Exception as exc:
        return (provider_key, f"FAIL: {type(exc).__name__}: {str(exc)[:300]}")


async def main() -> None:
    config_manager = ConfigManager()
    config_manager.load_config()
    router = get_model_router()
    registered = bootstrap_providers(router=router)
    print(f"providers registered: {registered}")
    print(f"router.providers keys: {list(router.providers.keys())}")
    print("-" * 80)
    # Pick one model per provider to test
    seen_providers = set()
    for key, provider in router.providers.items():
        prov_name = key.split(":", 1)[0]
        if prov_name in seen_providers:
            continue
        seen_providers.add(prov_name)
        result = await _ping(key, provider)
        print(f"{result[0]:50s}  {result[1]}")


if __name__ == "__main__":
    asyncio.run(main())
