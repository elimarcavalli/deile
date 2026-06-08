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


# --------------------------------------------------------------------------- #
# Janela de contexto (tokens) por família/versão de modelo. Fonte única para o #
# threshold de promoção-a-fresh do resume (claude_worker_server). Opus 4.5+ e   #
# Sonnet 4.6+ têm 1M; Opus 4.0/4.1, Sonnet <=4.5 e Haiku têm 200K. Conservador  #
# (200K) para desconhecidos: subestimar nunca arrisca estourar a janela real.   #
# --------------------------------------------------------------------------- #
CONTEXT_WINDOW_1M = 1_000_000
CONTEXT_WINDOW_200K = 200_000


def context_window_of_model(model: str) -> int:
    """Janela de contexto (tokens) de um modelo; 200K conservador p/ desconhecido."""
    m = (model or "").lower()
    if "haiku" in m:
        return CONTEXT_WINDOW_200K
    base = re.sub(r"-\d{6,}.*$", "", m)
    if "sonnet" in m:
        # Sonnet 4.6+ = 1M; 4.5 e anteriores = 200K.
        ver = re.search(r"sonnet[-_.]?(\d+)(?:[-_.](\d+))?", base)
        if not ver:
            return CONTEXT_WINDOW_200K
        major = int(ver.group(1))
        minor = int(ver.group(2)) if ver.group(2) is not None else 0
        return CONTEXT_WINDOW_1M if (major > 4 or (major == 4 and minor >= 6)) else CONTEXT_WINDOW_200K
    if "opus" in m:
        # Opus 4.5+ = 1M; 4.0/4.1 = 200K (espelha o split de preço em pricing_for).
        ver = re.search(r"opus[-_.]?(\d+)(?:[-_.](\d+))?", base)
        legacy = bool(ver) and int(ver.group(1)) == 4 and (
            ver.group(2) is None or int(ver.group(2)) <= 1)
        return CONTEXT_WINDOW_200K if legacy else CONTEXT_WINDOW_1M
    return CONTEXT_WINDOW_200K


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
# Tabela de preços da FROTA multi-worker (issue #445 — extensão aditiva).      #
#                                                                             #
# claude tem a sua própria tabela acima (``PRICING``/``pricing_for``). Os      #
# demais workers (opencode/codex/qwen/goose/aider) rodam modelos de outros     #
# provedores (OpenRouter, OpenAI Codex, Dashscope, DeepSeek) cujo preço NÃO    #
# segue a curva opus/sonnet/haiku. Esta seção é a FONTE ÚNICA do preço desses  #
# modelos — fora do claude.                                                    #
#                                                                             #
# Preços em USD por MILHÃO de tokens (input / cached-input / output). Fontes   #
# (verif. jun/2026): catálogos dos adapters (``cli_adapters/*.py`` campos      #
# ``price_in``/``cached_in``/``price_out`` do :class:`ModelInfo`), OpenRouter  #
# (openrouter.ai/<model>), OpenAI Codex (developers.openai.com/codex) e        #
# Dashscope. Quando um adapter declara o preço no ``ModelInfo`` ele PREVALECE  #
# (mais fresco que esta tabela) — ver ``fleet_tokens_audit.fleet_pricing``.    #
#                                                                             #
# Match por substring de model-id normalizado (case-insensitive), do mais      #
# específico para o mais genérico. ``read`` = preço de cache hit; default ao    #
# ``cached_in`` declarado ou 0.1x input (convenção da maioria dos provedores). #
# --------------------------------------------------------------------------- #
FLEET_PRICING_BY_SUBSTRING = (
    # (substring, {in, out, read})  — ordem: específico → genérico.
    # OpenAI Codex (developers.openai.com/codex; cached = 0.1x input).
    ("gpt-5.3-codex",        {"in": 1.75, "out": 14.0, "read": 0.175}),
    ("gpt-5.2-codex",        {"in": 1.75, "out": 14.0, "read": 0.175}),
    ("gpt-5.1-codex-mini",   {"in": 0.25, "out": 2.0,  "read": 0.025}),
    ("gpt-5.1-codex-max",    {"in": 1.25, "out": 10.0, "read": 0.125}),
    ("gpt-5.1-codex",        {"in": 1.25, "out": 10.0, "read": 0.125}),
    ("gpt-5-codex",          {"in": 1.25, "out": 10.0, "read": 0.125}),
    ("codex-mini",           {"in": 1.50, "out": 6.0,  "read": 0.375}),
    # OpenAI GPT (gpt-5.5 / gpt-5.4 — rota OpenAI direta / OpenRouter).
    ("gpt-5.5",              {"in": 2.50, "out": 15.0, "read": 0.25}),
    ("gpt-5.4",              {"in": 2.50, "out": 15.0, "read": 0.25}),
    # DeepSeek (OpenRouter / api.deepseek.com) — o grosso barato da frota.
    ("deepseek-v4-flash",    {"in": 0.0983, "out": 0.1966, "read": 0.00983}),
    ("deepseek-v4-pro",      {"in": 0.435,  "out": 0.87,   "read": 0.0435}),
    ("deepseek-chat",        {"in": 0.27,   "out": 1.10,   "read": 0.027}),
    ("deepseek",             {"in": 0.27,   "out": 1.10,   "read": 0.027}),
    # Qwen3 Coder (Dashscope / OpenRouter).
    ("qwen3-coder-next",     {"in": 0.11, "out": 0.80, "read": 0.011}),
    ("qwen3-coder-plus",     {"in": 1.00, "out": 5.0,  "read": 0.10}),
    ("qwen3-coder-480b",     {"in": 0.22, "out": 1.80, "read": 0.022}),
    ("qwen3-coder",          {"in": 0.22, "out": 1.80, "read": 0.022}),
    ("qwen",                 {"in": 0.22, "out": 1.80, "read": 0.022}),
    # Claude via OpenRouter (anthropic/claude-*) — preço público OpenRouter.
    ("claude-sonnet-4.6",    {"in": 3.0,  "out": 15.0, "read": 0.30}),
    ("claude-sonnet",        {"in": 3.0,  "out": 15.0, "read": 0.30}),
    ("claude-3.7-sonnet",    {"in": 3.0,  "out": 15.0, "read": 0.30}),
    ("claude-opus",          {"in": 5.0,  "out": 25.0, "read": 0.50}),
)

