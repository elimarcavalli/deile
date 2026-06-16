"""Regressão: ``render_table`` escapa markup no texto livre da sessão.

Bug observado em produção (tela ``[t]okens`` do painel): uma sessão cujo
``last_response`` ou ``brief`` continha colchetes — ex.: a string de endpoints
``[/backlog|/recent|/ledger|/reaper-preview]`` que o ``claude -p`` imprime, ou um
``[stage]`` literal no título — quebrava o parser de markup do Rich:
``rich.errors.MarkupError: closing tag ... doesn't match any open tag`` no
``console.print(t)``, crasheando o tool inteiro. Intermitente (dependia de
QUAIS sessões estavam na página).

Fix: escapar (``rich.markup.escape``) os dois cells de texto livre da sessão
(título e última-resposta) no call-site do ``render_table`` — sem tocar nos
cells de markup intencional (issue/PR, fim, tier/web, etc.).

``_panel``/``session_tokens_audit`` vivem em ``infra/k8s/`` (fora do pacote
``deile``); o path é inserido manualmente — mesma convenção dos demais testes
de infra.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from rich.console import Console

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import session_tokens_audit as sta  # noqa: E402


def _min_session(**ov: object) -> dict:
    """Sessão mínima com todas as chaves que ``render_table`` + helpers acessam."""
    s = {
        "errors": {"api_error": False, "max_tokens": False, "tool_error": False},
        "git": {},
        "cost_usd": 0.0,
        "assistant_rounds": 0,
        "tool_calls": 0,
        "tools": [],
        "totals": {"in": 0, "out": 0},
        "cache_pct": 0.0,
        "nocache_usd": 0.0,
        "total_tokens": 0,
        "per_model": {},
        "session_file": "deadbeef-0000-0000",
        "jsonl": "/home/claude/.claude/projects/x/deadbeef.jsonl",
        "pr_number": None,
        "stage": None,
        "brief": "",
        "ai_title": None,
        "last_response": "",
        "harvested": False,
        "terminal_stop_reason": "end_turn",
        "web_tool_calls": 0,
        "service_tier": "standard",
        "speed": "standard",
        "last_activity": "—",
        "created_s": 0.0,
        "pvc": "claude-worker-home",
        "meta_model": "anthropic:claude-opus-4-8",
        "meta_branch": None,
        "git_branch": None,
    }
    s.update(ov)
    return s


@pytest.mark.unit
def test_render_table_survives_markup_in_free_text():
    """Sessão com colchetes no título/resposta não pode levantar MarkupError."""
    payload = "diga [/backlog|/recent|/ledger|/reaper-preview] já"
    sessions = [
        _min_session(last_response=payload, brief=payload, session_file="aaaa1111"),
        _min_session(ai_title="[classify] elimarcavalli/deile #5", session_file="bbbb2222"),
    ]
    console = Console(file=io.StringIO(), width=160)
    # Antes do fix isto levantava rich.errors.MarkupError.
    sta.render_table(console, sessions, top=None)
    out = console.file.getvalue()
    # Conteúdo renderizado literalmente (escapado), não interpretado como tag.
    assert "backlog" in out
    assert "classify" in out
