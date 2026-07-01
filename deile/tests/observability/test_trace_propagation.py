"""Testes de propagação W3C traceparent cross-pod — issue #457.

ACs verificados (agora dirigindo a PRODUÇÃO real, não reimplementando o OTel
cru inline — assim o teste fica vermelho se a fiação de produção quebrar):
- AC1/D1: ``WorkerImplementer._dispatch()`` abre o span ``pipeline.dispatch_request``.
- AC2/D2: ``DeileWorkerClient._dispatch_once`` injeta traceparent nos headers HTTP.
- AC3/D3: ``activate_traceparent_from_env`` extrai o traceparent e ``deile.dispatch``
         vira filho.
- AC4: span ``pipeline.dispatch_request`` (trace_id=X, span_id=Y) →
         span ``deile.dispatch`` (trace_id=X, parent_span_id=Y).
- AC5: env sem TRACEPARENT → ``activate_traceparent_from_env`` retorna ``None`` e
         ``deile.dispatch`` é raiz (parent_span_id ausente). Sem exceção.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytestmark = pytest.mark.unit


async def _inject_via_worker_client(monkeypatch) -> Dict[str, Any]:
    """Dirige o REAL ``DeileWorkerClient._dispatch_once`` sob um span ativo.

    Substitui apenas o transporte (``_resolve_auth_and_httpx`` → token fake +
    httpx fake que captura a requisição). A injeção do traceparent nos headers
    é 100% produção (``propagate.inject`` dentro de ``_dispatch_once``). Abre e
    fecha o span pai ``pipeline.dispatch_request`` — que fica disponível no
    exporter para os asserts de hierarquia — e devolve os headers HTTP
    capturados da requisição real.
    """
    from opentelemetry import trace

    from deile.infrastructure import deile_worker_client as dwc

    captured: Dict[str, Any] = {}

    class _FakeResp:
        status_code = 200

        def json(self) -> Dict[str, Any]:
            return {"task_id": "t1", "ok": True}

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def post(self, endpoint, *, json=None, headers=None) -> _FakeResp:
            captured["headers"] = dict(headers or {})
            captured["endpoint"] = endpoint
            return _FakeResp()

    class _FakeTimeoutException(Exception):
        pass

    class _FakeHTTPError(Exception):
        pass

    fake_httpx = SimpleNamespace(
        Timeout=lambda *a, **k: object(),
        AsyncClient=_FakeAsyncClient,
        TimeoutException=_FakeTimeoutException,
        HTTPError=_FakeHTTPError,
    )
    monkeypatch.setattr(
        dwc, "_resolve_auth_and_httpx",
        AsyncMock(return_value=("tok", fake_httpx)),
    )

    client = dwc.DeileWorkerClient()
    tracer = trace.get_tracer("deile.pipeline")
    with tracer.start_as_current_span("pipeline.dispatch_request"):
        await client._dispatch_once(
            {"brief": "x"}, wait=False, endpoint_url="http://worker:8766",
        )
    return captured["headers"]


# ---------------------------------------------------------------------------
# AC1: pipeline side — a produção abre o span pipeline.dispatch_request
# ---------------------------------------------------------------------------


async def test_pipeline_dispatch_request_span_opened(in_memory_exporter):
    """AC1/D1: ``WorkerImplementer._dispatch()`` abre o span pela produção.

    Dirige o método real (com o seam HTTP ``_post_dispatch`` mockado) em vez de
    reabrir o span via OTel cru — assim o teste falha se a produção parar de
    instrumentar o dispatch (o antigo reimplementava e ficava falso-verde).
    """
    from deile.orchestration.pipeline.implementer import WorkerImplementer

    impl = WorkerImplementer(endpoint_override="http://worker:8766", ledger=Mock())
    with patch.object(impl, "_post_dispatch", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"task_id": "t1"}
        outcome = await impl._dispatch(
            brief="do it",
            channel_id="pipeline-issue-1",
            stage=None,
            nowait=True,
            ledger_key=None,
        )

    # A produção realmente atravessou o dispatch (o span embrulha o POST).
    assert mock_post.await_count == 1
    assert outcome.ok is True

    finished = in_memory_exporter.get_finished_spans()
    names = [s.name for s in finished]
    assert "pipeline.dispatch_request" in names, (
        f"span pipeline.dispatch_request não aberto pela produção; spans: {names}"
    )


def test_propagate_inject_no_op_without_span(in_memory_exporter):
    """propagate.inject sem span ativo não injeta traceparent (fallback silencioso)."""
    from opentelemetry import propagate

    headers: Dict[str, str] = {}
    propagate.inject(headers)
    # Sem span ativo, traceparent pode estar ausente (contexto root não válido).
    # Verificamos apenas que nenhuma exceção é levantada.


# ---------------------------------------------------------------------------
# AC3/AC4: worker side — produção extrai o context e deile.dispatch vira filho
# ---------------------------------------------------------------------------


async def test_deile_dispatch_is_child_of_pipeline_span(in_memory_exporter, monkeypatch):
    """AC4: pipeline.dispatch_request → deile.dispatch com mesmo trace_id e parent correto.

    Fim-a-fim pela produção: ``_dispatch_once`` injeta o traceparent (via
    ``propagate.inject``) e ``activate_traceparent_from_env`` o extrai/ativa —
    sem reimplementar inject/extract cru.
    """
    from opentelemetry import context as otel_context
    from opentelemetry import trace

    from deile.observability.tracer import activate_traceparent_from_env

    # 1. Produção do lado pipeline: abre span pai e injeta traceparent nos headers.
    headers = await _inject_via_worker_client(monkeypatch)
    traceparent = headers.get("traceparent")
    assert traceparent, f"traceparent deveria ter sido injetado; headers={headers}"

    # 2. Produção do lado worker: extrai o context do env e o ativa.
    monkeypatch.setenv("TRACEPARENT", traceparent)
    tracestate = headers.get("tracestate")
    if tracestate:
        monkeypatch.setenv("TRACESTATE", tracestate)
    token = activate_traceparent_from_env()
    assert token is not None, "activate_traceparent_from_env deveria anexar o context"

    worker_tracer = trace.get_tracer("deile.worker")
    try:
        with worker_tracer.start_as_current_span("deile.dispatch"):
            pass  # span abre e fecha sob o context extraído
    finally:
        otel_context.detach(token)

    # 3. Verifica hierarquia sobre os spans reais gravados no exporter.
    finished = in_memory_exporter.get_finished_spans()
    pipeline_spans = [s for s in finished if s.name == "pipeline.dispatch_request"]
    worker_spans = [s for s in finished if s.name == "deile.dispatch"]

    assert pipeline_spans, "span pipeline.dispatch_request não encontrado"
    assert worker_spans, "span deile.dispatch não encontrado"

    p_span = pipeline_spans[0]
    w_span = worker_spans[0]

    # AC4: mesmo trace_id
    assert p_span.context.trace_id == w_span.context.trace_id, (
        f"trace_id divergente: pipeline={hex(p_span.context.trace_id)} "
        f"worker={hex(w_span.context.trace_id)}"
    )

    # AC4: parent_span_id do worker == span_id do pipeline
    assert w_span.parent is not None, "deile.dispatch deveria ter parent (não é raiz)"
    assert w_span.parent.span_id == p_span.context.span_id, (
        f"parent_span_id errado: "
        f"esperado={hex(p_span.context.span_id)} "
        f"obtido={hex(w_span.parent.span_id)}"
    )


def test_deile_dispatch_is_root_when_no_traceparent(in_memory_exporter, monkeypatch):
    """AC5: env sem TRACEPARENT → activate_traceparent_from_env=None, deile.dispatch é raiz."""
    from opentelemetry import trace

    from deile.observability.tracer import activate_traceparent_from_env

    monkeypatch.delenv("TRACEPARENT", raising=False)
    monkeypatch.delenv("traceparent", raising=False)

    # Ramo real do projeto: sem header, o helper de produção retorna None.
    token = activate_traceparent_from_env()
    assert token is None, (
        "activate_traceparent_from_env deve retornar None quando TRACEPARENT ausente"
    )

    worker_tracer = trace.get_tracer("deile.worker")
    with worker_tracer.start_as_current_span("deile.dispatch"):
        pass

    finished = in_memory_exporter.get_finished_spans()
    worker_spans = [s for s in finished if s.name == "deile.dispatch"]
    assert worker_spans, "span deile.dispatch não encontrado"

    w_span = worker_spans[0]
    # Span raiz: parent deve ser None ou ter parent_id inválido (0)
    is_root = (
        w_span.parent is None
        or w_span.parent.span_id == 0
        or not w_span.parent.is_valid
    )
    assert is_root, (
        f"deile.dispatch deveria ser raiz quando traceparent ausente, "
        f"mas parent={w_span.parent}"
    )


# ---------------------------------------------------------------------------
# AC2: DeileWorkerClient._dispatch_once injeta traceparent nos headers reais
# ---------------------------------------------------------------------------


async def test_worker_client_injects_traceparent_in_headers(in_memory_exporter, monkeypatch):
    """D2: ``DeileWorkerClient._dispatch_once`` injeta traceparent no dict de headers.

    Chama o worker client REAL com httpx mockado e inspeciona os headers da
    requisição capturada — não reimplementa o inject inline.
    """
    headers = await _inject_via_worker_client(monkeypatch)

    assert "traceparent" in headers, (
        f"traceparent deveria estar nos headers HTTP; headers={headers}"
    )
