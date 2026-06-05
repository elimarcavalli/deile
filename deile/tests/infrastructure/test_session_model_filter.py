"""Testes da flag ``--model <SLUG>`` do ``session_tokens_audit`` (issue #532).

O filtro ``filter_sessions_by_model`` é a única fonte da verdade do recorte por
modelo: aplicado uma vez (antes de ``--top``/``--last``), vale para tabela,
agregados, detail, export e loop interativo. Estes testes provam as decisões de
design D1–D5 e os critérios de aceite AC2–AC7 sem precisar de cluster, carregando
o módulo via o padrão ``importlib`` já usado em ``test_audit_ledger_read.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(_INFRA_K8S / filename))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def audit():
    if str(_INFRA_K8S) not in sys.path:
        sys.path.insert(0, str(_INFRA_K8S))
    return _load("audit_model_filter_test", "session_tokens_audit.py")


def _sess(per_model_keys):
    """Sessão mínima com as chaves de ``per_model`` dadas."""
    return {"per_model": {k: {"input": 1, "output": 1} for k in per_model_keys}}


def test_family_match(audit):
    """D1/AC3: ``opus`` mantém um id opus completo; ``sonnet`` o descarta."""
    s = _sess(["claude-opus-4-8-20250514"])
    assert audit.filter_sessions_by_model([s], "opus") == [s]
    assert audit.filter_sessions_by_model([s], "sonnet") == []


def test_version_match(audit):
    """D1: o slug versionado casa contra o id completo; versão errada descarta."""
    s = _sess(["claude-opus-4-8-20250514"])
    assert audit.filter_sessions_by_model([s], "opus-4-8") == [s]
    assert audit.filter_sessions_by_model([s], "opus-4-7") == []


def test_case_insensitive(audit):
    """O match ignora caixa (``OPUS`` casa ``claude-opus-...``)."""
    s = _sess(["claude-opus-4-8-20250514"])
    assert audit.filter_sessions_by_model([s], "OPUS") == [s]


def test_multimodel_any(audit):
    """D2/AC4: sessão com ids opus+haiku é mantida por ``haiku`` E por ``opus``."""
    s = _sess(["claude-opus-4-8-20250514", "claude-haiku-4-5-20251001"])
    assert audit.filter_sessions_by_model([s], "opus") == [s]
    assert audit.filter_sessions_by_model([s], "haiku") == [s]


def test_synthetic_excluded(audit):
    """A chave sentinela ``<synthetic>`` é excluída do match."""
    s = _sess(["<synthetic>"])
    assert audit.filter_sessions_by_model([s], "synthetic") == []


def test_identity_no_slug(audit):
    """AC7/AC2: ``None`` e ``""`` retornam a lista intacta (mesmos objetos, mesma ordem)."""
    sessions = [_sess(["claude-opus-4-8-20250514"]), _sess(["claude-sonnet-4-6-20250101"])]
    for slug in (None, ""):
        out = audit.filter_sessions_by_model(sessions, slug)
        assert out is sessions or out == sessions
        assert len(out) == len(sessions)
        for a, b in zip(out, sessions):
            assert a is b  # identidade dos objetos-sessão


def test_filter_precedes_top(audit):
    """AC5: o corte ``[:N]`` aplicado APÓS o filtro devolve o top do recorte, não o global."""
    opus_top = _sess(["claude-opus-4-8-20250514"])
    sonnet = _sess(["claude-sonnet-4-6-20250101"])
    opus_2 = _sess(["claude-opus-4-8-20250514"])
    sessions = [sonnet, opus_top, opus_2]  # global top-1 seria a sonnet
    filtered = audit.filter_sessions_by_model(sessions, "opus")
    assert filtered[:1] == [opus_top]  # top-1 do recorte filtrado, não o global


def test_zero_match_exit(audit, monkeypatch):
    """AC6: no caminho não-interativo, filtro vazio chama ``sys.exit`` com code 1."""
    monkeypatch.setattr(sys, "argv", ["session_tokens_audit.py", "--model", "gpt", "--no-interactive"])
    monkeypatch.setattr(audit, "find_kubectl", lambda: "kubectl")
    monkeypatch.setattr(audit, "resolve_pod", lambda *a, **k: "pod-x")
    monkeypatch.setattr(audit, "resolve_pvc", lambda *a, **k: "pvc-x")
    monkeypatch.setattr(audit, "_console", lambda *a, **k: None)
    monkeypatch.setattr(audit, "fetch_sessions", lambda *a, **k: [_sess(["claude-opus-4-8-20250514"])])
    monkeypatch.setattr(audit, "enrich", lambda sessions, pvc: sessions)
    with pytest.raises(SystemExit) as exc:
        audit.main()
    assert exc.value.code == 1 or "Nenhuma sessão com modelo contendo 'gpt'." in str(exc.value.code)
