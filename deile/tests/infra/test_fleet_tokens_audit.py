"""Testes da auditoria de tokens da FROTA multi-worker (issue #445).

A auditoria multi-worker (``infra/k8s/fleet_tokens_audit.py``) agrega o uso de
tokens/custo de TODOS os worker-kinds da frota (claude/deile/opencode/codex/qwen/
goose/aider). Cada worker grava o uso num shape/local diferente do PVC; o parser
in-pod (``IN_POD_PARSER``) parametrizado por ``FLEET_KIND`` lê a fonte certa e
emite sessões normalizadas.

Cobertura (SEM kubectl real, SEM custo real):

  1. **Coletor de cada worker parseia o shape REAL** — o parser in-pod roda como
     subprocess (exatamente como no pod), com ``FLEET_ROOT`` apontando para um
     tmp_path com fixtures dos shapes investigados na doc oficial:
       * opencode → NDJSON ``step_finish`` (``part.tokens`` + ``part.cost``);
       * qwen     → array com ``result.stats.models[model].tokens``;
       * codex    → JSONL ``token_count`` cumulativo + ``turn_context.model``;
       * goose    → ``{messages, metadata:{total_tokens}}``;
       * aider    → texto livre "Tokens: N sent, M received." / "Cost: $X";
       * claude   → JSONL do ``claude -p`` (dedup por id);
       * deile    → SQLite ``usage_records``.
  2. **Agregação por modelo × worker** + custo via tabela de preço.
  3. **Custo nativo do CLI prevalece** sobre o estimado quando reportado.
  4. **Tela [T] no painel** rende sem quebrar / wiring do hotkey.
  5. **Extensão aditiva do jsonl_cost** (tabela da frota).

As fixtures de shape são derivadas da doc oficial de cada CLI (opencode
``step_finish``, qwen ``--output-format json``, codex ``token_count``, goose
``--output-format json``, aider stdout) e dos fixtures dos testes de adapter
correspondentes (test_*_adapter.py).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

# Insere infra/k8s no sys.path (mesma convenção dos demais testes de infra).
_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _REPO / "infra" / "k8s" / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fta():
    return _load("fleet_tokens_audit", "fleet_tokens_audit.py")


@pytest.fixture(scope="module")
def jc():
    return _load("jsonl_cost", "jsonl_cost.py")


# --------------------------------------------------------------------------- #
# Helper: roda o IN_POD_PARSER como subprocess (igual ao fluxo real no pod).   #
# --------------------------------------------------------------------------- #
def _run_parser(fta, kind: str, root: Path, env_extra: dict | None = None) -> list:
    env = {"FLEET_KIND": kind, "FLEET_ROOT": str(root), "PATH": "/usr/bin:/bin"}
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-"],
        input=fta.IN_POD_PARSER, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def _write_progress(root: Path, task_id: str, lines):
    pdir = root / ".progress"
    pdir.mkdir(parents=True, exist_ok=True)
    text = lines if isinstance(lines, str) else "\n".join(lines)
    (pdir / f"{task_id}.stdout.log").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. Coletores parseiam o shape REAL de cada worker                            #
# --------------------------------------------------------------------------- #
def test_opencode_parses_step_finish_tokens_and_native_cost(fta, tmp_path):
    # Shape oficial: NDJSON com step_finish carregando part.tokens + part.cost.
    _write_progress(tmp_path, "aabbccdd11223344", [
        json.dumps({"type": "step_start", "sessionID": "ses_1", "modelID": "deepseek/deepseek-v4-pro"}),
        json.dumps({"type": "step_finish", "sessionID": "ses_1", "part": {
            "type": "step-finish", "cost": 0.012,
            "tokens": {"input": 1500, "output": 300, "reasoning": 0,
                       "cache": {"read": 21415, "write": 100}}}}),
        json.dumps({"type": "step_finish", "sessionID": "ses_1", "part": {
            "type": "step-finish", "cost": 0.003,
            "tokens": {"input": 200, "output": 50, "cache": {"read": 0, "write": 0}}}}),
    ])
    sessions = _run_parser(fta, "opencode", tmp_path)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["worker"] == "opencode"
    m = s["models"]["deepseek/deepseek-v4-pro"]
    assert m["in"] == 1700 and m["out"] == 350
    assert m["cr"] == 21415 and m["cc"] == 100
    assert abs(s["native_cost"] - 0.015) < 1e-9


def test_qwen_parses_result_stats_models(fta, tmp_path):
    # Shape oficial: array de eventos; result.stats.models[model].tokens.
    events = [
        {"type": "system", "session_id": "qwen-x"},
        {"type": "assistant", "content": "trabalhando"},
        {"type": "result", "session_id": "qwen-x", "is_error": False, "result": "ok",
         "stats": {"models": {
             "qwen3-coder-plus": {"tokens": {"input": 5000, "output": 1200, "cached": 800, "total": 7000}}}}},
    ]
    _write_progress(tmp_path, "task_qwen0000001", json.dumps(events))
    sessions = _run_parser(fta, "qwen", tmp_path)
    assert len(sessions) == 1
    m = sessions[0]["models"]["qwen3-coder-plus"]
    assert m["in"] == 5000 and m["out"] == 1200 and m["cr"] == 800


def test_codex_parses_token_count_cumulative_delta(fta, tmp_path):
    # token_count é CUMULATIVO → o parser deve fazer delta entre eventos.
    _write_progress(tmp_path, "task_codex000001", [
        json.dumps({"type": "thread.started", "thread": {"id": "thr_1"}}),
        json.dumps({"type": "turn_context", "turn_context": {"model": "gpt-5.1-codex"}}),
        json.dumps({"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 1000, "output_tokens": 200, "cached_input_tokens": 50}}}),
        json.dumps({"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 1800, "output_tokens": 500, "cached_input_tokens": 120}}}),
    ])
    sessions = _run_parser(fta, "codex", tmp_path)
    assert len(sessions) == 1
    m = sessions[0]["models"]["gpt-5.1-codex"]
    # totais cumulativos finais: in=1800, out=500, cr=120 (soma dos deltas).
    assert m["in"] == 1800 and m["out"] == 500 and m["cr"] == 120


def test_codex_turn_completed_per_turn(fta, tmp_path):
    # turn.completed traz usage por-turn (não cumulativo) — fixture do adapter.
    _write_progress(tmp_path, "task_codex000002", [
        json.dumps({"type": "thread.started", "thread": {"id": "thr_2"}}),
        json.dumps({"type": "turn.completed", "model": "codex-mini-latest",
                    "usage": {"input_tokens": 400, "output_tokens": 40}}),
    ])
    sessions = _run_parser(fta, "codex", tmp_path)
    m = sessions[0]["models"]["codex-mini-latest"]
    assert m["in"] == 400 and m["out"] == 40


def test_goose_parses_metadata_total_tokens(fta, tmp_path):
    # Shape oficial: {messages:[...], metadata:{total_tokens}} — fixture adapter.
    _write_progress(tmp_path, "task_goose000001", json.dumps({
        "messages": [{"role": "assistant", "content": [{"type": "text", "text": "feito"}]}],
        "metadata": {"total_tokens": 81823, "status": "completed", "model": "deepseek/deepseek-v4-flash"},
    }))
    sessions = _run_parser(fta, "goose", tmp_path)
    assert len(sessions) == 1
    s = sessions[0]
    m = s["models"]["deepseek/deepseek-v4-flash"]
    # só total → estima ~25/75 input/output.
    assert m["in"] + m["out"] == 81823 and m["in"] > 0 and m["out"] > 0


def test_aider_parses_text_tokens_and_cost(fta, tmp_path):
    _write_progress(tmp_path, "task_aider000001", [
        "Model: openrouter/deepseek/deepseek-v4-pro with whole edit format",
        "Tokens: 12,500 sent, 3,200 received.",
        "Cost: $0.04 message, $0.04 session.",
    ])
    sessions = _run_parser(fta, "aider", tmp_path)
    assert len(sessions) == 1
    s = sessions[0]
    m = s["models"]["openrouter/deepseek/deepseek-v4-pro"]
    assert m["in"] == 12500 and m["out"] == 3200
    assert abs(s["native_cost"] - 0.04) < 1e-9


def test_claude_parses_jsonl_with_dedup(fta, tmp_path):
    # claude grava em projects/-home-claude-work-<task>/<session>.jsonl.
    base = tmp_path / ".claude" / "projects" / "-home-claude-work-abc"
    base.mkdir(parents=True)
    rec = {"timestamp": "2026-06-01T10:00:00Z", "requestId": "r1",
           "message": {"role": "assistant", "id": "m1", "model": "claude-sonnet-4-6",
                       "usage": {"input_tokens": 1000, "output_tokens": 200,
                                 "cache_read_input_tokens": 5000}}}
    lines = [json.dumps({"timestamp": "2026-06-01T09:59:00Z",
                         "message": {"role": "user", "content": "implemente X"}}),
             json.dumps(rec), json.dumps(rec)]  # duplicado (streaming delta)
    (base / "sess1.jsonl").write_text("\n".join(lines), encoding="utf-8")
    # claude usa HOME implícito — o parser hardcoda /home/claude; usamos FLEET_ROOT
    # só para os outros. Para claude, sobrescrevemos via symlink do HOME no env.
    env = {"HOME": str(tmp_path)}
    # O parser claude lê de /home/claude/.claude — re-aponta via PARSER patch:
    # rodamos com FLEET_KIND=claude mas BASE fixo; então testamos via cópia.
    code = fta.IN_POD_PARSER.replace(
        '"/home/claude/.claude/projects"', f'{json.dumps(str(base.parent))}')
    proc = subprocess.run([sys.executable, "-"], input=code, capture_output=True,
                          text=True, env={"FLEET_KIND": "claude", "PATH": "/usr/bin:/bin", **env})
    assert proc.returncode == 0, proc.stderr
    sessions = json.loads(proc.stdout.strip())
    assert len(sessions) == 1
    m = sessions[0]["models"]["claude-sonnet-4-6"]
    # dedup: contado UMA vez (não 2x).
    assert m["in"] == 1000 and m["out"] == 200 and m["cr"] == 5000


def test_deile_parses_usage_sqlite(fta, tmp_path):
    import sqlite3
    db = tmp_path / "usage.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE usage_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, provider_id TEXT,
        model_id TEXT, tier TEXT, session_id TEXT, prompt_tokens INTEGER,
        completion_tokens INTEGER, cached_tokens INTEGER, total_tokens INTEGER,
        cost_usd REAL, latency_ms INTEGER, success INTEGER, error_type TEXT)""")
    con.execute("INSERT INTO usage_records (timestamp, provider_id, model_id, tier, "
                "session_id, prompt_tokens, completion_tokens, cached_tokens, cost_usd) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (1_700_000_000.0, "deepseek", "deepseek-chat", "standard", "sess-a",
                 2000, 500, 0, 0.0021))
    con.commit()
    con.close()
    sessions = _run_parser(fta, "deile", tmp_path, env_extra={"FLEET_DEILE_DB": str(db)})
    assert len(sessions) == 1
    s = sessions[0]
    assert s["worker"] == "deile"
    m = s["models"]["deepseek:deepseek-chat"]
    assert m["in"] == 2000 and m["out"] == 500
    assert abs(s["native_cost"] - 0.0021) < 1e-9


