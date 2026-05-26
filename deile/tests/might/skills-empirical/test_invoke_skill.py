"""Empirical end-to-end test of the unified skills system.

Three categories of probe, all against ``deepseek:deepseek-v4-flash``:

A) **Catalog discovery** (1 probe per bundled skill)
   Prompt mentions the topic but does NOT auto-trigger the skill. Pass
   criterion: model calls ``invoke_skill('<name>')`` on its own initiative
   after seeing the catalog in the system prompt.

B) **Auto-trigger injection** (1 probe)
   Prompt references a ``*.py`` file → python skill body should be injected
   into the system prompt as an "Active Skill" so the model never needs
   to call a tool. Pass criterion: response contains a body marker.

C) **Explicit invocation** (1 probe)
   Prompt explicitly tells the model to use ``invoke_skill('python')``.
   Pass criterion: the tool was invoked successfully. Proves the
   plumbing works regardless of how eager the model is on its own.

Run::

    python3 deile/tests/might/skills-empirical/test_invoke_skill.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from deile.config.manager import ConfigManager  # noqa: E402
from deile.core.agent import DeileAgent  # noqa: E402
from deile.core.agent import _normalize_history_content  # noqa: E402
from deile.core.models.bootstrap import bootstrap_providers  # noqa: E402
from deile.core.models.router import get_model_router  # noqa: E402

MODEL_KEY = os.getenv("DEILE_SMOKE_MODEL", "deepseek:deepseek-v4-flash")
RESULTS_PATH = Path(__file__).parent / "results.json"


@dataclass
class Probe:
    pid: str
    category: str  # "catalog" | "auto-trigger" | "explicit"
    skill_name: str
    prompt: str
    body_marker: str
    # "invoke_skill_called" or "body_marker_in_response" depending on category.
    pass_criterion: str


PROBES: List[Probe] = [
    Probe(
        pid="A1",
        category="catalog",
        skill_name="python",
        prompt=(
            "Tenho uma dúvida geral sobre Python: qual é a melhor prática "
            "para lidar com CancelledError em código async? Responda em 2 "
            "linhas."
        ),
        body_marker="re-raise",
        pass_criterion="invoke_skill_called",
    ),
    Probe(
        pid="A2",
        category="catalog",
        skill_name="typescript",
        prompt=(
            "Pergunta rápida sobre TypeScript: para um lookup keyed, devo "
            "usar Record<string, T> ou Map? Responda em 2 linhas."
        ),
        body_marker="claims every string key",
        pass_criterion="invoke_skill_called",
    ),
    Probe(
        pid="A3",
        category="catalog",
        skill_name="tdd",
        prompt=(
            "Vou começar um módulo novo de processamento de pagamentos. "
            "Me descreva em 3 linhas seu ciclo de TDD."
        ),
        body_marker="Red",
        pass_criterion="invoke_skill_called",
    ),
    Probe(
        pid="B1",
        category="auto-trigger",
        skill_name="python",
        prompt=(
            "Estou revisando o arquivo deile/__version__.py — me lembre "
            "qual é a regra do projeto para tratar CancelledError, em 2 "
            "linhas."
        ),
        body_marker="re-raise",
        pass_criterion="body_marker_in_response",
    ),
    Probe(
        pid="C1",
        category="explicit",
        skill_name="python",
        prompt=(
            "Antes de responder, chame a tool invoke_skill com "
            'name="python" e use o conteúdo retornado para responder: '
            "qual a regra do projeto sobre CancelledError? Responda em "
            "1 linha citando a recomendação literal da skill."
        ),
        body_marker="re-raise",
        pass_criterion="invoke_skill_called",
    ),
]


@dataclass
class ProbeResult:
    pid: str
    category: str
    skill_name: str
    prompt: str
    pass_criterion: str
    duration_s: float
    invoke_skill_called: bool = False
    invoke_skill_arg: Optional[str] = None
    tool_calls: List[str] = field(default_factory=list)
    response_preview: str = ""
    body_marker_present: bool = False
    passed: bool = False
    error: Optional[str] = None


def _extract_tool_calls(response: Any) -> List[Dict[str, Any]]:
    """Pull tool_name + data out of each ToolResult in *response.tool_results*."""
    out: List[Dict[str, Any]] = []
    for tr in (getattr(response, "tool_results", None) or []):
        meta = getattr(tr, "metadata", None) or {}
        name = (
            getattr(tr, "tool_name", None)
            or meta.get("function_name")
            or meta.get("tool_name")
            or ""
        )
        data = getattr(tr, "data", None) or {}
        # Heuristic: invoke_skill payload is always a dict with name+body.
        if not name and isinstance(data, dict) and "body" in data and "name" in data:
            name = "invoke_skill"
        out.append({"name": name or "<unknown>", "data": data})
    return out


async def _run_one(agent: DeileAgent, probe: Probe) -> ProbeResult:
    r = ProbeResult(
        pid=probe.pid,
        category=probe.category,
        skill_name=probe.skill_name,
        prompt=probe.prompt,
        pass_criterion=probe.pass_criterion,
        duration_s=0.0,
    )

    session = agent.create_session(
        session_id=f"skills-empirical-{probe.pid}-{int(time.time())}",
        working_directory=PROJECT_ROOT,
    )
    session.context_data["forced_model"] = MODEL_KEY

    start = time.time()
    try:
        response = await agent.process_input(
            user_input=probe.prompt,
            session_id=session.session_id,
        )
    except Exception as exc:
        r.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        r.duration_s = time.time() - start
        return r

    r.duration_s = time.time() - start
    content = _normalize_history_content(response.content) or ""
    r.response_preview = content[:200]
    r.body_marker_present = probe.body_marker.lower() in content.lower()

    tool_records = _extract_tool_calls(response)
    r.tool_calls = [t["name"] for t in tool_records]
    for t in tool_records:
        if t["name"] == "invoke_skill":
            r.invoke_skill_called = True
            data = t["data"]
            if isinstance(data, dict):
                r.invoke_skill_arg = data.get("name") or r.invoke_skill_arg

    if probe.pass_criterion == "invoke_skill_called":
        r.passed = r.invoke_skill_called and (
            r.invoke_skill_arg is None or r.invoke_skill_arg == probe.skill_name
        )
    elif probe.pass_criterion == "body_marker_in_response":
        r.passed = r.body_marker_present

    return r


async def main() -> int:
    config_manager = ConfigManager()
    config_manager.load_config()
    router = get_model_router()
    registered = bootstrap_providers(router=router)
    if not registered:
        print("FATAL: nenhum provider registrado")
        return 1
    print(f"providers registered: {registered}")
    print(f"forced model: {MODEL_KEY}")
    print("=" * 80)

    agent = DeileAgent(model_router=router, config_manager=config_manager)
    await agent.initialize()

    from deile.skills.registry import get_skill_registry
    names = get_skill_registry().list_names()
    print(f"skills loaded: {names}")
    print(f"tools registered (filter skill_*): "
          f"{[t.name for t in agent.tool_registry.list_all() if 'skill' in t.name]}")
    print("=" * 80)

    results: List[ProbeResult] = []
    for probe in PROBES:
        print(f"\n[ {probe.pid} | {probe.category} | skill={probe.skill_name} ]")
        print(f"  prompt: {probe.prompt[:120]}")
        r = await _run_one(agent, probe)
        results.append(r)
        verdict = "PASS" if r.passed else ("ERR " if r.error else "FAIL")
        print(f"  {verdict} [{r.duration_s:5.1f}s]")
        print(f"       tool_calls: {r.tool_calls}")
        print(f"       invoke_skill_called={r.invoke_skill_called} arg={r.invoke_skill_arg!r}")
        print(f"       body_marker '{probe.body_marker}' present? {r.body_marker_present}")
        if r.error:
            print(f"       err: {r.error}")
        if r.response_preview:
            print(f"       resp: {r.response_preview!r}")

    print("\n" + "=" * 80)
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed and not r.error)
    errs = sum(1 for r in results if r.error)
    print(f"PASS: {passed}/{len(results)}  FAIL: {failed}  ERR: {errs}")
    print("=" * 80)

    RESULTS_PATH.write_text(
        json.dumps(
            {"model": MODEL_KEY, "results": [asdict(r) for r in results]},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"saved: {RESULTS_PATH}")

    try:
        await agent.shutdown()
    except Exception:
        pass

    return 0 if errs == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
