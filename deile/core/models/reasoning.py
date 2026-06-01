"""Vocabulário de *reasoning effort* e mapeamento por provider (fonte única).

Espelha o papel de :mod:`deile.orchestration.pipeline.model_resolver` para o
eixo **modelo**, mas aqui o eixo é o **esforço de raciocínio** ("reasoning
effort"). É a fonte de verdade única para três perguntas:

1. **Quais níveis o operador pode escolher** para um dado par
   (worker, provider) — :func:`valid_efforts_for`. O painel TUI roda de
   ``infra/k8s/`` (eventualmente sem o pacote ``deile`` no path) e mantém uma
   cópia estática espelhada destes conjuntos; este módulo é a autoridade.
2. **Se um valor é plausível** — :func:`is_valid_effort` (usado pelo validador
   de ``settings.py`` para rejeitar typos sem conhecer worker/provider).
3. **Como traduzir o nível escolhido para os parâmetros nativos de cada
   provider** — :func:`request_overrides` (anthropic / openai / deepseek, via
   ``extra_body``) e :func:`gemini_thinking_kwargs` (gemini, via
   ``ThinkingConfig``).

Vocabulário canônico (claude-worker E deile-worker com modelos anthropic):
``low | medium | high | xhigh | max | ultracode | auto`` — exatamente os
*efforts* que o ``claude`` CLI (claude-worker) aceita via ``--effort``. Para os
demais providers do deile-worker, os níveis foram levantados nas docs oficiais
(OpenAI ``reasoning_effort``; Gemini ``thinking_config``; DeepSeek
``reasoning_effort`` + ``thinking``) — ver tabela em cada conjunto abaixo.

Princípio de robustez: nenhum mapeamento levanta. Um nível desconhecido para
um provider colapsa para "sem override" (``{}`` / ``None``) — o turno roda no
default do provider. Isso torna o feature seguro mesmo quando o ``model_id`` é
mais novo que o SDK instalado.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# ── Conjuntos de níveis por contexto ──────────────────────────────────────

#: Vocabulário do Claude Code (``claude --effort``). Usado tanto pelo
#: claude-worker quanto pelo deile-worker quando o modelo é ``anthropic:*``
#: (decisão do Humano — paridade de vocabulário entre os dois workers).
CLAUDE_CODE_EFFORTS: Tuple[str, ...] = (
    "low", "medium", "high", "xhigh", "max", "ultracode", "auto",
)

#: OpenAI ``reasoning_effort`` (GPT-5.x). ``none`` = sem reasoning;
#: ``auto`` = omitir (default do modelo). ``minimal`` é alias legado pré-5.4.
OPENAI_EFFORTS: Tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "auto",
)

#: Gemini ``thinking_config``. 3.x usa ``thinking_level`` (minimal/low/medium/
#: high); 2.5 usa ``thinking_budget`` (inteiro). Aqui expomos um vocabulário
#: discreto uniforme — :func:`gemini_thinking_kwargs` resolve família/budget.
GEMINI_EFFORTS: Tuple[str, ...] = (
    "off", "minimal", "low", "medium", "high", "auto",
)

#: DeepSeek V4: ``reasoning_effort`` aceita ``high``/``max``; ``off`` desliga o
#: thinking (``thinking.type=disabled``); ``auto`` = default (thinking on,
#: effort high).
DEEPSEEK_EFFORTS: Tuple[str, ...] = (
    "off", "high", "max", "auto",
)

#: União de todos os tokens conhecidos — usado pelo validador de settings, que
#: não conhece worker/provider no momento do load e só precisa barrar typos.
KNOWN_EFFORTS: frozenset = frozenset(
    CLAUDE_CODE_EFFORTS + OPENAI_EFFORTS + GEMINI_EFFORTS + DEEPSEEK_EFFORTS
)


def normalize_effort(value: Any) -> Optional[str]:
    """Normaliza para token minúsculo sem espaços, ou ``None`` se vazio."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    return stripped or None


def is_valid_effort(value: Any) -> bool:
    """``True`` se *value* é um nível conhecido (qualquer provider)."""
    norm = normalize_effort(value)
    return norm is not None and norm in KNOWN_EFFORTS


def valid_efforts_for(*, worker: Optional[str], provider_id: Optional[str]) -> Tuple[str, ...]:
    """Retorna os níveis válidos para o picker, dado (worker, provider).

    - ``claude-worker`` → vocabulário Claude Code (sempre anthropic).
    - ``deile-worker`` + ``anthropic`` → vocabulário Claude Code (paridade).
    - ``deile-worker`` + ``openai``/``gemini``/``deepseek`` → conjunto do provider.
    - Desconhecido → vocabulário Claude Code (default seguro).
    """
    w = (worker or "").strip().lower()
    if w == "claude-worker":
        return CLAUDE_CODE_EFFORTS
    p = (provider_id or "").strip().lower()
    if p == "openai":
        return OPENAI_EFFORTS
    if p == "gemini":
        return GEMINI_EFFORTS
    if p == "deepseek":
        return DEEPSEEK_EFFORTS
    # anthropic e fallback
    return CLAUDE_CODE_EFFORTS


# ── Tradução para parâmetros nativos ──────────────────────────────────────


