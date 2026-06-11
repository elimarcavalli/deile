"""Persistência central do custo da frota CLI no UsageRepository (issue #638).

A frota CLI tinha custo só no PVC (``.progress`` + ledger por-worker), que some
quando o pod escala a zero / é ``force-delete``. Agora o pipeline faz PUSH
central: lê o bloco ``usage`` estruturado da resposta do ``/v1/dispatch`` e grava
1 :class:`UsageRecord` por modelo no SQLite central.

DECISÃO DE SCHEMA (sem migração): worker→``provider_id``, stage→``tier``,
channel_id→``session_id``, modelo→``model_id``. Um dispatch multi-modelo vira N
registros. Preço pela fonte única ``jsonl_cost.fleet_cost_of_model``.

Cobertura:
  1. Caminho ``wait`` grava N registros com o schema correto.
  2. Multi-modelo → N linhas; sem tokens → 0 linhas.
  3. ``model`` correto sem ``unknown`` (cai no ``cli_model``).
  4. Custo via fonte única de preço (sem duplicar tabela).
  5. Best-effort: response inválido / repo que explode → 0, sem propagar.
  6. Caminho fire-and-forget (resume-info) com dedup por task_id.
"""

from __future__ import annotations

import pytest

from deile.orchestration.pipeline.fleet_cost_recorder import (
    record_fleet_usage, record_fleet_usage_from_resume_info)
from deile.storage.usage_repository import UsageRepository


@pytest.fixture()
def repo(tmp_path):
    return UsageRepository(db_path=tmp_path / "usage.db")


def _all_records(repo: UsageRepository) -> list:
    with repo._connect() as conn:  # noqa: SLF001 — leitura direta no teste
        rows = conn.execute("SELECT * FROM usage_records ORDER BY id").fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 1-4. Caminho wait: schema, multi-modelo, model correto, preço único          #
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_wait_path_writes_one_record_with_correct_schema(repo):
    response = {
        "ok": True,
        "usage": {
            "worker": "opencode",
            "model": "deepseek/deepseek-v4-pro",
            "tokens_by_model": {
                "deepseek/deepseek-v4-pro": {
                    "in": 1500, "out": 300, "cache_read": 21415, "cache_write": 100,
                },
            },
        },
    }
    n = record_fleet_usage(
        response, worker_kind="opencode", stage="implement",
        channel_id="pipeline-issue-242", cli_model="deepseek/deepseek-v4-pro",
        repo=repo,
    )
    assert n == 1
    recs = _all_records(repo)
    assert len(recs) == 1
    r = recs[0]
    # DECISÃO DE SCHEMA: worker→provider_id, stage→tier, channel→session_id.
    assert r["provider_id"] == "opencode"
    assert r["tier"] == "implement"
    assert r["session_id"] == "pipeline-issue-242"
    assert r["model_id"] == "deepseek/deepseek-v4-pro"
    assert r["prompt_tokens"] == 1500
    assert r["completion_tokens"] == 300
    assert r["cached_tokens"] == 21415
    assert r["total_tokens"] == 1500 + 300 + 21415 + 100
    assert r["success"] == 1
    # Custo > 0 e via fonte única (deepseek-v4-pro = $0.435/MTok input).
    assert r["cost_usd"] > 0


@pytest.mark.unit
def test_cost_uses_single_price_source(repo):
    # 1M tokens de input em deepseek-v4-pro = $0.435 (FLEET_PRICING_BY_SUBSTRING).
    response = {
        "ok": True,
        "usage": {
            "worker": "qwen", "model": "deepseek-v4-pro",
            "tokens_by_model": {"deepseek-v4-pro": {"in": 1_000_000, "out": 0}},
        },
    }
    record_fleet_usage(
        response, worker_kind="qwen", stage="pr_review",
        channel_id="pipeline-pr-9", cli_model="deepseek-v4-pro", repo=repo,
    )
    r = _all_records(repo)[0]
    assert abs(r["cost_usd"] - 0.435) < 1e-6


@pytest.mark.unit
def test_multi_model_dispatch_writes_n_records(repo):
    response = {
        "ok": True,
        "usage": {
            "worker": "codex", "model": "gpt-5.1-codex",
            "tokens_by_model": {
                "gpt-5.1-codex": {"in": 1000, "out": 200},
                "gpt-5.1-codex-mini": {"in": 500, "out": 50},
            },
        },
    }
    n = record_fleet_usage(
        response, worker_kind="codex", stage="implement",
        channel_id="pipeline-issue-1", cli_model="gpt-5.1-codex", repo=repo,
    )
    assert n == 2
    models = {r["model_id"] for r in _all_records(repo)}
    assert models == {"gpt-5.1-codex", "gpt-5.1-codex-mini"}


