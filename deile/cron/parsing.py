"""Parser tolerante de agendamento — formatos múltiplos, BRT-aware.

Aceita, em ordem:
    1. Cron 5-field (``*/5 * * * *``) — interpretado como UTC pelo runner.
    2. ISO 8601 com timezone (``2026-05-15T12:30Z`` ou ``+00:00``).
    3. ISO 8601 naive (``2026-05-15T09:30``) — assumido BRT (UTC-3).
    4. BR humano (``15/05[/YYYY] HH[:MM|hMM]``) — sempre BRT.
    5. Linguagem natural (``hoje 23:00`` / ``amanhã 14h``) — sempre BRT.

Retorna ``(cron_expr, run_at_utc)`` — exatamente um é não-None.

Datas naive são convertidas explicitamente para UTC antes de retornar
porque o ``CronStore`` persiste tudo em UTC e o ``CronRunner`` compara
com ``datetime.now(timezone.utc)``.

Compartilhado entre:
    - ``deile/tools/cron_create_tool.py`` (LLM agendando via tool)
    - ``deilebot/providers/discord/cogs/cron_cog.py`` (slash command /agendar)
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple

# BRT é UTC-3 fixo (sem horário de verão desde 2019). Conservador.
BRT = timezone(timedelta(hours=-3))

# 5 campos: dígitos, *, ranges, steps.
_CRON_FIELD = r"(\*|[0-9*/,\-]+)"
_CRON_RE = re.compile(
    rf"^{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}$"
)

# Hora aceita: "14", "14h", "14:30", "14h30", "14h00".
_HOUR = r"(\d{1,2})(?:[:h](\d{2})?)?"
_BR_DATE_RE = re.compile(rf"^(\d{{1,2}})/(\d{{1,2}})(?:/(\d{{2,4}}))?\s+{_HOUR}$")
_REL_RE = re.compile(rf"^(hoje|amanh[aã]|amanha)\s+{_HOUR}$", re.I)


class ScheduleParseError(ValueError):
    """Falha ao interpretar a string de agendamento."""


def parse_iso_datetime(text: str, naive_tz: timezone = BRT) -> Optional[datetime]:
    """Interpreta uma string ISO-8601 e retorna um ``datetime`` em UTC.

    Aceita o sufixo ``Z``. Datetimes naive (sem offset) são interpretados
    em ``naive_tz`` antes da conversão para UTC. Retorna ``None`` quando a
    string não casa com ISO-8601.
    """
    stripped = text.strip()
    if stripped.endswith("Z"):
        stripped = stripped[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(stripped)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    return dt.astimezone(timezone.utc)


def parse_natural_schedule(text: str) -> Tuple[Optional[str], Optional[datetime]]:
    """Interpreta uma string de agendamento e retorna ``(cron_expr, run_at_utc)``.

    Exatamente um dos dois retornos é ``None``. Levanta
    :class:`ScheduleParseError` quando nenhum formato casa.

    Para horários no passado (ex: ``hoje 8h`` quando já são 9h), retorna
    o horário literal sem auto-promover para o dia seguinte — caller decide.
    """
    if not text or not text.strip():
        raise ScheduleParseError("texto de agendamento vazio")

    stripped = text.strip()

    # 1. Cron 5-field
    if _CRON_RE.match(stripped):
        return stripped, None

    # 2. ISO 8601 (com ou sem timezone). Naive assume BRT.
    dt = parse_iso_datetime(stripped, naive_tz=BRT)
    if dt is not None:
        return None, dt

    # 3. BR humano: DD/MM[/YYYY] HH[:MM|hMM]
    m = _BR_DATE_RE.match(stripped)
    if m:
        day_s, month_s, year_s, hour_s, minute_s = m.groups()
        try:
            day = int(day_s)
            month = int(month_s)
            hour = int(hour_s)
            minute = int(minute_s) if minute_s else 0
            if year_s:
                year = int(year_s)
                if year < 100:
                    year += 2000
            else:
                year = date.today().year
            dt = datetime(year, month, day, hour, minute, tzinfo=BRT)
        except ValueError as exc:
            raise ScheduleParseError(f"data inválida: {exc}") from exc
        return None, dt.astimezone(timezone.utc)

    # 4. Linguagem natural: hoje/amanhã + hora
    m = _REL_RE.match(stripped)
    if m:
        word, hour_s, minute_s = m.groups()
        word_norm = word.lower().replace("ã", "a")
        try:
            hour = int(hour_s)
            minute = int(minute_s) if minute_s else 0
            base = date.today()
            if word_norm in ("amanha",):
                base = base + timedelta(days=1)
            dt = datetime(base.year, base.month, base.day, hour, minute, tzinfo=BRT)
        except ValueError as exc:
            raise ScheduleParseError(f"hora inválida: {exc}") from exc
        return None, dt.astimezone(timezone.utc)

    raise ScheduleParseError(
        f"não consegui interpretar {text!r}. Formatos aceitos:\n"
        "• `*/5 * * * *` (cron 5 campos, UTC, recorrente)\n"
        "• `15/05/2026 09:30` ou `15/05 09:30` (BRT)\n"
        "• `2026-05-15T09:30` (ISO sem TZ — assume BRT)\n"
        "• `2026-05-15T12:30:00Z` (ISO em UTC)\n"
        "• `amanhã 14h` ou `hoje 23:00` (BRT)"
    )
