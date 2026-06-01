#!/usr/bin/env python3
"""Relatório de uso de Claude Code dentro dos PVCs/pods do cluster DEILE.

Varre os arquivos de sessão JSONL que o `claude -p` grava no PVC
``claude-worker-home`` (montado em ``/home/claude`` no pod ``claude-worker``),
calcula o custo em USD de cada sessão com os preços oficiais vigentes da API
Anthropic e apresenta um ranking ordenado da sessão mais cara para a mais
barata.

Como funciona
-------------
O trabalho pesado (parse dos JSONL + ``git status`` de cada worktree) roda
DENTRO do pod via um único ``kubectl exec`` — o pod tem os dados e o git. O
parser emite JSON cru (contagem de tokens por modelo, tool calls, timestamps,
brief, PR, erros, git status). O host então calcula o custo em USD, ordena e
renderiza a UI (tabela Rich + drill-down interativo).

Uso
---
    python3 infra/k8s/session_tokens_audit.py                 # tabela + modo interativo
    python3 infra/k8s/session_tokens_audit.py --top 40        # só as 40 mais caras
    python3 infra/k8s/session_tokens_audit.py --no-interactive
    python3 infra/k8s/session_tokens_audit.py --detail        # dump completo de cada sessão
    python3 infra/k8s/session_tokens_audit.py --export ./out  # grava JSON + CSV
    python3 infra/k8s/session_tokens_audit.py -n deile-gl     # outro namespace
    python3 infra/k8s/session_tokens_audit.py --pod claude-worker-xxxx

No modo interativo: digite o NÚMERO de uma linha para ver os detalhes completos
daquela sessão; ``a`` = agregados, ``e`` = exportar, ``q`` = sair.

Preços (USD/MTok) — fonte: https://platform.claude.com/docs/en/about-claude/pricing
(cache write assumido 5m = padrão do Claude Code, salvo breakdown explícito no JSONL).
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

# Fonte única da lógica de custo (issue #445) — compartilhada com o harvester
# do ledger em ``claude_worker_server``. Garante que o custo de uma sessão
# colhida para o ledger é idêntico ao calculado a partir do JSONL vivo.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jsonl_cost import cost_of_model, nocache_cost_of_model  # noqa: E402

# Todas as datas exibidas neste relatório são em BRT (UTC−3, sem DST desde 2019).
BRT = timezone(timedelta(hours=-3))


def _iso_brt(iso_str: str) -> str:
    """Converte um timestamp ISO (UTC, sufixo Z) para 'YYYY-MM-DD HH:MM:SS' em BRT."""
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(BRT).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str

# Preços e cálculo de custo migrados para ``jsonl_cost`` (fonte única, #445):
# ``PRICING`` / ``pricing_for`` / ``cost_of_model`` / ``nocache_cost_of_model``
# são importados no topo deste arquivo.


# --------------------------------------------------------------------------- #
# Parser que roda DENTRO do pod (stdlib pura).                                 #
# --------------------------------------------------------------------------- #
IN_POD_PARSER = r'''
import json, glob, os, subprocess, re

BASE = "/home/claude/.claude/projects"
TASKS = "/home/claude/.claude/tasks"
RUN_GIT = os.environ.get("AUDIT_NO_GIT") != "1"
# Filtro opt-in por mtime (epoch): pula JSONL mais antigos que SINCE. 0 = tudo
# (comportamento padrão). Usado pelo painel para custo "hoje" sem reparsear
# todo o histórico (carga proporcional só aos arquivos do dia).
try:
    SINCE_MTIME = float(os.environ.get("AUDIT_SINCE_MTIME") or 0)
except ValueError:
    SINCE_MTIME = 0.0

def read_task_meta(project_dir):
    # O JSONL vive em projects/-home-claude-work-<task_id>/<session>.jsonl; o
    # session.json (ground truth de model/effort/ultracode/stage) vive em
    # tasks/<task_id>/session.json. O task_id sai do sufixo do project_dir.
    base = os.path.basename(project_dir)
    marker = "-home-claude-work-"
    if marker not in base:
        return {}
    tid = base.split(marker)[-1]
    try:
        with open(os.path.join(TASKS, tid, "session.json")) as fh:
            return json.load(fh)
    except Exception:
        return {}

STAGE_HINTS = [
    ("pr_review",  re.compile(r"revisor de PR|Revise rigorosamente|review da PR|pr[_ ]review", re.I)),
    ("pr_unified", re.compile(r"abra a PR|estado real da PR|pr_unified|atender thread", re.I)),
    ("refine",     re.compile(r"refinamento|refine|reescrev|portão de refinamento", re.I)),
    ("classify",   re.compile(r"classifiqu|CLARO ou VAGO|triagem", re.I)),
    ("decompose",  re.compile(r"decomponha|decompos|derivad|architect", re.I)),
    ("implement",  re.compile(r"implement|\.deile-progress|abra (a )?PR ao final", re.I)),
]

def guess_stage(brief):
    for name, rx in STAGE_HINTS:
        if rx.search(brief or ""):
            return name
    return "unknown"

def git_status(cwd):
    if not RUN_GIT or not cwd:
        return {"state": "n/a", "modified": 0, "untracked": 0, "staged": 0, "root": None}
    if not os.path.isdir(cwd):
        return {"state": "gone", "modified": 0, "untracked": 0, "staged": 0, "root": None}
    # o repo pode estar no próprio cwd OU num subdir ./repo (clone do worker).
    # .git pode ser diretório (clone) ou arquivo (worktree) — os.path.exists cobre ambos.
    root = None
    for cand in (cwd, os.path.join(cwd, "repo")):
        if os.path.exists(os.path.join(cand, ".git")):
            root = cand
            break
    if root is None:
        return {"state": "no-git", "modified": 0, "untracked": 0, "staged": 0, "root": None}
    try:
        out = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", root, "status",
             "--porcelain=v1", "--untracked-files=all"],
            capture_output=True, text=True, timeout=8,
        )
    except Exception:
        return {"state": "error", "modified": 0, "untracked": 0, "staged": 0, "root": root}
    if out.returncode != 0:
        return {"state": "error", "modified": 0, "untracked": 0, "staged": 0, "root": root}
    mod = unt = stg = 0
    for ln in out.stdout.splitlines():
        if not ln:
            continue
        x, y = (ln[0] if len(ln) > 0 else " "), (ln[1] if len(ln) > 1 else " ")
        if x == "?" and y == "?":
            unt += 1
            continue
        if x not in (" ", "?"):
            stg += 1
        if y not in (" ", "?"):
            mod += 1
    state = "clean" if (mod + unt + stg) == 0 else "dirty"
    return {"state": state, "modified": mod, "untracked": unt, "staged": stg, "root": root}

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""

sessions = []
for f in sorted(glob.glob(os.path.join(BASE, "**", "*.jsonl"), recursive=True)):
    if SINCE_MTIME and os.path.getmtime(f) < SINCE_MTIME:
        continue
    rec = {
        "jsonl": f,
        "project_dir": os.path.dirname(f),
        "session_file": os.path.basename(f),
        "models": {},          # model -> {in,out,cc,cr,cc_5m,cc_1h}
        "tools": {},           # tool name -> count
        "assistant_rounds": 0,
        "user_msgs": 0,
        "tool_calls": 0,
        "cwd": None,
        "git_branch": None,
        "version": None,
        "permission_mode": None,
        "entrypoint": None,
        "ai_title": None,
        "pr_number": None,
        "pr_url": None,
        "pr_repo": None,
        "first_ts": None,
        "last_ts": None,
        "brief": None,
        "errors": {"synthetic": 0, "max_tokens": 0, "api_error": 0, "tool_error": 0},
        "stop_reasons": {},
    }
    seen_resp = set()   # dedup de respostas assistant por (message.id, requestId)
    nonid_ctr = [0]
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
                    if rec["first_ts"] is None:
                        rec["first_ts"] = ts
                    rec["last_ts"] = ts
                if o.get("cwd") and not rec["cwd"]:
                    rec["cwd"] = o.get("cwd")
                if o.get("gitBranch") and not rec["git_branch"]:
                    rec["git_branch"] = o.get("gitBranch")
                if o.get("version"):
                    rec["version"] = o.get("version")
                if o.get("permissionMode"):
                    rec["permission_mode"] = o.get("permissionMode")
                if o.get("entrypoint"):
                    rec["entrypoint"] = o.get("entrypoint")
                if o.get("aiTitle"):
                    rec["ai_title"] = o.get("aiTitle")
                if o.get("prNumber"):
                    rec["pr_number"] = o.get("prNumber")
                if o.get("prUrl"):
                    rec["pr_url"] = o.get("prUrl")
                if o.get("prRepository"):
                    rec["pr_repo"] = o.get("prRepository")
                if o.get("isApiErrorMessage"):
                    rec["errors"]["api_error"] += 1
                tur = o.get("toolUseResult")
                if isinstance(tur, dict) and tur.get("is_error"):
                    rec["errors"]["tool_error"] += 1

                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                if role == "user" or o.get("type") == "user":
                    rec["user_msgs"] += 1
                    if rec["brief"] is None:
                        t = text_of(msg.get("content"))
                        if t.strip():
                            rec["brief"] = t[:4000]
                if role == "assistant":
                    # claude grava o MESMO registro assistant repetido (deltas de
                    # streaming), todos com o usage final IDÊNTICO. Sem dedup, In/
                    # Out/Cache/Rnd/Tools/custo inflam ~13-32x. Conta cada resposta
                    # da API uma vez por (message.id, requestId) — provado contra o
                    # last_total_cost_usd do fornecedor (erro < 0,1%).
                    rkey = (msg.get("id"), o.get("requestId"))
                    if rkey == (None, None):
                        nonid_ctr[0] += 1
                        rkey = ("__noid__", nonid_ctr[0])
                    if rkey in seen_resp:
                        continue
                    seen_resp.add(rkey)
                    rec["assistant_rounds"] += 1
                    model = msg.get("model") or "unknown"
                    sr = msg.get("stop_reason")
                    if sr:
                        rec["stop_reasons"][sr] = rec["stop_reasons"].get(sr, 0) + 1
                        if sr == "max_tokens":
                            rec["errors"]["max_tokens"] += 1
                    if model == "<synthetic>":
                        rec["errors"]["synthetic"] += 1
                    txt = text_of(msg.get("content"))
                    if "Prompt is too long" in txt or "prompt is too long" in txt:
                        rec["errors"]["api_error"] += 1
                    c = msg.get("content")
                    if isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                rec["tool_calls"] += 1
                                nm = b.get("name", "?")
                                rec["tools"][nm] = rec["tools"].get(nm, 0) + 1
                    u = msg.get("usage")
                    if isinstance(u, dict):
                        mm = rec["models"].setdefault(
                            model, {"in": 0, "out": 0, "cc": 0, "cr": 0, "cc_5m": 0, "cc_1h": 0})
                        mm["in"] += u.get("input_tokens", 0) or 0
                        mm["out"] += u.get("output_tokens", 0) or 0
                        cc = u.get("cache_creation_input_tokens", 0) or 0
                        mm["cc"] += cc
                        mm["cr"] += u.get("cache_read_input_tokens", 0) or 0
                        ccd = u.get("cache_creation")
                        if isinstance(ccd, dict):
                            mm["cc_5m"] += ccd.get("ephemeral_5m_input_tokens", 0) or 0
                            mm["cc_1h"] += ccd.get("ephemeral_1h_input_tokens", 0) or 0
    except Exception:
        pass
    try:
        rec["mtime"] = os.path.getmtime(f)
    except Exception:
        rec["mtime"] = None
    rec["git"] = git_status(rec["cwd"])
    meta = read_task_meta(rec["project_dir"])
    rec["meta_model"] = meta.get("model")
    rec["reasoning_effort"] = meta.get("reasoning_effort")
    rec["ultracode"] = meta.get("ultracode")
    # stage do meta é ground truth (o pipeline gravou); regex do brief é fallback.
    rec["stage"] = meta.get("stage") or guess_stage(rec["brief"])
    # só inclui sessões que tenham ao menos uma resposta assistant com tokens
    if any((sum(v.values()) > 0) for v in rec["models"].values()):
        sessions.append(rec)

# Ledger de custo (issue #445): sessões já podadas do disco vivem só aqui.
# O harvester do claude_worker_server colheu o RESUMO COMPLETO de cada sessão
# (tokens + título/brief/tools/PR/erros/meta) ANTES de remover o JSONL
# volumoso. Emitimos um registro sintético por sessão colhida (não duplicando
# session_ids com JSONL vivo) com a MESMA estrutura — custo E detalhe idênticos
# aos da sessão viva. Registros v:1 (legados, só tokens) degradam para vazio
# nos campos de detalhe; v:2 carrega tudo.
LEDGER = os.environ.get("DEILE_CLAUDE_COST_LEDGER_PATH") or os.path.join(
    os.path.expanduser("~"), ".claude", "cost-ledger.jsonl")
live_ids = set()
for s in sessions:
    sf = s.get("session_file") or ""
    if sf.endswith(".jsonl"):
        live_ids.add(sf[:-6])
seen_led = set()
try:
    with open(LEDGER, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            sid = r.get("session_id")
            models = r.get("models") or {}
            if not sid or sid in live_ids or sid in seen_led:
                continue
            if not any((sum(v.values()) > 0) for v in models.values()):
                continue
            # "Última atividade" real = mtime do transcript colhido (source_mtime,
            # v:2); v:1 cai para harvested_at. O filtro de recência (painel
            # "custo hoje") usa o mesmo carimbo.
            harv = r.get("harvested_at") or 0
            rec_mtime = r.get("source_mtime") or harv or 0
            if SINCE_MTIME and rec_mtime and rec_mtime < SINCE_MTIME:
                continue
            seen_led.add(sid)
            tid = r.get("task_id") or ""
            errs = r.get("errors") or {}
            sessions.append({
                "jsonl": "<ledger>",
                "project_dir": os.path.join(BASE, "-home-claude-work-" + tid),
                "session_file": sid + ".jsonl",
                "models": models,
                "tools": r.get("tools") or {},
                "assistant_rounds": r.get("assistant_rounds", 0) or 0,
                "user_msgs": r.get("user_msgs", 0) or 0,
                "tool_calls": r.get("tool_calls", 0) or 0,
                "cwd": r.get("cwd"),
                "git_branch": r.get("git_branch"),
                "version": r.get("version"),
                "permission_mode": r.get("permission_mode"),
                "entrypoint": r.get("entrypoint"),
                "ai_title": r.get("ai_title"),
                "pr_number": r.get("pr_number"),
                "pr_url": r.get("pr_url"),
                "pr_repo": r.get("pr_repo"),
                "first_ts": r.get("first_ts"),
                "last_ts": r.get("last_ts"),
                "brief": r.get("brief"),
                "errors": {"synthetic": errs.get("synthetic", 0) or 0,
                           "max_tokens": errs.get("max_tokens", 0) or 0,
                           "api_error": errs.get("api_error", 0) or 0,
                           "tool_error": errs.get("tool_error", 0) or 0},
                "stop_reasons": r.get("stop_reasons") or {},
                "mtime": rec_mtime or None,
                "git": {"state": "harvested", "modified": 0,
                        "untracked": 0, "staged": 0, "root": None},
                "meta_model": r.get("meta_model"),
                "reasoning_effort": r.get("reasoning_effort"),
                "ultracode": r.get("ultracode"),
                "stage": r.get("stage") or "harvested",
                "harvested": True,
            })
except Exception:
    pass

print(json.dumps(sessions))
'''


# --------------------------------------------------------------------------- #
# Host helpers                                                                 #
# --------------------------------------------------------------------------- #
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


def resolve_pod(kubectl: str, ns: str, override: str | None) -> str:
    if override:
        return override
    data = kubectl_json(kubectl, ns, "get", "pods", "-l", "app=claude-worker",
                        "-o", "json")
    items = data.get("items", [])
    running = [it for it in items
               if it.get("status", {}).get("phase") == "Running"]
    pool = running or items
    if not pool:
        sys.exit(f"Nenhum pod claude-worker encontrado no namespace '{ns}'. "
                 f"Use --pod <nome> ou -n <namespace>.")
    return pool[0]["metadata"]["name"]


def resolve_pvc(kubectl: str, ns: str) -> str:
    data = kubectl_json(kubectl, ns, "get", "deploy", "claude-worker", "-o", "json")
    vols = data.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
    for v in vols:
        pvc = v.get("persistentVolumeClaim", {}).get("claimName")
        if pvc and "home" in v.get("name", ""):
            return pvc
    for v in vols:
        pvc = v.get("persistentVolumeClaim", {}).get("claimName")
        if pvc:
            return pvc
    return "claude-worker-home"


def fetch_sessions(kubectl: str, ns: str, pod: str, no_git: bool,
                   since_mtime: float = 0.0) -> list:
    env_prefix = "AUDIT_NO_GIT=1 " if no_git else ""
    if since_mtime:
        env_prefix += f"AUDIT_SINCE_MTIME={since_mtime} "
    cmd = [kubectl, "-n", ns, "exec", "-i", pod, "-c", "claude-worker", "--",
           "sh", "-c", f"{env_prefix}python3 -"]
    proc = subprocess.run(cmd, input=IN_POD_PARSER, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"Falha ao executar parser no pod:\n{proc.stderr[:2000]}")
    raw = proc.stdout.strip()
    # o exec pode prefixar a linha "Defaulted container..." no stderr (já isolado);
    # garante que pegamos só o JSON (última linha não-vazia).
    try:
        return json.loads(raw)
    except Exception:
        for ln in reversed(raw.splitlines()):
            ln = ln.strip()
            if ln.startswith("["):
                return json.loads(ln)
        sys.exit(f"Saída do parser não é JSON válido:\n{raw[:1000]}")


# --------------------------------------------------------------------------- #
# Cálculo / enriquecimento no host                                            #
# --------------------------------------------------------------------------- #
def enrich(sessions: list, pvc: str) -> list:
    now = datetime.now(timezone.utc)
    for s in sessions:
        tot = {"in": 0, "out": 0, "cc": 0, "cr": 0}
        cost = 0.0
        nocache = 0.0
        per_model = {}
        for model, tk in s["models"].items():
            c = cost_of_model(tk, model)
            cost += c
            nocache += nocache_cost_of_model(tk, model)
            for k in tot:
                tot[k] += tk.get(k, 0)
            per_model[model] = {**tk, "cost": c}
        s["pvc"] = pvc
        s["totals"] = tot
        s["cost_usd"] = cost
        s["nocache_usd"] = nocache
        s["per_model"] = per_model
        s["total_tokens"] = sum(tot.values())
        cr = tot["cr"]
        in_like = tot["in"] + tot["cc"] + tot["cr"]
        s["cache_pct"] = (cr / in_like * 100.0) if in_like else 0.0
        # duração e idade
        dur = age = created = None
        try:
            ft = datetime.fromisoformat(s["first_ts"].replace("Z", "+00:00"))
            lt = datetime.fromisoformat(s["last_ts"].replace("Z", "+00:00"))
            dur = (lt - ft).total_seconds()
            age = (now - lt).total_seconds()          # desde a última atividade
            created = (now - ft).total_seconds()       # desde a criação (1ª ts)
            s["last_date"] = lt.astimezone(BRT).strftime("%Y-%m-%d")
        except Exception:
            s["last_date"] = "?"
        s["duration_s"] = dur
        s["age_s"] = age
        s["created_s"] = created
        mt = s.get("mtime")
        try:
            if mt:
                s["last_activity"] = (
                    datetime.fromtimestamp(mt, BRT).strftime("%m-%d %H:%M:%S")
                    + f" ({fmt_age(now.timestamp() - mt)})")
            else:
                s["last_activity"] = "?"
        except Exception:
            s["last_activity"] = "?"
        s["tokens_per_min"] = (s["total_tokens"] / (dur / 60.0)) if dur else 0.0
        s["usd_per_round"] = (cost / s["assistant_rounds"]) if s["assistant_rounds"] else 0.0
    sessions.sort(key=lambda x: x["cost_usd"], reverse=True)
    return sessions


def _model_family(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    if model == "<synthetic>":
        return "synthetic"
    return model


def model_short(s: dict) -> str:
    names = []
    for m in sorted(s["per_model"], key=lambda k: -s["per_model"][k]["cost"]):
        ml = m.lower()
        if "opus" in ml:
            names.append("opus")
        elif "sonnet" in ml:
            names.append("sonnet")
        elif "haiku" in ml:
            names.append("haiku")
        elif m == "<synthetic>":
            continue
        else:
            names.append(m[:8])
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return "+".join(out) or "—"


def _short_model_slug(full: str) -> str:
    """'claude-opus-4-7-20250514' → 'opus-4-7'; 'claude-sonnet-4-6' → 'sonnet-4-6'."""
    m = (full or "").strip()
    if not m or m == "<synthetic>":
        return ""
    if m.startswith("claude-"):
        m = m[len("claude-"):]
    return re.sub(r"-\d{6,}.*$", "", m)  # tira o sufixo de data


# Assinatura do preâmbulo ultracode injetado pelo claude_worker_server (fallback
# para sessões antigas cujo session.json não tem o campo `ultracode`).
_ULTRA_SIG = "orquestre via workflow multi-agente"


def reasoning_suffix(s: dict) -> str:
    """Nível de reasoning para exibir ao lado do modelo.

    ultracode (= xhigh + workflow) ganha rótulo próprio; senão mostra o effort
    cru (low/medium/high/xhigh/max). Vazio quando não há info.
    """
    if s.get("ultracode") is True:
        return "ultracode"
    eff = (s.get("reasoning_effort") or "").strip()
    if (s.get("ultracode") is None and eff == "xhigh"
            and _ULTRA_SIG in (s.get("brief") or "").lower()):
        return "ultracode"
    return eff


def model_label(s: dict, dim: bool = False) -> str:
    """Modelo versionado + reasoning, ex.: 'opus-4-7 (xhigh)', 'sonnet-4-6 (ultracode)'."""
    full = s.get("meta_model")
    if not full:
        cand = [m for m in s.get("per_model", {}) if m != "<synthetic>"]
        if cand:
            full = max(cand, key=lambda k: s["per_model"][k]["cost"])
    slug = _short_model_slug(full) or model_short(s)
    r = reasoning_suffix(s)
    if not r:
        return slug
    return f"{slug} [dim]({r})[/dim]" if dim else f"{slug} ({r})"


def fmt_age(sec) -> str:
    if sec is None:
        return "?"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 120:
        return f"{sec // 60}m{sec % 60:02d}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    return f"{sec // 86400}d{(sec % 86400) // 3600:02d}h"


def fmt_dur(sec) -> str:
    if sec is None:
        return "?"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def git_label(g: dict) -> str:
    st = g.get("state")
    if st == "clean":
        return "limpo"
    if st == "dirty":
        parts = []
        if g.get("modified"):
            parts.append(f"{g['modified']}M")
        if g.get("untracked"):
            parts.append(f"{g['untracked']}U")
        if g.get("staged"):
            parts.append(f"{g['staged']}S")
        return " ".join(parts) or "dirty"
    return {"gone": "removida", "no-git": "sem-git",
            "error": "erro", "n/a": "—"}.get(st, st or "?")


def git_state_label(g: dict) -> str:
    return {"clean": "limpo", "dirty": "sujo", "gone": "removida",
            "no-git": "sem-git", "error": "erro", "n/a": "—"}.get(
                g.get("state"), g.get("state") or "?")


def working_label(g: dict) -> str:
    parts = []
    if g.get("modified"):
        parts.append(f"{g['modified']}M")
    if g.get("untracked"):
        parts.append(f"{g['untracked']}U")
    if g.get("staged"):
        parts.append(f"{g['staged']}S")
    return " ".join(parts) or "—"


def session_kind(s: dict) -> str:
    """'agent' para disparos de subagente (agent-*.jsonl / sob /subagents/), senão 'sessão'."""
    if s.get("session_file", "").startswith("agent-") or "/subagents/" in (s.get("jsonl") or ""):
        return "agent"
    return "sessão"


def pr_label(s: dict) -> str:
    n = s.get("pr_number")
    if not n:
        return "—"
    return f"trab #{n}" if s.get("stage") in ("pr_review", "pr_unified") else f"abriu #{n}"


def title_of(s: dict) -> str:
    return (s.get("ai_title")
            or (f"[{s['stage']}] {s.get('pr_repo') or ''} #{s['pr_number']}"
                if s.get("pr_number") else None)
            or s.get("stage")
            or s["session_file"][:8])


# --------------------------------------------------------------------------- #
# Rendering (Rich, adaptativo a console.width — sem larguras literais)         #
# --------------------------------------------------------------------------- #
def _console():
    try:
        from rich.console import Console
        return Console()
    except Exception:
        return None


def render_table(console, sessions: list, top: int | None, start: int = 0,
                 count: int | None = None, sort_col: str | None = None,
                 sort_desc: bool = True):
    rows = sessions[:top] if top else sessions
    page_rows = rows[start:start + count] if count is not None else rows
    if console is None:
        # fallback texto puro
        print(f"{'#':>3} {'USD':>8} {'model':<22} {'git':<10} {'rounds':>6} "
              f"{'tools':>5} {'age':>7}  title")
        for i, s in enumerate(page_rows, start + 1):
            print(f"{i:>3} {s['cost_usd']:>8.4f} {model_label(s):<22} "
                  f"{git_label(s['git']):<10} {s['assistant_rounds']:>6} "
                  f"{s['tool_calls']:>5} {fmt_age(s.get('created_s')):>7}  {title_of(s)[:50]}")
        return
    from rich.table import Table
    pvc = page_rows[0]["pvc"] if page_rows else (rows[0]["pvc"] if rows else "?")
    rng = (f"  (linhas {start + 1}–{start + len(page_rows)} de {len(rows)})"
           if count is not None else "")
    t = Table(show_lines=False, expand=True,
              title=f"Claude Code — uso por sessão (PVC {pvc}) — {len(sessions)} sessões{rng}")
    t.add_column("#", justify="right", style="bold", no_wrap=True)
    t.add_column("Tipo", no_wrap=True)
    t.add_column("USD", justify="right", style="bold green", no_wrap=True)
    t.add_column("Modelo", style="cyan", no_wrap=True)
    t.add_column("Título / brief", overflow="ellipsis", no_wrap=True, max_width=36)
    t.add_column("PR", justify="right", style="blue", no_wrap=True)
    t.add_column("Git", justify="center", no_wrap=True)
    t.add_column("Working", justify="center", no_wrap=True)
    t.add_column("Rnd", justify="right", no_wrap=True)
    t.add_column("Tools c/d", justify="right", no_wrap=True)
    t.add_column("In", justify="right", no_wrap=True)
    t.add_column("Out", justify="right", no_wrap=True)
    t.add_column("Cache%", justify="right", no_wrap=True)
    t.add_column("Últ.ativ", justify="right", style="dim", no_wrap=True)
    t.add_column("Idade", justify="right", style="dim", no_wrap=True)
    if sort_col:  # setinha ↓/↑ na coluna ordenada
        arrow = "↓" if sort_desc else "↑"
        for col in t.columns:
            if col.header == sort_col:
                col.header = f"{sort_col} {arrow}"
                break
    for i, s in enumerate(page_rows, start + 1):
        err = s["errors"]
        err_flag = ""
        if err["api_error"] or err["max_tokens"] or err["tool_error"]:
            err_flag = " [red]⚠[/red]"
        st = git_state_label(s["git"])
        gstyle = ("green" if st == "limpo"
                  else "dim" if st in ("removida", "—", "sem-git")
                  else "yellow")
        wl = working_label(s["git"])
        wl_cell = f"[yellow]{wl}[/yellow]" if wl != "—" else "—"
        kind = session_kind(s)
        kind_cell = "[magenta]🤖 agent[/magenta]" if kind == "agent" else "💬 sessão"
        t.add_row(
            str(i),
            kind_cell,
            f"{s['cost_usd']:.4f}",
            model_label(s, dim=True),
            title_of(s) + err_flag,
            pr_label(s),
            f"[{gstyle}]{st}[/{gstyle}]",
            wl_cell,
            str(s["assistant_rounds"]),
            f"{s['tool_calls']}/{len(s['tools'])}",
            _human(s["totals"]["in"]),
            _human(s["totals"]["out"]),
            f"{s['cache_pct']:.0f}%",
            s.get("last_activity", "?"),
            fmt_age(s.get("created_s")),
        )
    console.print(t)
    _render_grand_footer(console, sessions)


def _human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _render_grand_footer(console, sessions: list):
    total_usd = sum(s["cost_usd"] for s in sessions)
    total_nocache = sum(s["nocache_usd"] for s in sessions)
    total_tok = sum(s["total_tokens"] for s in sessions)
    by_model = {}
    for s in sessions:
        for m, pm in s["per_model"].items():
            by_model[m] = by_model.get(m, 0.0) + pm["cost"]
    parts = "  ".join(f"{_model_family(m)}=${v:.2f}"
                      for m, v in sorted(by_model.items(), key=lambda x: -x[1])
                      if v > 0)
    if console is None:
        print(f"\nTOTAL: ${total_usd:.4f} | tokens {total_tok:,} | "
              f"sem-cache custaria ${total_nocache:.2f} "
              f"(cache economizou ${total_nocache - total_usd:.2f})")
        print(f"Por modelo: {parts}")
        return
    from rich.panel import Panel
    body = (f"[bold green]Custo total: ${total_usd:.4f}[/bold green]   "
            f"tokens: {total_tok:,}   sessões: {len(sessions)}\n"
            f"Por modelo: {parts}\n"
            f"[dim]Sem prompt caching, esse mesmo trabalho custaria "
            f"~${total_nocache:.2f} — o cache economizou "
            f"${total_nocache - total_usd:.2f} "
            f"({(1 - total_usd / total_nocache) * 100 if total_nocache else 0:.0f}%).[/dim]")
    console.print(Panel(body, title="TOTAL", border_style="green"))


def render_detail(console, s: dict, rank: int):
    if console is None:
        _render_detail_plain(s, rank)
        return
    from rich.panel import Panel
    from rich.table import Table
    from rich.console import Group

    head = Table.grid(padding=(0, 2))
    head.add_column(style="bold cyan", justify="right", no_wrap=True)
    head.add_column(overflow="fold")
    head.add_row("Sessão", f"#{rank}  {s['session_file']}")
    head.add_row("Título IA", s.get("ai_title") or "—")
    head.add_row("Modelo / effort", model_label(s, dim=True))
    head.add_row("Stage", s.get("stage", "?"))
    head.add_row("PVC", s["pvc"])
    head.add_row("Pasta JSONL", s["jsonl"])
    head.add_row("Worktree (cwd)", s.get("cwd") or "—")
    head.add_row("Git status", _git_detail(s["git"]))
    head.add_row("Branch", s.get("git_branch") or "—")
    if s.get("pr_number"):
        head.add_row("PR", f"#{s['pr_number']}  {s.get('pr_repo') or ''}  {s.get('pr_url') or ''}")
    head.add_row("Claude Code", f"v{s.get('version') or '?'}  "
                                f"(entrypoint={s.get('entrypoint') or '?'}, "
                                f"permission={s.get('permission_mode') or '?'})")
    head.add_row("Quando", f"início {_iso_brt(s.get('first_ts'))} → fim {_iso_brt(s.get('last_ts'))} BRT  "
                           f"(dur {fmt_dur(s.get('duration_s'))}, há {fmt_age(s.get('age_s'))})")

    # custo por modelo
    mt = Table(title="Custo & tokens por modelo", expand=True)
    mt.add_column("Modelo", style="cyan")
    mt.add_column("input", justify="right")
    mt.add_column("output", justify="right")
    mt.add_column("cache write", justify="right")
    mt.add_column("cache read", justify="right")
    mt.add_column("USD", justify="right", style="bold green")
    for m, pm in sorted(s["per_model"].items(), key=lambda x: -x[1]["cost"]):
        mt.add_row(m, f"{pm['in']:,}", f"{pm['out']:,}",
                   f"{pm['cc']:,}", f"{pm['cr']:,}", f"{pm['cost']:.4f}")
    tot = s["totals"]
    mt.add_row("[bold]TOTAL[/bold]", f"[bold]{tot['in']:,}[/bold]",
               f"[bold]{tot['out']:,}[/bold]", f"[bold]{tot['cc']:,}[/bold]",
               f"[bold]{tot['cr']:,}[/bold]", f"[bold]{s['cost_usd']:.4f}[/bold]")

    # atividade
    tools = "  ".join(f"{k}:{v}" for k, v in
                      sorted(s["tools"].items(), key=lambda x: -x[1])) or "—"
    err = s["errors"]
    errline = (f"synthetic={err['synthetic']}  max_tokens={err['max_tokens']}  "
               f"api_error={err['api_error']}  tool_error={err['tool_error']}")
    stops = "  ".join(f"{k}:{v}" for k, v in s["stop_reasons"].items()) or "—"
    act = Table.grid(padding=(0, 2))
    act.add_column(style="bold", justify="right")
    act.add_column()
    act.add_row("Rodadas assistant", str(s["assistant_rounds"]))
    act.add_row("Mensagens user", str(s["user_msgs"]))
    act.add_row("Tool calls (total)", str(s["tool_calls"]))
    act.add_row("Tools (breakdown)", tools)
    act.add_row("Stop reasons", stops)
    act.add_row("Erros/interrupções", errline)
    save = s["nocache_usd"] - s["cost_usd"]
    save_txt = (f"economia ${save:.4f}" if save >= 0
                else f"[yellow]cache CUSTOU ${-save:.4f} a mais (write sem read)[/yellow]")
    act.add_row("Cache eficiência", f"{s['cache_pct']:.1f}% do input veio de cache read  "
                                    f"(sem cache custaria ~${s['nocache_usd']:.4f}, {save_txt})")
    act.add_row("Throughput", f"{s['tokens_per_min']:,.0f} tok/min  •  "
                              f"${s['usd_per_round']:.4f}/rodada")

    brief = (s.get("brief") or "(sem brief capturado)").strip()
    console.print(Panel(Group(head, "", mt, "", act),
                        title=f"DETALHE — ${s['cost_usd']:.4f}",
                        border_style="green"))
    console.print(Panel(brief, title="Brief / comando claude -p (até 4000 chars)",
                        border_style="blue"))


def _git_detail(g: dict) -> str:
    root = f"  root={g['root']}" if g.get("root") else ""
    return (f"{git_label(g)}  "
            f"(modified={g.get('modified', 0)}, untracked={g.get('untracked', 0)}, "
            f"staged={g.get('staged', 0)}){root}")


def _render_detail_plain(s: dict, rank: int):
    print(f"\n===== #{rank}  {s['session_file']}  —  ${s['cost_usd']:.4f} =====")
    print(f"Título IA   : {s.get('ai_title') or '—'}")
    print(f"Modelo      : {model_label(s)}")
    print(f"Stage       : {s.get('stage')}")
    print(f"PVC         : {s['pvc']}")
    print(f"JSONL       : {s['jsonl']}")
    print(f"Worktree    : {s.get('cwd') or '—'}")
    print(f"Git         : {_git_detail(s['git'])}")
    print(f"Branch      : {s.get('git_branch') or '—'}")
    if s.get("pr_number"):
        print(f"PR          : #{s['pr_number']}  {s.get('pr_repo') or ''}  {s.get('pr_url') or ''}")
    print(f"Claude Code : v{s.get('version')}  entrypoint={s.get('entrypoint')}  "
          f"permission={s.get('permission_mode')}")
    print(f"Quando      : {_iso_brt(s.get('first_ts'))} → {_iso_brt(s.get('last_ts'))} BRT  "
          f"(dur {fmt_dur(s.get('duration_s'))}, há {fmt_age(s.get('age_s'))})")
    print("Tokens/custo por modelo:")
    for m, pm in sorted(s["per_model"].items(), key=lambda x: -x[1]["cost"]):
        print(f"  {m:<28} in={pm['in']:>9,} out={pm['out']:>9,} "
              f"cc={pm['cc']:>10,} cr={pm['cr']:>11,}  ${pm['cost']:.4f}")
    print(f"Rodadas={s['assistant_rounds']}  user={s['user_msgs']}  "
          f"tool_calls={s['tool_calls']}  cache={s['cache_pct']:.0f}%")
    print("Tools: " + ("  ".join(f"{k}:{v}" for k, v in
                                  sorted(s['tools'].items(), key=lambda x: -x[1])) or "—"))
    print(f"Erros: {s['errors']}")
    print(f"Brief:\n{(s.get('brief') or '—')[:2000]}")


def render_aggregates(console, sessions: list):
    by_model = {}
    by_day = {}
    by_repo = {}
    by_cwd = {}
    tok_by_model = {}
    for s in sessions:
        by_day[s["last_date"]] = by_day.get(s["last_date"], 0.0) + s["cost_usd"]
        repo = s.get("pr_repo") or "(sem PR)"
        by_repo[repo] = by_repo.get(repo, 0.0) + s["cost_usd"]
        cwd = s.get("cwd") or "(removida)"
        by_cwd[cwd] = by_cwd.get(cwd, 0.0) + s["cost_usd"]
        for m, pm in s["per_model"].items():
            by_model[m] = by_model.get(m, 0.0) + pm["cost"]
            tok_by_model[m] = tok_by_model.get(m, 0) + sum(
                v for k, v in pm.items() if k != "cost")

    def _block(title, d, money=True, topn=None):
        items = sorted(d.items(), key=lambda x: -x[1])
        if topn:
            items = items[:topn]
        if console is None:
            print(f"\n[{title}]")
            for k, v in items:
                print(f"  {('$' + format(v, '.4f')) if money else v:>12}  {k}")
            return
        from rich.table import Table
        t = Table(title=title, expand=True)
        t.add_column("USD" if money else "valor", justify="right", style="green")
        t.add_column("Chave")
        for k, v in items:
            t.add_row(f"${v:.4f}" if money else str(v), str(k))
        console.print(t)

    _block("Custo por modelo", by_model)
    _block("Custo por dia (data da última atividade)", by_day)
    _block("Custo por repositório (PR)", by_repo)
    _block("Top 12 worktrees mais caras", by_cwd, topn=12)


# --------------------------------------------------------------------------- #
# Export                                                                       #
# --------------------------------------------------------------------------- #
def export(sessions: list, outdir: str):
    os.makedirs(outdir, exist_ok=True)
    jpath = os.path.join(outdir, "session_tokens_audit.json")
    cpath = os.path.join(outdir, "session_tokens_audit.csv")
    with open(jpath, "w") as fh:
        json.dump(sessions, fh, indent=2, ensure_ascii=False)
    cols = ["rank", "cost_usd", "nocache_usd", "session_file", "ai_title", "stage",
            "meta_model", "reasoning_effort", "ultracode",
            "pvc", "cwd", "jsonl", "git_state", "git_modified", "git_untracked",
            "git_staged", "git_branch", "pr_number", "pr_repo", "pr_url", "version",
            "permission_mode", "models", "assistant_rounds", "user_msgs", "tool_calls",
            "total_tokens", "in", "out", "cache_creation", "cache_read", "cache_pct",
            "duration_s", "age_s", "last_date", "tokens_per_min", "usd_per_round",
            "errors", "tools"]
    with open(cpath, "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(cols)
        for i, s in enumerate(sessions, 1):
            g = s["git"]
            tot = s["totals"]
            w.writerow([
                i, f"{s['cost_usd']:.6f}", f"{s['nocache_usd']:.6f}", s["session_file"],
                s.get("ai_title") or "", s.get("stage") or "",
                s.get("meta_model") or "", s.get("reasoning_effort") or "",
                s.get("ultracode"), s["pvc"], s.get("cwd") or "",
                s["jsonl"], g.get("state"), g.get("modified"), g.get("untracked"),
                g.get("staged"), s.get("git_branch") or "", s.get("pr_number") or "",
                s.get("pr_repo") or "", s.get("pr_url") or "", s.get("version") or "",
                s.get("permission_mode") or "", "+".join(sorted(s["per_model"])),
                s["assistant_rounds"], s["user_msgs"], s["tool_calls"], s["total_tokens"],
                tot["in"], tot["out"], tot["cc"], tot["cr"], f"{s['cache_pct']:.1f}",
                s.get("duration_s") or "", s.get("age_s") or "", s["last_date"],
                f"{s['tokens_per_min']:.0f}", f"{s['usd_per_round']:.6f}",
                json.dumps(s["errors"]), json.dumps(s["tools"]),
            ])
    return jpath, cpath


# --------------------------------------------------------------------------- #
# Interactive loop                                                             #
# --------------------------------------------------------------------------- #
# Modos de ordenação cicláveis pela tecla [s]. (label, key, reverse, coluna).
# `coluna` é o título da coluna na tabela onde a setinha ↓/↑ é desenhada
# (None quando não há coluna 1:1 — ex.: tokens totais).
SORT_MODES = [
    ("custo USD ↓", lambda s: s["cost_usd"], True, "USD"),
    ("custo USD ↑", lambda s: s["cost_usd"], False, "USD"),
    ("última atividade ↓", lambda s: s.get("mtime") or 0, True, "Últ.ativ"),
    ("tokens ↓", lambda s: s["total_tokens"], True, None),
    ("rodadas ↓", lambda s: s["assistant_rounds"], True, "Rnd"),
    ("tool calls ↓", lambda s: s["tool_calls"], True, "Tools c/d"),
    ("cache% ↓", lambda s: s["cache_pct"], True, "Cache%"),
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


def _status_line(console, last_refresh, next_refresh, flash, sort_name="",
                 page_info="", numbuf=""):
    lr = last_refresh.strftime("%H:%M:%S")
    nr = next_refresh.strftime("%H:%M:%S")
    cmds = ("número+Enter=detalhe  •  ←/→=página  •  a=agregados  •  t=tabela  •  "
            "s=ordenar  •  e=export  •  r/Enter=atualizar  •  Esc=sair")
    pg = f"   página: {page_info}" if page_info else ""
    msg = (f"🔄 última atualização: {lr}   próxima: ~{nr} BRT (auto 60s)   "
           f"ordenação: {sort_name}{pg}\n{cmds}")
    if numbuf:
        msg += f"\n→ abrir linha: {numbuf}_  (Enter confirma · Esc/⌫ cancela)"
    if flash:
        msg += f"\n» {flash}"
    if console is not None:
        from rich.panel import Panel
        console.print(Panel(msg, border_style="cyan", expand=True))
    else:
        print("\n" + msg)
    sys.stdout.write("> " + numbuf)
    sys.stdout.flush()


class _RawKeys:
    """cbreak no stdin: leitura tecla-a-tecla (sem Enter), com timeout.

    ``read(timeout)`` devolve um caractere (``'q'``, ``'7'``...), ``'ENTER'``,
    ``'ESC'``, ``'BACKSPACE'``, ``'ARROW'`` (setas/CSI — ignoradas) ou ``None``
    no timeout. Restaura o termios em qualquer saída. Mesma técnica do painel.
    """

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
            # ESC sozinho vs sequência CSI (setas).
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
            return {b"A": "UP", b"B": "DOWN", b"C": "RIGHT",
                    b"D": "LEFT"}.get(code, "ARROW")
        return b.decode("utf-8", errors="ignore") or None


def interactive(console, sessions: list, top: int | None, refetch, interval: int = 60):
    view = "table"          # "table" | "agg" | "detail"
    sel_file = None         # session_file selecionada quando view == "detail"
    # ordenação padrão ao abrir: última atividade ↓
    sort_idx = next((i for i, m in enumerate(SORT_MODES)
                     if "última atividade" in m[0]), 0)
    page = 0
    page_size = 35
    flash = ""
    numbuf = ""             # dígitos acumulados para abrir uma linha por número
    last_refresh = datetime.now(BRT)
    with _RawKeys() as keys:
        while True:
            sort_name = _apply_sort(sessions, sort_idx)
            _sm = SORT_MODES[sort_idx % len(SORT_MODES)]
            sort_col, sort_desc = _sm[3], _sm[2]
            worklen = min(len(sessions), top) if top else len(sessions)
            max_page = max(0, (worklen - 1) // page_size)
            page = min(page, max_page)
            next_refresh = last_refresh + timedelta(seconds=interval)
            _clear(console)
            page_info = ""
            if view == "agg":
                render_aggregates(console, sessions)
            elif view == "detail":
                s = next((x for x in sessions if x["session_file"] == sel_file), None)
                if s is None:
                    flash = "sessão saiu do PVC (cleanup) — voltando ao ranking"
                    view = "table"
                    render_table(console, sessions, top, page * page_size,
                                 page_size, sort_col, sort_desc)
                    page_info = f"{page + 1}/{max_page + 1}"
                else:
                    render_detail(console, s, sessions.index(s) + 1)
            else:
                render_table(console, sessions, top, page * page_size,
                             page_size, sort_col, sort_desc)
                page_info = f"{page + 1}/{max_page + 1}"
            _status_line(console, last_refresh, next_refresh, flash,
                         sort_name, page_info, numbuf)
            flash = ""

            timeout = max(0.0, (next_refresh - datetime.now(BRT)).total_seconds())
            try:
                if keys.enabled:
                    k = keys.read(timeout)
                else:  # fallback line-based (sem cbreak): precisa de Enter
                    rl, _, _ = select.select([sys.stdin], [], [], timeout)
                    if not rl:
                        k = None
                    else:
                        ln = sys.stdin.readline()
                        k = "EOF" if ln == "" else (ln.strip().lower() or "ENTER")
            except (KeyboardInterrupt, OSError):
                print()
                return

            if k is None:  # timeout → auto-refresh
                new = refetch()
                last_refresh = datetime.now(BRT)
                if new is not None:
                    sessions[:] = new
                else:
                    flash = "falha ao atualizar (pod indisponível?) — mantendo dados"
                continue
            if k == "EOF":
                return
            if k == "RIGHT":  # seta direita = próxima página
                view = "table"
                page += 1  # clamp no topo do loop
                continue
            if k == "LEFT":   # seta esquerda = página anterior
                view = "table"
                page = max(0, page - 1)
                continue
            if k in ("UP", "DOWN", "ARROW"):
                continue
            if k == "BACKSPACE":
                numbuf = numbuf[:-1]
                continue
            if k == "ESC":
                if numbuf:          # cancela número pendente primeiro
                    numbuf = ""
                    continue
                if view != "table":  # numa sub-view, Esc volta à tabela
                    view = "table"
                    continue
                return              # Esc na tabela = sair

            # fallback line-based pode entregar uma palavra/número inteiro de uma vez
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
                        sel_file = sessions[idx - 1]["session_file"]
                    else:
                        flash = f"linha fora do intervalo (1..{len(sessions)})"
                else:  # Enter vazio = refresh
                    new = refetch()
                    last_refresh = datetime.now(BRT)
                    flash = ("atualizado" if new is not None
                             else "falha ao atualizar (pod indisponível?)")
                    if new is not None:
                        sessions[:] = new
                continue
            if k.isdigit():
                numbuf += k
                continue

            numbuf = ""  # qualquer comando de letra cancela número pendente
            if k == "q":
                return
            if k == "r":
                new = refetch()
                last_refresh = datetime.now(BRT)
                flash = ("atualizado manualmente" if new is not None
                         else "falha ao atualizar (pod indisponível?)")
                if new is not None:
                    sessions[:] = new
                continue
            if k == "s":
                sort_idx = (sort_idx + 1) % len(SORT_MODES)
                flash = f"ordenação: {SORT_MODES[sort_idx][0]}"
                view = "table"
                page = 0
                continue
            if k == "n":
                view = "table"
                page += 1  # clamp no topo do loop
                continue
            if k == "p":
                view = "table"
                page = max(0, page - 1)
                continue
            if k in ("t", "b"):
                view = "table"
                continue
            if k == "a":
                view = "agg"
                continue
            if k == "e":
                j, _c = export(sessions, "./audit-out")
                flash = f"exportado: {j}"
                continue
            flash = "tecla não reconhecida"


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Relatório de uso de Claude Code dentro dos PVCs/pods do cluster DEILE.")
    ap.add_argument("-n", "--namespace", default=os.environ.get("DEILE_K8S_NAMESPACE", "deile"))
    ap.add_argument("--pod", default=None, help="nome do pod claude-worker (default: auto)")
    ap.add_argument("--top", type=int, default=None, help="mostra só as N sessões mais caras")
    ap.add_argument("--detail", action="store_true",
                    help="dump completo de cada sessão (não-interativo)")
    ap.add_argument("--no-interactive", action="store_true",
                    help="só imprime tabela + agregados e sai")
    ap.add_argument("--export", nargs="?", const="./audit-out", default=None,
                    metavar="DIR", help="grava JSON + CSV no diretório (default ./audit-out)")
    ap.add_argument("--no-git", action="store_true",
                    help="pula git status por worktree (mais rápido)")
    ap.add_argument("--last", nargs="?", type=int, const=-1, default=None, metavar="N",
                    help="ordena por última atividade ↓ (em vez de custo); "
                         "com N, mostra só as N sessões mais recentes")
    args = ap.parse_args()

    kubectl = find_kubectl()
    ns = args.namespace
    pod = resolve_pod(kubectl, ns, args.pod)
    pvc = resolve_pvc(kubectl, ns)
    console = _console()

    print(f"• namespace={ns}  pod={pod}  pvc={pvc}", file=sys.stderr)
    print("• parseando JSONL + git status dentro do pod...", file=sys.stderr)
    sessions = fetch_sessions(kubectl, ns, pod, args.no_git)
    if not sessions:
        sys.exit("Nenhuma sessão com tokens encontrada no PVC.")
    sessions = enrich(sessions, pvc)
    last_requested = args.last is not None
    last_n = args.last if (last_requested and args.last and args.last > 0) else 0
    if last_requested:
        sessions.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
        if last_n:
            sessions = sessions[:last_n]
    print(f"• {len(sessions)} sessões com uso de tokens.\n", file=sys.stderr)

    if args.export is not None:
        j, c = export(sessions, args.export)
        print(f"Exportado:\n  {j}\n  {c}")

    if args.detail:
        for i, s in enumerate(sessions[:args.top] if args.top else sessions, 1):
            render_detail(console, s, i)
        return

    if args.no_interactive or not sys.stdin.isatty():
        col = "Últ.ativ" if last_requested else "USD"
        render_table(console, sessions, args.top, sort_col=col, sort_desc=True)
        render_aggregates(console, sessions)
        return

    def refetch():
        """Re-resolve o pod (sobrevive a restart) e recarrega os dados.

        Retorna None em falha — o loop mantém os dados anteriores.
        """
        try:
            p = resolve_pod(kubectl, ns, args.pod)
            data = enrich(fetch_sessions(kubectl, ns, p, args.no_git), pvc)
            if last_n:  # preserva o corte das N mais recentes no refresh
                data.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
                data = data[:last_n]
            return data
        except (SystemExit, Exception):
            return None

    interactive(console, sessions, args.top, refetch)


if __name__ == "__main__":
    main()
