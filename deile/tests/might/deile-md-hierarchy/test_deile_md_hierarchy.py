"""Empirical test of hierarchical DEILE.md loading (Issue #62 / Feature #64).

Prova com LLM real que DEILE respeita SIMULTANEAMENTE os três níveis de
DEILE.md (Core → Usuário → CWD) em uma única resposta.

Os três arquivos foram instrumentados com regras-marcador exclusivas:

    core/DEILE.md      → "SEMPRE diga uma frase impactante sobre IA autônoma..."
    ~/.deile/DEILE.md  → "SEMPRE diga 'OLA MEU CUPINXA'..."
    ./DEILE.md         → "SEMPRE diga uma RECETIA DE BOLO..."

Em uma conversa nova, mandamos um único "oi" e verificamos que a resposta
contém marcadores das três camadas. Se uma camada falhar, sabemos qual.

Saída: TOOLS chamadas + RESPONSE TEXT + veredito por camada (PASS/FAIL).
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

# Ensure project root is importable
# Path: deile/tests/might/deile-md-hierarchy/test_deile_md_hierarchy.py
# parents[0]=deile-md-hierarchy, [1]=might, [2]=tests, [3]=deile, [4]=project root
PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from deile.config.manager import ConfigManager  # noqa: E402
from deile.core.agent import DeileAgent  # noqa: E402
from deile.core.models.bootstrap import bootstrap_providers  # noqa: E402
from deile.core.models.router import get_model_router  # noqa: E402


# ── Detectores leniente por camada ──────────────────────────────────────────

# CORE: "frase impactante sobre IA autônoma". Aceitamos qualquer menção
# a "IA" / "inteligência artificial" / "AI" — o modelo pode generalizar,
# o importante é que a resposta contenha esse marcador.
CORE_PATTERNS = [
    r"\bia\b",
    r"\bintelig[eê]ncia\s+artificial\b",
    r"\bagentes?\s+aut[oô]nomos?\b",
]

# USER: deve aparecer "ola meu cupinxa" (ou "olá meu cupinxa")
USER_PATTERNS = [
    r"ol[aá]\s+meu\s+cupinxa",
]

# CWD: "uma receita de bolo". O marcador é a co-ocorrência de "bolo"
# + qualquer indicador de receita (ingredientes / receita / modo de preparo
# / xícara / colher).
CWD_PATTERNS = [
    r"\breceita\s+de\s+bolo\b",
    r"\brecet?ia\s+de\s+bolo\b",
]
CWD_RECIPE_HINTS = [
    r"\bingredientes?\b",
    r"\bmodo\s+de\s+preparo\b",
    r"\bx[ií]caras?\b",
    r"\bcolheres?\b",
    r"\bfermento\b",
]


def _check(patterns: list[str], text: str) -> tuple[bool, str | None]:
    lowered = text.lower()
    for pat in patterns:
        m = re.search(pat, lowered, re.DOTALL)
        if m:
            return True, m.group(0)
    return False, None


def _check_cwd(text: str) -> tuple[bool, str | None]:
    lowered = text.lower()
    # Caminho 1: aparece "receita de bolo" textualmente.
    direct, match = _check(CWD_PATTERNS, text)
    if direct:
        return True, match
    # Caminho 2: aparece "bolo" + ≥1 hint de receita.
    if re.search(r"\bbolo\b", lowered):
        for hint in CWD_RECIPE_HINTS:
            m = re.search(hint, lowered)
            if m:
                return True, f"bolo + {m.group(0)}"
    return False, None


def _format_tool_calls(tool_results) -> str:
    if not tool_results:
        return "  (none)"
    lines = []
    for i, tr in enumerate(tool_results, 1):
        name = tr.metadata.get("function_name", "?")
        status = tr.status.value if hasattr(tr.status, "value") else str(tr.status)
        lines.append(f"  [{i}] {name} status={status}")
    return "\n".join(lines)


async def main():
    print("=" * 100)
    print("EMPIRICAL TEST — DEILE.md hierarchy (Core → User → CWD)")
    print("=" * 100)

    # Bootstrap igual ao deile.py CLI
    print("\nBootstrapping providers...")
    config_manager = ConfigManager()
    config_manager.load_config()
    router = get_model_router()
    registered = bootstrap_providers(router=router)
    print(f"Providers registered: {registered}")

    agent = DeileAgent(config_manager=config_manager, model_router=router)
    if hasattr(agent, "initialize"):
        await agent.initialize()

    session_id = "deile-md-hierarchy-test"
    session = agent._get_or_create_session(session_id)
    # Força um modelo previsível e barato.
    session.context_data["forced_model"] = "deepseek:deepseek-v4-flash"
    print(f"Forced model: {session.context_data['forced_model']}")

    # ── Sonda única: greeting trigger ─────────────────────────────────────
    user_input = "oi"
    print(f"\nUSER: {user_input}\n")

    t0 = time.time()
    response = await agent.process_input(user_input, session_id=session_id)
    dt = time.time() - t0

    print(f"--- TOOL CALLS ({len(response.tool_results)}) ---")
    print(_format_tool_calls(response.tool_results))

    print("\n--- RESPONSE TEXT ---")
    print(response.content)

    print("\n--- METADATA ---")
    print(f"  duration: {dt:.2f}s")
    print(f"  model_used: {response.metadata.get('model_used', '?')}")
    print(f"  status: {response.status.value if hasattr(response.status, 'value') else response.status}")

    # ── Veredito por camada ───────────────────────────────────────────────
    text = response.content or ""
    core_ok, core_match = _check(CORE_PATTERNS, text)
    user_ok, user_match = _check(USER_PATTERNS, text)
    cwd_ok, cwd_match = _check_cwd(text)

    print("\n--- LAYER VERDICT ---")
    print(f"  🔴 CORE   (frase sobre IA autônoma): {'PASS' if core_ok else 'FAIL'}"
          + (f"  → match: {core_match!r}" if core_match else ""))
    print(f"  🟡 USER   (olá meu cupinxa):          {'PASS' if user_ok else 'FAIL'}"
          + (f"  → match: {user_match!r}" if user_match else ""))
    print(f"  🟢 CWD    (receita de bolo):          {'PASS' if cwd_ok else 'FAIL'}"
          + (f"  → match: {cwd_match!r}" if cwd_match else ""))

    overall = core_ok and user_ok and cwd_ok
    print(f"\n  ▶ OVERALL: {'PASS — DEILE respeitou as 3 camadas' if overall else 'FAIL — alguma camada não foi respeitada'}")

    # Exit code para CI / scripts
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
