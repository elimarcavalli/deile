"""Parsers do stdout nativo de cada worker da FROTA CLI (issue #445).

Fonte ÚNICA do parsing de ``<root>/.progress/<task_id>.stdout.log`` por
worker-kind (opencode/codex/qwen/goose/aider). Stdlib-pura de propósito: roda
tanto no host (auditoria ``fleet_tokens_audit.py``, que inlina este código no
``IN_POD_PARSER`` via :func:`source`) quanto dentro do pod (o harvester do
ledger durável em ``cli_worker_server._harvest_progress_to_ledger`` importa
``parse_progress_text``).

Cada parser recebe o TEXTO de um ``stdout.log`` + o ``task_id`` e devolve um
dict de sessão normalizado::

    {"models": {model -> {in,out,cc,cr}}, "native_cost": float|None,
     "first_ts": None, "last_ts": None}

O custo final (tabela de preço) e o restante do enriquecimento ficam a cargo
do chamador (``jsonl_cost.fleet_cost_of_model`` no host; o harvester só guarda
tokens por modelo no ledger e o custo é recomputado na leitura — paridade com
o ledger do claude).
"""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, Optional


def _empty() -> Dict[str, int]:
    return {"in": 0, "out": 0, "cc": 0, "cr": 0}


def _add(models: dict, model: Optional[str], tk: dict) -> None:
    mm = models.setdefault(model or "unknown", _empty())
    for k in ("in", "out", "cc", "cr"):
        mm[k] += int(tk.get(k, 0) or 0)