def test_parser_ignores_sessions_without_tokens(fta, tmp_path):
    _write_progress(tmp_path, "empty00000000001", [
        json.dumps({"type": "step_start", "sessionID": "ses_e"}),
    ])
    sessions = _run_parser(fta, "opencode", tmp_path)
    assert sessions == []


def test_parser_tolerates_malformed_lines(fta, tmp_path):
    _write_progress(tmp_path, "malformed0000001", [
        "not json at all",
        json.dumps({"type": "step_finish", "modelID": "x/y",
                    "part": {"tokens": {"input": 10, "output": 5}, "cost": 0.001}}),
        "{ broken",
    ])
    sessions = _run_parser(fta, "opencode", tmp_path)
    assert len(sessions) == 1


# --------------------------------------------------------------------------- #
# 2-3. Agregação por modelo × worker + custo (nativo vs estimado)             #
# --------------------------------------------------------------------------- #
def test_enrich_costs_per_model_and_native_preference(fta):
    declared = {}
    collectors = {k: fta.collector_for(k, declared) for k in fta.fleet_worker_kinds()}
    sessions = [
        # opencode com custo nativo → prevalece.
        {"worker": "opencode", "task_id": "t1", "source": "x", "native_cost": 0.50,
         "models": {"deepseek/deepseek-v4-pro": {"in": 1_000_000, "out": 100_000, "cc": 0, "cr": 0}},
         "first_ts": None, "last_ts": None, "mtime": None, "brief": None},
        # qwen sem custo nativo → estimado via tabela.
        {"worker": "qwen", "task_id": "t2", "source": "y", "native_cost": None,
         "models": {"qwen3-coder-plus": {"in": 1_000_000, "out": 0, "cc": 0, "cr": 0}},
         "first_ts": None, "last_ts": None, "mtime": None, "brief": None},
    ]
    out = fta.enrich(sessions, collectors)
    oc = next(s for s in out if s["worker"] == "opencode")
    qw = next(s for s in out if s["worker"] == "qwen")
    assert oc["cost_basis"] == "nativo" and oc["cost_usd"] == 0.50
    # qwen3-coder-plus = $1.00/MTok input → 1M tokens = $1.00.
    assert qw["cost_basis"] == "estimado" and abs(qw["cost_usd"] - 1.00) < 1e-6
    assert qw["per_model"]["qwen3-coder-plus"]["cost"] > 0


