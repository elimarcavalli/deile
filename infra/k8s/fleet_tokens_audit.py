#!/usr/bin/env python3
"""Auditoria de tokens/custo da frota multi-worker do cluster DEILE (issue #445).

Agrega todos os worker-kinds (claude, deile, opencode, codex, qwen, goose,
aider) numa visão única ``worker-kind × modelo`` — tokens in/out/cache + custo.
O ``session_tokens_audit.py`` legado cobre apenas o claude-worker.

Camadas (SRP): coleta (I/O via ``kubectl exec``) → normalização
(``jsonl_cost`` como fonte única de custo, #445) → apresentação (Rich, adaptativo
ao terminal — princípio 15).

Descoberta da frota: ``cli_adapters.ADAPTERS`` é a fonte de truth — registrar um
adapter novo inclui o worker automaticamente.

Uso::

    python3 infra/k8s/fleet_tokens_audit.py                 # tabela + modo interativo
    python3 infra/k8s/fleet_tokens_audit.py --by-worker      # resumo por worker
    python3 infra/k8s/fleet_tokens_audit.py --top 40         # 40 sessões mais caras
    python3 infra/k8s/fleet_tokens_audit.py --last 20        # 20 mais recentes
    python3 infra/k8s/fleet_tokens_audit.py --worker opencode,qwen
    python3 infra/k8s/fleet_tokens_audit.py --model deepseek
    python3 infra/k8s/fleet_tokens_audit.py --no-interactive
    python3 infra/k8s/fleet_tokens_audit.py --export ./out   # JSON + CSV
    python3 infra/k8s/fleet_tokens_audit.py -n deile-gl      # outro namespace

Interativo: número+Enter = detalhe; ``w`` = by-worker/sessões; ``s`` = ordenação;
``e`` = export; ``r``/Enter = atualizar; ``Esc``/``q`` = sair.

Fontes de token por worker:

* claude  → ``~/.claude/projects/-home-claude-work-*/*.jsonl``
* opencode→ ``<root>/.progress/<task>.stdout.log`` NDJSON ``step_finish`` (custo nativo)
* codex   → ``<root>/.progress/<task>.stdout.log`` ``token_count``/``turn.completed``
* qwen    → ``<root>/.progress/<task>.stdout.log`` ``result.stats.models``
* goose   → ``<root>/.progress/<task>.stdout.log`` ``metadata.total_tokens``
* aider   → ``<root>/.progress/<task>.stdout.log`` texto "Tokens:"/"Cost:"
* deile   → ``~/.deile/db/usage.db`` SQLite ``usage_records``
"""

from __future__ import annotations

import argparse
import csv as _csv
import json
import os
import re
import select
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

try:
    import termios
    import tty
    _HAS_CBREAK = True
except ImportError:  # Windows / sem POSIX termios
    _HAS_CBREAK = False

# fonte única de custo — issue #445
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_progress_parse  # noqa: E402 — fonte única dos parsers de .progress
from jsonl_cost import (  # noqa: E402
    cost_of_model,
    fleet_cost_of_model,
    nocache_cost_of_model,
)


def _fleet_progress_parse_source() -> str:
    """Source de ``fleet_progress_parse`` para inlinar no parser in-pod.

    O parser roda via ``kubectl exec python3 -`` em pods que podem não ter o
    módulo no ``sys.path``; inlinamos o source para manter a fonte única.
    """
    import inspect
    return inspect.getsource(fleet_progress_parse)

# UTC−3, sem DST desde 2019
BRT = timezone(timedelta(hours=-3))

#: Núcleo: não vêm do registro de adapters CLI.
CORE_WORKERS = ("claude", "deile")


def _iso_brt(iso_str: str) -> str:
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(BRT).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str


def fleet_worker_kinds() -> list:
    """Núcleo + adapters CLI em ordem alfabética. Tolerante a falha (sem adapters → só núcleo)."""
    kinds = list(CORE_WORKERS)
    try:
        import cli_adapters  # noqa: PLC0415 — pacote opcional em infra/k8s
    except Exception:  # noqa: BLE001 — frota CLI é opcional
        return kinds
    for kind in sorted(cli_adapters.ADAPTERS):
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def adapter_declared_prices() -> dict:
    """``{model_id: {in,out,read}}`` dos preços em ``adapter.list_models()``.

    Esses preços prevalecem sobre a tabela de substring do ``jsonl_cost``
    (são mais frescos, curados pelo dono do adapter). Tolerante a falha → ``{}``.
    """
    out: dict = {}
    try:
        import cli_adapters  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return out
    for adapter in cli_adapters.ADAPTERS.values():
        try:
            models = adapter.list_models()
        except Exception:  # noqa: BLE001 — list_models é best-effort
            continue
        for m in models:
            if m.price_in is None and m.price_out is None:
                continue
            out[m.id] = {
                "in": m.price_in or 0.0,
                "out": m.price_out or 0.0,
                "read": m.cached_in if m.cached_in is not None else (m.price_in or 0.0) * 0.1,
            }
    return out


