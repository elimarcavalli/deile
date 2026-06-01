"""Fonte única da lógica de custo das sessões ``claude -p`` (issue #445).

Centraliza o que antes vivia duplicado/embutido em ``session_tokens_audit.py``:

* Tabela de preços oficial (``PRICING``) e o mapeamento modelo → preço.
* Cálculo de custo de um bloco de tokens (``cost_of_model`` /
  ``nocache_cost_of_model``).
* ``aggregate_jsonl`` — agrega UM arquivo JSONL de sessão no dicionário de
  tokens por modelo, com a MESMA dedup ``(message.id, requestId)`` provada
  contra o ``last_total_cost_usd`` do fornecedor (erro < 0,1%).

Stdlib-pura de propósito: roda tanto no host (``session_tokens_audit.py``)
quanto dentro do pod (``claude_worker_server.py`` importa ``aggregate_jsonl``
para o harvester do ledger de custo). Sem dependências externas.

O ledger de custo (issue #445) usa ``aggregate_jsonl`` para colher os tokens
de uma sessão ANTES de a poda remover o JSONL volumoso — o custo histórico
sobrevive em escala de KB mesmo após o transcript ser removido.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, Optional

# --------------------------------------------------------------------------- #
# Preços oficiais (USD por MILHÃO de tokens). read = cache hit (0.1x input).   #
# w5 = cache write 5m (1.25x input); w1h = cache write 1h (2x input).          #
# --------------------------------------------------------------------------- #
PRICING = {
    "opus":         {"in": 5.0,  "out": 25.0, "w5": 6.25,  "w1h": 10.0, "read": 0.50},
    "opus_legacy":  {"in": 15.0, "out": 75.0, "w5": 18.75, "w1h": 30.0, "read": 1.50},
    "sonnet":       {"in": 3.0,  "out": 15.0, "w5": 3.75,  "w1h": 6.0,  "read": 0.30},
    "haiku":        {"in": 1.0,  "out": 5.0,  "w5": 1.25,  "w1h": 2.0,  "read": 0.10},
    "haiku_legacy": {"in": 0.80, "out": 4.0,  "w5": 1.0,   "w1h": 1.60, "read": 0.08},
    "free":         {"in": 0.0,  "out": 0.0,  "w5": 0.0,   "w1h": 0.0,  "read": 0.0},
}


def pricing_for(model: str) -> dict:
    """Mapeia um nome de modelo para a sua tabela de preços."""
    m = (model or "").lower()
    if "haiku" in m:
        return PRICING["haiku_legacy"] if ("3-5" in m or "3.5" in m) else PRICING["haiku"]
    if "sonnet" in m:
        return PRICING["sonnet"]
    if "opus" in m:
        # Opus 4.0 e 4.1 usam preço legado ($15/$75); 4.5+ usam o novo ($5/$25).
        # Parseia major/minor da versão, descartando o sufixo de data (-YYYYMMDD):
        # 'claude-opus-4-20250514' → major=4, minor=0 → legado;
        # 'claude-opus-4-7-...'     → major=4, minor=7 → novo.
        base = re.sub(r"-\d{6,}.*$", "", m)
        ver = re.search(r"opus[-_.]?(\d+)(?:[-_.](\d+))?", base)
        legacy = bool(ver) and int(ver.group(1)) == 4 and (
            ver.group(2) is None or int(ver.group(2)) <= 1)
        return PRICING["opus_legacy"] if legacy else PRICING["opus"]
    return PRICING["free"]  # <synthetic> e desconhecidos não são cobrados


def cost_of_model(tk: dict, model: str) -> float:
    """Custo em USD de um bloco de tokens {in,out,cc,cr,cc_5m,cc_1h} de um modelo."""
    p = pricing_for(model)
    cc_5m = tk.get("cc_5m") or 0
    cc_1h = tk.get("cc_1h") or 0
    if not cc_5m and not cc_1h:
        cc_5m = tk.get("cc", 0)  # sem breakdown → trata tudo como write 5m
    return (
        tk.get("in", 0) * p["in"]
        + tk.get("out", 0) * p["out"]
        + cc_5m * p["w5"]
        + cc_1h * p["w1h"]
        + tk.get("cr", 0) * p["read"]
    ) / 1_000_000.0


def nocache_cost_of_model(tk: dict, model: str) -> float:
    """Custo hipotético se NADA fosse cacheado (todo input a preço cheio)."""
    p = pricing_for(model)
    fresh = tk.get("in", 0) + tk.get("cc", 0) + tk.get("cr", 0)
    return (fresh * p["in"] + tk.get("out", 0) * p["out"]) / 1_000_000.0


# --------------------------------------------------------------------------- #
# Agregação de um JSONL de sessão.                                            #
#                                                                             #
# NOTA DE PARIDADE: este laço de dedup+soma é a fonte única da agregação de   #
# custo. O parser in-pod embutido em ``session_tokens_audit.IN_POD_PARSER``   #
# (caminho live) implementa o MESMO algoritmo documentado; a paridade é       #
# travada por ``test_jsonl_cost.test_aggregate_parity_with_inpod_reference``. #
# --------------------------------------------------------------------------- #
def _empty_model() -> Dict[str, int]:
    return {"in": 0, "out": 0, "cc": 0, "cr": 0, "cc_5m": 0, "cc_1h": 0}


def aggregate_jsonl(path: str) -> dict:
    """Agrega UM arquivo JSONL de sessão ``claude -p`` nos tokens por modelo.

    Conta cada resposta da API uma única vez por ``(message.id, requestId)``
    — o claude grava o mesmo registro assistant repetido (deltas de
    streaming) com o ``usage`` final idêntico; sem dedup os totais inflam
    13-32x. Respostas sem ``id``/``requestId`` recebem chave sintética.

    Returns:
        dict com ``session_id`` (stem do arquivo), ``models`` (model ->
        {in,out,cc,cr,cc_5m,cc_1h}), ``first_ts``, ``last_ts`` e
        ``assistant_rounds``.
    """
    session_id = os.path.splitext(os.path.basename(path))[0]
    models: Dict[str, Dict[str, int]] = {}
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    rounds = 0
    seen = set()
    noid = 0

    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                ts = o.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                msg = o.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                rkey = (msg.get("id"), o.get("requestId"))
                if rkey == (None, None):
                    noid += 1
                    rkey = ("__noid__", noid)
                if rkey in seen:
                    continue
                seen.add(rkey)
                rounds += 1
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                model = msg.get("model") or "unknown"
                mm = models.setdefault(model, _empty_model())
                mm["in"] += u.get("input_tokens", 0) or 0
                mm["out"] += u.get("output_tokens", 0) or 0
                mm["cc"] += u.get("cache_creation_input_tokens", 0) or 0
                mm["cr"] += u.get("cache_read_input_tokens", 0) or 0
                ccd = u.get("cache_creation")
                if isinstance(ccd, dict):
                    mm["cc_5m"] += ccd.get("ephemeral_5m_input_tokens", 0) or 0
                    mm["cc_1h"] += ccd.get("ephemeral_1h_input_tokens", 0) or 0
    except OSError:
        pass

    return {
        "session_id": session_id,
        "models": models,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "assistant_rounds": rounds,
    }