def _anthropic_effort(model_id: str, effort: str) -> Optional[str]:
    """Mapeia o nível para o valor de ``output_config.effort`` (ou ``None``).

    - ``auto`` → ``None`` (omitir = default ``high``).
    - ``ultracode`` → ``max`` (não é valor de API; é o teto).
    - ``xhigh`` só existe em modelos ``opus`` → cai pra ``max`` em sonnet.
    - ``haiku`` não suporta o parâmetro ``effort`` → ``None`` (omitir).
    - níveis de outros providers (``none``/``off``/``minimal``) → ``low``.
    """
    mid = (model_id or "").lower()
    if effort in (None, "auto"):
        return None
    if "haiku" in mid:
        return None  # haiku não aceita output_config.effort
    e = effort
    if e == "ultracode":
        e = "max"
    if e in ("none", "off", "minimal"):
        e = "low"
    if e == "xhigh" and "opus" not in mid:
        e = "max"  # só opus tem xhigh
    if e not in ("low", "medium", "high", "xhigh", "max"):
        return None
    return e


def _openai_effort(model_id: str, effort: str) -> Optional[str]:
    """Mapeia para ``reasoning_effort`` da OpenAI, ou ``None`` (omitir)."""
    mid = (model_id or "").lower()
    if effort in (None, "auto"):
        return None
    if "nano" in mid:
        return None  # nano não é modelo de reasoning
    e = effort
    if e in ("ultracode", "max"):
        e = "xhigh"
    if e == "off":
        e = "none"
    if e not in ("none", "minimal", "low", "medium", "high", "xhigh"):
        return None
    return e


def _deepseek_overrides(effort: str) -> Dict[str, Any]:
    """Mapeia para os campos DeepSeek (``reasoning_effort`` + ``thinking``)."""
    if effort in (None, "auto"):
        return {}
    if effort == "off":
        return {"thinking": {"type": "disabled"}}
    if effort in ("max", "ultracode", "xhigh"):
        return {"reasoning_effort": "max"}
    # high/low/medium/none/minimal → high (DeepSeek V4 grada só high/max)
    return {"reasoning_effort": "high"}


def request_overrides(provider_id: str, model_id: str, effort: Any) -> Dict[str, Any]:
    """Parâmetros nativos a fundir em ``extra_body`` (anthropic/openai/deepseek).

    Retorna ``{}`` quando não há override (auto, nível não suportado, gemini —
    que é tratado por :func:`gemini_thinking_kwargs`). Nunca levanta.
    """
    norm = normalize_effort(effort)
    if norm is None:
        return {}
    pid = (provider_id or "").strip().lower()
    if pid == "anthropic":
        e = _anthropic_effort(model_id, norm)
        return {"output_config": {"effort": e}} if e else {}
    if pid == "deepseek":
        return _deepseek_overrides(norm)
    if pid == "openai":
        e = _openai_effort(model_id, norm)
        return {"reasoning_effort": e} if e else {}
    return {}


def resolve_session_reasoning(session: Any) -> Optional[str]:
    """Esforço efetivo para o turno do agente (deile-worker / CLI).

    Precedência: ``session.context_data["reasoning_effort"]`` (injetado pelo
    worker a partir do ``DispatchPayload``, ou setado pelo comando ``/reasoning``)
    > ``settings.reasoning_effort`` (global, configurável no DEILE CLI) > ``None``.
    Best-effort: nunca levanta.
    """
    try:
        cd = getattr(session, "context_data", None) or {}
        v = normalize_effort(cd.get("reasoning_effort"))
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    try:
        from deile.config.settings import get_settings
        return normalize_effort(get_settings().reasoning_effort)
    except Exception:  # noqa: BLE001
        return None


def gemini_thinking_kwargs(model_id: str, effort: Any) -> Optional[Dict[str, Any]]:
    """Kwargs para ``types.ThinkingConfig`` do Gemini, ou ``None`` (omitir).

    Família 2.5 usa ``thinking_budget`` (inteiro; ``-1`` dinâmico, ``0`` off);
    família 3.x usa ``thinking_level`` (minimal/low/medium/high). NÃO misturar
    os dois na mesma request (a API rejeita) — por isso este helper devolve
    apenas um dos dois.
    """
    norm = normalize_effort(effort)
    if norm is None or norm == "auto":
        return None  # dynamic/default
    mid = (model_id or "").lower()
    is_25 = "2.5" in mid or "2-5" in mid
    if is_25:
        budget = {
            "off": 0, "minimal": 0, "low": 1024,
            "medium": 8192, "high": 24576, "xhigh": 24576,
            "max": 24576, "ultracode": 24576,
        }.get(norm)
        if budget is None:
            return None
        # 2.5-pro não desliga thinking (mínimo 128); demais aceitam 0.
        if budget == 0 and "pro" in mid:
            budget = 128
        return {"thinking_budget": budget}
    # 3.x — thinking_level
    level = {
        "off": "minimal", "minimal": "minimal", "low": "low",
        "medium": "medium", "high": "high", "xhigh": "high",
        "max": "high", "ultracode": "high",
    }.get(norm)
    if level is None:
        return None
    return {"thinking_level": level}