# Parser stdlib pura que roda dentro do pod via `kubectl exec python3 -` (stdin).
# Parametrizado por FLEET_KIND; emite sessões normalizadas como JSON.
IN_POD_PARSER = r'''
import json, glob, os, re, sqlite3

KIND = os.environ.get("FLEET_KIND", "")
ROOT = os.environ.get("FLEET_ROOT") or ("/home/%s/work" % KIND)
PROGRESS = os.path.join(ROOT, ".progress")
try:
    SINCE_MTIME = float(os.environ.get("FLEET_SINCE_MTIME") or 0)
except ValueError:
    SINCE_MTIME = 0.0

EMPTY = lambda: {"in": 0, "out": 0, "cc": 0, "cr": 0}


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _brief_for(task_id):
    # Tenta os nomes conhecidos que o servidor grava no workdir.
    for name in (".brief.md", "brief.md", ".deile-brief.md"):
        for cand in (os.path.join(ROOT, task_id, name),
                     os.path.join(ROOT, task_id, "repo", name)):
            try:
                with open(cand, errors="replace") as fh:
                    return fh.read()[:4000]
            except OSError:
                continue
    return None


# Subdir persistido pelo servidor (cli_worker_server._RESULT_SUBDIR).
# cli_model = fonte de verdade do modelo: goose/codex/aider NÃO emitem o modelo no stdout.
SESSIONS_DIR = os.path.join(ROOT, ".sessions")


def _meta_model_for(task_id):
    path = os.path.join(SESSIONS_DIR, "%s.json" % task_id)
    try:
        with open(path, errors="replace") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    m = meta.get("cli_model")
    return m.strip() if isinstance(m, str) and m.strip() else None


def _new_session(task_id, src):
    return {
        "worker": KIND, "task_id": task_id, "source": src,
        "models": {}, "native_cost": None,
        "first_ts": None, "last_ts": None, "brief": _brief_for(task_id),
        "mtime": _mtime(src), "meta_model": _meta_model_for(task_id),
    }


def _add(session, model, tk):
    mm = session["models"].setdefault(model or "unknown", EMPTY())
    for k in ("in", "out", "cc", "cr"):
        mm[k] += int(tk.get(k, 0) or 0)


def _apply_meta_model(session):
    # Remapeia "unknown" → cli_model do meta apenas quando o CLI não emitiu
    # nenhum modelo real (nunca sobrescreve modelo emitido de fato pelo CLI).
    meta = session.get("meta_model")
    if not meta or "unknown" not in session["models"]:
        return
    unk = session["models"].pop("unknown")
    dst = session["models"].setdefault(meta, EMPTY())
    for k in ("in", "out", "cc", "cr"):
        dst[k] += int(unk.get(k, 0) or 0)


def _iter_progress_logs():
    for f in sorted(glob.glob(os.path.join(PROGRESS, "*.stdout.log"))):
        mt = _mtime(f)
        if SINCE_MTIME and mt is not None and mt < SINCE_MTIME:
            continue
        task_id = os.path.basename(f)[:-len(".stdout.log")]
        try:
            with open(f, errors="replace") as fh:
                yield task_id, f, fh.read()
        except OSError:
            continue


# exec em namespace isolado: evita colisão com _add() local (assinatura diferente).
# O placeholder __FLEET_PROGRESS_PARSE_SOURCE__ é substituído pelo host antes de pipar.
_FPP = {}
exec(compile(__FLEET_PROGRESS_PARSE_SOURCE__, "<fleet_progress_parse>", "exec"), _FPP)
_parse_progress_text = _FPP["parse_progress_text"]


def _ledger_path():
    # Espelha cli_worker_server._cost_ledger_path.
    env = os.environ.get("DEILE_CLI_WORKER_COST_LEDGER_PATH", "").strip()
    return env or os.path.join(ROOT, ".cost-ledger.jsonl")


def _ledger_sessions(live_task_ids):
    # Sessões históricas já podadas do .progress (issue #445). Dedup: live prevalecem.
    out = []
    path = _ledger_path()
    seen = set()
    try:
        fh = open(path, errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            tid = r.get("task_id")
            models = r.get("models") or {}
            if not tid or tid in live_task_ids or tid in seen:
                continue
            if not any(sum(int(x or 0) for x in v.values()) > 0
                       for v in models.values() if isinstance(v, dict)):
                continue
            mtime = r.get("source_mtime") or r.get("harvested_at") or 0
            if SINCE_MTIME and mtime and mtime < SINCE_MTIME:
                continue
            seen.add(tid)
            out.append({
                "worker": r.get("worker") or KIND, "task_id": tid,
                "source": "<ledger>", "models": models,
                "native_cost": r.get("native_cost"),
                "first_ts": None, "last_ts": None, "brief": None,
                "mtime": mtime or None, "harvested": True,
            })
    return out


def parse_progress_kind():
    # Genérico para os 5 workers que gravam em .progress (opencode/codex/qwen/
    # goose/aider). Delega o parsing do shape nativo ao módulo compartilhado e
    # embrulha numa sessão (brief/mtime/meta_model). Espelha o harvester do ledger.
    sessions = []
    live_ids = set()
    for task_id, f, text in _iter_progress_logs():
        parsed = _parse_progress_text(KIND, text, task_id)
        if parsed is None:
            continue
        s = _new_session(task_id, f)
        for model, tk in parsed.get("models", {}).items():
            _add(s, model, tk)
        nc = parsed.get("native_cost")
        if isinstance(nc, (int, float)):
            s["native_cost"] = nc
        _apply_meta_model(s)
        if any(sum(v.values()) > 0 for v in s["models"].values()):
            sessions.append(s)
            live_ids.add(task_id)
    sessions.extend(_ledger_sessions(live_ids))
    return sessions


def parse_claude():
    BASE = "/home/claude/.claude/projects"
    sessions = []
    for f in sorted(glob.glob(os.path.join(BASE, "**", "*.jsonl"), recursive=True)):
        mt = _mtime(f)
        if SINCE_MTIME and mt is not None and mt < SINCE_MTIME:
            continue
        sid = os.path.splitext(os.path.basename(f))[0]
        s = {"worker": "claude", "task_id": sid, "source": f, "models": {},
             "native_cost": None, "first_ts": None, "last_ts": None,
             "brief": None, "mtime": mt}
        seen = set(); noid = 0
        try:
            with open(f, errors="replace") as fh:
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
                        if s["first_ts"] is None:
                            s["first_ts"] = ts
                        s["last_ts"] = ts
                    msg = o.get("message")
                    if not isinstance(msg, dict):
                        continue
                    if (msg.get("role") == "user" or o.get("type") == "user") and s["brief"] is None:
                        c = msg.get("content")
                        if isinstance(c, str) and c.strip():
                            s["brief"] = c[:4000]
                        elif isinstance(c, list):
                            parts = [b.get("text", "") for b in c
                                     if isinstance(b, dict) and b.get("type") == "text"]
                            t = "\n".join(parts)
                            if t.strip():
                                s["brief"] = t[:4000]
                    if msg.get("role") != "assistant":
                        continue
                    rkey = (msg.get("id"), o.get("requestId"))
                    if rkey == (None, None):
                        noid += 1; rkey = ("__noid__", noid)
                    if rkey in seen:
                        continue
                    seen.add(rkey)
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    _add(s, msg.get("model") or "unknown", {
                        "in": u.get("input_tokens", 0),
                        "out": u.get("output_tokens", 0),
                        "cc": u.get("cache_creation_input_tokens", 0),
                        "cr": u.get("cache_read_input_tokens", 0),
                    })
        except Exception:
            pass
        if any(sum(v.values()) > 0 for v in s["models"].values()):
            sessions.append(s)
    return sessions


def parse_deile():
    db = os.environ.get("FLEET_DEILE_DB") or os.path.join(
        os.path.expanduser("~"), ".deile", "db", "usage.db")
    if not os.path.isfile(db):
        return []
    sessions = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT timestamp, provider_id, model_id, session_id, "
            "prompt_tokens, completion_tokens, cached_tokens, cost_usd "
            "FROM usage_records ORDER BY timestamp").fetchall()
        con.close()
    except Exception:
        return []
    for r in rows:
        if SINCE_MTIME and r["timestamp"] < SINCE_MTIME:
            continue
        sid = r["session_id"] or "?"
        s = sessions.get(sid)
        if s is None:
            s = {"worker": "deile", "task_id": sid, "source": db, "models": {},
                 "native_cost": 0.0, "first_ts": None, "last_ts": None,
                 "brief": None, "mtime": r["timestamp"]}
            sessions[sid] = s
        model = "%s:%s" % (r["provider_id"] or "?", r["model_id"] or "?")
        _add(s, model, {"in": r["prompt_tokens"], "out": r["completion_tokens"],
                        "cr": r["cached_tokens"]})
        s["native_cost"] = (s["native_cost"] or 0.0) + float(r["cost_usd"] or 0.0)
        s["mtime"] = max(s["mtime"] or 0, r["timestamp"])
    return [s for s in sessions.values()
            if any(sum(v.values()) > 0 for v in s["models"].values())]


PARSERS = {k: parse_progress_kind for k in _FPP["PROGRESS_PARSERS"]}
PARSERS["claude"] = parse_claude
PARSERS["deile"] = parse_deile

fn = PARSERS.get(KIND)
print(json.dumps(fn() if fn else []))
'''