#: Preço de fallback quando o model-id não casa nenhuma substring nem há preço
#: declarado pelo adapter. Conservador: usa um custo-benefício médio de coding
#: (~DeepSeek Pro) para não subestimar grosseiramente. ``read`` = 0.1x input.
FLEET_PRICING_DEFAULT = {"in": 0.435, "out": 0.87, "read": 0.0435}


def fleet_pricing_for(model: str, *, declared: Optional[dict] = None) -> dict:
    """Tabela de preço {in,out,read} de um modelo da frota (não-claude).

    Resolução (primeiro que existir vence):

    1. ``declared`` — preço vindo do ``ModelInfo`` do adapter (``price_in``/
       ``price_out``/``cached_in``); mais fresco, prevalece.
    2. :data:`FLEET_PRICING_BY_SUBSTRING` — match por substring do model-id.
    3. :data:`FLEET_PRICING_DEFAULT` — fallback conservador.

    Args:
        model: model-id nativo do CLI (ex.: ``openrouter/deepseek/deepseek-v4-pro``,
            ``gpt-5.1-codex``, ``qwen3-coder-plus``).
        declared: dict opcional ``{"in":..,"out":..,"read":..}`` com o preço que
            o adapter declarou (já normalizado pelo chamador). ``None`` ignora.

    Returns:
        dict ``{"in":float, "out":float, "read":float}`` em USD/MTok.
    """
    if declared:
        return {
            "in": float(declared.get("in") or 0.0),
            "out": float(declared.get("out") or 0.0),
            "read": float(
                declared["read"] if declared.get("read") is not None
                else (declared.get("in") or 0.0) * 0.1
            ),
        }
    m = (model or "").lower()
    for needle, price in FLEET_PRICING_BY_SUBSTRING:
        if needle in m:
            return dict(price)
    return dict(FLEET_PRICING_DEFAULT)


def fleet_cost_of_model(tk: dict, model: str, *, declared: Optional[dict] = None) -> float:
    """Custo USD de um bloco de tokens {in,out,cr} de um modelo da frota.

    Espelha :func:`cost_of_model` mas com a tabela de preço da frota e o
    breakdown de cache simples (read a preço de hit; tokens de cache-write são
    raros fora do claude e, quando presentes, somam ao input a preço cheio).
    """
    p = fleet_pricing_for(model, declared=declared)
    return (
        tk.get("in", 0) * p["in"]
        + tk.get("out", 0) * p["out"]
        + tk.get("cc", 0) * p["in"]
        + tk.get("cr", 0) * p["read"]
    ) / 1_000_000.0


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


#: Teto do brief capturado no resumo (mesmo do parser in-pod do audit). Mantém
#: o registro do ledger em escala de KB mesmo preservando o comando completo.
_BRIEF_CAP = 4000


