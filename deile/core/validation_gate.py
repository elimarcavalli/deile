"""Validation gate (anti-hallucination + post-write enforcement).

Extracted from ``DeileAgent`` (god-object refactor, SRP). The gate is a
deterministic enforcement layer that complements persona-side rules:

* ``detect_unvalidated_writes`` flags ``write_file`` results that the tool
  itself marked as needing post-write validation when no follow-up
  execution tool was invoked in the same turn.
* ``contains_promise_pattern`` matches Portuguese and English phrases the
  model commonly uses to promise an action (test / run / install) without
  taking it.
* ``apply_validation_gate`` re-invokes the supplied iterative function-call
  driver exactly once with a synthetic gate prompt when one of the above
  signals fires.

The module deliberately does NOT import ``DeileAgent`` or ``AgentSession``
(circular-import hazard). Callers pass the session and the retry callable
explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from ..parsers.base import ParseResult
from ..tools.base import ToolResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROMISE_PATTERNS: List[str] = [
    # Portuguese — actions the model commonly promises but skips
    r"\bvou\s+(?:testar|rodar|executar|validar|verificar|instalar|conferir|checar)\b",
    r"\b(?:testar|rodar|executar|validar|verificar|instalar)\s+(?:agora|isso|isto|esse|essa)\b",
    r"\bdeixa\s+eu\s+(?:testar|rodar|executar|validar|verificar)\b",
    r"\bvamos\s+(?:testar|rodar|executar|validar|verificar)\b",
    # English
    r"\b(?:I'?ll|I\s+will|let\s+me)\s+(?:test|run|verify|check|install|validate|execute)\b",
    r"\b(?:testing|running|executing|validating|verifying|installing)\s+(?:it|that|now|this)\b",
]

VALIDATION_TOOL_NAMES = {
    "bash_execute", "python_execute", "run_tests",
}

# Eager-compile the promise patterns once at module-load (item 12). Avoids the
# lazy-init race that the previous ``Optional[...] = None`` form had, and
# trades nothing material — module load is single-threaded by the import lock.
_COMPILED_PROMISE_RE: List[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in PROMISE_PATTERNS
]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def contains_promise_pattern(text: str) -> bool:
    """Return True when ``text`` contains one of the promise patterns.

    Patterns and matching are case-insensitive; an empty/None input
    returns False.
    """
    if not text:
        return False
    return any(rx.search(text) for rx in _COMPILED_PROMISE_RE)


def detect_unvalidated_writes(tool_results: List[ToolResult]) -> List[ToolResult]:
    """Return write_file results for executable files that lack a following validation tool call."""
    # All write_file results that the tool flagged as needing validation
    flagged_writes = [
        tr for tr in tool_results
        if tr.metadata.get("post_write_validation_required") is True
    ]
    if not flagged_writes:
        return []
    # Any subsequent execution tool counts as "the model tried to validate"
    validated = any(
        tr.metadata.get("function_name") in VALIDATION_TOOL_NAMES
        for tr in tool_results
    )
    if validated:
        return []
    return flagged_writes


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

# Type alias for the retry callable provided by DeileAgent
# (kwargs-only mirror of ``_process_iterative_function_calling``).
RetryCallable = Callable[..., Awaitable[Tuple[str, List[ToolResult]]]]


async def apply_validation_gate(
    *,
    user_input: str,
    parse_result: Optional[ParseResult],
    session: Any,
    content: str,
    tool_results: List[ToolResult],
    retry: RetryCallable,
) -> Tuple[str, List[ToolResult]]:
    """Re-invoke the model once if it wrote executable code without testing
    or promised an action without taking it. Persona-side rules already ask
    for this; the gate is the deterministic enforcement layer.

    Recursion is impossible: the gate marks the session, runs at most one
    retry, and clears the marker. If the retry still violates, the result
    is returned to the user unaltered — surfacing the failure rather than
    masking it.

    The ``retry`` callable mirrors ``DeileAgent._process_iterative_function_calling``
    (kwargs ``user_input``, ``parse_result``, ``session``) and is injected
    by the caller to avoid a circular import on ``DeileAgent``.
    """
    # Single-shot per turn — and re-entry from a workflow path also skips
    if session.context_data.get("_validation_gate_active"):
        return content, tool_results

    unvalidated = detect_unvalidated_writes(tool_results)
    # Promise gate only fires on SHORT replies — long explanations may use
    # "vamos testar a hipótese" / "let me check" rhetorically without
    # actually intending to invoke a tool. The gate's value is catching
    # the model saying "vou rodar agora!" and stopping cold.
    promise_without_action = (
        not tool_results
        and len(content) <= 500
        and contains_promise_pattern(content)
    )

    if not unvalidated and not promise_without_action:
        return content, tool_results

    if unvalidated:
        paths = [tr.metadata.get("file_path", "?") for tr in unvalidated]
        cmds = [
            tr.metadata.get("post_write_validation_command")
            for tr in unvalidated
            if tr.metadata.get("post_write_validation_command")
        ]
        cmd_block = "\n".join(f"  - {c}" for c in cmds) if cmds else "  (none suggested)"
        gate_prompt = (
            "[INTERNAL_VALIDATION_GATE] You wrote the following executable file(s) "
            "but did not validate them in the same turn:\n"
            f"  {', '.join(paths)}\n\n"
            "Per the Definition of Done, you MUST validate now using the tools. "
            "Suggested validation commands (run via bash_execute):\n"
            f"{cmd_block}\n\n"
            "If validation fails (exit code != 0 or stderr non-empty), diagnose "
            "and fix the file with write_file, then re-validate. Use pip_install "
            "for any ModuleNotFoundError. Only after exit 0 do you report the "
            "task complete to the user — and the report MUST include the actual "
            "validation output, not a summary."
        )
    else:
        gate_prompt = (
            "[INTERNAL_VALIDATION_GATE] Your previous response promised an action "
            "(test / run / install / validate) but no tool was invoked in that "
            "turn. Per the anti-hallucination rule in your persona, that is a "
            "policy violation. Either invoke the tool now to fulfill the promise, "
            "or revise the answer to not promise. Do not produce a final answer "
            "until the action is actually taken."
        )

    # Checkpoint antes de adicionar entradas [INTERNAL_VALIDATION_GATE] ao
    # histórico, para poder fazer rollback se o retry falhar com Exception.
    _history_checkpoint = len(session.conversation_history)

    # Persist the pre-gate assistant turn so the model sees the gap
    session.add_to_history("assistant", content, {"validation_gate_pre": True})
    session.add_to_history("user", gate_prompt, {"validation_gate": True})
    session.context_data["_validation_gate_active"] = True
    try:
        try:
            new_content, new_tool_results = await retry(
                user_input=gate_prompt,
                parse_result=parse_result,
                session=session,
            )
        except asyncio.CancelledError:
            # Pilar 03 §6: never swallow CancelledError — let the caller
            # observe the cancellation and clean up.
            raise
        except Exception as exc:
            # Item 13: if the retry fails, we must not let the failure
            # discard the pre-gate content/tool_results — that work was
            # already executed and shown to the user. Rollback das entradas
            # fantasma do gate para não vazar histórico residual.
            logger.warning(
                "validation_gate retry failed (%s); returning pre-gate result",
                exc,
            )
            del session.conversation_history[_history_checkpoint:]
            return content, tool_results
    finally:
        session.context_data.pop("_validation_gate_active", None)

    return new_content, list(tool_results) + list(new_tool_results)
