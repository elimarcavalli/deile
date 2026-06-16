"""Testes do harvester de custo + poda de JSONL órfão (issue #445).

O cleanup só varria ``/home/claude/work`` e deixava 200+ JSONL órfãos em
``~/.claude/projects`` (85 MB). A correção colhe o custo de cada sessão órfã
para um ledger durável ANTES de podar o transcript volumoso — o custo
histórico sobrevive em escala de KB.

Cobre:
- Harvest + poda de dir órfão (workdir-pai ausente), preservando o ativo.
- Grace period: órfão recém-modificado NÃO é podado (guarda TOCTOU).
- Retenção em dias (default 30d): órfão dentro da janela NÃO é podado, mesmo
  com workdir-pai ausente — o Humano controla o período (issue #445 parte 2).
- Registro RICO (v:2): preserva título/brief/tools/PR/erros/meta, não só
  tokens — a sessão colhida fica idêntica à viva na tela de tokens.
- Idempotência: rodar duas vezes não duplica no ledger.
- ``dry_run``: reporta candidatos sem deletar nem escrever.
- Integração no preview ``_cleanup_scan`` (campo ``orphan_jsonl_dirs`` +
  bytes somados a ``total_candidate_bytes``).
- Fail-safe: sem o extrator (jsonl_cost ausente) a poda é abortada.

Nota: os testes de MECÂNICA passam ``retention_days=0`` para isolar o piso
grace (TOCTOU) do gatilho de retenção — a retenção em si tem testes próprios.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"


@pytest.fixture
def cws():
    # ``infra/k8s`` não é package; in-pod o script roda com seu dir no
    # sys.path (por isso importa ``dispatch_logger``/``jsonl_cost`` siblings).
    # Replicamos isso aqui para o load dinâmico resolver os irmãos isolado.
    if str(_INFRA_K8S) not in sys.path:
        sys.path.insert(0, str(_INFRA_K8S))
    spec = importlib.util.spec_from_file_location(
        "cws_ledger_test",
        str(_INFRA_K8S / "claude_worker_server.py"),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cws_ledger_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


TASK_A = "aaaaaaaaaaaa0001"
TASK_B = "bbbbbbbbbbbb0002"


def _assistant_line(
    model="claude-opus-4-5-20260101", mid="m1", rid="r1", in_tok=100, out_tok=50
):
    return json.dumps(
        {
            "type": "assistant",
            "requestId": rid,
            "timestamp": "2026-06-01T10:00:00.000Z",
            "message": {
                "id": mid,
                "role": "assistant",
                "model": model,
                "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
            },
        }
    )


def _make_project(
    projects_dir: Path,
    task_id: str,
    session_id: str,
    *,
    mtime_age_s: float,
    in_tok=100,
    out_tok=50,
) -> Path:
    pdir = projects_dir / f"-home-claude-work-{task_id}"
    pdir.mkdir(parents=True)
    jsonl = pdir / f"{session_id}.jsonl"
    jsonl.write_text(
        _assistant_line(in_tok=in_tok, out_tok=out_tok) + "\n", encoding="utf-8"
    )
    old = time.time() - mtime_age_s
    import os

    os.utime(jsonl, (old, old))
    os.utime(pdir, (old, old))
    return pdir


@pytest.fixture
def env(tmp_path):
    """Monta HOME falso com projects/ + work/ e devolve os paths."""
    home = tmp_path / "home"
    projects = home / ".claude" / "projects"
    projects.mkdir(parents=True)
    work = home / "work"
    work.mkdir()
    ledger = home / ".claude" / "cost-ledger.jsonl"
    return {"home": home, "projects": projects, "work": work, "ledger": ledger}


def _read_ledger(ledger: Path):
    if not ledger.exists():
        return []
    return [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]


def test_harvest_and_prune_orphan_preserves_active(cws, env):
    # A: órfão (workdir ausente, antigo). B: workdir presente → preservar.
    pdir_a = _make_project(env["projects"], TASK_A, "sess-a", mtime_age_s=7200)
    pdir_b = _make_project(env["projects"], TASK_B, "sess-b", mtime_age_s=7200)
    (env["work"] / TASK_B).mkdir()  # workdir de B existe

    result = cws._harvest_and_prune_orphan_jsonl(
        env["work"],
        projects_dir=env["projects"],
        ledger_path=env["ledger"],
        grace_s=3600,
        retention_days=0,
        now=time.time(),
    )

    assert result["sessions_harvested"] == 1
    assert result["jsonl_dirs_removed"] == 1
    assert not pdir_a.exists()  # órfão podado
    assert pdir_b.exists()  # ativo preservado

    led = _read_ledger(env["ledger"])
    assert len(led) == 1
    rec = led[0]
    assert rec["session_id"] == "sess-a"
    assert rec["task_id"] == TASK_A
    assert rec["models"]["claude-opus-4-5-20260101"]["in"] == 100
    assert rec["models"]["claude-opus-4-5-20260101"]["out"] == 50


def test_grace_period_protects_recent_orphan(cws, env):
    pdir = _make_project(env["projects"], TASK_A, "sess-a", mtime_age_s=60)  # 1min

    result = cws._harvest_and_prune_orphan_jsonl(
        env["work"],
        projects_dir=env["projects"],
        ledger_path=env["ledger"],
        grace_s=3600,
        retention_days=0,
        now=time.time(),
    )

    assert result["sessions_harvested"] == 0
    assert result["jsonl_dirs_removed"] == 0
    assert pdir.exists()  # protegido pelo grace
    assert _read_ledger(env["ledger"]) == []


def test_retention_days_protects_orphan_within_window(cws, env):
    """O gatilho PRINCIPAL: um órfão (workdir-pai ausente) com 5 dias NÃO é
    podado sob retenção de 30d — mesmo muito além do grace de 1h. É o pedido
    do Humano: nunca ceifar sessões com menos de N dias (issue #445)."""
    five_days = 5 * 86400
    pdir = _make_project(env["projects"], TASK_A, "sess-a", mtime_age_s=five_days)

    result = cws._harvest_and_prune_orphan_jsonl(
        env["work"],
        projects_dir=env["projects"],
        ledger_path=env["ledger"],
        grace_s=3600,
        retention_days=30,
        now=time.time(),
    )

    assert result["sessions_harvested"] == 0
    assert result["jsonl_dirs_removed"] == 0
    assert pdir.exists()  # protegido pela retenção 30d
    assert _read_ledger(env["ledger"]) == []


def test_retention_days_prunes_orphan_past_window(cws, env):
    """Além da retenção (40 dias > 30d), o órfão é colhido + podado."""
    forty_days = 40 * 86400
    pdir = _make_project(env["projects"], TASK_A, "sess-a", mtime_age_s=forty_days)

    result = cws._harvest_and_prune_orphan_jsonl(
        env["work"],
        projects_dir=env["projects"],
        ledger_path=env["ledger"],
        grace_s=3600,
        retention_days=30,
        now=time.time(),
    )

    assert result["sessions_harvested"] == 1
    assert result["jsonl_dirs_removed"] == 1
    assert not pdir.exists()
    assert len(_read_ledger(env["ledger"])) == 1


def test_idempotent_no_duplicate(cws, env):
    _make_project(env["projects"], TASK_A, "sess-a", mtime_age_s=7200)
    cws._harvest_and_prune_orphan_jsonl(
        env["work"],
        projects_dir=env["projects"],
        ledger_path=env["ledger"],
        grace_s=3600,
        retention_days=0,
        now=time.time(),
    )
    # recria a mesma sessão (simula reaparição) e roda de novo
    _make_project(env["projects"], TASK_A, "sess-a", mtime_age_s=7200)
    cws._harvest_and_prune_orphan_jsonl(
        env["work"],
        projects_dir=env["projects"],
        ledger_path=env["ledger"],
        grace_s=3600,
        retention_days=0,
        now=time.time(),
    )
    led = _read_ledger(env["ledger"])
    assert len(led) == 1  # session_id sess-a aparece uma única vez


def test_dry_run_reports_without_side_effects(cws, env):
    pdir = _make_project(env["projects"], TASK_A, "sess-a", mtime_age_s=7200)
    result = cws._harvest_and_prune_orphan_jsonl(
        env["work"],
        projects_dir=env["projects"],
        ledger_path=env["ledger"],
        grace_s=3600,
        retention_days=0,
        now=time.time(),
        dry_run=True,
    )
    assert result["orphan_jsonl_dirs"]  # candidato reportado
    assert result["jsonl_dirs_removed"] == 0
    assert result["sessions_harvested"] == 0
    assert pdir.exists()  # nada deletado
    assert not env["ledger"].exists()  # nada escrito


def test_cleanup_scan_includes_orphan_jsonl(cws, env, monkeypatch):
    monkeypatch.setenv("HOME", str(env["home"]))
    # retenção 0 → o piso grace (1h) governa; órfão de 2h aparece no preview.
    monkeypatch.setattr(cws, "_JSONL_RETENTION_DAYS", 0)
    _make_project(env["projects"], TASK_A, "sess-a", mtime_age_s=7200)
    scan = cws._cleanup_scan(env["work"])
    assert (
        str(env["projects"] / f"-home-claude-work-{TASK_A}")
        in scan["orphan_jsonl_dirs"]
    )
    assert scan["total_candidate_bytes"] > 0


# --------------------------------------------------------------------------- #
# Fail-safe (incidente 01/jun): NUNCA podar dados de custo não colhidos.
# Regressão para o bug em que `aggregate_jsonl` ausente da imagem (import
# falho → None) fazia o harvester colher 0 e podar mesmo assim (337 sessões
# deletadas sem ledger).
# --------------------------------------------------------------------------- #
def test_no_prune_when_aggregator_unavailable(cws, env, monkeypatch):
    """Sem o extrator (jsonl_cost ausente da imagem), poda é ABORTADA."""
    pdir = _make_project(env["projects"], TASK_A, "sess-a", mtime_age_s=7200)
    monkeypatch.setattr(cws, "_summarize_jsonl", None)
    result = cws._harvest_and_prune_orphan_jsonl(
        env["work"],
        projects_dir=env["projects"],
        ledger_path=env["ledger"],
        grace_s=3600,
        retention_days=0,
        now=time.time(),
    )
    assert result["jsonl_dirs_removed"] == 0
    assert result["sessions_harvested"] == 0
    assert pdir.exists()  # dir órfão PRESERVADO
    assert not env["ledger"].exists()
    assert any("fail-safe" in e for e in result["errors"])


def test_no_prune_when_aggregation_raises(cws, env, monkeypatch):
    """Se o resumo de um JSONL falha, o dir inteiro é preservado."""
    pdir = _make_project(env["projects"], TASK_A, "sess-a", mtime_age_s=7200)

    def _boom(_path):
        raise ValueError("parse explodiu")

    monkeypatch.setattr(cws, "_summarize_jsonl", _boom)
    result = cws._harvest_and_prune_orphan_jsonl(
        env["work"],
        projects_dir=env["projects"],
        ledger_path=env["ledger"],
        grace_s=3600,
        retention_days=0,
        now=time.time(),
    )
    assert result["jsonl_dirs_removed"] == 0
    assert pdir.exists()  # preservado: havia custo não colhido
    assert any("summarize" in e for e in result["errors"])


def test_harvested_record_preserves_full_detail(cws, env, monkeypatch):
    """O registro do ledger (v:2) carrega o DETALHE que a tela de tokens
    mostra — título/brief/tools/PR/erros + meta (model/stage) — não só tokens.
    É o pedido do Humano: 'se tem coisa que mostra na tabela, tem que manter'."""
    pdir = env["projects"] / f"-home-claude-work-{TASK_A}"
    pdir.mkdir(parents=True)
    jsonl = pdir / "sess-rich.jsonl"
    jsonl.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "type": "user",
                    "timestamp": "2026-04-01T09:00:00.000Z",
                    "cwd": "/home/claude/work/x/repo",
                    "gitBranch": "auto/issue-9",
                    "version": "2.1.158",
                    "permissionMode": "bypassPermissions",
                    "entrypoint": "cli",
                    "aiTitle": "Corrige X",
                    "prNumber": 42,
                    "prUrl": "https://github.com/o/r/pull/42",
                    "prRepository": "o/r",
                    "message": {"role": "user", "content": "implementa a issue 9"},
                },
                {
                    "type": "assistant",
                    "requestId": "r1",
                    "timestamp": "2026-04-01T09:01:00.000Z",
                    "message": {
                        "id": "m1",
                        "role": "assistant",
                        "model": "claude-opus-4-5-20260101",
                        "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "name": "Read"}],
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    import os

    old = time.time() - 40 * 86400
    os.utime(jsonl, (old, old))
    os.utime(pdir, (old, old))
    # session.json (ground truth do pipeline) — meta lida pelo harvester.
    meta_dir = env["home"] / ".claude" / "tasks" / TASK_A
    meta_dir.mkdir(parents=True)
    (meta_dir / "session.json").write_text(
        json.dumps(
            {
                "model": "anthropic:claude-opus-4-5",
                "reasoning_effort": "xhigh",
                "ultracode": True,
                "stage": "implement",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(env["home"]))

    result = cws._harvest_and_prune_orphan_jsonl(
        env["work"],
        projects_dir=env["projects"],
        ledger_path=env["ledger"],
        grace_s=3600,
        retention_days=30,
        now=time.time(),
    )
    assert result["sessions_harvested"] == 1
    rec = _read_ledger(env["ledger"])[0]
    assert rec["v"] == 2
    assert rec["ai_title"] == "Corrige X"
    assert rec["brief"] == "implementa a issue 9"
    assert rec["tools"] == {"Read": 1}
    assert rec["tool_calls"] == 1
    assert rec["user_msgs"] == 1
    assert rec["pr_number"] == 42
    assert rec["pr_repo"] == "o/r"
    assert rec["git_branch"] == "auto/issue-9"
    assert rec["version"] == "2.1.158"
    assert rec["stop_reasons"] == {"tool_use": 1}
    assert rec["meta_model"] == "anthropic:claude-opus-4-5"
    assert rec["reasoning_effort"] == "xhigh"
    assert rec["ultracode"] is True
    assert rec["stage"] == "implement"
    assert rec["source_mtime"] == pytest.approx(old, abs=2)