def test_enrich_claude_uses_claude_pricing(fta):
    collectors = {k: fta.collector_for(k, {}) for k in fta.fleet_worker_kinds()}
    s = [{"worker": "claude", "task_id": "c1", "source": "z", "native_cost": None,
          "models": {"claude-sonnet-4-6": {"in": 1_000_000, "out": 0, "cc": 0, "cr": 0}},
          "first_ts": None, "last_ts": None, "mtime": None, "brief": None}]
    out = fta.enrich(s, collectors)
    # sonnet input = $3/MTok (tabela claude do jsonl_cost).
    assert abs(out[0]["cost_usd"] - 3.0) < 1e-6


def test_collector_declared_price_overrides_table(fta):
    declared = {"some-exotic-model": {"in": 99.0, "out": 0.0, "read": 0.0}}
    coll = fta.collector_for("opencode", declared)
    cost = coll.cost_for_model({"in": 1_000_000, "out": 0, "cc": 0, "cr": 0},
                               "some-exotic-model")
    assert abs(cost - 99.0) < 1e-6


# --------------------------------------------------------------------------- #
# Tabela de preço da frota (extensão aditiva do jsonl_cost)                    #
# --------------------------------------------------------------------------- #
def test_fleet_pricing_known_models(jc):
    assert jc.fleet_pricing_for("openrouter/deepseek/deepseek-v4-flash")["in"] == 0.0983
    assert jc.fleet_pricing_for("gpt-5.1-codex")["in"] == 1.25
    assert jc.fleet_pricing_for("qwen3-coder-plus")["out"] == 5.0