@pytest.mark.unit
def test_model_unknown_falls_back_to_cli_model(repo):
    # goose/aider emitem tokens sob "unknown"; o server já remapeia, mas se vier
    # "unknown" no wire (worker antigo), o recorder cai no cli_model do payload.
    response = {
        "ok": True,
        "usage": {
            "worker": "goose", "model": "",
            "tokens_by_model": {"unknown": {"in": 2000, "out": 6000}},
        },
    }
    record_fleet_usage(
        response, worker_kind="goose", stage="implement",
        channel_id="pipeline-issue-7", cli_model="deepseek/deepseek-v4-flash",
        repo=repo,
    )
    r = _all_records(repo)[0]
    assert r["model_id"] == "deepseek/deepseek-v4-flash"  # sem "unknown"


@pytest.mark.unit
def test_zero_token_models_are_skipped(repo):
    response = {
        "ok": True,
        "usage": {
            "worker": "qwen", "model": "qwen3-coder-plus",
            "tokens_by_model": {"qwen3-coder-plus": {"in": 0, "out": 0}},
        },
    }
    assert record_fleet_usage(
        response, worker_kind="qwen", stage="classify",
        channel_id="pipeline-issue-3", cli_model="qwen3-coder-plus", repo=repo,
    ) == 0
    assert _all_records(repo) == []


@pytest.mark.unit
def test_worker_from_usage_block_prevails_over_kwarg(repo):
    # O server é a autoridade do worker; o derivado da URL é só fallback.
    response = {
        "ok": True,
        "usage": {
            "worker": "opencode", "model": "x/y",
            "tokens_by_model": {"x/y": {"in": 10, "out": 5}},
        },
    }
    record_fleet_usage(
        response, worker_kind="WRONG", stage="implement",
        channel_id="c", cli_model=None, repo=repo,
    )
    assert _all_records(repo)[0]["provider_id"] == "opencode"


# --------------------------------------------------------------------------- #
# 5. Best-effort: nunca propaga, nunca derruba o dispatch                       #
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_no_usage_block_is_noop(repo):
    assert record_fleet_usage(
        {"ok": True}, worker_kind="opencode", stage="implement",
        channel_id="c", cli_model=None, repo=repo,
    ) == 0
    assert record_fleet_usage(
        "not a dict", worker_kind="opencode", stage="implement",
        channel_id="c", cli_model=None, repo=repo,
    ) == 0


@pytest.mark.unit
def test_repo_failure_is_swallowed(repo):
    class _BoomRepo:
        def record(self, *_a, **_k):
            raise RuntimeError("disk full")

    response = {
        "ok": True,
        "usage": {"worker": "opencode", "model": "x/y",
                  "tokens_by_model": {"x/y": {"in": 1, "out": 1}}},
    }
    # NÃO deve propagar — retorna 0.
    assert record_fleet_usage(
        response, worker_kind="opencode", stage="implement",
        channel_id="c", cli_model=None, repo=_BoomRepo(),
    ) == 0


# --------------------------------------------------------------------------- #
# 6. Fire-and-forget (resume-info) com dedup por task_id                        #
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_resume_info_path_writes_and_dedups(repo):
    info = {
        "last_is_error": False,
        "cli_model": "qwen3-coder-plus",
        "usage": {
            "model": "qwen3-coder-plus",
            "tokens_by_model": {"qwen3-coder-plus": {"in": 4000, "out": 900}},
        },
    }
    n1 = record_fleet_usage_from_resume_info(
        info, worker_kind="qwen", stage="implement",
        channel_id="pipeline-issue-50", task_id="abc123", repo=repo,
    )
    assert n1 == 1
    r = _all_records(repo)[0]
    # session_id dedupável = <channel>#<task_id>.
    assert r["session_id"] == "pipeline-issue-50#abc123"
    assert r["provider_id"] == "qwen" and r["tier"] == "implement"
    # Segunda chamada (próximo tick) → no-op idempotente.
    n2 = record_fleet_usage_from_resume_info(
        info, worker_kind="qwen", stage="implement",
        channel_id="pipeline-issue-50", task_id="abc123", repo=repo,
    )
    assert n2 == 0
    assert len(_all_records(repo)) == 1


@pytest.mark.unit
def test_resume_info_without_usage_is_noop(repo):
    # claude/deile resume-info não trazem bloco usage → 0.
    assert record_fleet_usage_from_resume_info(
        {"last_is_error": False}, worker_kind="claude", stage="implement",
        channel_id="pipeline-issue-9", task_id="t", repo=repo,
    ) == 0
