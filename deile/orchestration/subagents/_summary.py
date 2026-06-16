"""Renderizadores de :class:`SubAgentResult` para LLM context e para o
histórico Markdown da CLI replay.

Extraído de ``orchestrator.SubAgentResult.consolidated_summary`` e
``markdown_summary``: ambas as funções operam exclusivamente sobre os
dados do resultado (states, ok/error counts, cancelled, elapsed) — típico
feature envy. Mover para módulo dedicado mantém :class:`SubAgentResult`
como pure data e libera a lógica de render para evolução isolada (novos
formatos, internacionalização, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from .orchestrator import SubAgentResult


_CONSOLIDATED_REASON_LABELS = {
    "user_esc": "cancelado pelo usuário (ESC)",
    "budget_exceeded": "cancelado por budget estourado",
    "parent_cancel": "cancelado pelo caller (parent)",
}

_MARKDOWN_REASON_LABELS = {
    "user_esc": "ESC do usuário",
    "budget_exceeded": "budget estourado",
    "parent_cancel": "cancelado pelo caller",
}

# Emoji constants — single source of truth so render functions don't sprinkle
# hardcoded glyphs across the module (item 6 — nit). _STATUS_GLYPHS is keyed by
# SubAgentState.status (used by both renderers); the header summary picks one
# based on aggregate state (success / cancelled / warning).
# Kept as named constants to facilitate a future "no-emoji" mode (limited
# terminal, structured logs, internationalization).
_HEADER_OK_EMOJI = "✅"
_HEADER_CANCELLED_EMOJI = "⏹"
_HEADER_WARN_EMOJI = "⚠️"
_STATUS_ERROR_EMOJI = "❌"

_STATUS_GLYPHS = {
    "ok": _HEADER_OK_EMOJI,
    "error": _STATUS_ERROR_EMOJI,
    "cancelled": _HEADER_CANCELLED_EMOJI,
}


def render_consolidated(result: "SubAgentResult") -> str:
    """Resumo curto agregado para o LLM (≤2KB).

    Cada frente vira ~2 linhas: status + descrição + arquivos. NÃO inclui o
    ``result_text`` completo — o LLM já viu o painel ao vivo e deve apenas
    consolidar; despejar resultados longos satura o contexto e induz o LLM
    a re-narrar (anti-padrão da issue #257).
    """
    lines: List[str] = []
    header = (
        f"sub-DEILEs paralelos · {result.ok_count} ok · "
        f"{result.error_count} erro · {result.elapsed_s:.1f}s total"
    )
    if result.cancelled:
        reason_label = _CONSOLIDATED_REASON_LABELS.get(
            result.cancellation_reason or "", "cancelado"
        )
        header += f" · {reason_label}"
    lines.append(header)
    for st in result.states:
        glyph = _STATUS_GLYPHS.get(st.status, "•")
        files = ", ".join(st.files_touched[:5])
        if len(st.files_touched) > 5:
            files += f" (+{len(st.files_touched) - 5})"
        head = f"  #{st.task.index} {glyph} {st.task.description}"
        if files:
            head += f" — {files}"
        if st.elapsed_s:
            head += f" · {st.elapsed_s:.1f}s"
        lines.append(head)
        if st.error:
            lines.append(f"      erro: {st.error[:120]}")
    full = "\n".join(lines)
    return full[:2000]


def render_markdown(result: "SubAgentResult") -> str:
    """Versão markdown do resumo para gravar no histórico e replay.

    Diferente de :func:`render_consolidated`, este é renderizado num bloco
    Markdown (a CLI replay usa ``ui.display_response`` que parseia
    Markdown), então deve usar formatação rica e legível.
    """
    lines: List[str] = []
    status_emoji = (
        _HEADER_OK_EMOJI
        if result.ok_global
        else (_HEADER_CANCELLED_EMOJI if result.cancelled else _HEADER_WARN_EMOJI)
    )
    header = (
        f"{status_emoji} **Sub-DEILEs paralelos** · "
        f"{result.ok_count} ok · {result.error_count} erro · "
        f"{result.elapsed_s:.1f}s total"
    )
    if result.cancelled and result.cancellation_reason:
        reason_label = _MARKDOWN_REASON_LABELS.get(
            result.cancellation_reason, result.cancellation_reason
        )
        header += f" · _{reason_label}_"
    lines.append(header)
    lines.append("")
    for st in result.states:
        glyph = _STATUS_GLYPHS.get(st.status, "•")
        line = f"- {glyph} **#{st.task.index} {st.task.description}**"
        if st.elapsed_s:
            line += f" _({st.elapsed_s:.1f}s)_"
        lines.append(line)
        if st.files_touched:
            files = ", ".join(f"`{f}`" for f in st.files_touched[:5])
            if len(st.files_touched) > 5:
                files += f" _(+{len(st.files_touched) - 5})_"
            lines.append(f"  - Arquivos: {files}")
        if st.error:
            lines.append(f"  - Erro: `{st.error[:200]}`")
    return "\n".join(lines)
