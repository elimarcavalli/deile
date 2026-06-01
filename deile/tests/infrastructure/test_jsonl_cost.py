"""Testes do módulo compartilhado ``infra/k8s/jsonl_cost.py`` (issue #445).

Fonte única da lógica de custo do claude-worker. Cobre:
- ``aggregate_jsonl``: dedup por ``(message.id, requestId)``, soma de tokens
  por modelo, breakdown de cache 5m/1h, fallback sem breakdown, registros
  assistant sem id/requestId, e filtro de sessões sem tokens.
- ``cost_of_model`` / ``nocache_cost_of_model`` / ``pricing_for``: paridade
  com os números provados em produção (golden determinístico).
- Paridade: ``aggregate_jsonl`` reproduz exatamente a agregação documentada
  do parser in-pod (dedup + soma), garantindo que o custo colhido para o
  ledger é idêntico ao do caminho live.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"


@pytest.fixture
def jc():
    """Carrega ``jsonl_cost`` dinamicamente (infra/k8s não é package)."""
    spec = importlib.util.spec_from_file_location(
        "jsonl_cost_test", str(_INFRA_K8S / "jsonl_cost.py"),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _assistant(model, mid, rid, usage):
    return {
        "type": "assistant",
        "requestId": rid,
        "timestamp": "2026-06-01T10:00:00.000Z",
        "message": {"id": mid, "role": "assistant", "model": model, "usage": usage},
    }


def _write_session(path: Path, records):
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8",
    )


@pytest.fixture
def golden_session(tmp_path):
    """JSONL determinístico exercitando todos os ramos da agregação."""
    f = tmp_path / "sess-abc123.jsonl"
    rec_a = _assistant(
        "claude-opus-4-5-20260101", "m1", "r1",
        {
            "input_tokens": 100, "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 300,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 150,
                "ephemeral_1h_input_tokens": 50,
            },
        },
    )
    records = [
        {"type": "user", "timestamp": "2026-06-01T09:59:00.000Z",
         "message": {"role": "user", "content": "oi"}},
        rec_a,
        # duplicata de streaming: MESMO (id, requestId) → deve ser deduplicada.
        dict(rec_a),
        _assistant(
            "claude-sonnet-4-5-20260101", "m2", "r2",
            {"input_tokens": 10, "output_tokens": 5,
             "cache_creation_input_tokens": 20, "cache_read_input_tokens": 0},
        ),
        # assistant sem id/requestId mas com tokens → conta uma vez.
        _assistant(
            "claude-haiku-4-5", None, None,
            {"input_tokens": 7, "output_tokens": 3},
        ),
    ]
    # remove id/requestId do último para simular o caso (None, None)
    records[-1]["requestId"] = None
    records[-1]["message"]["id"] = None
    _write_session(f, records)
    return f


def test_aggregate_dedup_and_sums(jc, golden_session):
    agg = jc.aggregate_jsonl(str(golden_session))
    assert agg["session_id"] == "sess-abc123"
    assert agg["assistant_rounds"] == 3  # A (dedup), B, noid
    m = agg["models"]
    assert m["claude-opus-4-5-20260101"] == {
        "in": 100, "out": 50, "cc": 200, "cr": 300, "cc_5m": 150, "cc_1h": 50,
    }
    assert m["claude-sonnet-4-5-20260101"] == {
        "in": 10, "out": 5, "cc": 20, "cr": 0, "cc_5m": 0, "cc_1h": 0,
    }
    assert m["claude-haiku-4-5"]["in"] == 7
    assert agg["first_ts"] == "2026-06-01T09:59:00.000Z"
    assert agg["last_ts"] == "2026-06-01T10:00:00.000Z"


def test_aggregate_skips_sessions_without_tokens(jc, tmp_path):
    f = tmp_path / "empty.jsonl"
    _write_session(f, [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])
    agg = jc.aggregate_jsonl(str(f))
    assert agg["models"] == {}
    assert agg["assistant_rounds"] == 0


def test_aggregate_tolerates_malformed_lines(jc, tmp_path):
    f = tmp_path / "bad.jsonl"
    f.write_text(
        "{not json\n"
        + json.dumps(_assistant("claude-opus-4-5", "m1", "r1",
                                 {"input_tokens": 5, "output_tokens": 2})) + "\n"
        + "\n",  # linha vazia
        encoding="utf-8",
    )
    agg = jc.aggregate_jsonl(str(f))
    assert agg["models"]["claude-opus-4-5"]["in"] == 5


def test_cost_of_model_golden(jc, golden_session):
    agg = jc.aggregate_jsonl(str(golden_session))
    opus = agg["models"]["claude-opus-4-5-20260101"]
    sonnet = agg["models"]["claude-sonnet-4-5-20260101"]
    # opus 4.5 = pricing novo {in5,out25,w5 6.25,w1h10,read0.5}
    assert jc.cost_of_model(opus, "claude-opus-4-5-20260101") == pytest.approx(
        (100 * 5 + 50 * 25 + 150 * 6.25 + 50 * 10 + 300 * 0.5) / 1_000_000.0
    )
    # sonnet: cc=20 sem breakdown → tratado como w5
    assert jc.cost_of_model(sonnet, "claude-sonnet-4-5-20260101") == pytest.approx(
        (10 * 3 + 5 * 15 + 20 * 3.75) / 1_000_000.0
    )


def test_pricing_opus_legacy_vs_new(jc):
    # opus 4.0 e 4.1 = legado ($15/$75); 4.5 = novo ($5/$25)
    assert jc.pricing_for("claude-opus-4-20250514")["in"] == 15.0
    assert jc.pricing_for("claude-opus-4-1-20250805")["in"] == 15.0
    assert jc.pricing_for("claude-opus-4-5-20260101")["in"] == 5.0


def test_nocache_cost(jc):
    tk = {"in": 100, "out": 50, "cc": 200, "cr": 300}
    # sem cache: todo input (in+cc+cr) a preço cheio de input
    assert jc.nocache_cost_of_model(tk, "claude-opus-4-5") == pytest.approx(
        ((100 + 200 + 300) * 5 + 50 * 25) / 1_000_000.0
    )


def test_aggregate_parity_with_inpod_reference(jc, golden_session):
    """``aggregate_jsonl`` deve reproduzir a agregação documentada do parser
    in-pod (dedup por (id, requestId) + soma). Referência mínima inline."""
    seen = set()
    noid = [0]
    models: dict = {}
    rounds = 0
    with open(golden_session, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            msg = o.get("message")
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            rkey = (msg.get("id"), o.get("requestId"))
            if rkey == (None, None):
                noid[0] += 1
                rkey = ("__noid__", noid[0])
            if rkey in seen:
                continue
            seen.add(rkey)
            rounds += 1
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            model = msg.get("model") or "unknown"
            mm = models.setdefault(
                model, {"in": 0, "out": 0, "cc": 0, "cr": 0, "cc_5m": 0, "cc_1h": 0})
            mm["in"] += u.get("input_tokens", 0) or 0
            mm["out"] += u.get("output_tokens", 0) or 0
            cc = u.get("cache_creation_input_tokens", 0) or 0
            mm["cc"] += cc
            mm["cr"] += u.get("cache_read_input_tokens", 0) or 0
            ccd = u.get("cache_creation")
            if isinstance(ccd, dict):
                mm["cc_5m"] += ccd.get("ephemeral_5m_input_tokens", 0) or 0
                mm["cc_1h"] += ccd.get("ephemeral_1h_input_tokens", 0) or 0

    agg = jc.aggregate_jsonl(str(golden_session))
    assert agg["models"] == models
    assert agg["assistant_rounds"] == rounds


# --------------------------------------------------------------------------- #
# summarize_jsonl — extrator rico do harvest do ledger (issue #445 parte 2)    #
# --------------------------------------------------------------------------- #
def test_aggregate_is_subset_of_summarize(jc, golden_session):
    """``aggregate_jsonl`` é exatamente o subset de custo de ``summarize_jsonl``
    — mesmos models/rounds/timestamps (paridade preservada no refactor)."""
    agg = jc.aggregate_jsonl(str(golden_session))
    summ = jc.summarize_jsonl(str(golden_session))
    assert agg["session_id"] == summ["session_id"]
    assert agg["models"] == summ["models"]
    assert agg["assistant_rounds"] == summ["assistant_rounds"]
    assert agg["first_ts"] == summ["first_ts"]
    assert agg["last_ts"] == summ["last_ts"]


def test_summarize_extracts_full_detail(jc, tmp_path):
    """``summarize_jsonl`` colhe TUDO que a tela de tokens mostra no detalhe."""
    f = tmp_path / "sess-detail.jsonl"
    records = [
        {"type": "user", "timestamp": "2026-06-01T09:00:00.000Z",
         "cwd": "/home/claude/work/abc/repo", "gitBranch": "auto/issue-9",
         "version": "2.1.158", "permissionMode": "bypassPermissions",
         "entrypoint": "cli", "aiTitle": "Corrige bug X",
         "prNumber": 42, "prUrl": "https://github.com/o/r/pull/42",
         "prRepository": "o/r",
         "message": {"role": "user", "content": "implementa a issue 9 por favor"}},
        {"type": "assistant", "requestId": "r1",
         "timestamp": "2026-06-01T09:01:00.000Z",
         "message": {"id": "m1", "role": "assistant",
                     "model": "claude-opus-4-5-20260101", "stop_reason": "tool_use",
                     "content": [
                         {"type": "text", "text": "vou ler o arquivo"},
                         {"type": "tool_use", "name": "Read"},
                         {"type": "tool_use", "name": "Edit"},
                     ],
                     "usage": {"input_tokens": 100, "output_tokens": 50}}},
        # tool result com erro → conta tool_error
        {"type": "user", "timestamp": "2026-06-01T09:02:00.000Z",
         "toolUseResult": {"is_error": True},
         "message": {"role": "user", "content": "erro!"}},
        {"type": "assistant", "requestId": "r2",
         "timestamp": "2026-06-01T09:03:00.000Z",
         "message": {"id": "m2", "role": "assistant",
                     "model": "claude-opus-4-5-20260101", "stop_reason": "end_turn",
                     "content": [{"type": "tool_use", "name": "Read"}],
                     "usage": {"input_tokens": 10, "output_tokens": 5}}},
    ]
    _write_session(f, records)
    s = jc.summarize_jsonl(str(f))

    assert s["session_id"] == "sess-detail"
    assert s["cwd"] == "/home/claude/work/abc/repo"
    assert s["git_branch"] == "auto/issue-9"
    assert s["version"] == "2.1.158"
    assert s["permission_mode"] == "bypassPermissions"
    assert s["entrypoint"] == "cli"
    assert s["ai_title"] == "Corrige bug X"
    assert s["pr_number"] == 42
    assert s["pr_url"] == "https://github.com/o/r/pull/42"
    assert s["pr_repo"] == "o/r"
    assert s["brief"] == "implementa a issue 9 por favor"  # 1ª msg user
    assert s["user_msgs"] == 2
    assert s["assistant_rounds"] == 2
    assert s["tool_calls"] == 3
    assert s["tools"] == {"Read": 2, "Edit": 1}
    assert s["stop_reasons"] == {"tool_use": 1, "end_turn": 1}
    assert s["errors"]["tool_error"] == 1
    assert s["models"]["claude-opus-4-5-20260101"]["in"] == 110


def test_summarize_counts_synthetic_and_max_tokens(jc, tmp_path):
    f = tmp_path / "sess-err.jsonl"
    records = [
        {"type": "assistant", "requestId": "r1",
         "message": {"id": "m1", "role": "assistant", "model": "<synthetic>",
                     "stop_reason": "max_tokens",
                     "content": "prompt is too long: blah",
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    _write_session(f, records)
    s = jc.summarize_jsonl(str(f))
    assert s["errors"]["synthetic"] == 1
    assert s["errors"]["max_tokens"] == 1
    assert s["errors"]["api_error"] == 1  # "prompt is too long"
    assert s["stop_reasons"] == {"max_tokens": 1}


def test_summarize_brief_capped(jc, tmp_path):
    f = tmp_path / "sess-big.jsonl"
    big = "x" * 9000
    _write_session(f, [
        {"type": "user", "message": {"role": "user", "content": big}},
        {"type": "assistant", "requestId": "r1",
         "message": {"id": "m1", "role": "assistant", "model": "claude-opus-4-5",
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ])
    s = jc.summarize_jsonl(str(f))
    assert s["brief"] is not None
    assert len(s["brief"]) == jc._BRIEF_CAP  # capado em 4000
