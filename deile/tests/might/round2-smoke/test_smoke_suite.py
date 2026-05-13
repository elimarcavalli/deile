"""R2 smoke suite — exercita as superficies user-facing do DEILE.

Cada teste roda contra um LLM real (DeepSeek por padrão, configuravel via
``DEILE_SMOKE_MODEL``). Captura resposta + tool_calls e classifica
pass/fail com criterios explicitos.

Total: ~6 chamadas LLM curtas. Custo estimado: < $0.05.

Para executar:

    DEILE_SMOKE_MODEL=deepseek:deepseek-v4-flash \\
        python deile/tests/might/round2-smoke/test_smoke_suite.py
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
from deile.core.agent import DeileAgent, _normalize_history_content  # noqa: E402
from deile.core.models.bootstrap import bootstrap_providers  # noqa: E402
from deile.core.models.router import get_model_router  # noqa: E402

MODEL_KEY = os.getenv("DEILE_SMOKE_MODEL", "deepseek:deepseek-v4-flash")
RESULTS_PATH = Path(__file__).parent / "results.json"


@dataclass
class TestResult:
    test_id: str
    name: str
    status: str  # "pass" | "fail" | "skip"
    duration_s: float
    response_preview: str = ""
    tool_calls: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None


async def _run_turn(agent: DeileAgent, session_id: str, user_input: str) -> Dict[str, Any]:
    """Run one turn and return response + tool calls used."""
    start = time.time()
    try:
        response = await agent.process_input(
            user_input=user_input,
            session_id=session_id,
        )
        duration = time.time() - start
        return {
            "status": "ok",
            "content": _normalize_history_content(response.content),
            "tool_calls": [
                t.tool_name if hasattr(t, "tool_name") else str(t)
                for t in (response.tool_results or [])
            ],
            "duration_s": duration,
            "metadata": response.metadata or {},
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "duration_s": time.time() - start,
        }


async def _make_session(agent: DeileAgent, label: str):
    session = agent.create_session(
        session_id=f"smoke-{label}-{int(time.time())}",
        working_directory=PROJECT_ROOT,
    )
    session.context_data["forced_model"] = MODEL_KEY
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def t_a1_simple_qa(agent: DeileAgent) -> TestResult:
    """Conversa simples — 1 pergunta direta, 1 resposta."""
    r = TestResult(test_id="A1", name="simple Q&A", status="fail", duration_s=0.0)
    session = await _make_session(agent, "a1")
    turn = await _run_turn(agent, session.session_id, "Diga apenas 'ok' e nada mais.")
    r.duration_s = turn["duration_s"]
    if turn["status"] != "ok":
        r.error = turn.get("error")
        return r
    r.response_preview = (turn["content"] or "")[:120]
    r.tool_calls = turn.get("tool_calls", [])
    if turn["content"]:
        r.status = "pass"
    else:
        r.notes.append("resposta vazia")
    return r


async def t_a2_multi_turn_context(agent: DeileAgent) -> TestResult:
    """2 turnos — segundo turno deve usar informacao do primeiro."""
    r = TestResult(test_id="A2", name="multi-turn context", status="fail", duration_s=0.0)
    session = await _make_session(agent, "a2")
    t1 = await _run_turn(agent, session.session_id, "Meu nome é Xandao. Repita meu nome.")
    if t1["status"] != "ok":
        r.error = f"turn1: {t1.get('error')}"
        return r
    t2 = await _run_turn(agent, session.session_id, "Qual e o nome que eu te disse antes?")
    r.duration_s = t1["duration_s"] + t2["duration_s"]
    if t2["status"] != "ok":
        r.error = f"turn2: {t2.get('error')}"
        return r
    r.response_preview = f"T1: {(t1['content'] or '')[:60]} | T2: {(t2['content'] or '')[:60]}"
    if "xandao" in (t2["content"] or "").lower():
        r.status = "pass"
    else:
        r.notes.append("turn2 nao recuperou nome do turn1 — contexto pode estar quebrado")
    return r


async def t_b1_read_file_tool(agent: DeileAgent) -> TestResult:
    """Tool: leitura de arquivo. Modelo deve invocar read_file."""
    r = TestResult(test_id="B1", name="read_file tool", status="fail", duration_s=0.0)
    session = await _make_session(agent, "b1")
    turn = await _run_turn(
        agent,
        session.session_id,
        "Use a tool read_file para ler o arquivo deile/__version__.py e me diga apenas qual o valor da variavel __version__.",
    )
    r.duration_s = turn["duration_s"]
    if turn["status"] != "ok":
        r.error = turn.get("error")
        return r
    r.response_preview = (turn["content"] or "")[:120]
    r.tool_calls = turn.get("tool_calls", [])
    read_called = any("read" in tc.lower() for tc in r.tool_calls)
    import re as _re
    has_version = bool(_re.search(r"\d+\.\d+\.\d+", turn["content"] or ""))
    if read_called and has_version:
        r.status = "pass"
    else:
        if not read_called:
            r.notes.append("read_file nao foi invocado")
        if not has_version:
            r.notes.append("resposta nao contem versao X.Y.Z")
    return r


async def t_d1_bash_tool(agent: DeileAgent) -> TestResult:
    """Tool: bash. Modelo deve rodar pwd via bash tool."""
    r = TestResult(test_id="D1", name="bash tool (pwd)", status="fail", duration_s=0.0)
    session = await _make_session(agent, "d1")
    turn = await _run_turn(
        agent,
        session.session_id,
        "Use a tool bash para rodar o comando 'pwd' e me diga apenas o path resultante.",
    )
    r.duration_s = turn["duration_s"]
    if turn["status"] != "ok":
        r.error = turn.get("error")
        return r
    r.response_preview = (turn["content"] or "")[:120]
    r.tool_calls = turn.get("tool_calls", [])
    bash_called = any("bash" in tc.lower() or "execute" in tc.lower() for tc in r.tool_calls)
    expected_dir = PROJECT_ROOT.name
    if bash_called and expected_dir in (turn["content"] or ""):
        r.status = "pass"
    else:
        if not bash_called:
            r.notes.append("bash tool nao foi invocado")
        if expected_dir not in (turn["content"] or ""):
            r.notes.append(f"resposta nao contem nome do projeto ({expected_dir!r})")
    return r


async def t_e1_grep_tool(agent: DeileAgent) -> TestResult:
    """Tool: grep. Modelo deve buscar uma string no projeto."""
    r = TestResult(test_id="E1", name="grep/search tool", status="fail", duration_s=0.0)
    session = await _make_session(agent, "e1")
    turn = await _run_turn(
        agent,
        session.session_id,
        "Use uma tool de busca para encontrar a string 'class DeileAgent' nos arquivos .py do projeto e me diga em qual arquivo ela aparece.",
    )
    r.duration_s = turn["duration_s"]
    if turn["status"] != "ok":
        r.error = turn.get("error")
        return r
    r.response_preview = (turn["content"] or "")[:200]
    r.tool_calls = turn.get("tool_calls", [])
    search_called = any(
        any(k in tc.lower() for k in ("search", "grep", "find"))
        for tc in r.tool_calls
    )
    if search_called and "agent" in (turn["content"] or "").lower():
        r.status = "pass"
    else:
        if not search_called:
            r.notes.append("nenhuma tool de busca foi invocada")
    return r


async def t_f1_help_command(agent: DeileAgent) -> TestResult:
    """Slash command: /help — sem custo LLM (resolvido localmente)."""
    r = TestResult(test_id="F1", name="/help command", status="fail", duration_s=0.0)
    session = await _make_session(agent, "f1")
    turn = await _run_turn(agent, session.session_id, "/help")
    r.duration_s = turn["duration_s"]
    if turn["status"] != "ok":
        r.error = turn.get("error")
        return r
    r.response_preview = (turn["content"] or "")[:200]
    content = (turn["content"] or "").lower()
    # /help deve listar comandos conhecidos
    expected_cmds = ["help", "history", "fork", "rewind", "model"]
    found = [c for c in expected_cmds if c in content]
    if len(found) >= 3:
        r.status = "pass"
        r.notes.append(f"listou {len(found)}/{len(expected_cmds)} comandos esperados: {found}")
    else:
        r.notes.append(f"listou apenas {found} de {expected_cmds}")
    return r


async def t_f2_history_command(agent: DeileAgent) -> TestResult:
    """Slash command: /history — sem custo LLM."""
    r = TestResult(test_id="F2", name="/history command", status="fail", duration_s=0.0)
    session = await _make_session(agent, "f2")
    turn = await _run_turn(agent, session.session_id, "/history")
    r.duration_s = turn["duration_s"]
    if turn["status"] != "ok":
        r.error = turn.get("error")
        return r
    r.response_preview = (turn["content"] or "")[:200]
    if turn["content"] is not None:
        r.status = "pass"
    else:
        r.notes.append("/history retornou content vazio")
    return r


async def t_i1_persona_loaded(agent: DeileAgent) -> TestResult:
    """Persona default carregou pelo agent inicializado."""
    r = TestResult(test_id="I1", name="default persona loaded", status="fail", duration_s=0.0)
    start = time.time()
    try:
        # O agent já foi inicializado pelo runner — a persona ativa
        # vive no próprio agent, não em um manager autônomo.
        pm = getattr(agent, "persona_manager", None)
        active = None
        if pm is not None:
            for attr in ("get_active_persona", "active_persona", "current_persona", "get_current"):
                obj = getattr(pm, attr, None)
                if callable(obj):
                    try:
                        active = obj()
                    except Exception:
                        active = None
                else:
                    active = obj
                if active is not None:
                    break
        r.duration_s = time.time() - start
        if active is not None:
            r.status = "pass"
            r.response_preview = f"persona: {getattr(active, 'name', None) or str(active)[:80]}"
        else:
            r.notes.append("agent.persona_manager não expôs persona ativa")
    except Exception as exc:
        r.duration_s = time.time() - start
        r.error = f"{type(exc).__name__}: {str(exc)[:200]}"
    return r


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    t_a1_simple_qa,
    t_a2_multi_turn_context,
    t_b1_read_file_tool,
    t_d1_bash_tool,
    t_e1_grep_tool,
    t_f1_help_command,
    t_f2_history_command,
    t_i1_persona_loaded,
]


async def main() -> int:
    config_manager = ConfigManager()
    config_manager.load_config()
    router = get_model_router()
    registered = bootstrap_providers(router=router)
    if not registered:
        print("FATAL: nenhum provider registrado")
        return 1
    print(f"providers: {registered}")
    print(f"model: {MODEL_KEY}")
    print("=" * 80)

    agent = DeileAgent(model_router=router, config_manager=config_manager)
    await agent.initialize()

    results: List[TestResult] = []
    for fn in TESTS:
        print(f"\n[ {fn.__name__} ]")
        try:
            r = await fn(agent)
        except Exception as exc:
            r = TestResult(
                test_id="?",
                name=fn.__name__,
                status="fail",
                duration_s=0.0,
                error=f"runner: {type(exc).__name__}: {str(exc)[:300]}",
            )
        results.append(r)
        print(f"  {r.status.upper():4s} [{r.duration_s:5.1f}s] {r.test_id} {r.name}")
        if r.tool_calls:
            print(f"       tools: {r.tool_calls}")
        if r.notes:
            for n in r.notes:
                print(f"       note: {n}")
        if r.error:
            print(f"       err: {r.error}")
        if r.response_preview:
            print(f"       resp: {r.response_preview}")

    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    print("\n" + "=" * 80)
    print(f"PASS: {passed}/{len(results)}   FAIL: {failed}/{len(results)}")
    print("=" * 80)

    RESULTS_PATH.write_text(
        json.dumps({"model": MODEL_KEY, "results": [asdict(r) for r in results]}, indent=2),
        encoding="utf-8",
    )
    print(f"saved: {RESULTS_PATH}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
