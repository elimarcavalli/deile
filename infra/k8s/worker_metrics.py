"""worker_metrics — registry Prometheus hand-rolled (zero deps) do deile-worker.

Implementa o subconjunto do text exposition format 0.0.4 que o ``/v1/metrics``
do worker precisa expor (issue #620 AC2), sem trazer o client_python como
dependência: o worker já tem orçamento de imagem apertado e o conjunto de
métricas é pequeno e estável.

Tipos suportados:
  * Counter — monotônico, com labels.
  * Gauge — valor instantâneo (sem labels neste worker; resolvido via callbacks
    no momento do scrape).
  * Histogram **skeleton** (AC2/§Fora-de-escopo) — buckets registrados, mas
    sem ``.observe()`` em V1: o output mostra ``_bucket``/``_count``/``_sum``
    com 0. Coleta real difere para #621.

Thread-safety: cada mutação de counter é protegida por um ``threading.Lock``;
o worker roda single event loop, mas os incrementos podem ocorrer de dentro
de tasks concorrentes e o lock mantém o estado consistente sem custo relevante.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, Iterable, List, Tuple

#: Content-Type exigido pelo AC2 (Prometheus text exposition v0.0.4).
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: Buckets do histograma de duração de dispatch (segundos) — AC2 item 2.
DISPATCH_DURATION_BUCKETS: Tuple[float, ...] = (
    1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600,
)

_LabelKey = Tuple[Tuple[str, str], ...]


def _format_labels(labels: _LabelKey) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    """Escapa label values conforme o text format (\\, \" e newline)."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


class Counter:
    """Counter Prometheus com labels, monotônico e thread-safe."""

    def __init__(self, name: str, help_text: str, labelnames: Iterable[str] = ()):
        self.name = name
        self.help_text = help_text
        self.labelnames = tuple(labelnames)
        self._values: Dict[_LabelKey, float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def reset(self) -> None:
        """Zera todas as séries. Só para testes (os counters são singletons
        de processo, então estado vazaria entre testes)."""
        with self._lock:
            self._values.clear()

    def _key(self, labels: Dict[str, str]) -> _LabelKey:
        if set(labels) != set(self.labelnames):
            raise ValueError(
                f"counter {self.name} expects labels {self.labelnames}, "
                f"got {tuple(labels)}"
            )
        return tuple(sorted((k, str(v)) for k, v in labels.items()))

    def render(self) -> List[str]:
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} counter",
        ]
        with self._lock:
            items = list(self._values.items())
        if not items:
            # Sem séries ainda: emite a linha com valor 0 (e sem labels) para
            # que o nome da métrica apareça no scrape mesmo antes do 1º evento.
            lines.append(f"{self.name} 0")
            return lines
        for key, value in items:
            lines.append(f"{self.name}{_format_labels(key)} {_render_number(value)}")
        return lines


class Gauge:
    """Gauge resolvido por callback no momento do scrape (sem labels).

    Usar callback evita estado duplicado: o valor canônico vive em ``_TASKS`` /
    no circuit breaker do cliente; o gauge apenas o lê quando scrapeado.
    """

    def __init__(self, name: str, help_text: str, callback: Callable[[], float]):
        self.name = name
        self.help_text = help_text
        self._callback = callback

    def render(self) -> List[str]:
        try:
            value = float(self._callback())
        except Exception:  # noqa: BLE001 — métrica nunca derruba o scrape
            value = 0.0
        return [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} gauge",
            f"{self.name} {_render_number(value)}",
        ]


class HistogramSkeleton:
    """Histograma **skeleton** (AC2): buckets registrados, sem coleta.

    Emite ``_bucket`` (cumulativo, todos 0), ``_count=0`` e ``_sum=0``. A
    coleta real (``.observe()``) difere para #621 — manter como skeleton evita
    falsear latência antes de a instrumentação por-fase (AC8) estar plugada na
    métrica.
    """

    def __init__(self, name: str, help_text: str, buckets: Iterable[float]):
        self.name = name
        self.help_text = help_text
        self.buckets = tuple(buckets)

    def render(self) -> List[str]:
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} histogram",
        ]
        for b in self.buckets:
            lines.append(f'{self.name}_bucket{{le="{_render_number(b)}"}} 0')
        lines.append(f'{self.name}_bucket{{le="+Inf"}} 0')
        lines.append(f"{self.name}_count 0")
        lines.append(f"{self.name}_sum 0")
        return lines


def _render_number(value: float) -> str:
    """Renderiza inteiros sem ``.0`` e floats compactos (saída determinística)."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


class MetricsRegistry:
    """Coleção ordenada de métricas com render do text exposition format."""

    def __init__(self) -> None:
        self._metrics: List[object] = []

    def register(self, metric: object) -> object:
        self._metrics.append(metric)
        return metric

    def reset(self) -> None:
        """Zera todos os counters registrados (gauges/histogramas são
        stateless). Só para testes."""
        for m in self._metrics:
            reset = getattr(m, "reset", None)
            if callable(reset):
                reset()

    def render(self) -> str:
        blocks = ["\n".join(m.render()) for m in self._metrics]  # type: ignore[attr-defined]
        return "\n".join(blocks) + "\n"