def _text_of(content) -> str:
    """Extrai o texto de um ``content`` (string ou lista de blocos)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""


def summarize_jsonl(path: str) -> dict:
    """Resumo COMPLETO de uma sessão ``claude -p`` — superset de :func:`aggregate_jsonl`.

    Extrai os MESMOS campos que o parser in-pod do ``session_tokens_audit``
    usa para renderizar a tabela e o detalhe — tokens por modelo, tools,
    rodadas, mensagens, brief, título IA, PR, versão, erros, stop reasons —
    EXCETO o git status (que depende do ``cwd`` vivo, já removido na poda).

    É a fonte única do harvest rico do ledger de custo (issue #445): a sessão
    colhida para o ledger antes da poda fica IDÊNTICA à viva na tela de tokens,
    não uma casca só com tokens. A agregação de custo (dedup por
    ``(message.id, requestId)`` + soma) é a mesma de :func:`aggregate_jsonl`.

    Returns:
        dict com ``session_id``, ``models`` (model -> {in,out,cc,cr,cc_5m,
        cc_1h}), ``tools``, ``assistant_rounds``, ``user_msgs``, ``tool_calls``,
        ``cwd``, ``git_branch``, ``version``, ``permission_mode``,
        ``entrypoint``, ``ai_title``, ``pr_number``, ``pr_url``, ``pr_repo``,
        ``first_ts``, ``last_ts``, ``brief``, ``errors`` e ``stop_reasons``.
    """
    session_id = os.path.splitext(os.path.basename(path))[0]
    models: Dict[str, Dict[str, int]] = {}
    tools: Dict[str, int] = {}
    stop_reasons: Dict[str, int] = {}
    errors = {"synthetic": 0, "max_tokens": 0, "api_error": 0, "tool_error": 0}
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    cwd = git_branch = version = permission_mode = entrypoint = None
    ai_title = pr_number = pr_url = pr_repo = None
    brief: Optional[str] = None
    rounds = user_msgs = tool_calls = 0
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
                if o.get("cwd") and not cwd:
                    cwd = o.get("cwd")
                if o.get("gitBranch") and not git_branch:
                    git_branch = o.get("gitBranch")
                if o.get("version"):
                    version = o.get("version")
                if o.get("permissionMode"):
                    permission_mode = o.get("permissionMode")
                if o.get("entrypoint"):
                    entrypoint = o.get("entrypoint")
                if o.get("aiTitle"):
                    ai_title = o.get("aiTitle")
                if o.get("prNumber"):
                    pr_number = o.get("prNumber")
                if o.get("prUrl"):
                    pr_url = o.get("prUrl")
                if o.get("prRepository"):
                    pr_repo = o.get("prRepository")
                if o.get("isApiErrorMessage"):
                    errors["api_error"] += 1
                tur = o.get("toolUseResult")
                if isinstance(tur, dict) and tur.get("is_error"):
                    errors["tool_error"] += 1

                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                if role == "user" or o.get("type") == "user":
                    user_msgs += 1
                    if brief is None:
                        t = _text_of(msg.get("content"))
                        if t.strip():
                            brief = t[:_BRIEF_CAP]
                if role != "assistant":
                    continue
                rkey = (msg.get("id"), o.get("requestId"))
                if rkey == (None, None):
                    noid += 1
                    rkey = ("__noid__", noid)
                if rkey in seen:
                    continue
                seen.add(rkey)
                rounds += 1
                model = msg.get("model") or "unknown"
                sr = msg.get("stop_reason")
                if sr:
                    stop_reasons[sr] = stop_reasons.get(sr, 0) + 1
                    if sr == "max_tokens":
                        errors["max_tokens"] += 1
                if model == "<synthetic>":
                    errors["synthetic"] += 1
                if "prompt is too long" in _text_of(msg.get("content")).lower():
                    errors["api_error"] += 1
                c = msg.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            tool_calls += 1
                            nm = b.get("name", "?")
                            tools[nm] = tools.get(nm, 0) + 1
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
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
        "tools": tools,
        "assistant_rounds": rounds,
        "user_msgs": user_msgs,
        "tool_calls": tool_calls,
        "cwd": cwd,
        "git_branch": git_branch,
        "version": version,
        "permission_mode": permission_mode,
        "entrypoint": entrypoint,
        "ai_title": ai_title,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "pr_repo": pr_repo,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "brief": brief,
        "errors": errors,
        "stop_reasons": stop_reasons,
    }


def aggregate_jsonl(path: str) -> dict:
    """Subset de custo de :func:`summarize_jsonl` (back-compat + paridade #445).

    Conta cada resposta da API uma única vez por ``(message.id, requestId)``
    — o claude grava o mesmo registro assistant repetido (deltas de
    streaming) com o ``usage`` final idêntico; sem dedup os totais inflam
    13-32x. Respostas sem ``id``/``requestId`` recebem chave sintética.

    Returns:
        dict com ``session_id`` (stem do arquivo), ``models`` (model ->
        {in,out,cc,cr,cc_5m,cc_1h}), ``first_ts``, ``last_ts`` e
        ``assistant_rounds``.
    """
    s = summarize_jsonl(path)
    return {
        "session_id": s["session_id"],
        "models": s["models"],
        "first_ts": s["first_ts"],
        "last_ts": s["last_ts"],
        "assistant_rounds": s["assistant_rounds"],
    }
