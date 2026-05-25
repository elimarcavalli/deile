"""Configuração de observabilidade — lida via env vars.

Centraliza a leitura de ``DEILE_OTLP_*`` (princípio 7: configuração centralizada).
Núcleo (``deile/core/``) NÃO lê ``os.environ`` diretamente para OTLP — usa
:func:`get_observability_config`.

Vars suportadas:

================================  ===========  ==========================================
Variável                          Default      Significado
================================  ===========  ==========================================
``DEILE_OTLP_ENDPOINT``           ``""``       Endpoint gRPC do collector. Vazio = OTLP off
``DEILE_OTLP_HEADERS``            ``""``       ``key1=val1,key2=val2`` (auth do collector)
``DEILE_OTLP_INSECURE``           ``"true"``   ``"false"`` ativa TLS
``DEILE_OTLP_SERVICE_NAME``       ``"deile"``  resource attribute ``service.name``
``DEILE_OTLP_SAMPLE_RATIO``       ``"1.0"``    sampling ratio (0.0-1.0)
``DEILE_OBSERVABILITY_DISABLED``  ``"false"``  kill-switch global (vence sobre endpoint)
================================  ===========  ==========================================

Nenhum import de ``opentelemetry.*`` aqui — este módulo é safe mesmo quando
o SDK não está instalado. A integração real vive em ``tracer.py`` / ``metrics.py``
e usa lazy import.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional

__all__ = [
    "ObservabilityConfig",
    "get_observability_config",
    "reset_observability_config",
    "ENV_ENDPOINT",
    "ENV_HEADERS",
    "ENV_INSECURE",
    "ENV_SERVICE_NAME",
    "ENV_SAMPLE_RATIO",
    "ENV_DISABLED",
]

ENV_ENDPOINT = "DEILE_OTLP_ENDPOINT"
ENV_HEADERS = "DEILE_OTLP_HEADERS"
ENV_INSECURE = "DEILE_OTLP_INSECURE"
ENV_SERVICE_NAME = "DEILE_OTLP_SERVICE_NAME"
ENV_SAMPLE_RATIO = "DEILE_OTLP_SAMPLE_RATIO"
ENV_DISABLED = "DEILE_OBSERVABILITY_DISABLED"

_DEFAULT_SERVICE_NAME = "deile"
_DEFAULT_SAMPLE_RATIO = 1.0


def _parse_bool(value: str, default: bool) -> bool:
    """Aceita ``true``/``false`` (case-insensitive); inválido → default."""
    v = (value or "").strip().lower()
    if v in {"true", "1", "yes", "on"}:
        return True
    if v in {"false", "0", "no", "off"}:
        return False
    return default


def _parse_float(value: str, default: float, lo: float, hi: float) -> float:
    """Parse ``value`` como float em ``[lo, hi]``; inválido → default."""
    try:
        parsed = float((value or "").strip())
    except (TypeError, ValueError):
        return default
    if parsed < lo or parsed > hi:
        return default
    return parsed


def _parse_headers(raw: str) -> Dict[str, str]:
    """Parse ``"key1=val1,key2=val2"`` num dict (descarta entradas malformadas)."""
    out: Dict[str, str] = {}
    if not raw:
        return out
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            out[key] = value
    return out


@dataclass(frozen=True)
class ObservabilityConfig:
    """Snapshot imutável das envs de observabilidade.

    Atributos:
        endpoint: gRPC do collector OTLP (vazio = OTLP desligado por config).
        headers: dict de auth para o exporter (pode estar vazio).
        insecure: ``True`` desliga TLS; default ``True`` para dev local.
        service_name: ``service.name`` do Resource OpenTelemetry.
        sample_ratio: razão de amostragem ``[0.0, 1.0]`` (1.0 = tudo).
        disabled: kill-switch global; ``True`` força no-op mesmo com endpoint.
    """

    endpoint: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    insecure: bool = True
    service_name: str = _DEFAULT_SERVICE_NAME
    sample_ratio: float = _DEFAULT_SAMPLE_RATIO
    disabled: bool = False

    @classmethod
    def from_env(cls) -> "ObservabilityConfig":
        """Carrega a config a partir das env vars (snapshot único)."""
        return cls(
            endpoint=(os.environ.get(ENV_ENDPOINT, "") or "").strip(),
            headers=_parse_headers(os.environ.get(ENV_HEADERS, "")),
            insecure=_parse_bool(os.environ.get(ENV_INSECURE, "true"), True),
            service_name=(
                (os.environ.get(ENV_SERVICE_NAME, "") or "").strip()
                or _DEFAULT_SERVICE_NAME
            ),
            sample_ratio=_parse_float(
                os.environ.get(ENV_SAMPLE_RATIO, ""),
                _DEFAULT_SAMPLE_RATIO,
                0.0,
                1.0,
            ),
            disabled=_parse_bool(os.environ.get(ENV_DISABLED, "false"), False),
        )

    @property
    def is_enabled(self) -> bool:
        """Retorna ``True`` quando OTLP deve emitir (endpoint set + not disabled).

        NÃO checa disponibilidade do SDK — isso é responsabilidade do tracer
        (lazy import). Aqui é apenas a parte declarativa.
        """
        if self.disabled:
            return False
        return bool(self.endpoint)


# ── singleton ────────────────────────────────────────────────────────────

_config_singleton: Optional[ObservabilityConfig] = None


def get_observability_config() -> ObservabilityConfig:
    """Retorna o singleton; cria do env no primeiro acesso (cache imutável)."""
    global _config_singleton
    if _config_singleton is None:
        _config_singleton = ObservabilityConfig.from_env()
    return _config_singleton


def reset_observability_config() -> None:
    """Apenas para testes — força releitura na próxima chamada."""
    global _config_singleton
    _config_singleton = None
