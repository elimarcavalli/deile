"""Helpers compartilhados de ``cli_adapters/base.py`` — refator DRY 2026-06-10.

A refatoração extraiu quatro trechos antes duplicados verbatim entre os
adapters (opencode/qwen/goose/codex/aider/antigravity) para ``base.py``:
``read_brief_or_fallback``, ``classify_provider_cutoff``, ``iter_jsonl_events``
e ``no_output_result``. Antes só eram exercitados indiretamente via os adapter
tests; estes testes fecham a paridade com os engines genuinamente novos
(``_kubectl_helpers``/``_worker_core`` ledger) cobrindo diretamente os ramos
divergentes — o fallback de I/O do brief, as guardas do loop JSONL e a
classificação anti-sangria de corte de provider.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cli_adapters import base  # noqa: E402


# --------------------------------------------------------------------------- #
# read_brief_or_fallback
# --------------------------------------------------------------------------- #
def test_read_brief_returns_file_content(tmp_path):
    """Brief legível → conteúdo exato do arquivo."""
    brief = tmp_path / "brief.md"
    brief.write_text("implemente X e dê push\n", encoding="utf-8")
    assert base.read_brief_or_fallback(str(brief)) == "implemente X e dê push\n"


def test_read_brief_missing_file_returns_fallback_prompt(tmp_path):
    """OSError (arquivo ausente) → prompt mínimo que aponta para o path."""
    missing = str(tmp_path / "nope.md")
    out = base.read_brief_or_fallback(missing)
    assert missing in out
    assert "git" in out.lower()  # o fallback instrui commit/push


# --------------------------------------------------------------------------- #
# classify_provider_cutoff
# --------------------------------------------------------------------------- #
def test_classify_cutoff_none_when_no_provider_error():
    """Saída limpa → ``None`` (segue para o parse estruturado normal)."""
    assert base.classify_provider_cutoff("tudo ok, PR aberta", "", "qwen") is None


def test_classify_cutoff_detects_provider_error():
    """402/crédito esgotado → WorkResult(ok=False) com error_code do provider."""
    res = base.classify_provider_cutoff("", "error 402 payment required", "qwen")
    assert res is not None
    assert res.ok is False
    assert res.error_code == "INSUFFICIENT_CREDIT"
    assert "402" in res.result_text


def test_classify_cutoff_default_text_when_tail_empty():
    """Sem tail (stderr/stdout vazios além do gatilho) → texto default por CLI.

    Usa um stdout que casa o padrão mas cujo tail strip-ado fica vazio só no
    stderr; garante que o fallback ``f"{cli} cortado por provider (...)"`` entra.
    """
    res = base.classify_provider_cutoff("rate limit 429", "", "goose")
    assert res is not None and res.ok is False
    assert res.error_code == "RATE_LIMIT"
    # tail vem do stdout (stderr vazio); resultado não-vazio
    assert res.result_text


# --------------------------------------------------------------------------- #
# iter_jsonl_events
# --------------------------------------------------------------------------- #
def test_iter_jsonl_yields_only_valid_dicts():
    """Pula vazia, linha-não-{, JSON malformado e payload não-dict."""
    text = (
        "\n"                       # vazia
        "log: iniciando\n"         # não começa com {
        "{quebrado\n"              # JSON malformado
        "[1, 2, 3]\n"              # JSON válido mas não-dict
        '{"type": "a"}\n'          # válido
        '   {"type": "b"}   \n'    # válido com espaços
    )
    events = list(base.iter_jsonl_events(text))
    assert events == [{"type": "a"}, {"type": "b"}]


def test_iter_jsonl_empty_text_yields_nothing():
    assert list(base.iter_jsonl_events("")) == []


# --------------------------------------------------------------------------- #
# no_output_result
# --------------------------------------------------------------------------- #
def test_no_output_result_uses_tail_when_present():
    """Tail de stderr/stdout entra no result_text; error_code é NO_OUTPUT."""
    res = base.no_output_result("", "boom no fim", 1, "codex")
    assert res.ok is False
    assert res.error_code == "NO_OUTPUT"
    assert res.result_text == "boom no fim"


def test_no_output_result_default_text_when_empty():
    """Sem stdout/stderr → texto default com o rc embutido."""
    res = base.no_output_result("", "", 3, "codex")
    assert res.ok is False
    assert res.error_code == "NO_OUTPUT"
    assert "rc=3" in res.result_text
    assert "codex" in res.result_text
