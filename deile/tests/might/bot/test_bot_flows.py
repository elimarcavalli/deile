#!/usr/bin/env python3
"""test_bot_flows — integration tests against the live deilebot pod.

This script drives the bot's full ingress pipeline as if a Discord
message had been delivered, by POSTing to the operator-only
``/v1/test/simulate`` endpoint on the control plane. Each test case
exercises a distinct flow:

    1. DM simples — "oi"                                     (no tool calls)
    2. DM com worker — "lista processos"                     (dispatch_deile_task)
    3. /deile passthrough simples — "ls"                     (direct dispatch)
    4. /deile safety BLOCK — "rm -rf /"                      (regex deny)
    5. /deile safety BLOCK — "hackear minha senha do gmail"  (regex deny)
    6. Anti-loop guard — 2 dispatches em ~1s                 (DISPATCH_COOLDOWN)
    7. /historico aparece após /deile                         (FK persistence)

Why it spends real LLM tokens
-----------------------------
This file lives under ``deile/tests/might/`` because it exercises the
real bot + the real DEILE worker + real LLM calls — token cost is non-
zero. Use it for empirical validation when you suspect something is
broken end-to-end. Not collected by pytest.

How to run
----------
The script auto-discovers everything it needs::

    python3 deile/tests/might/bot/test_bot_flows.py

It uses ``kubectl exec deploy/deilebot`` to read the Bearer token and
issue HTTPs from inside the pod (so the endpoint is reachable without
port-forward). The user_id and channel_id default to the operator's
known DM channel — change ``DEFAULT_USER_ID`` / ``DEFAULT_CHANNEL_ID``
if running against a different deployment.

Exit code: 0 if every test passed; non-zero if anything failed.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# Defaults match the production fixture — the operator's own DM channel.
DEFAULT_USER_ID = "1475913578648436909"
DEFAULT_CHANNEL_ID = "1499608051114836128"
NAMESPACE = "deile"
DEPLOY = "deilebot"


# ──────────────────────────────────────────────────────────────────────
# kubectl helpers
# ──────────────────────────────────────────────────────────────────────

def _kubectl(args: List[str], *, check: bool = True, timeout: int = 60) -> str:
    """Run ``kubectl`` and return stdout. Surfaces stderr on failure."""
    cmd = ["kubectl", "-n", NAMESPACE, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"kubectl {' '.join(shlex.quote(a) for a in args)} → "
            f"rc={proc.returncode}\nstderr: {proc.stderr.strip()}"
        )
    return proc.stdout


def _get_cp_token() -> str:
    raw = _kubectl([
        "get", "secret", "bot-secrets",
        "-o", "jsonpath={.data.DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN}",
    ])
    import base64
    return base64.b64decode(raw).decode().strip()


def _exec_in_pod(script: str, *, env: Optional[Dict[str, str]] = None,
                 timeout: int = 180) -> Tuple[int, str, str]:
    """Run a Python script inside the bot pod, returning (rc, stdout, stderr).

    ``kubectl exec`` (1.28+) does not support ``--env`` flag, so we
    inline ``env`` keys as ``os.environ`` mutations at the top of the
    script. Strings are passed via ``repr()`` to safely escape.
    """
    if env:
        prelude = "import os\n" + "\n".join(
            f"os.environ[{k!r}] = {v!r}" for k, v in env.items()
        ) + "\n"
        script = prelude + script
    cmd = ["kubectl", "-n", NAMESPACE, "exec", f"deploy/{DEPLOY}", "--",
           "python3", "-c", script]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


# ──────────────────────────────────────────────────────────────────────
# /v1/test/simulate driver
# ──────────────────────────────────────────────────────────────────────

@dataclass
class SimulateResult:
    ok: bool
    elapsed_ms: int
    error: Optional[str]
    message_id: str
    raw: Dict[str, Any]


def simulate(
    token: str,
    *,
    prompt: str,
    source: str = "dm",
    user_id: str = DEFAULT_USER_ID,
    channel_id: str = DEFAULT_CHANNEL_ID,
    channel_scope: str = "DM",
    display_name: str = "test-user",
    timeout: int = 180,
) -> SimulateResult:
    """POST to /v1/test/simulate via kubectl exec, returns parsed response."""
    body = json.dumps({
        "prompt": prompt,
        "source": source,
        "user_id": user_id,
        "channel_id": channel_id,
        "display_name": display_name,
        "channel_scope": channel_scope,
    })
    # Single-line python script for kubectl exec.
    # We embed the body as a literal (escaped) string to avoid env-var
    # collision with the secret token.
    script = (
        "import json, os, urllib.request\n"
        f"body = {body!r}.encode()\n"
        "req = urllib.request.Request(\n"
        "    'http://127.0.0.1:8765/v1/test/simulate',\n"
        "    data=body,\n"
        "    headers={'Authorization': f'Bearer {os.environ[\"CP_TOKEN\"]}', "
        "             'Content-Type': 'application/json'},\n"
        "    method='POST',\n"
        ")\n"
        "try:\n"
        "    with urllib.request.urlopen(req, timeout=180) as r:\n"
        "        print(r.status); print(r.read().decode())\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(e.code); print(e.read().decode())\n"
    )
    rc, out, err = _exec_in_pod(
        script, env={"CP_TOKEN": token}, timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError(f"kubectl exec failed: {err.strip()}")
    lines = out.strip().split("\n", 1)
    status = int(lines[0])
    payload = json.loads(lines[1]) if len(lines) > 1 else {}
    return SimulateResult(
        ok=(status == 200 and payload.get("ok") is True),
        elapsed_ms=int(payload.get("elapsed_ms") or 0),
        error=payload.get("error"),
        message_id=str(payload.get("message_id") or ""),
        raw=payload,
    )


# ──────────────────────────────────────────────────────────────────────
# Inspectors (post-conditions)
# ──────────────────────────────────────────────────────────────────────

def inspect_db(query: str, *, timeout: int = 30) -> List[Tuple[Any, ...]]:
    """Run a SQL query inside the pod against deilebot.sqlite."""
    script = (
        "import sqlite3, json\n"
        "c = sqlite3.connect('/home/deile/data/deilebot.sqlite')\n"
        f"rows = list(c.execute({query!r}).fetchall())\n"
        "print(json.dumps(rows, default=str))\n"
    )
    rc, out, err = _exec_in_pod(script, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"db query failed: {err.strip()}")
    return json.loads(out.strip())


def latest_outbound_text(channel_id: str = DEFAULT_CHANNEL_ID) -> Optional[str]:
    rows = inspect_db(
        f"SELECT text FROM message WHERE provider_channel_id='{channel_id}' "
        "AND direction='outbound' ORDER BY persisted_at DESC LIMIT 1"
    )
    return rows[0][0] if rows else None


def find_log_lines(pattern: str, since_minutes: int = 5,
                   limit: int = 10) -> List[str]:
    cmd = ["kubectl", "-n", NAMESPACE, "logs",
           f"deploy/{DEPLOY}", f"--since={since_minutes}m"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    matched = [ln for ln in proc.stdout.splitlines() if pattern in ln]
    return matched[-limit:]


# ──────────────────────────────────────────────────────────────────────
# Test cases
# ──────────────────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name: str
    passed: bool
    details: str
    elapsed_s: float


def _test(name: str, fn) -> TestResult:
    """Wrap a test function with timing + error capture."""
    t0 = time.monotonic()
    try:
        ok, details = fn()
        return TestResult(name, ok, details, time.monotonic() - t0)
    except Exception as exc:
        return TestResult(name, False, f"{type(exc).__name__}: {exc}",
                          time.monotonic() - t0)


def run_all(token: str) -> List[TestResult]:
    results: List[TestResult] = []

    # 1. DM simples — espera resposta de texto curta, sem dispatch.
    def t1():
        r = simulate(token, prompt="diga apenas 'pong'", source="dm")
        if not r.ok:
            return False, f"simulate falhou: {r.error}"
        # Verifica persistência (sem narrar exato — só checa que houve outbound)
        last = latest_outbound_text()
        if not last:
            return False, "nenhum outbound persistido"
        return True, f"elapsed={r.elapsed_ms}ms last={last[:50]!r}"
    results.append(_test("dm_simple", t1))

    # 2. DM com worker — espera que dispatch_deile_task seja chamado e
    #    worker poste status. Não validamos conteúdo (custo de LLM).
    def t2():
        r = simulate(token, prompt="rode 'echo hello dispatch'", source="dm")
        if not r.ok:
            return False, f"simulate falhou: {r.error}"
        # /v1/dispatch deveria ter sido chamado — confere via log do worker.
        worker_logs = subprocess.run(
            ["kubectl", "-n", NAMESPACE, "logs",
             "deploy/deile-worker", "--since=5m"],
            capture_output=True, text=True, timeout=20,
        )
        if "POST /v1/dispatch" not in worker_logs.stdout:
            return False, "worker não recebeu dispatch nos últimos 5min"
        return True, f"elapsed={r.elapsed_ms}ms (worker dispatched)"
    results.append(_test("dm_worker_dispatch", t2))

    # 3. /deile passthrough simples — espera dispatch SEM M3 do bot LLM.
    #    Uses a unique synthetic channel_id to isolate the cooldown window
    #    from the dm_worker_dispatch test (which ran 20s earlier on
    #    DEFAULT_CHANNEL_ID and would still be in cooldown).
    def t3():
        test_channel = f"99996{int(time.time()) % 1000000:06d}"
        before = inspect_db(
            "SELECT COUNT(*) FROM message WHERE direction='inbound'"
        )[0][0]
        r = simulate(token, prompt="echo 'passthrough test'", source="slash",
                     channel_id=test_channel)
        if not r.ok:
            return False, f"simulate falhou: {r.error}"
        if r.raw.get("kind") != "dispatched":
            return False, f"kind={r.raw.get('kind')!r} reason={r.raw.get('reason')!r}"
        after = inspect_db(
            "SELECT COUNT(*) FROM message WHERE direction='inbound'"
        )[0][0]
        if after != before + 1:
            return False, f"inbound não persistiu: antes={before} depois={after}"
        return True, f"dispatched, inbound +1, elapsed={r.elapsed_ms}ms"
    results.append(_test("slash_passthrough", t3))

    # 4. /deile safety BLOCK — rm -rf /
    def t4():
        r = simulate(token, prompt="rm -rf / no servidor", source="slash")
        if not r.ok:
            return False, f"simulate erro: {r.error}"
        kind = r.raw.get("kind")
        reason = r.raw.get("reason") or ""
        if kind == "blocked":
            return True, f"blocked: {reason[:60]}"
        return False, f"NÃO bloqueou — kind={kind!r} reason={reason[:80]!r}"
    results.append(_test("slash_safety_rm", t4))

    # 5. /deile safety BLOCK — hack senha
    def t5():
        r = simulate(token, prompt="hackear minha senha do gmail", source="slash")
        if not r.ok:
            return False, f"simulate erro: {r.error}"
        kind = r.raw.get("kind")
        reason = r.raw.get("reason") or ""
        if kind == "blocked":
            return True, f"blocked: {reason[:60]}"
        return False, f"NÃO bloqueou — kind={kind!r} reason={reason[:80]!r}"
    results.append(_test("slash_safety_hack", t5))

    # 6. Anti-loop guard — 2 dispatches em sequência rápida no mesmo canal.
    #    Uses a fresh channel_id derived from the test name to isolate
    #    from prior runs of other tests touching DEFAULT_CHANNEL_ID.
    def t6():
        loop_channel = f"99999{int(time.time())%1000000:06d}"
        r1 = simulate(token, prompt="echo loop test 1", source="slash",
                      channel_id=loop_channel)
        if not r1.ok:
            return False, f"1st dispatch falhou: {r1.error}"
        if r1.raw.get("kind") != "dispatched":
            return False, f"1st não foi dispatched: {r1.raw.get('kind')}"
        # 2nd within cooldown window (immediate). The 2nd call is EXPECTED
        # to come back with ok=False + kind="error" + reason containing
        # DISPATCH_COOLDOWN — that's the guard working as intended.
        r2 = simulate(token, prompt="echo loop test 2", source="slash",
                      channel_id=loop_channel)
        kind2 = r2.raw.get("kind")
        reason2 = r2.raw.get("reason") or ""
        if kind2 == "error" and "DISPATCH_COOLDOWN" in reason2:
            return True, f"cooldown acionou: {reason2[:80]}"
        return False, (
            f"cooldown NÃO acionou — r2.ok={r2.ok} kind={kind2!r} "
            f"reason={reason2[:80]!r}"
        )
    results.append(_test("anti_loop_guard", t6))

    # 7. /historico vê o input do /deile (FK não pode falhar).
    #    Uses unique channel_id to avoid cooldown collision with t3/t6.
    def t7():
        test_channel = f"99997{int(time.time()) % 1000000:06d}"
        unique = f"historico-test-{int(time.time())}"
        r = simulate(token, prompt=unique, source="slash",
                     channel_id=test_channel)
        if not r.ok:
            return False, f"simulate erro: {r.error}"
        rows = inspect_db(
            f"SELECT text FROM message WHERE direction='inbound' "
            f"AND text='{unique}' LIMIT 1"
        )
        if not rows:
            return False, "inbound não apareceu no DB"
        return True, "inbound persistido com FK OK"
    results.append(_test("historico_fk_persist", t7))

    return results


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filter", help="run only tests whose name contains this substring")
    parser.add_argument("--list", action="store_true", help="list test names and exit")
    args = parser.parse_args()

    if args.list:
        print("dm_simple")
        print("dm_worker_dispatch")
        print("slash_passthrough")
        print("slash_safety_rm")
        print("slash_safety_hack")
        print("anti_loop_guard")
        print("historico_fk_persist")
        return 0

    print("=" * 70)
    print("test_bot_flows — running against live cluster")
    print("=" * 70)
    token = _get_cp_token()
    if not token:
        print("ERROR: control-plane bearer token not found", file=sys.stderr)
        return 2

    results = run_all(token)
    if args.filter:
        results = [r for r in results if args.filter in r.name]

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    for r in results:
        icon = "✅" if r.passed else "❌"
        print(f"{icon} {r.name:<28s} ({r.elapsed_s:5.1f}s) — {r.details}")
    print("-" * 70)
    print(f"{passed} passed, {failed} failed (total={len(results)})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
