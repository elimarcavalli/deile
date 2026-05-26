"""Counter-test: does deepseek-v4-flash call tools spontaneously at all?

If YES (it calls list_files/read_file on its own), then the A1-A3 misses
are NOT a "model doesn't tool-call" issue — they're a "model didn't
associate the question with a skill" issue. Different fix.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from deile.config.manager import ConfigManager  # noqa: E402
from deile.core.agent import DeileAgent  # noqa: E402
from deile.core.agent import _normalize_history_content  # noqa: E402
from deile.core.models.bootstrap import bootstrap_providers  # noqa: E402
from deile.core.models.router import get_model_router  # noqa: E402

MODEL_KEY = "deepseek:deepseek-v4-flash"

PROBES = [
    ("D1", "Lista os arquivos da pasta deile/skills."),
    ("D2", "Quantas linhas tem o arquivo deile/__version__.py?"),
    ("D3", "Procura por 'invoke_skill' nos arquivos .py do projeto e me diz em quais aparece."),
]


def _tool_names(response):
    out = []
    for tr in (getattr(response, "tool_results", None) or []):
        meta = getattr(tr, "metadata", None) or {}
        name = (
            getattr(tr, "tool_name", None)
            or meta.get("function_name")
            or meta.get("tool_name")
            or ""
        )
        data = getattr(tr, "data", None) or {}
        if not name and isinstance(data, dict) and "body" in data and "name" in data:
            name = "invoke_skill"
        out.append(name or "<unknown>")
    return out


async def main():
    cm = ConfigManager()
    cm.load_config()
    router = get_model_router()
    registered = bootstrap_providers(router=router)
    print(f"providers: {registered}  model: {MODEL_KEY}")

    agent = DeileAgent(model_router=router, config_manager=cm)
    await agent.initialize()
    print("=" * 80)

    for pid, prompt in PROBES:
        session = agent.create_session(
            session_id=f"counter-{pid}-{int(time.time())}",
            working_directory=PROJECT_ROOT,
        )
        session.context_data["forced_model"] = MODEL_KEY
        print(f"\n[ {pid} ] {prompt}")
        start = time.time()
        try:
            r = await agent.process_input(user_input=prompt, session_id=session.session_id)
        except Exception as exc:
            print(f"  ERR: {type(exc).__name__}: {exc}")
            continue
        elapsed = time.time() - start
        tools = _tool_names(r)
        content = _normalize_history_content(r.content) or ""
        print(f"  [{elapsed:5.1f}s] tool_calls: {tools}")
        print(f"  resp: {content[:200]!r}")

    try:
        await agent.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
