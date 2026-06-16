"""Engine compartilhada do cost-ledger JSONL extraída para ``_worker_core``.

A refatoração DRY (daily 2026-06-10) moveu o I/O do cost-ledger — antes
duplicado entre ``cli_worker_server`` (dedup por ``task_id``) e
``claude_worker_server`` (dedup por ``session_id``) — para
``_worker_core.ledger_harvested_ids`` / ``ledger_append_record``. Cada server
preserva seu PATH e sua CHAVE de dedup; o engine de I/O é o mesmo.

Dado o histórico do incidente do ledger (#445 — 337 transcripts apagados por
podar ANTES de colher), estes testes blindam o roundtrip harvest→append→dedup,
a tolerância a corrupção parcial e a fidelidade do ``ensure_ascii`` por-server.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _worker_core as core  # noqa: E402


# --------------------------------------------------------------------------- #
# ledger_harvested_ids
# --------------------------------------------------------------------------- #
def test_harvested_ids_missing_file_returns_empty_set(tmp_path):
    """Ledger inexistente → ``set()`` (sem raise) — caso do primeiro harvest."""
    assert core.ledger_harvested_ids(tmp_path / "nope.jsonl", key="task_id") == set()


def test_harvested_ids_reads_key_per_server(tmp_path):
    """A CHAVE de dedup é parametrizada (task_id para cli, session_id p/ claude)."""
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"task_id": "t1", "session_id": "s1"})
        + "\n"
        + json.dumps({"task_id": "t2", "session_id": "s2"})
        + "\n"
    )
    assert core.ledger_harvested_ids(ledger, key="task_id") == {"t1", "t2"}
    assert core.ledger_harvested_ids(ledger, key="session_id") == {"s1", "s2"}


def test_harvested_ids_tolerates_partial_corruption(tmp_path):
    """Linhas vazias, JSON malformado, não-dict e sem-a-chave são puladas."""
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        "\n"  # vazia
        + "{not valid json\n"  # malformada
        + "[1, 2, 3]\n"  # JSON válido mas não-dict
        + json.dumps({"other": "x"})
        + "\n"  # dict sem a chave
        + json.dumps({"task_id": ""})
        + "\n"  # chave falsy → ignorada
        + json.dumps({"task_id": "ok"})
        + "\n"  # único válido
    )
    assert core.ledger_harvested_ids(ledger, key="task_id") == {"ok"}


# --------------------------------------------------------------------------- #
# ledger_append_record
# --------------------------------------------------------------------------- #
def test_append_creates_parent_and_returns_byte_count(tmp_path):
    """Cria diretório-pai inexistente e retorna os bytes escritos (utf-8)."""
    ledger = tmp_path / "sub" / "dir" / "ledger.jsonl"
    written = core.ledger_append_record(ledger, {"task_id": "t1", "cost": 0.5})
    assert ledger.exists()
    assert written == len(ledger.read_bytes())


def test_append_is_append_only_and_roundtrips_with_harvest(tmp_path):
    """append+append → dedup vê os dois ids; nada é sobrescrito."""
    ledger = tmp_path / "ledger.jsonl"
    core.ledger_append_record(ledger, {"task_id": "t1"})
    core.ledger_append_record(ledger, {"task_id": "t2"})
    assert core.ledger_harvested_ids(ledger, key="task_id") == {"t1", "t2"}
    assert len(ledger.read_text().splitlines()) == 2


def test_append_ensure_ascii_fidelity_per_server(tmp_path):
    """cli usa ensure_ascii=False (emoji legível); claude usa True (escapado)."""
    cli_ledger = tmp_path / "cli.jsonl"
    claude_ledger = tmp_path / "claude.jsonl"
    record = {"task_id": "t1", "msg": "café ✅"}

    core.ledger_append_record(cli_ledger, record, ensure_ascii=False)
    core.ledger_append_record(claude_ledger, record, ensure_ascii=True)

    assert "café ✅" in cli_ledger.read_text()
    claude_raw = claude_ledger.read_text()
    assert "café" not in claude_raw and "\\u" in claude_raw

    # Ambos round-trip para o MESMO objeto apesar do escaping divergente.
    assert json.loads(cli_ledger.read_text()) == json.loads(claude_raw) == record
