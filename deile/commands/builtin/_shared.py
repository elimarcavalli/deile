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


def get_memory_manager(context: CommandContext) -> MemoryManager | None:
    """Retorna ``context.agent.memory_manager`` ou ``None`` quando ausente —
    padrão antes duplicado em compact, memory e status commands."""
    agent = getattr(context, "agent", None)
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


def format_relative_time(
    timestamp: datetime,
    *,
    with_seconds: bool = True,
    suffix: str = "",
    now: datetime | None = None,
) -> str:
    """Formata ``timestamp`` como tempo relativo curto (``"5s"``, ``"30m"``, ``"13:42"``).

    Substitui dois padrões duplicados em ``logs_command``:

    * Visão geral (linhas 167-173) — ``with_seconds=True``, ``suffix=""``
    * Eventos de segurança (linhas 339-344) — ``with_seconds=False``,
      ``suffix=" atrás"``

    Para timestamps com mais de 1 hora, retorna ``"%H:%M"`` (formato curto
    do dia atual). ``now`` opcional facilita testes determinísticos.
    """
    current = now if now is not None else datetime.now()
    delta = current - timestamp
    secs = delta.total_seconds()
    if with_seconds and secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs / 60)}m{suffix}"
    return timestamp.strftime("%H:%M")


def truncate(text: str, width: int, ellipsis: str = "...") -> str:
    """Trunca ``text`` a ``width`` caracteres, anexando ``ellipsis`` se cortou.

    Substitui o padrão duplicado em ~16 sites de comandos builtin:
    ``text[:N] + ("..." if len(text) > N else "")``. Versões legadas
    sem o ``if len(...)`` (ex. ``actor[:10] + "..."``) são corrigidas
    como efeito colateral — antes elas anexavam ``...`` mesmo quando o
    texto cabia inteiro nos N caracteres.

    Output pode exceder ``width`` em ``len(ellipsis)`` chars quando há
    truncamento, preservando o comportamento visual atual.
    """
    if not text or len(text) <= width:
        return text
    return text[:width] + ellipsis


# ---------------------------------------------------------------------------
# Mapas de emojis canônicos compartilhados pelos comandos builtin.
#
# Antes desta centralização cada comando redefinia o seu próprio dict inline,
# divergindo em pequenas variações (ex.: "running" como "🔄" em plan/run vs
# "⚡" no spec do projeto). Estas constantes resolvem essa inconsistência:
# qualquer comando novo que precise mostrar status/risco/ação importa daqui.
# ---------------------------------------------------------------------------

RISK_EMOJI: dict[str, str] = {
    "low": "🟢",
    "medium": "🟡",
    "high": "🔴",
    "critical": "🚨",
}

ACTION_EMOJI: dict[str, str] = {
    "modified": "📝",
    "created": "✨",
    "deleted": "🗑️",
}

PLAN_STATUS_EMOJI: dict[str, str] = {
    "draft": "📝",
    "ready": "🚀",
    "running": "⚡",
    "paused": "⏸️",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "🚫",
}

STEP_STATUS_EMOJI: dict[str, str] = {
    "pending": "⏳",
    "running": "⚡",
    "completed": "✅",
    "failed": "❌",
    "skipped": "⏭️",
    "requires_approval": "⏸️",
}


def risk_indicator(level: str) -> str:
    """Retorna emoji de destaque apenas para risco ``high``/``critical``.

    Usado quando queremos sinalizar visualmente apenas riscos relevantes —
    para a renderização completa (incluindo ``low``/``medium``) use
    diretamente ``RISK_EMOJI``.
    """
    if level in ("high", "critical"):
        return RISK_EMOJI[level]
    return ""