# repr() escapa o source como literal Python seguro para substituição no placeholder.
IN_POD_PARSER = IN_POD_PARSER.replace(
    "__FLEET_PROGRESS_PARSE_SOURCE__",
    repr(_fleet_progress_parse_source()),
)


def find_kubectl() -> str:
    cand = os.path.expanduser("~/.rd/bin/kubectl")
    if os.path.exists(cand):
        return cand
    found = shutil.which("kubectl")
    if found:
        return found
    sys.exit("kubectl não encontrado (nem em ~/.rd/bin/kubectl nem no PATH).")


def kubectl_json(kubectl: str, ns: str, *args: str) -> dict:
    out = subprocess.run([kubectl, "-n", ns, *args], capture_output=True, text=True)
    if out.returncode != 0:
        return {}
    try:
        return json.loads(out.stdout)
    except Exception:
        return {}


def _app_name_for(kind: str) -> str:
    """Nome do app/Deployment do worker ``<kind>`` (label ``app=`` + container)."""
    return "claude-worker" if kind == "claude" else (
        "deile-worker" if kind == "deile" else f"{kind}-worker")


def resolve_pod_for_worker(kubectl: str, ns: str, kind: str) -> str | None:
    """Pod Running do worker ``<kind>``; ``None`` se ausente (worker não instalado)."""
    app = _app_name_for(kind)
    data = kubectl_json(kubectl, ns, "get", "pods", "-l", f"app={app}", "-o", "json")
    items = data.get("items", [])
    running = [it for it in items
               if it.get("status", {}).get("phase") == "Running"]
    pool = running or items
    if not pool:
        return None
    return pool[0]["metadata"]["name"]


def worker_unavailable_reason(kubectl: str, ns: str, kind: str) -> str:
    """Motivo legível de um worker sem pod Running (distingue ausente × replicas=0).

    Consulta barata do Deployment (``kubectl get deploy <app>``) para não MENTIR
    que o worker "não está instalado" quando, na verdade, o Deployment existe e
    está apenas escalado a zero (frota pausada). Retorna a frase para o operador.
    """
    app = _app_name_for(kind)
    data = kubectl_json(kubectl, ns, "get", "deploy", app, "-o", "json")
    if not data:
        return "Deployment ausente (worker não instalado) — pulado"
    spec_replicas = (data.get("spec") or {}).get("replicas")
    ready = (data.get("status") or {}).get("readyReplicas", 0) or 0
    if spec_replicas == 0:
        return "Deployment existe mas replicas=0 (frota pausada) — pulado"
    if not ready:
        return (f"Deployment replicas={spec_replicas} mas nenhum pod Running "
                "(subindo/crash?) — pulado")
    return "sem pod Running no momento — pulado"


def _container_for(kind: str) -> str:
    return _app_name_for(kind)


