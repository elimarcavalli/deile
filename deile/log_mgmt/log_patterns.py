"""Catálogo de regex patterns para detecção de anomalias em logs DEILE.

Cada pattern recebe um nome canónico, uma regex compilada e uma severidade
associada. O :class:`LogAnalyzer` usa este catálogo para classificar
linhas de log durante o scan periódico.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Pattern


class Severity:
    """Níveis de severidade para patterns de log."""

    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class LogPattern:
    """Um pattern de detecção com regex compilada e metadados."""

    name: str
    """Nome canónico do pattern (ex: ``auth_expired``)."""

    pattern: Pattern[str]
    """Regex compilada (case-insensitive por padrão)."""

    severity: str
    """Severidade (:attr:`Severity`)."""

    description: str
    """Descrição legível do que o pattern detecta."""


# ── Patterns de autenticação ────────────────────────────────────────────────

AUTH_EXPIRED_PATTERNS: List[LogPattern] = [
    LogPattern(
        name="auth_expired_anthropic",
        pattern=re.compile(
            r"(?:not logged in|invalid authentication credentials|"
            r"401 unauthorized|please run /login|"
            r"please run `claude auth login`)",
            re.IGNORECASE,
        ),
        severity=Severity.CRITICAL,
        description="Token OAuth Anthropic expirado ou inválido",
    ),
    LogPattern(
        name="auth_expired_openai",
        pattern=re.compile(
            r"(?:incorrect api key provided|invalid api key|"
            r"you didn't provide an api key|"
            r"401.*invalid api key)",
            re.IGNORECASE,
        ),
        severity=Severity.CRITICAL,
        description="API key OpenAI inválida ou expirada",
    ),
    LogPattern(
        name="auth_expired_google",
        pattern=re.compile(
            r"(?:api key not valid|permission denied.*api key|"
            r"403.*access not configured)",
            re.IGNORECASE,
        ),
        severity=Severity.CRITICAL,
        description="API key Google/Gemini inválida ou sem permissão",
    ),
    LogPattern(
        name="worker_auth_expired",
        pattern=re.compile(
            r"WORKER_AUTH_EXPIRED|worker.*auth.*expired|"
            r"bad bearer|UNAUTHORIZED.*worker",
            re.IGNORECASE,
        ),
        severity=Severity.CRITICAL,
        description="Bearer token do worker expirado",
    ),
]

# ── Patterns de crash / restart ─────────────────────────────────────────────

CRASH_PATTERNS: List[LogPattern] = [
    LogPattern(
        name="crash_loop_backoff",
        pattern=re.compile(
            r"back-off.*restarting failed container|"
            r"container.*killed.*oom|"
            r"crashloopbackoff",
            re.IGNORECASE,
        ),
        severity=Severity.CRITICAL,
        description="Crash loop detectado no container",
    ),
    LogPattern(
        name="sigterm_timeout",
        pattern=re.compile(
            r"received sigterm.*without timely shutdown|"
            r"force killing after grace period",
            re.IGNORECASE,
        ),
        severity=Severity.WARNING,
        description="Container não desligou no grace period",
    ),
    LogPattern(
        name="segfault",
        pattern=re.compile(
            r"segmentation fault|sigsegv|signal 11",
            re.IGNORECASE,
        ),
        severity=Severity.CRITICAL,
        description="Segmentation fault em processo DEILE",
    ),
]

# ── Patterns de erro de runtime ─────────────────────────────────────────────

RUNTIME_ERROR_PATTERNS: List[LogPattern] = [
    LogPattern(
        name="module_not_found",
        pattern=re.compile(
            r"ModuleNotFoundError: No module named",
        ),
        severity=Severity.ERROR,
        description="Import de módulo falhou",
    ),
    LogPattern(
        name="connection_refused",
        pattern=re.compile(
            r"(?:connection refused|connect refused|"
            r"ConnectionRefusedError|"
            r"could not connect to)",
            re.IGNORECASE,
        ),
        severity=Severity.ERROR,
        description="Conexão recusada em chamada de rede",
    ),
    LogPattern(
        name="timeout",
        pattern=re.compile(
            r"(?:timeout|timed out|TimeoutError|" r"asyncio\.TimeoutError)",
            re.IGNORECASE,
        ),
        severity=Severity.WARNING,
        description="Timeout em operação",
    ),
    LogPattern(
        name="disk_full",
        pattern=re.compile(
            r"(?:no space left on device|disk full|" r"ENOSPC|out of disk space)",
            re.IGNORECASE,
        ),
        severity=Severity.CRITICAL,
        description="Disco do nó sem espaço",
    ),
    LogPattern(
        name="memory_error",
        pattern=re.compile(
            r"(?:memoryerror|out of memory|cannot allocate memory|" r"killed.*oom)",
            re.IGNORECASE,
        ),
        severity=Severity.CRITICAL,
        description="Erro de memória / OOM kill",
    ),
]

# ── Patterns de pipeline ────────────────────────────────────────────────────

PIPELINE_PATTERNS: List[LogPattern] = [
    LogPattern(
        name="pipeline_error_rate",
        pattern=re.compile(
            r"(?:ERROR|CRITICAL|Traceback|Exception)",
        ),
        severity=Severity.ERROR,
        description="Linha contendo ERROR/CRITICAL/Traceback",
    ),
    LogPattern(
        name="pipeline_tick_silent",
        pattern=re.compile(
            r"tick completed.*activity=0|no eligible issues",
            re.IGNORECASE,
        ),
        severity=Severity.INFO,
        description="Tick do pipeline sem atividade",
    ),
    LogPattern(
        name="dispatch_failed",
        pattern=re.compile(
            r"dispatch.*failed|WORKER_TIMEOUT|"
            # Accept both wire formats: canonical dot ``dispatch.completed``
            # and legacy snake ``dispatch_completed`` (a detector must never
            # miss a failed dispatch — false-negatives here = silent outages).
            r"dispatch[._]completed.*ok=False|" r"implement.*BLOCKED|review.*BLOCKED",
            re.IGNORECASE,
        ),
        severity=Severity.ERROR,
        description="Dispatch para worker/claude-worker falhou",
    ),
]

# ── Catálogo completo ───────────────────────────────────────────────────────

ALL_PATTERNS: List[LogPattern] = (
    AUTH_EXPIRED_PATTERNS + CRASH_PATTERNS + RUNTIME_ERROR_PATTERNS + PIPELINE_PATTERNS
)


def match_line(line: str) -> List[LogPattern]:
    """Retorna todos os patterns que casam com a linha.

    Args:
        line: Uma linha de log (pode conter timestamp, level, etc.).

    Returns:
        Lista de :class:`LogPattern` que deram match (vazia se nenhum).
    """
    matches: List[LogPattern] = []
    for pat in ALL_PATTERNS:
        if pat.pattern.search(line):
            matches.append(pat)
    return matches


def match_critical(line: str) -> List[LogPattern]:
    """Como :func:`match_line`, mas apenas patterns critical."""
    return [p for p in match_line(line) if p.severity == Severity.CRITICAL]