def test_fleet_pricing_fallback_for_unknown(jc):
    p = jc.fleet_pricing_for("totally-unknown-model-xyz")
    assert p == jc.FLEET_PRICING_DEFAULT


def test_fleet_pricing_declared_prevails(jc):
    p = jc.fleet_pricing_for("deepseek-v4-flash", declared={"in": 7.0, "out": 8.0})
    assert p["in"] == 7.0 and p["out"] == 8.0


def test_fleet_cost_of_model(jc):
    cost = jc.fleet_cost_of_model(
        {"in": 1_000_000, "out": 0, "cc": 0, "cr": 0}, "qwen3-coder-plus")
    assert abs(cost - 1.0) < 1e-9


def test_claude_pricing_untouched(jc):
    # A extensão da frota NÃO mexe na tabela claude (regressão).
    assert jc.pricing_for("claude-sonnet-4-6")["in"] == 3.0
    assert jc.pricing_for("claude-opus-4-5-20260101")["in"] == 5.0


# --------------------------------------------------------------------------- #
# 4. Renderer não quebra + descoberta de workers                              #
# --------------------------------------------------------------------------- #
def test_fleet_worker_kinds_includes_core_and_cli(fta):
    kinds = fta.fleet_worker_kinds()
    assert "claude" in kinds and "deile" in kinds
    # frota CLI descoberta via cli_adapters.ADAPTERS.
    for cli in ("opencode", "codex", "qwen", "goose", "aider"):
        assert cli in kinds


