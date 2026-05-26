"""Backends no-op para tracer/metrics — usados quando OTLP está desligado.

Critérios para cair aqui:
  - ``opentelemetry`` não está instalado;
  - ``DEILE_OTLP_ENDPOINT`` está vazio;
  - ``DEILE_OBSERVABILITY_DISABLED=true``.

Todos os métodos são no-op silenciosos. A API espelha ``DeileTracer`` /
``DeileMetrics`` para que o resto do código não precise checar disponibilidade.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

__all__ = ["NoOpSpan", "NoOpTracer", "NoOpMetrics"]


class NoOpSpan:
    """Span no-op — todos os métodos viram no-op silencioso.

    Mimica a API de ``opentelemetry.trace.Span`` (subset que o DEILE usa).
    """

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_attributes(self, attributes: Any) -> None:
        return None

    def add_event(self, name: str, attributes: Optional[Any] = None) -> None:
        return None

    def set_status(self, status: Any, description: Optional[str] = None) -> None:
        return None

    def record_exception(
        self,
        exception: BaseException,
        attributes: Optional[Any] = None,
    ) -> None:
        return None

    def is_recording(self) -> bool:
        return False

    def end(self, end_time: Optional[int] = None) -> None:
        return None

    def get_span_context(self) -> Any:
        return None

    def __enter__(self) -> "NoOpSpan":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return False


class NoOpTracer:
    """Tracer no-op com a mesma API pública de ``DeileTracer``."""

    @contextmanager
    def turn(
        self,
        session_id: str,
        turn_number: int,
        persona: str = "",
        model: str = "",
        input_length: int = 0,
    ) -> Iterator[NoOpSpan]:
        yield NoOpSpan()

    @contextmanager
    def tool(
        self,
        tool_name: str,
        args_size: int = 0,
    ) -> Iterator[NoOpSpan]:
        yield NoOpSpan()

    @contextmanager
    def llm_call(
        self,
        provider: str,
        model: str,
    ) -> Iterator[NoOpSpan]:
        yield NoOpSpan()

    def shutdown(self) -> None:
        return None


class NoOpMetrics:
    """Coletor de métricas no-op com a mesma API pública de ``DeileMetrics``."""

    def record_tokens(
        self,
        provider: str,
        model: str,
        direction: str,
        count: int,
    ) -> None:
        return None

    def record_cost(
        self,
        provider: str,
        model: str,
        usd: float,
    ) -> None:
        return None

    def record_tool_duration(
        self,
        tool_name: str,
        status: str,
        duration_ms: int,
    ) -> None:
        return None

    def record_turn_duration(
        self,
        persona: str,
        duration_ms: int,
    ) -> None:
        return None

    def record_error(
        self,
        error_type: str,
        component: str,
    ) -> None:
        return None

    def shutdown(self) -> None:
        return None
