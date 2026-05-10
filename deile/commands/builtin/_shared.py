"""Helpers compartilhados pelos comandos builtin.

Ponto único de mudança para padrões que se repetiam em 6+ comandos
(parsing de ``context.args``, painéis Rich coloridos, auditoria,
recuperação de subsistemas, mapas PT-BR de descrições).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from rich.panel import Panel
from rich.text import Text

from ..base import CommandContext

if TYPE_CHECKING:
    from ...memory.memory_manager import MemoryManager
    from ...security.audit_logger import AuditEventType, SeverityLevel

logger = logging.getLogger(__name__)


def export_timestamp() -> str:
    """Timestamp UTC ``YYYYMMDD_HHMMSS`` para nomes de arquivos exportados.

    UTC garante consistência entre fusos horários, alinhado com export_command.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _colored_panel(message: str, title: str | None, color: str) -> Panel:
    """Implementação interna — callers externos usam error/warning/success_panel."""
    return Panel(Text(message, style=color), title=title, border_style=color)


def error_panel(message: str, title: str | None = "Erro") -> Panel:
    """Painel vermelho — usado em paths de falha."""
    return _colored_panel(message, title, "red")


def warning_panel(message: str, title: str | None = "Aviso") -> Panel:
    """Painel amarelo — usado em paths de aviso/indisponível."""
    return _colored_panel(message, title, "yellow")


def success_panel(message: str, title: str | None = "Sucesso") -> Panel:
    """Painel verde — usado em paths de sucesso."""
    return _colored_panel(message, title, "green")


# Mapa canônico consumido por /version e /welcome — manter em sync
# com ``deile.__version__.FEATURES``.
PROJECT_LINKS: dict[str, str] = {
    "Repositório": "https://github.com/elimarcavalli/deile",
    "Documentação": "docs/system_design/00-VISAO-GERAL.md",
    "Licença": "MIT — https://opensource.org/licenses/MIT",
    "Issues": "https://github.com/elimarcavalli/deile/issues",
}


FLAG_DESCRICOES_PTBR: dict[str, str] = {
    "orchestration": "Orquestração multi-step e gestão de planos",
    "security": "Permissões, audit log e sandbox",
    "ui_polish": "Interface polida e atalhos de teclado",
    "testing": "Suíte de testes automatizados",
    "ci_cd": "Integração e entrega contínua",
    "documentation": "Documentação estruturada por pilares",
    "events": "Arquitetura orientada a eventos",
    "evolution": "Motor de auto-aprendizado",
    "memory": "Memória em quatro camadas (working/episodic/semantic/procedural)",
    "personas": "Troca dinâmica de personas",
    "plugins": "Arquitetura extensível de plugins",
    "config_profiles": "Perfis de configuração por ambiente",
}


def emit_audit_event(
    *,
    event_type: AuditEventType,
    severity: SeverityLevel,
    resource: str,
    action: str,
    result: str = "initiated",
    details: dict[str, Any] | None = None,
    actor: str = "user",
) -> None:
    """Auditoria best-effort — falhas no logger NUNCA propagam ao comando.

    Pilar 03 §6 proíbe ``except Exception: pass``: registramos a falha em
    nível DEBUG antes de suprimir, preservando o contrato fail-silent que
    `permissions_command` e `status_command` exigiam originalmente.
    """
    try:
        from ...security.audit_logger import get_audit_logger
        get_audit_logger().log_event(
            event_type=event_type,
            severity=severity,
            actor=actor,
            resource=resource,
            action=action,
            result=result,
            details=details or {},
        )
    except Exception as exc:  # audit é best-effort — nunca aborta o comando
        logger.debug("emit_audit_event falhou: %s", exc)


def get_agent(context: CommandContext | None) -> Any | None:
    """Retorna ``context.agent`` ou ``None`` quando ausente — pattern duplicado
    em context, cost, export, memory, model, skills commands. Aceita ``None``
    para casos em que ``context`` pode não ter sido construído ainda."""
    if context is None:
        return None
    return getattr(context, "agent", None)


def get_session(context: CommandContext | None) -> Any | None:
    """Retorna ``context.session`` ou ``None`` — companion de :func:`get_agent`."""
    if context is None:
        return None
    return getattr(context, "session", None)


def get_memory_manager(context: CommandContext) -> MemoryManager | None:
    """Retorna ``context.agent.memory_manager`` ou ``None`` quando ausente —
    padrão antes duplicado em compact, memory e status commands."""
    agent = get_agent(context)
    return getattr(agent, "memory_manager", None) if agent else None


def split_args(context: CommandContext) -> list[str]:
    """Tokeniza ``context.args``; trata ``None``/vazio/só-espaços como ``[]``.

    Substitui a duplicação ``args = context.args if hasattr(...) else ""``
    seguida de ``parts = args.strip().split() if args.strip() else []``
    que aparecia em 16 comandos.
    """
    raw = getattr(context, "args", "") or ""
    stripped = raw.strip()
    return stripped.split() if stripped else []


FILE_ACTION_EMOJI: dict[str, str] = {
    "modified": "📝",
    "created": "✨",
    "deleted": "🗑️",
}
"""Emojis canônicos para ações de arquivo — apply/diff/patch commands."""


RISK_EMOJI: dict[str, str] = {
    "low": "🟢",
    "medium": "🟡",
    "high": "🔴",
    "critical": "🚨",
}
"""Emojis canônicos por nível de risco — approve/plan/run commands."""


PLAN_STATUS_EMOJI: dict[str, str] = {
    "draft": "📝",
    "ready": "⚡",
    "running": "🔄",
    "paused": "⏸️",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "🚫",
}
"""Emojis canônicos para ``PlanStatus`` — plan/run/stop commands."""


STEP_STATUS_EMOJI: dict[str, str] = {
    "pending": "⏳",
    "running": "🔄",
    "completed": "✅",
    "failed": "❌",
    "skipped": "⏭️",
    "requires_approval": "⚠️",
}
"""Emojis canônicos para ``StepStatus`` — plan_command (steps recentes/atuais)."""


def file_action_emoji(action: str) -> str:
    """Resolve emoji para a ação de arquivo; fallback ``❓`` para desconhecidos."""
    return FILE_ACTION_EMOJI.get(action, "❓")


def risk_emoji(risk_level: str) -> str:
    """Resolve emoji para o nível de risco; fallback ``❓`` para desconhecidos."""
    return RISK_EMOJI.get(risk_level, "❓")


def plan_status_emoji(status: str) -> str:
    """Resolve emoji para ``PlanStatus``; fallback ``❓`` para desconhecidos."""
    return PLAN_STATUS_EMOJI.get(status, "❓")


def step_status_emoji(status: str) -> str:
    """Resolve emoji para ``StepStatus``; fallback ``❓`` para desconhecidos."""
    return STEP_STATUS_EMOJI.get(status, "❓")


def truncate(text: str | None, max_chars: int, suffix: str = "...") -> str:
    """Recorta ``text`` para ``max_chars`` caracteres + ``suffix`` quando excede.

    Padrão equivalente a ``text[:max_chars] + suffix if len(text) > max_chars
    else text`` que estava duplicado em 14+ sites entre logs/approve/diff/
    permissions/plan/run/stop/tools commands. Output fica em
    ``max_chars + len(suffix)`` chars quando truncado, ou no comprimento
    original quando não. ``None`` é tratado como string vazia.
    """
    if not text:
        return ""
    return text[:max_chars] + suffix if len(text) > max_chars else text
