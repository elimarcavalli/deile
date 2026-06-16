"""Analisador de logs — scan periódico com detecção de anomalias.

Roda como DaemonSet em cada nó K8s, lendo logs dos pods DEILE
e detectando: error spikes, auth expiry, flooding, crash loops, e
silêncio do pipeline.

Uso standalone::

    python3 -m deile.log_mgmt.log_analyzer --log-dir /var/log/containers

Ou como entrypoint do DaemonSet::

    python3 /app/log_analyzer.py
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from deile.log_mgmt.log_patterns import (
    AUTH_EXPIRED_PATTERNS,
    PIPELINE_PATTERNS,
    Severity,
    match_line,
)

logger = logging.getLogger("deile.log_analyzer")


# ── Tipos ───────────────────────────────────────────────────────────────────


@dataclass
class Anomaly:
    """Uma anomalia detectada pelo analyzer."""

    pattern_name: str
    """Nome do pattern que disparou (ex: ``auth_expired_anthropic``)."""

    severity: str
    """Severidade (:attr:`Severity`)."""

    pod_name: str
    """Nome do pod onde a anomalia foi detectada."""

    sample_lines: List[str] = field(default_factory=list)
    """Amostra das linhas de log que dispararam o alerta (máx. 5)."""

    count: int = 0
    """Número de ocorrências do pattern no intervalo."""

    threshold: Optional[int] = None
    """Threshold que foi excedido (se aplicável)."""

    def to_dict(self) -> dict:
        return {
            "pattern_name": self.pattern_name,
            "severity": self.severity,
            "pod_name": self.pod_name,
            "sample_lines": self.sample_lines[:5],
            "count": self.count,
            "threshold": self.threshold,
        }


# ── Config ──────────────────────────────────────────────────────────────────


def _get_config() -> dict:
    """Lê configuração do analyzer via env vars."""
    return {
        "enabled": os.environ.get("DEILE_LOG_ANALYZER_ENABLED", "true").lower()
        != "false",
        "interval_s": int(os.environ.get("DEILE_LOG_ANALYZER_INTERVAL_S", "300")),
        "error_rate_threshold": int(
            os.environ.get("DEILE_LOG_ERROR_RATE_THRESHOLD", "10")
        ),
        "flood_threshold": int(os.environ.get("DEILE_LOG_FLOOD_THRESHOLD", "200")),
        "silent_tick_threshold": int(
            os.environ.get("DEILE_LOG_PIPELINE_SILENT_TICK_THRESHOLD", "30")
        ),
        "auto_dispatch": os.environ.get("DEILE_LOG_AUTO_DISPATCH", "false").lower()
        == "true",
        "log_dir": os.environ.get("DEILE_LOG_DIR", "/home/deile/logs"),
        "namespace": os.environ.get("DEILE_NAMESPACE", "deile"),
    }


# ── Scan engine ─────────────────────────────────────────────────────────────


def _scan_files(
    log_dir: str, pod_filter: Optional[List[str]] = None
) -> Dict[str, List[str]]:
    """Lê todos os arquivos .log no diretório e retorna linhas por pod.

    Args:
        log_dir: Diretório raiz de logs (contém subpastas por pod).
        pod_filter: Lista opcional de nomes de pods para filtrar.

    Returns:
        Dict mapeando ``pod_name -> [linhas recentes...]``.
    """
    base = Path(log_dir)
    if not base.is_dir():
        return {}

    pods: Dict[str, List[str]] = {}
    for pod_dir in sorted(base.iterdir()):
        if not pod_dir.is_dir():
            continue
        pod_name = pod_dir.name
        if pod_filter and pod_name not in pod_filter:
            continue
        log_file = pod_dir / f"{pod_name}.log"
        if not log_file.is_file():
            continue
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            pods[pod_name] = lines
        except OSError:
            logger.debug("could not read %s", log_file)
            continue
    return pods


def _detect_error_spike(
    pod_name: str,
    lines: List[str],
    threshold: int,
    window_minutes: int = 5,
) -> List[Anomaly]:
    """Detecta spike de erros: >N erros/minuto.

    Conta linhas contendo ERROR/CRITICAL/Traceback e verifica se a taxa
    excede ``threshold`` por minuto em uma janela de ``window_minutes``.
    """
    errors: List[int] = []  # timestamp de cada linha de erro
    for line in lines:
        matches = match_line(line)
        if any(m.severity in (Severity.ERROR, Severity.CRITICAL) for m in matches):
            # Timestamp aproximado: usa a posição como proxy
            # (em produção usaríamos parse de timestamp real)
            errors.append(1)

    if not errors:
        return []

    # Taxa de erro por minuto
    total = len(errors)
    rate_per_min = total / max(window_minutes, 1)

    if rate_per_min > threshold:
        return [
            Anomaly(
                pattern_name="error_rate_spike",
                severity=Severity.WARNING,
                pod_name=pod_name,
                sample_lines=lines[-5:],
                count=total,
                threshold=threshold,
            )
        ]
    return []


def _detect_auth_expiry(pod_name: str, lines: List[str]) -> List[Anomaly]:
    """Detecta tokens/auth expirados nos logs."""
    anomalies: List[Anomaly] = []
    auth_lines: List[str] = []

    for line in lines:
        for pat in AUTH_EXPIRED_PATTERNS:
            if pat.pattern.search(line):
                auth_lines.append(line)
                break

    if auth_lines:
        # Agrupa por pattern
        for pat in AUTH_EXPIRED_PATTERNS:
            matching = [line for line in auth_lines if pat.pattern.search(line)]
            if matching:
                anomalies.append(
                    Anomaly(
                        pattern_name=pat.name,
                        severity=pat.severity,
                        pod_name=pod_name,
                        sample_lines=matching[:5],
                        count=len(matching),
                    )
                )
    return anomalies


def _detect_flooding(pod_name: str, lines: List[str], threshold: int) -> List[Anomaly]:
    """Detecta flooding: 200+ linhas idênticas consecutivas."""
    if len(lines) < threshold:
        return []

    # Conta linhas idênticas (ignora timestamp prefix)
    line_counts: Counter = Counter()
    for line in lines:
        # Remove timestamp prefix para comparação (formato ISO 8601)
        # Ex: "2026-05-28T14:00:00 ERROR ..." -> "ERROR ..."
        normalized = line
        if len(line) > 26 and line[4] == "-" and line[10] == "T":
            normalized = line[26:].strip()
        line_counts[normalized] += 1

    anomalies: List[Anomaly] = []
    for normalized_line, count in line_counts.items():
        if count >= threshold and normalized_line.strip():
            anomalies.append(
                Anomaly(
                    pattern_name="log_flooding",
                    severity=Severity.WARNING,
                    pod_name=pod_name,
                    sample_lines=[normalized_line],
                    count=count,
                    threshold=threshold,
                )
            )
    return anomalies[:3]  # limita a 3 tipos de flood por scan


def _detect_silent_pipeline(
    pod_name: str, lines: List[str], threshold: int
) -> List[Anomaly]:
    """Detecta pipeline silencioso: N ticks sem atividade.

    Conta ocorrências de ``tick completed.*activity=0`` ou ``no eligible issues``.
    """
    silent_count = 0
    for line in reversed(lines):
        for pat in PIPELINE_PATTERNS:
            if pat.name == "pipeline_tick_silent" and pat.pattern.search(line):
                silent_count += 1
                break
        else:
            # Linha não-silenciosa quebra a sequência
            if silent_count > 0:
                break

    if silent_count >= threshold:
        return [
            Anomaly(
                pattern_name="pipeline_silent",
                severity=Severity.INFO,
                pod_name=pod_name,
                sample_lines=lines[-3:],
                count=silent_count,
                threshold=threshold,
            )
        ]
    return []


# ── Scan principal ──────────────────────────────────────────────────────────


def scan_logs(
    log_dir: Optional[str] = None,
    *,
    error_rate_threshold: Optional[int] = None,
    flood_threshold: Optional[int] = None,
    silent_tick_threshold: Optional[int] = None,
    pod_filter: Optional[List[str]] = None,
) -> List[Anomaly]:
    """Executa um scan completo de todos os logs.

    Args:
        log_dir: Diretório raiz de logs. Default via env var ou ``/home/deile/logs``.
        error_rate_threshold: Erros/minuto para disparar alerta.
        flood_threshold: Linhas idênticas para detectar flooding.
        silent_tick_threshold: Ticks consecutivos vazios.
        pod_filter: Lista opcional de pods para escanear.

    Returns:
        Lista de :class:`Anomaly` detectadas.
    """
    cfg = _get_config()

    if log_dir is None:
        log_dir = cfg["log_dir"]
    if error_rate_threshold is None:
        error_rate_threshold = cfg["error_rate_threshold"]
    if flood_threshold is None:
        flood_threshold = cfg["flood_threshold"]
    if silent_tick_threshold is None:
        silent_tick_threshold = cfg["silent_tick_threshold"]

    if not cfg["enabled"]:
        logger.debug("analyzer disabled via DEILE_LOG_ANALYZER_ENABLED=false")
        return []

    pods = _scan_files(log_dir, pod_filter=pod_filter)
    if not pods:
        logger.debug("no log files found in %s", log_dir)
        return []

    all_anomalies: List[Anomaly] = []

    for pod_name, lines in pods.items():
        if not lines:
            continue

        # 1. Error spike
        all_anomalies.extend(_detect_error_spike(pod_name, lines, error_rate_threshold))

        # 2. Auth expiry
        all_anomalies.extend(_detect_auth_expiry(pod_name, lines))

        # 3. Flooding
        all_anomalies.extend(_detect_flooding(pod_name, lines, flood_threshold))

        # 4. Pipeline silencioso (apenas para pods pipeline)
        if "pipeline" in pod_name.lower():
            all_anomalies.extend(
                _detect_silent_pipeline(pod_name, lines, silent_tick_threshold)
            )

    return all_anomalies


def scan_crash_loops(
    restart_counts: Dict[str, int],
    *,
    threshold: int = 3,
    window_minutes: int = 10,
) -> List[Anomaly]:
    """Detecta crash loops baseado em contagem de restarts.

    Args:
        restart_counts: Dict ``pod_name -> restart_count`` (do K8s API).
        threshold: Número de restarts para considerar crash loop.
        window_minutes: Janela de tempo em minutos.

    Returns:
        Lista de anomalias de crash loop.
    """
    anomalies: List[Anomaly] = []
    for pod_name, count in restart_counts.items():
        if count >= threshold:
            anomalies.append(
                Anomaly(
                    pattern_name="crash_loop",
                    severity=Severity.CRITICAL,
                    pod_name=pod_name,
                    count=count,
                    threshold=threshold,
                    sample_lines=[
                        f"pod {pod_name}: {count} restarts in {window_minutes}min"
                    ],
                )
            )
    return anomalies


# ── Entrypoint standalone ───────────────────────────────────────────────────


def main() -> int:
    """Entrypoint do log analyzer (DaemonSet).

    Loop infinito: scan → report → sleep.
    """
    cfg = _get_config()
    if not cfg["enabled"]:
        print("log_analyzer: disabled via DEILE_LOG_ANALYZER_ENABLED=false")
        return 0

    print(
        f"log_analyzer: starting scan loop (interval={cfg['interval_s']}s, "
        f"log_dir={cfg['log_dir']})"
    )

    while True:
        try:
            anomalies = scan_logs()
            if anomalies:
                _report_anomalies(anomalies, cfg)
            else:
                logger.debug("scan: no anomalies detected")
        except Exception:
            logger.exception("scan failed")

        time.sleep(cfg["interval_s"])

    return 0


def _report_anomalies(anomalies: List[Anomaly], cfg: dict) -> None:
    """Reporta anomalias detectadas (log + dispatch opcional)."""
    for a in anomalies:
        logger.warning(
            "ANOMALY: %s | pod=%s | severity=%s | count=%d | threshold=%s",
            a.pattern_name,
            a.pod_name,
            a.severity,
            a.count,
            a.threshold,
        )

    # Auto-dispatch se habilitado
    if cfg["auto_dispatch"]:
        _dispatch_anomalies(anomalies)


def _dispatch_anomalies(anomalies: List[Anomaly]) -> None:
    """Dispara workers investigativos para anomalias complexas.

    Import lazy para evitar dependência circular.
    """
    try:
        from deile.log_mgmt.log_dispatcher import dispatch_anomalies

        dispatch_anomalies(anomalies)
    except ImportError:
        logger.warning("log_dispatcher not available — skipping auto-dispatch")


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