def fetch_worker_sessions(kubectl: str, ns: str, kind: str, pod: str,
                          since_mtime: float = 0.0) -> list:
    """Roda o parser in-pod no pod do worker e devolve as sessões normalizadas."""
    env_prefix = f"FLEET_KIND={kind} "
    if since_mtime:
        env_prefix += f"FLEET_SINCE_MTIME={since_mtime} "
    cmd = [kubectl, "-n", ns, "exec", "-i", pod, "-c", _container_for(kind), "--",
           "sh", "-c", f"{env_prefix}python3 -"]
    proc = subprocess.run(cmd, input=IN_POD_PARSER, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    raw = proc.stdout.strip()
    try:
        return json.loads(raw)
    except Exception:
        for ln in reversed(raw.splitlines()):
            ln = ln.strip()
            if ln.startswith("["):
                try:
                    return json.loads(ln)
                except Exception:
                    return []
        return []


class TokenCollector:
    """Contrato de coleta de tokens de um worker-kind.

    A coleta I/O é comum em :func:`fetch_worker_sessions`; o que diverge por
    worker é apenas a tabela de custo. Subclasses sobrescrevem :meth:`cost_for_model`.
    """

    kind: str = ""
    uses_native_cost: bool = False  # custo nativo do CLI prevalece quando há.

    def __init__(self, declared_prices: dict | None = None) -> None:
        self.declared_prices = declared_prices or {}

    def cost_for_model(self, tk: dict, model: str) -> float:
        """Custo USD de um bloco de tokens de *model* — tabela da frota."""
        return fleet_cost_of_model(
            tk, model, declared=self.declared_prices.get(model))

    def nocache_for_model(self, tk: dict, model: str) -> float:
        """Custo hipotético sem cache (todo input a preço cheio)."""
        decl = self.declared_prices.get(model)
        p = fleet_cost_of_model
        fresh = {"in": tk.get("in", 0) + tk.get("cc", 0) + tk.get("cr", 0),
                 "out": tk.get("out", 0)}
        return p(fresh, model, declared=decl)


class ClaudeCollector(TokenCollector):
    """claude-worker — JSONL do ``claude -p``; custo via tabela opus/sonnet/haiku."""

    kind = "claude"

    def cost_for_model(self, tk: dict, model: str) -> float:
        return cost_of_model(tk, model)

    def nocache_for_model(self, tk: dict, model: str) -> float:
        return nocache_cost_of_model(tk, model)


class _NativeCostCollector(TokenCollector):
    """CLI que reporta custo nativo; aplica ``native_cost`` em :func:`enrich`, senão herda tabela da frota."""

    uses_native_cost = True


class OpenCodeCollector(_NativeCostCollector):
    kind = "opencode"


class AiderCollector(_NativeCostCollector):
    kind = "aider"


class DeileCollector(_NativeCostCollector):
    """deile-worker — UsageRepository SQLite; tokens+custo nativos do DEILE."""

    kind = "deile"


class CodexCollector(TokenCollector):
    kind = "codex"


class QwenCollector(TokenCollector):
    kind = "qwen"


class GooseCollector(TokenCollector):
    kind = "goose"


COLLECTORS = {
    c.kind: c for c in (
        ClaudeCollector, DeileCollector, OpenCodeCollector, CodexCollector,
        QwenCollector, GooseCollector, AiderCollector,
    )
}


def collector_for(kind: str, declared_prices: dict) -> TokenCollector:
    """Coletor do *kind* (genérico da frota como fallback p/ kinds novos)."""
    cls = COLLECTORS.get(kind)
    if cls is not None:
        return cls(declared_prices)
    generic = TokenCollector(declared_prices)
    generic.kind = kind
    return generic


def enrich(sessions: list, collectors: dict) -> list:
    """Calcula custo por modelo + totais de cada sessão (camada de normalização)."""
    now = datetime.now(timezone.utc)
    for s in sessions:
        coll = collectors[s["worker"]]
        tot = {"in": 0, "out": 0, "cc": 0, "cr": 0}
        per_model = {}
        cost = 0.0
        nocache = 0.0
        for model, tk in s["models"].items():
            c = coll.cost_for_model(tk, model)
            cost += c
            nocache += coll.nocache_for_model(tk, model)
            for k in tot:
                tot[k] += tk.get(k, 0)
            per_model[model] = {**tk, "cost": c}
        # Custo nativo do CLI prevalece sobre o estimado.
        native = s.get("native_cost")
        s["estimated_cost_usd"] = cost
        if coll.uses_native_cost and isinstance(native, (int, float)) and native > 0:
            s["cost_usd"] = float(native)
            s["cost_basis"] = "nativo"
        else:
            s["cost_usd"] = cost
            s["cost_basis"] = "estimado"
        s["nocache_usd"] = nocache
        s["totals"] = tot
        s["per_model"] = per_model
        s["total_tokens"] = sum(tot.values())
        try:
            ft = datetime.fromisoformat((s.get("first_ts") or "").replace("Z", "+00:00"))
            lt = datetime.fromisoformat((s.get("last_ts") or "").replace("Z", "+00:00"))
            s["duration_s"] = (lt - ft).total_seconds()
            s["last_date"] = lt.astimezone(BRT).strftime("%Y-%m-%d")
        except Exception:
            s["duration_s"] = None
            s["last_date"] = "?"
        mt = s.get("mtime")
        try:
            s["last_activity"] = (
                datetime.fromtimestamp(mt, BRT).strftime("%m-%d %H:%M")
                + f" ({fmt_age(now.timestamp() - mt)})") if mt else "?"
        except Exception:
            s["last_activity"] = "?"
    sessions.sort(key=lambda x: x["cost_usd"], reverse=True)
    return sessions


def fmt_age(sec) -> str:
    if sec is None:
        return "?"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def _human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def model_short(model: str) -> str:
    """Encurta um model-id para a tabela (remove prefixos provider/data)."""
    m = (model or "").strip()
    if not m or m == "unknown":
        return m or "?"
    # provider/sub/model → último segmento significativo, sem sufixo de data.
    base = m.split("/")[-1] if "/" in m else m
    base = re.sub(r"-\d{6,}.*$", "", base)
    return base[:26]


def title_of(s: dict) -> str:
    brief = (s.get("brief") or "").strip()
    if brief:
        return re.sub(r"\s+", " ", brief)[:80]
    return s["task_id"][:16]


def _console():
    try:
        from rich.console import Console
        return Console()
    except Exception:
        return None


class FleetRenderer:
    """Apresentação Rich, adaptativa ao terminal. Três visões: ``by-worker``, ``sessions``, ``detail``."""

    def __init__(self, console) -> None:
        self.console = console

    def render_by_worker(self, sessions: list) -> None:
        agg = {}  # worker -> {model -> {in,out,cc,cr,cost}}
        worker_tot = {}  # worker -> {tokens,cost,sessions}
        for s in sessions:
            w = s["worker"]
            wd = agg.setdefault(w, {})
            wt = worker_tot.setdefault(w, {"tokens": 0, "cost": 0.0, "sessions": 0})
            wt["sessions"] += 1
            wt["cost"] += s["cost_usd"]
            wt["tokens"] += s["total_tokens"]
            for model, pm in s["per_model"].items():
                md = wd.setdefault(model, {"in": 0, "out": 0, "cc": 0, "cr": 0, "cost": 0.0})
                for k in ("in", "out", "cc", "cr"):
                    md[k] += pm.get(k, 0)
                md["cost"] += pm["cost"]
        if self.console is None:
            for w in sorted(agg, key=lambda k: -worker_tot[k]["cost"]):
                wt = worker_tot[w]
                print(f"\n[{w}]  ${wt['cost']:.4f}  tokens={wt['tokens']:,}  "
                      f"sessões={wt['sessions']}")
                for model, md in sorted(agg[w].items(), key=lambda x: -x[1]["cost"]):
                    print(f"   {model_short(model):<28} in={md['in']:>9,} "
                          f"out={md['out']:>9,} cr={md['cr']:>9,}  ${md['cost']:.4f}")
            self._grand_total_plain(sessions)
            return
        from rich.table import Table
        t = Table(show_lines=False, expand=True,
                  title=f"Frota DEILE — tokens & custo por worker × modelo "
                        f"({len(sessions)} sessões)")
        t.add_column("Worker", style="bold magenta", no_wrap=True)
        t.add_column("Modelo", style="cyan", overflow="ellipsis", no_wrap=True, ratio=1, min_width=14)
        t.add_column("Sessões", justify="right", no_wrap=True)
        t.add_column("In", justify="right", no_wrap=True)
        t.add_column("Out", justify="right", no_wrap=True)
        t.add_column("Cache rd", justify="right", no_wrap=True)
        t.add_column("USD", justify="right", style="bold green", no_wrap=True)
        for w in sorted(agg, key=lambda k: -worker_tot[k]["cost"]):
            first = True
            for model, md in sorted(agg[w].items(), key=lambda x: -x[1]["cost"]):
                t.add_row(
                    w if first else "",
                    model_short(model),
                    str(worker_tot[w]["sessions"]) if first else "",
                    _human(md["in"]), _human(md["out"]), _human(md["cr"]),
                    f"{md['cost']:.4f}",
                )
                first = False
            wt = worker_tot[w]
            t.add_row("", "[dim]— subtotal —[/dim]", "",
                      "", "", "", f"[bold]{wt['cost']:.4f}[/bold]")
        self.console.print(t)
        self._grand_total(sessions)

    def render_sessions(self, sessions: list, top=None, start=0, count=None,
                        sort_col=None, sort_desc=True) -> None:
        rows = sessions[:top] if top else sessions
        page = rows[start:start + count] if count is not None else rows
        if self.console is None:
            print(f"{'#':>3} {'worker':<9} {'USD':>8} {'model':<22} {'tokens':>8}  title")
            for i, s in enumerate(page, start + 1):
                models = "+".join(model_short(m) for m in s["per_model"]) or "—"
                print(f"{i:>3} {s['worker']:<9} {s['cost_usd']:>8.4f} {models[:22]:<22} "
                      f"{_human(s['total_tokens']):>8}  {title_of(s)[:50]}")
            self._grand_total_plain(sessions)
            return
        from rich.table import Table
        rng = (f"  (linhas {start + 1}–{start + len(page)} de {len(rows)})"
               if count is not None else "")
        t = Table(show_lines=False, expand=True,
                  title=f"Frota DEILE — sessões por custo ({len(sessions)} sessões){rng}")
        t.add_column("#", justify="right", style="bold", no_wrap=True, min_width=3)
        t.add_column("Worker", style="bold magenta", no_wrap=True)
        t.add_column("USD", justify="right", style="bold green", no_wrap=True, max_width=8)
        t.add_column("$", no_wrap=True, max_width=4)
        t.add_column("Modelo", style="cyan", no_wrap=True, overflow="ellipsis", max_width=22)
        t.add_column("Título / brief", overflow="ellipsis", no_wrap=True, ratio=1, min_width=16)
        t.add_column("In", justify="right", no_wrap=True)
        t.add_column("Out", justify="right", no_wrap=True)
        t.add_column("Cache rd", justify="right", no_wrap=True)
        t.add_column("Últ.ativ", justify="right", style="dim", no_wrap=True)
        if sort_col:
            arrow = "↓" if sort_desc else "↑"
            for col in t.columns:
                if col.header == sort_col:
                    col.header = f"{sort_col} {arrow}"
                    break
        for i, s in enumerate(page, start + 1):
            models = "+".join(model_short(m) for m in s["per_model"]) or "—"
            basis = "[dim]≈[/dim]" if s.get("cost_basis") == "estimado" else "[green]✓[/green]"
            t.add_row(
                str(i), s["worker"], f"{s['cost_usd']:.4f}", basis,
                models, title_of(s),
                _human(s["totals"]["in"]), _human(s["totals"]["out"]),
                _human(s["totals"]["cr"]), s.get("last_activity", "?"),
            )
        self.console.print(t)
        self._grand_total(sessions)

    def render_detail(self, s: dict, rank: int) -> None:
        if self.console is None:
            print(f"\n===== #{rank} [{s['worker']}] {s['task_id']} — ${s['cost_usd']:.4f} =====")
            print(f"Fonte    : {s.get('source')}")
            print(f"Custo    : ${s['cost_usd']:.4f} ({s.get('cost_basis')})")
            print(f"Quando   : {_iso_brt(s.get('first_ts'))} → {_iso_brt(s.get('last_ts'))} BRT")
            for m, pm in sorted(s["per_model"].items(), key=lambda x: -x[1]["cost"]):
                print(f"  {m:<30} in={pm['in']:>9,} out={pm['out']:>9,} "
                      f"cr={pm['cr']:>9,}  ${pm['cost']:.4f}")
            print(f"Brief:\n{s.get('brief') or '—'}")
            return
        from rich.panel import Panel
        from rich.table import Table
        from rich.console import Group
        head = Table.grid(padding=(0, 2))
        head.add_column(style="bold cyan", justify="right", no_wrap=True)
        head.add_column(overflow="fold")
        head.add_row("Worker", s["worker"])
        head.add_row("Task / sessão", s["task_id"])
        head.add_row("Fonte", str(s.get("source")))
        head.add_row("Custo", f"${s['cost_usd']:.4f} ({s.get('cost_basis')})")
        head.add_row("Quando", f"{_iso_brt(s.get('first_ts'))} → {_iso_brt(s.get('last_ts'))} BRT")
        mt = Table(title="Tokens & custo por modelo", expand=True)
        mt.add_column("Modelo", style="cyan")
        mt.add_column("input", justify="right")
        mt.add_column("output", justify="right")
        mt.add_column("cache write", justify="right")
        mt.add_column("cache read", justify="right")
        mt.add_column("USD", justify="right", style="bold green")
        for m, pm in sorted(s["per_model"].items(), key=lambda x: -x[1]["cost"]):
            mt.add_row(m, f"{pm['in']:,}", f"{pm['out']:,}", f"{pm['cc']:,}",
                       f"{pm['cr']:,}", f"{pm['cost']:.4f}")
        brief = (s.get("brief") or "(sem brief capturado)").strip()
        self.console.print(Panel(Group(head, "", mt),
                                 title=f"DETALHE — ${s['cost_usd']:.4f}",
                                 border_style="green"))
        self.console.print(Panel(brief, title="Brief", border_style="blue"))

    def _grand_total(self, sessions: list) -> None:
        total = sum(s["cost_usd"] for s in sessions)
        by_worker = {}
        tok = 0
        for s in sessions:
            by_worker[s["worker"]] = by_worker.get(s["worker"], 0.0) + s["cost_usd"]
            tok += s["total_tokens"]
        parts = "  ".join(f"{w}=${v:.2f}" for w, v in
                          sorted(by_worker.items(), key=lambda x: -x[1]) if v > 0)
        from rich.panel import Panel
        body = (f"[bold green]Custo total da frota: ${total:.4f}[/bold green]   "
                f"tokens: {tok:,}   sessões: {len(sessions)}\n"
                f"Por worker: {parts}\n"
                f"[dim]✓ = custo nativo do CLI   ≈ = estimado via tabela de preços[/dim]")
        self.console.print(Panel(body, title="TOTAL DA FROTA", border_style="green"))

    def _grand_total_plain(self, sessions: list) -> None:
        total = sum(s["cost_usd"] for s in sessions)
        tok = sum(s["total_tokens"] for s in sessions)
        by_worker = {}
        for s in sessions:
            by_worker[s["worker"]] = by_worker.get(s["worker"], 0.0) + s["cost_usd"]
        parts = "  ".join(f"{w}=${v:.2f}" for w, v in
                          sorted(by_worker.items(), key=lambda x: -x[1]) if v > 0)
        print(f"\nTOTAL DA FROTA: ${total:.4f} | tokens {tok:,} | sessões {len(sessions)}")
        print(f"Por worker: {parts}")


def export(sessions: list, outdir: str):
    os.makedirs(outdir, exist_ok=True)
    jpath = os.path.join(outdir, "fleet_tokens_audit.json")
    cpath = os.path.join(outdir, "fleet_tokens_audit.csv")
    with open(jpath, "w") as fh:
        json.dump(sessions, fh, indent=2, ensure_ascii=False)
    cols = ["rank", "worker", "task_id", "cost_usd", "cost_basis", "estimated_cost_usd",
            "nocache_usd", "models", "in", "out", "cache_write", "cache_read",
            "total_tokens", "first_ts", "last_ts", "last_date", "source", "brief"]
    with open(cpath, "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(cols)
        for i, s in enumerate(sessions, 1):
            tot = s["totals"]
            w.writerow([
                i, s["worker"], s["task_id"], f"{s['cost_usd']:.6f}",
                s.get("cost_basis"), f"{s.get('estimated_cost_usd', 0):.6f}",
                f"{s['nocache_usd']:.6f}", "+".join(sorted(s["per_model"])),
                tot["in"], tot["out"], tot["cc"], tot["cr"], s["total_tokens"],
                s.get("first_ts") or "", s.get("last_ts") or "", s["last_date"],
                s.get("source") or "", (s.get("brief") or "").replace("\n", " ")[:200],
            ])
    return jpath, cpath


def collect_fleet(kubectl: str, ns: str, kinds: list, collectors: dict,
                  since_mtime: float = 0.0) -> list:
    """Varre cada worker da frota (kubectl exec) e devolve sessões enriquecidas."""
    all_sessions = []
    for kind in kinds:
        pod = resolve_pod_for_worker(kubectl, ns, kind)
        if not pod:
            reason = worker_unavailable_reason(kubectl, ns, kind)
            print(f"• {kind}: {reason}", file=sys.stderr)
            continue
        print(f"• {kind}: parseando uso no pod {pod}...", file=sys.stderr)
        sess = fetch_worker_sessions(kubectl, ns, kind, pod, since_mtime)
        print(f"    {len(sess)} sessões com tokens.", file=sys.stderr)
        all_sessions.extend(sess)
    return enrich(all_sessions, collectors)


SORT_MODES = [
    ("custo USD ↓", lambda s: s["cost_usd"], True, "USD"),
    ("custo USD ↑", lambda s: s["cost_usd"], False, "USD"),
    ("última atividade ↓", lambda s: s.get("mtime") or 0, True, "Últ.ativ"),
    ("tokens ↓", lambda s: s["total_tokens"], True, "In"),
    ("worker", lambda s: s["worker"], False, "Worker"),
]


def _apply_sort(sessions: list, idx: int) -> str:
    name, key, rev = SORT_MODES[idx % len(SORT_MODES)][:3]
    sessions.sort(key=key, reverse=rev)
    return name


def _clear(console):
    if console is not None:
        console.clear()
    else:
        os.system("cls" if os.name == "nt" else "clear")


class _RawKeys:
    """cbreak no stdin — leitura tecla-a-tecla (mesma técnica do legado/painel)."""

    def __init__(self):
        self._fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        self._old = None

    def __enter__(self):
        if self._fd is not None and _HAS_CBREAK:
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc):
        if self._fd is not None and self._old is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            except (OSError, termios.error):
                pass

    @property
    def enabled(self) -> bool:
        return self._fd is not None and _HAS_CBREAK

    def read(self, timeout: float):
        if self._fd is None:
            return None
        if not select.select([self._fd], [], [], timeout)[0]:
            return None
        b = os.read(self._fd, 1)
        if not b:
            return "EOF"
        if b in (b"\r", b"\n"):
            return "ENTER"
        if b in (b"\x7f", b"\x08"):
            return "BACKSPACE"
        if b == b"\x1b":
            if not select.select([self._fd], [], [], 0.05)[0]:
                return "ESC"
            code = b""
            if os.read(self._fd, 1) == b"[":
                while select.select([self._fd], [], [], 0.05)[0]:
                    c = os.read(self._fd, 1)
                    if not c:
                        break
                    code += c
                    if c.isalpha() or c == b"~":
                        break
            return {b"A": "UP", b"B": "DOWN", b"C": "RIGHT", b"D": "LEFT"}.get(code, "ARROW")
        return b.decode("utf-8", errors="ignore") or None


def _status_line(console, last_refresh, flash, sort_name, view, page_info, numbuf):
    lr = last_refresh.strftime("%H:%M:%S")
    cmds = ("número+Enter=detalhe  •  ←/→=página  •  w=por-worker/sessões  •  "
            "s=ordenar  •  e=export  •  r/Enter=atualizar  •  Esc=sair")
    pg = f"   página: {page_info}" if page_info else ""
    msg = (f"🔄 atualizado: {lr} BRT   visão: {view}   ordenação: {sort_name}{pg}\n{cmds}")
    if numbuf:
        msg += f"\n→ abrir linha: {numbuf}_"
    if flash:
        msg += f"\n» {flash}"
    if console is not None:
        from rich.panel import Panel
        console.print(Panel(msg, border_style="cyan", expand=True))
    else:
        print("\n" + msg)
    sys.stdout.write("> " + numbuf)
    sys.stdout.flush()


def interactive(console, sessions: list, top, refetch, interval: int = 60):
    renderer = FleetRenderer(console)
    view = "sessions"      # "sessions" | "by-worker" | "detail"
    sel_key = None
    sort_idx = next((i for i, m in enumerate(SORT_MODES) if "última atividade" in m[0]), 0)
    page = 0
    page_size = 30
    flash = ""
    numbuf = ""
    last_refresh = datetime.now(BRT)
    with _RawKeys() as keys:
        while True:
            sort_name = _apply_sort(sessions, sort_idx)
            _sm = SORT_MODES[sort_idx % len(SORT_MODES)]
            sort_col, sort_desc = _sm[3], _sm[2]
            worklen = min(len(sessions), top) if top else len(sessions)
            max_page = max(0, (worklen - 1) // page_size)
            page = min(page, max_page)
            _clear(console)
            page_info = ""
            if view == "by-worker":
                renderer.render_by_worker(sessions)
            elif view == "detail":
                s = next((x for x in sessions
                          if (x["worker"], x["task_id"]) == sel_key), None)
                if s is None:
                    flash = "sessão saiu do PVC — voltando ao ranking"
                    view = "sessions"
                    renderer.render_sessions(sessions, top, page * page_size,
                                             page_size, sort_col, sort_desc)
                    page_info = f"{page + 1}/{max_page + 1}"
                else:
                    renderer.render_detail(s, sessions.index(s) + 1)
            else:
                renderer.render_sessions(sessions, top, page * page_size,
                                         page_size, sort_col, sort_desc)
                page_info = f"{page + 1}/{max_page + 1}"
            _status_line(console, last_refresh, flash, sort_name, view, page_info, numbuf)
            flash = ""

            next_refresh = last_refresh + timedelta(seconds=interval)
            timeout = max(0.0, (next_refresh - datetime.now(BRT)).total_seconds())
            try:
                if keys.enabled:
                    k = keys.read(timeout)
                else:
                    rl, _, _ = select.select([sys.stdin], [], [], timeout)
                    if not rl:
                        k = None
                    else:
                        ln = sys.stdin.readline()
                        k = "EOF" if ln == "" else (ln.strip().lower() or "ENTER")
            except (KeyboardInterrupt, OSError):
                print()
                return

            if k is None:
                new = refetch()
                last_refresh = datetime.now(BRT)
                if new is not None:
                    sessions[:] = new
                else:
                    flash = "falha ao atualizar — mantendo dados"
                continue
            if k == "EOF":
                return
            if k == "RIGHT":
                view = "sessions"
                page += 1
                continue
            if k == "LEFT":
                view = "sessions"
                page = max(0, page - 1)
                continue
            if k in ("UP", "DOWN", "ARROW"):
                continue
            if k == "BACKSPACE":
                numbuf = numbuf[:-1]
                continue
            if k == "ESC":
                if numbuf:
                    numbuf = ""
                    continue
                if view != "sessions":
                    view = "sessions"
                    continue
                return
            if not keys.enabled and k != "ENTER":
                if k.isdigit():
                    numbuf = k
                    k = "ENTER"
                else:
                    k = k[:1]
            if k == "ENTER":
                if numbuf:
                    idx = int(numbuf)
                    numbuf = ""
                    if 1 <= idx <= len(sessions):
                        view = "detail"
                        sel_key = (sessions[idx - 1]["worker"], sessions[idx - 1]["task_id"])
                    else:
                        flash = f"linha fora do intervalo (1..{len(sessions)})"
                else:
                    new = refetch()
                    last_refresh = datetime.now(BRT)
                    flash = "atualizado" if new is not None else "falha ao atualizar"
                    if new is not None:
                        sessions[:] = new
                continue
            if k.isdigit():
                numbuf += k
                continue
            numbuf = ""
            if k == "q":
                return
            if k == "r":
                new = refetch()
                last_refresh = datetime.now(BRT)
                flash = "atualizado" if new is not None else "falha ao atualizar"
                if new is not None:
                    sessions[:] = new
                continue
            if k == "s":
                sort_idx = (sort_idx + 1) % len(SORT_MODES)
                flash = f"ordenação: {SORT_MODES[sort_idx][0]}"
                view = "sessions"
                page = 0
                continue
            if k == "w":
                view = "by-worker" if view != "by-worker" else "sessions"
                continue
            if k == "e":
                j, _c = export(sessions, "./fleet-audit-out")
                flash = f"exportado: {j}"
                continue
            flash = "tecla não reconhecida"


def _parse_worker_filter(arg, kinds):
    if not arg:
        return kinds
    want = {w.strip().lower() for w in arg.split(",") if w.strip()}
    return [k for k in kinds if k in want]


def filter_by_model(sessions: list, slug):
    if not slug:
        return sessions
    needle = slug.lower()
    return [s for s in sessions
            if any(needle in m.lower() for m in s.get("per_model", {}))]


def main():
    ap = argparse.ArgumentParser(
        description="Auditoria de tokens/custo da FROTA multi-worker do cluster DEILE.")
    ap.add_argument("-n", "--namespace", default=os.environ.get("DEILE_K8S_NAMESPACE", "deile"))
    ap.add_argument("--worker", default=None, metavar="KINDS",
                    help="CSV de worker-kinds a auditar (ex.: opencode,qwen); default = todos")
    ap.add_argument("--top", type=int, default=None, help="mostra só as N sessões mais caras")
    ap.add_argument("--by-worker", action="store_true",
                    help="só o resumo por worker × modelo (não-interativo)")
    ap.add_argument("--no-interactive", action="store_true",
                    help="imprime tabelas e sai")
    ap.add_argument("--export", nargs="?", const="./fleet-audit-out", default=None,
                    metavar="DIR", help="grava JSON + CSV no diretório")
    ap.add_argument("--last", nargs="?", type=int, const=-1, default=None, metavar="N",
                    help="ordena por última atividade; com N, só as N mais recentes")
    ap.add_argument("--model", default=None, metavar="SLUG",
                    help="filtra sessões cujo modelo contém SLUG (substring)")
    args = ap.parse_args()

    kubectl = find_kubectl()
    ns = args.namespace
    kinds = _parse_worker_filter(args.worker, fleet_worker_kinds())
    if not kinds:
        sys.exit("Nenhum worker-kind selecionado (verifique --worker).")
    declared = adapter_declared_prices()
    collectors = {k: collector_for(k, declared) for k in fleet_worker_kinds()}
    console = _console()

    print(f"• namespace={ns}  workers={','.join(kinds)}", file=sys.stderr)
    sessions = collect_fleet(kubectl, ns, kinds, collectors)
    sessions = filter_by_model(sessions, args.model)
    if not sessions:
        sys.exit("Nenhuma sessão com tokens encontrada na frota.")

    last_requested = args.last is not None
    last_n = args.last if (last_requested and args.last and args.last > 0) else 0
    if last_requested:
        sessions.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
        if last_n:
            sessions = sessions[:last_n]
    print(f"• {len(sessions)} sessões com uso de tokens na frota.\n", file=sys.stderr)

    renderer = FleetRenderer(console)

    if args.export is not None:
        j, c = export(sessions, args.export)
        print(f"Exportado:\n  {j}\n  {c}")

    if args.by_worker:
        renderer.render_by_worker(sessions)
        return

    if args.no_interactive or not sys.stdin.isatty():
        renderer.render_sessions(sessions, args.top)
        renderer.render_by_worker(sessions)
        return

    def refetch():
        try:
            data = collect_fleet(kubectl, ns, kinds, collectors)
            data = filter_by_model(data, args.model)
            if last_n:
                data.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
                data = data[:last_n]
            return data
        except (SystemExit, Exception):
            return None

    interactive(console, sessions, args.top, refetch)


if __name__ == "__main__":
    main()