def _ndjson_events(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except Exception:  # noqa: BLE001 — linha malformada é tolerada
            continue
        if isinstance(o, dict):
            yield o


# opencode: NDJSON step_finish (part.tokens + part.cost nativo)
def parse_opencode(text: str, task_id: str) -> dict:
    models: dict = {}
    cost = 0.0
    saw_cost = False
    model = "unknown"
    for o in _ndjson_events(text):
        model = o.get("modelID") or o.get("model") or model
        if str(o.get("type", "")) != "step_finish":
            continue
        part = o.get("part") if isinstance(o.get("part"), dict) else o
        tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
        _add(models, model, {
            "in": tok.get("input", 0), "out": tok.get("output", 0),
            "cc": cache.get("write", 0), "cr": cache.get("read", 0),
        })
        c = part.get("cost")
        if isinstance(c, (int, float)):
            cost += float(c)
            saw_cost = True
    return {"models": models, "native_cost": cost if saw_cost else None,
            "first_ts": None, "last_ts": None}


# codex: JSONL token_count / turn.completed (turn_context.model)
def parse_codex(text: str, task_id: str) -> dict:
    models: dict = {}
    model = "unknown"
    prev_in = prev_out = prev_cr = 0
    for o in _ndjson_events(text):
        tc = o.get("turn_context")
        if isinstance(tc, dict) and tc.get("model"):
            model = tc.get("model")
        if o.get("model"):
            model = o.get("model")
        etype = o.get("type") or ""
        msg = o.get("msg") if isinstance(o.get("msg"), dict) else None
        if msg and msg.get("type"):
            etype = msg.get("type")
        info = (msg or o).get("info") if isinstance((msg or o).get("info"), dict) else None
        usage = None
        if etype == "token_count" and info:
            usage = info.get("total_token_usage") or info.get("last_token_usage")
        elif etype in ("turn.completed", "turn_complete") or o.get("usage"):
            usage = o.get("usage") or (msg.get("usage") if msg else None)
        if not isinstance(usage, dict):
            continue
        cur_in = int(usage.get("input_tokens", 0) or 0)
        cur_out = int(usage.get("output_tokens", 0) or 0)
        cur_cr = int(usage.get("cached_input_tokens", 0)
                     or usage.get("cached_tokens", 0) or 0)
        if etype == "token_count":
            d_in, d_out, d_cr = (max(0, cur_in - prev_in),
                                 max(0, cur_out - prev_out),
                                 max(0, cur_cr - prev_cr))
            prev_in, prev_out, prev_cr = cur_in, cur_out, cur_cr
        else:
            d_in, d_out, d_cr = cur_in, cur_out, cur_cr
        _add(models, model, {"in": d_in, "out": d_out, "cr": d_cr})
    return {"models": models, "native_cost": None,
            "first_ts": None, "last_ts": None}


# qwen: array de eventos; result.stats.models[model].tokens
def parse_qwen(text: str, task_id: str) -> dict:
    models: dict = {}
    whole = text.strip()
    events = None
    if whole.startswith("["):
        try:
            events = json.loads(whole)
        except Exception:  # noqa: BLE001
            events = None
    if events is None:
        events = list(_ndjson_events(text))
    if not isinstance(events, list):
        return {"models": models, "native_cost": None,
                "first_ts": None, "last_ts": None}
    result_ev = next((e for e in reversed(events)
                      if isinstance(e, dict) and e.get("type") == "result"), None)
    stats = result_ev.get("stats") if isinstance(result_ev, dict) else None
    mstats = stats.get("models") if isinstance(stats, dict) else None
    if isinstance(mstats, dict):
        for model, mdata in mstats.items():
            tok = mdata.get("tokens") if isinstance(mdata, dict) else {}
            if not isinstance(tok, dict):
                continue
            _add(models, model, {
                "in": tok.get("input", 0) or tok.get("prompt", 0),
                "out": tok.get("output", 0) or tok.get("candidates", 0),
                "cr": tok.get("cached", 0) or tok.get("cache", 0),
            })
    elif isinstance(result_ev, dict) and isinstance(result_ev.get("usage"), dict):
        u = result_ev["usage"]
        _add(models, result_ev.get("model") or "unknown", {
            "in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
            "cr": u.get("cached", 0) or u.get("cached_tokens", 0),
        })
    return {"models": models, "native_cost": None,
            "first_ts": None, "last_ts": None}


# goose: {messages, metadata:{total_tokens,...}}
def parse_goose(text: str, task_id: str) -> dict:
    models: dict = {}
    whole = text.strip()
    obj = None
    if whole.startswith("{"):
        try:
            obj = json.loads(whole)
        except Exception:  # noqa: BLE001
            obj = None
    meta = obj.get("metadata") if isinstance(obj, dict) else None
    if not isinstance(meta, dict):
        for o in _ndjson_events(text):
            m = o.get("metadata")
            if isinstance(m, dict):
                meta = m
    if isinstance(meta, dict):
        model = meta.get("model") or (obj.get("model") if isinstance(obj, dict) else None) or "unknown"
        tin = int(meta.get("input_tokens", 0) or 0)
        tout = int(meta.get("output_tokens", 0) or 0)
        ttot = int(meta.get("total_tokens", 0) or meta.get("accumulated_total_tokens", 0) or 0)
        if not tin and not tout and ttot:
            tin = ttot // 4
            tout = ttot - tin
        _add(models, model, {"in": tin, "out": tout, "cr": int(meta.get("cached_tokens", 0) or 0)})
    return {"models": models, "native_cost": None,
            "first_ts": None, "last_ts": None}


# aider: texto livre "Tokens: N sent, M received." / "Cost: $X"
_AIDER_TOK = re.compile(r"[Tt]okens:\s*([\d,\.]+)k?\s*sent,\s*([\d,\.]+)k?\s*received")
_AIDER_COST = re.compile(r"[Cc]ost:\s*\$?([\d\.]+)")
_AIDER_MODEL = re.compile(r"[Mm]odel:\s*([\w\-./:]+)")


def _num(s: str) -> float:
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_aider(text: str, task_id: str) -> dict:
    models: dict = {}
    sent = recv = 0
    cost = 0.0
    saw_cost = False
    model = "unknown"
    mm = _AIDER_MODEL.search(text)
    if mm:
        model = mm.group(1)
    for line in text.splitlines():
        tk = _AIDER_TOK.search(line)
        if tk:
            a = _num(tk.group(1))
            b = _num(tk.group(2))
            if "k sent" in line.lower() or "k received" in line.lower():
                a *= 1000
                b *= 1000
            sent += int(a)
            recv += int(b)
        cm = _AIDER_COST.search(line)
        if cm:
            cost += _num(cm.group(1))
            saw_cost = True
    if sent or recv:
        _add(models, model, {"in": sent, "out": recv})
    return {"models": models, "native_cost": cost if saw_cost else None,
            "first_ts": None, "last_ts": None}


#: Parsers por worker-kind (fonte única). claude → JSONL e deile → SQLite não têm parser aqui.
PROGRESS_PARSERS: Dict[str, Callable[[str, str], dict]] = {
    "opencode": parse_opencode,
    "codex": parse_codex,
    "qwen": parse_qwen,
    "goose": parse_goose,
    "aider": parse_aider,
}


def parse_progress_text(kind: str, text: str, task_id: str) -> Optional[dict]:
    """Parseia o stdout nativo de *kind*; ``None`` se o kind não usa ``.progress``."""
    fn = PROGRESS_PARSERS.get(kind)
    if fn is None:
        return None
    return fn(text, task_id)
