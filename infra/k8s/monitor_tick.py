"""DEILE-Monitor deterministic tick (Phase A) — orchestrator + entry point.

One process = one tick. The shell loop in ``55-deile-monitor-deployment.yaml``
runs this module every ``DEILE_MONITOR_TICK_INTERVAL_S`` seconds. It performs the
full mechanical sweep (kill-switch, steer commands, vigias V1–V8, anti-flood,
state, structured emit) WITHOUT any LLM. It only escalates to Phase B (the
``monitor`` persona via ``wrapper.py monitor``) when V8 follow-up candidates
survive the deterministic pre-filters — by writing ``/state/monitor-judgment.json``
and exiting; the shell loop runs Phase B iff that file exists.

Run: ``python3 /app/monitor_tick.py`` (the Dockerfile flattens this module and
its siblings ``monitor_core``/``monitor_vigias`` into ``/app``). Exit code 0
always (a single failing vigia must never crash the heartbeat).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional

import monitor_core as core
import monitor_vigias as vigias

_DURATION_RE = re.compile(r"^(\d+)\s*([smh])$")
_ACK_DURATION_S = 24 * 3600
_KUBE_DEPENDENT_VIGIAS = ("V1", "V2", "V6", "V7")


def parse_duration_s(text: str) -> Optional[int]:
    m = _DURATION_RE.match((text or "").strip())
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


def _default_kube_probe(run: Callable[..., core.CmdResult]) -> Callable[[str], int]:
    def probe(endpoint: str) -> int:
        return run(
            ["kubectl", "--server", endpoint, "version", "--request-timeout=3s", "--client=false"],
            timeout=5,
        ).rc
    return probe


def _resolve_kube(
    run: Callable[..., core.CmdResult],
    kube_probe: Optional[Callable[[str], int]],
    sa_dir: Optional[str],
    kubeconfig_path: Optional[str],
):
    """Return ``(kube_api, kubeconfig_path|None)``.

    Tests inject ``kube_probe`` → legacy ``--server`` path (no SA). In-pod
    (``kube_probe is None``) with a ServiceAccount token present, build a 0600
    kubeconfig (issue #504) so kubectl authenticates; the resolved server doubles
    as the reachability signal.
    """
    if kube_probe is not None:
        return core.resolve_kube_api(kube_probe), None
    sa = sa_dir or core.SA_DIR_DEFAULT
    if os.path.exists(os.path.join(sa, "token")):
        kc = kubeconfig_path or os.path.join(tempfile.gettempdir(), "deile-monitor-kubeconfig")
        server = core.resolve_incluster_kube(run, kc, sa_dir=sa)
        return (server, kc) if server else (None, None)
    # Local/dev outside a pod: no SA → legacy probe.
    return core.resolve_kube_api(_default_kube_probe(run)), None


# ---------------------------------------------------------------------------
# Kill-switch / steer commands
# ---------------------------------------------------------------------------

def _handle_kill_switch(state_dir: Path, state: Dict[str, Any], emitter: core.Emitter,
                        now: datetime) -> bool:
    """Returns True if the tick must stop now (pause active)."""
    pause_flag = state_dir / "monitor-pause"
    if not pause_flag.exists():
        return False
    paused_until = state.get("paused_until")
    expiry = None
    if paused_until:
        try:
            expiry = datetime.fromisoformat(paused_until.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            expiry = None
    if expiry is not None and now >= expiry:
        try:
            pause_flag.unlink()
        except OSError:
            pass
        state["paused_until"] = None
        emitter.emit("monitor.command from=auto kind=resume reason='pause expired' ok=true")
        return False
    return True  # still paused


def _process_steer_commands(state_dir: Path, state: Dict[str, Any], emitter: core.Emitter,
                            now: datetime) -> None:
    cmd_dir = state_dir / "monitor-commands"
    if not cmd_dir.is_dir():
        return
    for path in sorted(cmd_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        _apply_steer(state_dir, state, emitter, now, content)
        try:
            path.unlink()
        except OSError:
            pass


def _apply_steer(state_dir: Path, state: Dict[str, Any], emitter: core.Emitter,
                 now: datetime, content: str) -> None:
    parts = content.split()
    kind = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    if kind == "pause":
        dur = parse_duration_s(arg) or 1800
        (state_dir / "monitor-pause").write_text("")
        state["paused_until"] = core.iso(now + timedelta(seconds=dur))
        emitter.emit(f"monitor.command from=bot kind=pause duration={arg or '30m'} ok=true")
    elif kind == "resume":
        try:
            (state_dir / "monitor-pause").unlink()
        except OSError:
            pass
        state["paused_until"] = None
        emitter.emit("monitor.command from=bot kind=resume ok=true")
    elif kind == "force-tick":
        emitter.emit("monitor.command from=bot kind=force-tick ok=true")
    elif kind == "status":
        emitter.emit("monitor.command from=bot kind=status ok=true")
    elif kind == "ack" and arg:
        entry = state.setdefault("known_anomalies", {}).setdefault(arg, {})
        entry["acked_until"] = core.iso(now + timedelta(seconds=_ACK_DURATION_S))
        emitter.emit("monitor.command from=bot kind=ack ok=true")
    else:
        head = content[:60].replace("'", " ")
        emitter.emit(f"monitor.command from=bot kind=unknown ok=false reason='parse failed: {head}'")


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------

async def run_tick(
    state_dir: str,
    *,
    now: datetime,
    run: Callable[..., core.CmdResult],
    renew: Callable[[], Awaitable[Any]],
    repo: str,
    namespace: str,
    bot_endpoint: str,
    bot_token: str,
    user_id: str,
    kube_probe: Optional[Callable[[str], int]] = None,
    sa_dir: Optional[str] = None,
    kubeconfig_path: Optional[str] = None,
) -> Dict[str, Any]:
    sd = Path(state_dir)
    state_path = sd / "monitor-state.json"
    audit_path = sd / "monitor-audit.log"
    notif_log = sd / "monitor-notifications.log"

    state = core.load_state(str(state_path))
    flags = core.TickFlags()
    tick_n = int(state.get("last_tick", 0)) + 1
    emitter = core.Emitter(str(audit_path), flags, clock=lambda: now, tick_n=tick_n)
    start = now

    # Steer commands first — a queued 'resume' must be able to lift an active
    # timed pause before the kill-switch short-circuits the tick (issue #696).
    _process_steer_commands(sd, state, emitter, now)

    # Kill-switch / auto-resume (re-evaluated after steer commands drain).
    if _handle_kill_switch(sd, state, emitter, now):
        core.save_state(str(state_path), state)
        return {"paused": True, "needs_phase_b": False}

    # Re-check: a steer 'pause' command applied above may have re-paused.
    if (sd / "monitor-pause").exists() and state.get("paused_until"):
        core.save_state(str(state_path), state)
        return {"paused": True, "needs_phase_b": False}

    notifier = core.Notifier(
        state=state, emitter=emitter, flags=flags, run=run,
        bot_endpoint=bot_endpoint, bot_token=bot_token, user_id=user_id,
        notif_log_path=str(notif_log), clock=lambda: now,
    )
    kube_api, kubeconfig = _resolve_kube(run, kube_probe, sa_dir, kubeconfig_path)
    ctx = vigias.MonitorContext(
        run=run, emitter=emitter, notifier=notifier, state=state, flags=flags,
        now=now, repo=repo, namespace=namespace, kube_api=kube_api,
        kubeconfig=kubeconfig,
    )

    skipped: List[str] = []
    if kube_api is None:
        for v in _KUBE_DEPENDENT_VIGIAS:
            emitter.emit(f"monitor.vigia.skip V={v} reason=K8S_API_UNREACHABLE")
            skipped.append(v)
    else:
        await _safe(emitter, "V1", vigias.vigia_oauth(ctx, renew))
        _safe_sync(emitter, "V2", lambda: vigias.vigia_error_pods(ctx))
        _safe_sync(emitter, "V6", lambda: vigias.vigia_failed_jobs(ctx))
        _safe_sync(emitter, "V7", lambda: vigias.vigia_pipeline_health(ctx))

    # Forge vigias (do not depend on the kube API).
    _safe_sync(emitter, "V3", lambda: vigias.vigia_orphan_issues(ctx))
    _safe_sync(emitter, "V4", lambda: vigias.vigia_pr_attempts(ctx))
    _safe_sync(emitter, "V5", lambda: vigias.vigia_stakeholder(ctx))
    fu_candidates: List[Dict[str, Any]] = []
    try:
        fu_candidates = vigias.vigia_collect_followups(ctx)
    except Exception as exc:  # noqa: BLE001 — never crash the tick
        emitter.emit(f"monitor.vigia.fail V=V8 reason='{type(exc).__name__}'")

    # Persist state + tick summary.
    state["last_tick"] = tick_n
    state["last_tick_epoch"] = int(now.timestamp())
    core.save_state(str(state_path), state)
    elapsed = max(0, int((now - start).total_seconds()))
    anomalies = len(state.get("known_anomalies", {}))
    emitter.emit(
        f"monitor.tick #{tick_n} done in {elapsed}s: actions={flags.actions} "
        f"notify={flags.notifications} skipped=[{','.join(skipped)}] anomalias={anomalies}"
    )

    needs_phase_b = bool(fu_candidates)
    if needs_phase_b:
        _write_judgment(sd, tick_n, fu_candidates, state, now, repo)
    return {"paused": False, "needs_phase_b": needs_phase_b, "candidates": fu_candidates}


async def _safe(emitter: core.Emitter, vname: str, coro: Awaitable[None]) -> None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        emitter.emit(f"monitor.vigia.fail V={vname} reason='{type(exc).__name__}'")


def _safe_sync(emitter: core.Emitter, vname: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        emitter.emit(f"monitor.vigia.fail V={vname} reason='{type(exc).__name__}'")


def _write_judgment(state_dir: Path, tick_n: int, candidates: List[Dict[str, Any]],
                    state: Dict[str, Any], now: datetime, repo: str) -> None:
    payload = {
        "tick": tick_n,
        "generated_at": core.iso(now),
        # Issue #612: report the SAME repo the tick actually ran against
        # (resolved once in main() via the canonical resolver) instead of
        # re-reading the env independently — single read, no divergence.
        "repo": repo,
        "fu_candidates": candidates,
        "fu_created_today": state.get("fu_created_today", 0),
        "fu_day_slot": state.get("fu_day_slot", ""),
        "instructions": (
            "Phase B: para cada fu_candidate, julgue se é promessa real de follow-up "
            "(não FP em prosa/código), cheque jaccard vs issues abertas (já rastreado?), "
            "e abra issue [FU] com labels ~workflow:nova,~origem:fu-monitor respeitando "
            "caps (3/tick, 10/dia UTC). Emita monitor.v8.create / monitor.v8.skip / "
            "monitor.v8.scan e atualize fu_fingerprints/fu_created_today no estado."
        ),
    }
    import json
    try:
        with open(state_dir / "monitor-judgment.json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _resolve_repo() -> str:
    """Resolve the monitor's target repo through the canonical resolver.

    Issue #612 (project-agnostic): the monitor used to read ``DEILE_PIPELINE_REPO``
    directly — but ``Settings`` *silently ignores* that env var (it was "truly
    removed"; only ``DEILE_FORGE_REPO`` / ``forge.repo`` / ``pipeline.repo`` feed
    settings). That meant the monitor read a source the rest of the harness no
    longer honours. This routes the read through the SAME
    :func:`resolve_forge_repo` the pipeline uses, with the legacy
    ``DEILE_PIPELINE_REPO`` env kept as a deployment-boundary fallback so the
    reference manifest (ConfigMap ``pipeline.repo`` → ``DEILE_PIPELINE_REPO``)
    keeps working unchanged.

    ``require=False`` + graceful fallback: a missing repo degrades with a
    ``WARNING``, never raises — the deterministic Phase-A tick must never crash
    the heartbeat (a hard fail-loud belongs to the pipeline's startup, not to
    the supervisor's per-tick sweep). The import is lazy so the flattened
    ``/app/monitor_tick.py`` keeps a clean module-import surface, and any
    config-layer error falls back to the raw env rather than killing the tick.
    """
    env_repo = os.environ.get("DEILE_PIPELINE_REPO", "").strip()
    try:
        from deile.orchestration.pipeline.constants import resolve_forge_repo
        return resolve_forge_repo(require=False, fallback=env_repo)
    except Exception:  # noqa: BLE001 — config import must never crash the tick
        return env_repo


def main(argv: Optional[List[str]] = None) -> int:
    state_dir = os.environ.get("DEILE_MONITOR_STATE_DIR", "/state")
    repo = _resolve_repo()
    namespace = os.environ.get("DEILE_K8S_NAMESPACE", "deile")
    bot_endpoint = os.environ.get("DEILE_BOT_ENDPOINT", "http://deilebot:8765")
    bot_token = _read_secret("DEILE_BOT_AUTH_TOKEN")
    user_id = _read_user_id(state_dir)

    async def renew():
        # Issue #603: auth migrou para o token de ~1 ano (setup-token,
        # CLAUDE_CODE_OAUTH_TOKEN via Secret). Não há mais refresh headless
        # in-pod — quando o token de fato expira (~1×/ano) o Humano renova com
        # ``deploy.py k8s claude-setup-token``. O vigia de OAuth, ao receber
        # este resultado, notifica em vez de tentar curar sozinho.
        return SimpleNamespace(
            ok=False,
            error="setup-token (issue #603): sem refresh headless; "
            "renove com `deploy.py k8s claude-setup-token`",
        )

    try:
        asyncio.run(run_tick(
            state_dir, now=core.utcnow(), run=core.run_cmd, renew=renew,
            repo=repo, namespace=namespace, bot_endpoint=bot_endpoint,
            bot_token=bot_token, user_id=user_id,
        ))
    except Exception as exc:  # noqa: BLE001 — heartbeat must survive any failure
        print(f"monitor.tick.fatal reason='{type(exc).__name__}: {exc}'", file=sys.stderr)
    return 0


def _read_secret(name: str) -> str:
    if name in os.environ:
        return os.environ[name]
    path = Path("/run/secrets/deile") / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_user_id(state_dir: str) -> str:
    try:
        return (Path(state_dir) / "notify-user-id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


if __name__ == "__main__":
    sys.exit(main(sys.argv))
