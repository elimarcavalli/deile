"""Tests para captura/persistência de usage no GeminiProvider.

Cobre o bug histórico em que `gemini_provider.py` nunca chamava
`_record_usage` nem `_record_failed_usage`, deixando a tabela
`usage_records` sem nenhuma entrada de `provider_id='gemini'`. Os fixes
adicionaram dois helpers (`_extract_gemini_usage`, `_aggregate_usage`)
e ligaram o record nos caminhos `generate`, `chat_with_tools` e
`generate_stream`. Estes testes lockam o contrato sem depender do SDK
do Google nem de cluster real.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core.models.base import ModelUsage
from deile.core.models.gemini_provider import GeminiProvider

# -------- helpers de fixture -------------------------------------------------

def _fake_usage_md(prompt=0, candidates=0, total=0, cached=0):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=candidates,
        total_token_count=total,
        cached_content_token_count=cached,
    )


def _fake_response(usage_md=None):
    return SimpleNamespace(usage_metadata=usage_md)


# -------- _extract_gemini_usage ---------------------------------------------

class TestExtractGeminiUsage:
    def test_reads_all_four_token_fields(self):
        prov = MagicMock(spec=GeminiProvider)
        prov._compute_cost = lambda u: 0.0  # ignore cost in this test
        resp = _fake_response(_fake_usage_md(
            prompt=1500, candidates=380, total=1880, cached=200,
        ))
        usage = GeminiProvider._extract_gemini_usage(prov, resp, request_time=2.5)
        assert usage.prompt_tokens == 1500
        assert usage.completion_tokens == 380
        assert usage.total_tokens == 1880
        # cached_content_token_count → cached_tokens (era hardcoded 0)
        assert usage.cached_tokens == 200
        assert usage.request_time == 2.5

    def test_missing_usage_metadata_returns_zeros(self):
        prov = MagicMock(spec=GeminiProvider)
        prov._compute_cost = lambda u: 0.0
        resp = _fake_response(usage_md=None)
        usage = GeminiProvider._extract_gemini_usage(prov, resp)
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.cached_tokens == 0

    def test_total_falls_back_to_sum_when_zero(self):
        prov = MagicMock(spec=GeminiProvider)
        prov._compute_cost = lambda u: 0.0
        resp = _fake_response(_fake_usage_md(prompt=100, candidates=50, total=0))
        usage = GeminiProvider._extract_gemini_usage(prov, resp)
        assert usage.total_tokens == 150     # fallback: prompt + completion

    def test_calls_compute_cost(self):
        prov = MagicMock(spec=GeminiProvider)
        prov._compute_cost = MagicMock(return_value=0.0042)
        resp = _fake_response(_fake_usage_md(prompt=1000, candidates=500, cached=100))
        usage = GeminiProvider._extract_gemini_usage(prov, resp)
        prov._compute_cost.assert_called_once()
        assert usage.cost_estimate == 0.0042

    def test_cost_calc_exception_does_not_propagate(self):
        # Cost calc nunca pode quebrar a request — falha em DEBUG, devolve 0.
        prov = MagicMock(spec=GeminiProvider)
        prov._compute_cost = MagicMock(side_effect=RuntimeError("catalog miss"))
        resp = _fake_response(_fake_usage_md(prompt=10, candidates=5))
        usage = GeminiProvider._extract_gemini_usage(prov, resp)
        assert usage.cost_estimate == 0.0    # default, sem propagar
        assert usage.prompt_tokens == 10     # tokens ainda capturados


# -------- _compute_cost (real method, sem mock) -----------------------------
#
# Regressão: o código chamava ``self._compute_cost(usage)`` mas o método NÃO
# existia na classe (o nome do catálogo é ``estimate_cost``). O ``AttributeError``
# era engolido pelo ``except`` de ``_extract_gemini_usage`` (fail-open de cost),
# então TODA request Gemini reportava ``cost_estimate=0.0`` em silêncio — o exato
# bug que os comentários do módulo afirmavam estar corrigido. Os testes acima
# mockam ``prov._compute_cost`` e por isso nunca exerceram o método real.
# Estes testes constroem um GeminiProvider REAL (sem mock do _compute_cost) e
# travam a presença + a semântica superset-aware do cálculo.

class _FakeHandle:
    def __init__(self, pricing) -> None:
        self.pricing = pricing


def _real_gemini_provider(input_per_1m, output_per_1m, cached_per_1m):
    """GeminiProvider real com pricing stubado — sem init de cliente/SDK."""
    prov = GeminiProvider.__new__(GeminiProvider)
    prov._handle = _FakeHandle(SimpleNamespace(
        input_per_1m_usd=input_per_1m,
        output_per_1m_usd=output_per_1m,
        cached_input_per_1m_usd=cached_per_1m,
    ))
    return prov


class TestComputeCostReal:
    def test_method_exists_on_class(self):
        # Trava o nome do método — um rename quebra o cálculo de custo em silêncio.
        assert hasattr(GeminiProvider, "_compute_cost")

    def test_cost_is_non_zero_for_real_provider(self):
        prov = _real_gemini_provider(2.0, 12.0, 0.5)
        resp = _fake_response(_fake_usage_md(
            prompt=1000, candidates=500, total=1500, cached=100,
        ))
        usage = GeminiProvider._extract_gemini_usage(prov, resp, request_time=1.0)
        # 900 não-cacheado @ $2 + 500 saída @ $12 + 100 cache @ $0.5
        expected = (900 / 1e6) * 2.0 + (500 / 1e6) * 12.0 + (100 / 1e6) * 0.5
        assert usage.cost_estimate == pytest.approx(round(expected, 8))
        assert usage.cost_estimate > 0.0

    def test_cached_tokens_not_double_charged(self):
        # prompt_token_count do Gemini INCLUI cached_content_token_count (docs
        # google-genai): a parcela cacheada não pode ser cobrada duas vezes.
        prov = _real_gemini_provider(5.0, 10.0, 1.0)
        usage = ModelUsage(
            prompt_tokens=1_000_000, completion_tokens=0,
            total_tokens=1_000_000, cached_tokens=700_000,
        )
        # 300k @ $5 + 700k @ $1 = $1.50 + $0.70 = $2.20 (base double-cobraria $5.70)
        assert prov._compute_cost(usage) == pytest.approx(2.20)

    def test_no_pricing_returns_zero(self):
        prov = GeminiProvider.__new__(GeminiProvider)
        prov._handle = None
        usage = ModelUsage(prompt_tokens=1_000_000, cached_tokens=100_000)
        assert prov._compute_cost(usage) == 0.0


# -------- _aggregate_usage --------------------------------------------------

class TestAggregateUsage:
    def test_sums_all_fields(self):
        a = ModelUsage(
            prompt_tokens=100, completion_tokens=50, cached_tokens=10,
            total_tokens=150, request_time=1.0, cost_estimate=0.001,
        )
        b = ModelUsage(
            prompt_tokens=200, completion_tokens=80, cached_tokens=20,
            total_tokens=280, request_time=2.5, cost_estimate=0.002,
        )
        c = GeminiProvider._aggregate_usage(a, b)
        assert c.prompt_tokens == 300
        assert c.completion_tokens == 130
        assert c.cached_tokens == 30
        assert c.total_tokens == 430
        assert c.request_time == 3.5
        assert c.cost_estimate == pytest.approx(0.003)

    def test_aggregate_with_empty_usage_is_idempotent(self):
        empty = ModelUsage()
        other = ModelUsage(
            prompt_tokens=10, completion_tokens=5, cost_estimate=0.001,
        )
        # Empty + outro = outro
        result = GeminiProvider._aggregate_usage(empty, other)
        assert result.prompt_tokens == 10
        assert result.cost_estimate == pytest.approx(0.001)

    def test_aggregate_handles_none_cost(self):
        # Em alguns providers cost_estimate pode vir como None.
        a = ModelUsage(prompt_tokens=10, cost_estimate=None)
        b = ModelUsage(prompt_tokens=20, cost_estimate=0.002)
        c = GeminiProvider._aggregate_usage(a, b)
        assert c.prompt_tokens == 30
        assert c.cost_estimate == pytest.approx(0.002)


# -------- chat_with_tools chama _record_usage --------------------------------

@pytest.mark.asyncio
class TestChatWithToolsPersists:
    async def test_records_usage_on_success(self):
        """Sucesso path: deve chamar _record_usage com session_id + usage agregado."""
        prov = MagicMock(spec=GeminiProvider)
        prov.provider_id = "gemini"
        prov.model_name = "gemini-3.1-pro-preview"
        # _gemini_chat_with_tools devolve 3-tuple agora (text, results, usage).
        agg = ModelUsage(prompt_tokens=500, completion_tokens=200,
                         cached_tokens=50, total_tokens=700,
                         cost_estimate=0.003)
        prov._gemini_chat_with_tools = AsyncMock(
            return_value=("hi", [], agg),
        )
        prov._extract_system = MagicMock(return_value="system")
        prov._messages_to_gemini_user_input = MagicMock(return_value="hello")
        prov.create_chat_session = AsyncMock(return_value=MagicMock())
        prov._chat_sessions = {}
        prov._record_usage = AsyncMock()
        prov._record_failed_usage = AsyncMock()

        text, results, usage = await GeminiProvider.chat_with_tools(
            prov, messages=[], tools=[], system_instruction="sys",
            session_id="sess-42",
        )

        assert text == "hi"
        prov._record_usage.assert_awaited_once()
        kwargs = prov._record_usage.call_args.kwargs
        assert kwargs["session_id"] == "sess-42"
        assert kwargs["usage"] is agg
        assert kwargs["success"] is True
        # error path NÃO foi tocado
        prov._record_failed_usage.assert_not_awaited()

    async def test_records_failed_usage_on_exception(self):
        """Erro path: _gemini_chat_with_tools lança → record_failed_usage + raise."""
        from deile.core.models.errors import ProviderInvocationError

        prov = MagicMock(spec=GeminiProvider)
        prov.provider_id = "gemini"
        prov.model_name = "gemini-3.1-pro-preview"
        prov._gemini_chat_with_tools = AsyncMock(
            side_effect=RuntimeError("API down"),
        )
        prov._extract_system = MagicMock(return_value="system")
        prov._messages_to_gemini_user_input = MagicMock(return_value="hello")
        prov.create_chat_session = AsyncMock(return_value=MagicMock())
        prov._chat_sessions = {}
        prov._record_usage = AsyncMock()
        prov._record_failed_usage = AsyncMock()

        with patch("deile.core.models.gemini_provider.make_gemini_envelope") \
                as mk_env:
            mk_env.return_value = MagicMock()
            with pytest.raises(ProviderInvocationError):
                await GeminiProvider.chat_with_tools(
                    prov, messages=[], tools=[], system_instruction="sys",
                    session_id="sess-99",
                )

        prov._record_failed_usage.assert_awaited_once()
        kwargs = prov._record_failed_usage.call_args.kwargs
        assert kwargs["session_id"] == "sess-99"
        prov._record_usage.assert_not_awaited()


# -------- generate() chama _record_usage no sucesso ------------------------

@pytest.mark.asyncio
class TestGenerateRecords:
    async def test_record_called_with_response_usage(self):
        # Stub mínimo do response retornado por _generate_with_new_sdk
        usage = ModelUsage(prompt_tokens=120, completion_tokens=60,
                           cached_tokens=10, total_tokens=180,
                           cost_estimate=0.0015)
        response = SimpleNamespace(usage=usage, content="ok")
        prov = MagicMock(spec=GeminiProvider)
        prov.provider_id = "gemini"
        prov.model_name = "gemini-3.1-pro-preview"
        prov._compose_system_instruction = MagicMock(return_value="sys")
        prov._process_messages_for_gemini = MagicMock(return_value=[])
        prov._create_generation_config = MagicMock(return_value={})
        prov._generate_with_new_sdk = AsyncMock(return_value=response)
        prov.debug_logger = MagicMock(request_count=0)
        prov._record_usage = AsyncMock()
        prov._last_request_time = 0.0

        with patch("deile.core.models.gemini_provider.is_debug_enabled",
                   return_value=False):
            result = await GeminiProvider.generate(
                prov, messages=[], system_instruction="x", session_id="s1",
            )

        assert result is response
        prov._record_usage.assert_awaited_once()
        kwargs = prov._record_usage.call_args.kwargs
        assert kwargs["session_id"] == "s1"
        assert kwargs["usage"] is usage
        assert kwargs["success"] is True