def test_renderer_by_worker_and_sessions_no_crash(fta):
    from rich.console import Console
    console = Console(file=__import__("io").StringIO(), width=120)
    renderer = fta.FleetRenderer(console)
    collectors = {k: fta.collector_for(k, {}) for k in fta.fleet_worker_kinds()}
    sessions = fta.enrich([
        {"worker": "opencode", "task_id": "t1", "source": "x", "native_cost": 0.01,
         "models": {"deepseek/deepseek-v4-pro": {"in": 100, "out": 20, "cc": 0, "cr": 0}},
         "first_ts": "2026-06-01T10:00:00Z", "last_ts": "2026-06-01T10:05:00Z",
         "mtime": 1_700_000_000.0, "brief": "implemente algo"},
        {"worker": "qwen", "task_id": "t2", "source": "y", "native_cost": None,
         "models": {"qwen3-coder-plus": {"in": 5000, "out": 1000, "cc": 0, "cr": 0}},
         "first_ts": None, "last_ts": None, "mtime": None, "brief": None},
    ], collectors)
    renderer.render_by_worker(sessions)   # não deve lançar
    renderer.render_sessions(sessions)
    renderer.render_detail(sessions[0], 1)
    out = console.file.getvalue()
    assert "opencode" in out and "qwen" in out
    assert "TOTAL DA FROTA" in out


def test_renderer_plain_fallback_no_crash(fta):
    renderer = fta.FleetRenderer(None)  # sem Rich → texto puro
    collectors = {k: fta.collector_for(k, {}) for k in fta.fleet_worker_kinds()}
    sessions = fta.enrich([
        {"worker": "aider", "task_id": "t3", "source": "z", "native_cost": 0.02,
         "models": {"openrouter/deepseek/deepseek-v4-pro": {"in": 9000, "out": 800, "cc": 0, "cr": 0}},
         "first_ts": None, "last_ts": None, "mtime": None, "brief": None},
    ], collectors)
    renderer.render_by_worker(sessions)
    renderer.render_sessions(sessions)


def test_worker_filter_and_model_filter(fta):
    kinds = fta.fleet_worker_kinds()
    assert set(fta._parse_worker_filter("opencode,qwen", kinds)) == {"opencode", "qwen"}
    assert fta._parse_worker_filter(None, kinds) == kinds
    sessions = [
        {"per_model": {"qwen3-coder-plus": {}}, "worker": "qwen"},
        {"per_model": {"gpt-5.1-codex": {}}, "worker": "codex"},
    ]
    assert len(fta.filter_by_model(sessions, "qwen")) == 1
    assert len(fta.filter_by_model(sessions, None)) == 2


# --------------------------------------------------------------------------- #
# 5. Wiring [T] no painel                                                      #
# --------------------------------------------------------------------------- #
def test_panel_T_hotkey_suspends_fleet_audit():
    panel = _load("_panel", "_panel.py")
    view = panel.DashboardView(data=None)
    result = view.handle_key("T", app=None)
    assert result.kind == panel.Action.SUSPEND
    cmd = result.payload.get("command")
    assert cmd and any("fleet_tokens_audit.py" in str(c) for c in cmd)


def test_panel_t_lowercase_still_claude_legacy():
    panel = _load("_panel", "_panel.py")
    view = panel.DashboardView(data=None)
    result = view.handle_key("t", app=None)
    assert result.kind == panel.Action.SUSPEND
    cmd = result.payload.get("command")
    assert cmd and any("session_tokens_audit.py" in str(c) for c in cmd)
