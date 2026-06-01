"""O ``session_tokens_audit`` deve ler o ledger de custo (issue #445).

Sessões já podadas do disco vivem só no ledger. O ``IN_POD_PARSER`` (que roda
dentro do pod) precisa emitir um registro sintético por sessão colhida, com a
MESMA estrutura ``models`` — para o custo histórico sobreviver à poda.

Estes testes executam o ``IN_POD_PARSER`` num subprocess (como o audit faz no
pod), apontando o ledger via ``DEILE_CLAUDE_COST_LEDGER_PATH``, e conferem que:
- a sessão colhida aparece marcada ``harvested`` com os tokens corretos;
- o custo calculado pela função compartilhada bate com o esperado.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
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
    return _load("audit_ledger_test", "session_tokens_audit.py")


@pytest.fixture
def jc():
    return _load("jc_ledger_test", "jsonl_cost.py")


def _run_parser(parser_src: str, ledger_path: Path) -> list:
    proc = subprocess.run(
        [sys.executable, "-"],
        input=parser_src,
        capture_output=True, text=True,
        env={
            "DEILE_CLAUDE_COST_LEDGER_PATH": str(ledger_path),
            "AUDIT_NO_GIT": "1",
            "HOME": str(ledger_path.parent.parent),
            "PATH": "/usr/bin:/bin",
        },
    )
    assert proc.returncode == 0, proc.stderr
    raw = proc.stdout.strip()
    for ln in reversed(raw.splitlines()):
        ln = ln.strip()
        if ln.startswith("["):
            return json.loads(ln)
    raise AssertionError(f"saída não é JSON: {raw[:500]}")


def test_parser_emits_harvested_session(audit, jc, tmp_path):
    ledger = tmp_path / ".claude" / "cost-ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    models = {"claude-opus-4-5-20260101": {
        "in": 100, "out": 50, "cc": 200, "cr": 300, "cc_5m": 150, "cc_1h": 50}}
    ledger.write_text(json.dumps({
        "v": 1, "session_id": "sess-harvested-1", "task_id": "aaaaaaaaaaaa0001",
        "models": models, "first_ts": "2026-06-01T10:00:00.000Z",
        "last_ts": "2026-06-01T10:05:00.000Z", "assistant_rounds": 3,
        "harvested_at": 1_780_000_000.0,
    }) + "\n", encoding="utf-8")

    sessions = _run_parser(audit.IN_POD_PARSER, ledger)
    harvested = [s for s in sessions if s.get("session_file") == "sess-harvested-1.jsonl"]
    assert len(harvested) == 1
    s = harvested[0]
    assert s["harvested"] is True
    assert s["models"] == models
    # custo idêntico ao do JSONL vivo (mesma função compartilhada)
    expected = jc.cost_of_model(models["claude-opus-4-5-20260101"],
                                "claude-opus-4-5-20260101")
    assert expected == pytest.approx(
        (100 * 5 + 50 * 25 + 150 * 6.25 + 50 * 10 + 300 * 0.5) / 1_000_000.0)


def test_parser_dedups_ledger_session_ids(audit, tmp_path):
    ledger = tmp_path / ".claude" / "cost-ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    rec = {
        "v": 1, "session_id": "dup-1", "task_id": "bbbbbbbbbbbb0002",
        "models": {"claude-sonnet-4-5": {"in": 10, "out": 5, "cc": 0,
                                         "cr": 0, "cc_5m": 0, "cc_1h": 0}},
        "first_ts": None, "last_ts": None, "assistant_rounds": 1,
        "harvested_at": 1_780_000_000.0,
    }
    # duas linhas com o mesmo session_id (não deve duplicar na saída)
    ledger.write_text(json.dumps(rec) + "\n" + json.dumps(rec) + "\n",
                      encoding="utf-8")
    sessions = _run_parser(audit.IN_POD_PARSER, ledger)
    dups = [s for s in sessions if s.get("session_file") == "dup-1.jsonl"]
    assert len(dups) == 1


def test_audit_cost_functions_come_from_shared_module(audit, jc):
    # fonte única: o audit importa as funções de custo do jsonl_cost (não
    # define cópia local). Prova estrutural via __module__ + equivalência.
    assert audit.cost_of_model.__module__ == "jsonl_cost"
    assert audit.nocache_cost_of_model.__module__ == "jsonl_cost"
    tk = {"in": 100, "out": 50, "cc": 200, "cr": 300, "cc_5m": 150, "cc_1h": 50}
    assert audit.cost_of_model(tk, "claude-opus-4-5") == jc.cost_of_model(
        tk, "claude-opus-4-5")
