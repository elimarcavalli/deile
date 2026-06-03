"""Vigias (V1–V8) of the DEILE-Monitor deterministic tick (Phase A).

Each vigia is a mechanical check (kubectl/gh + JSON parse) that records anomalies,
fires safe autonomous cures (delete abandoned Job pods; renew OAuth via the real
headless path) and notifies through the anti-flood gate. NO LLM: the only thing
escalated to Phase B is the set of surviving V8 follow-up candidates.

Shared state travels in :class:`MonitorContext`. All external calls go through
``ctx.run`` (a :func:`monitor_core.run_cmd`-compatible callable) so the whole
module is unit testable against a fake runner.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

import monitor_core as core

OAUTH_TTL_THRESHOLD_S = 1800        # renew when < 30 min remaining
_OAUTH_FINGERPRINT = "oauth_expired_claude-worker-all"

ORPHAN_STALE_S = 12 * 3600          # 12h
ORPHAN_RENOTIFY_S = 6 * 3600        # 6h (V3-specific, longer than P1 default)
STAKEHOLDER_WAIT_S = 4 * 3600       # 4h

_ORPHAN_LABELS = "~workflow:em_revisao,~workflow:em_implementacao,~workflow:em_pr"

# V8 follow-up promise patterns (case-insensitive). Mirror monitor.md §295-308.
_FU_PATTERNS = [
    re.compile(p, re.IGNORECASE | re.MULTILINE)
    for p in (
        r"vou abrir (?:uma )?issue",
        r"abrir(?:ei)? (?:uma )?issue",
        r"follow[-\s]?up\s*:",
        r"\bFU\s*:",
        r"^[-*\s]*TODO\b",
        r"fica para depois",
        r"vai pra? issue (?:separada|nova)",
        r"próxima (?:iteração|sessão)\b.*(?:vou|vamos|iremos|farei)",
    )
]

# Pod/branch name fragments that mark a "critical service" for crashloop alerts.
_CRITICAL_NAME_FRAGMENTS = ("pipeline", "worker")


@dataclass
class MonitorContext:
    run: Callable[..., "core.CmdResult"]
    emitter: "core.Emitter"
    notifier: Any                # core.Notifier (or a recording double in tests)
    state: Dict[str, Any]
    flags: "core.TickFlags"
    now: datetime
    repo: str
    namespace: str
    kube_api: Optional[str] = None
    kubeconfig: Optional[str] = None

    def kubectl(self, *args: str, timeout: int = 15) -> "core.CmdResult":
        cmd = ["kubectl"]
        if self.kubeconfig:
            # In-pod: the kubeconfig carries server + SA token + CA (issue #504).
            cmd += ["--kubeconfig", self.kubeconfig]
        elif self.kube_api:
            # Local/dev fallback (no ServiceAccount kubeconfig).
            cmd += ["--server", self.kube_api]
        cmd += ["-n", self.namespace, *args]
        return self.run(cmd, timeout=timeout)

    def gh(self, *args: str, timeout: int = 20) -> "core.CmdResult":
        return self.run(["gh", *args], timeout=timeout)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _age_seconds(ts: Optional[str], now: datetime) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (now - dt).total_seconds()


def _human_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    if seconds < 3600:
        return f"{int(seconds // 60)}min"
    return f"{seconds / 3600:.0f}h"


# ---------------------------------------------------------------------------
# V1 — OAuth health + real headless renew (P0)
# ---------------------------------------------------------------------------

def _oauth_remaining_s(creds_json: str, now: datetime) -> Optional[float]:
    """Parse ``expiresAt`` (epoch ms) and return seconds remaining, or None."""
    data = _parse_json(creds_json)
    if not isinstance(data, dict):
        return None
    exp = data.get("expiresAt")
    if not isinstance(exp, (int, float)):
        return None
    return (exp / 1000.0) - now.timestamp()


def _detect_oauth_needs_renew(ctx: MonitorContext) -> Dict[str, Any]:
    """Proactive (TTL) + reactive (V1b log scan) OAuth health detection."""
    names = ctx.kubectl(
        "get", "pod", "-l", "app=claude-worker",
        "-o", "jsonpath={.items[*].metadata.name}",
    ).out.split()
    min_remaining: Optional[float] = None
    no_creds = False
    for pod in names:
        res = ctx.kubectl("exec", pod, "--", "cat", "/home/claude/.claude/credentials.json")
        if res.rc != 0:
            no_creds = True
            continue
        remaining = _oauth_remaining_s(res.out, ctx.now)
        if remaining is None:
            no_creds = True
            continue
        min_remaining = remaining if min_remaining is None else min(min_remaining, remaining)

    auth_err_count = ctx.kubectl(
        "logs", "deploy/deile-pipeline", "--tail=200", "--since=10m",
    ).out.count("WORKER_AUTH_EXPIRED")

    needs_renew = (
        no_creds
        or (min_remaining is not None and min_remaining < OAUTH_TTL_THRESHOLD_S)
        or auth_err_count > 0
    )
    return {
        "pods": names,
        "needs_renew": needs_renew,
        "no_creds": no_creds,
        "min_remaining_s": min_remaining,
        "auth_err_count": auth_err_count,
    }


async def vigia_oauth(
    ctx: MonitorContext,
    renew: Callable[[], Awaitable[Any]],
) -> None:
    """V1 + V1b. ``renew`` is an async callable returning an object with
    ``ok`` / ``error`` (``try_refresh_claude_credentials`` in production)."""
    status = _detect_oauth_needs_renew(ctx)
    if not status["pods"]:
        return  # claude-worker not deployed — nothing to guard

    if not status["needs_renew"]:
        ctx.emitter.emit("monitor.vigia.fix V=V1 kind=oauth_check target=claude-worker elapsed_s=0")
        # Clear a previously-tracked anomaly (recovered).
        ctx.state.get("known_anomalies", {}).pop(_OAUTH_FINGERPRINT, None)
        return

    outcome = await renew()
    ok = bool(getattr(outcome, "ok", False))
    reason = ("auth_err in pipeline logs" if status["auth_err_count"]
              else "no credential file" if status["no_creds"]
              else "expires_in_s<1800")
    ctx.emitter.emit(
        f"monitor.action V=V1 kind=oauth_renew target=claude-worker-all "
        f"reason='{reason}' ok={'true' if ok else 'false'} elapsed_s=0"
    )
    ctx.flags.actions += 1
    if ok:
        ctx.emitter.emit("monitor.vigia.fix V=V1 kind=oauth_renew target=claude-worker-all elapsed_s=0")
        ctx.state.get("known_anomalies", {}).pop(_OAUTH_FINGERPRINT, None)
        return

    # Renew failed. Distinguish fatal (refresh_token dead — human needed) from
    # transient (retry next tick). Either way notify ONCE (P0 cooldown gates it);
    # NEVER hammer interactive login.
    err = (getattr(outcome, "error", None) or getattr(outcome, "message", "") or "").lower()
    fatal = "refresh_token" in err or status["no_creds"]
    core.record_anomaly(
        ctx.state, _OAUTH_FINGERPRINT, severity="P0", atype="oauth_expired",
        now=ctx.now, pods=status["pods"], renew_result="fail",
        renew_reason=str(getattr(outcome, "error", "") or getattr(outcome, "message", "")),
    )
    if fatal:
        body = ("Renovação automática headless falhou (refresh_token expirado ou sem "
                "credential file). Ação: `claude auth login --switch` no host + "
                "`deploy.py k8s claude-login`.")
    else:
        body = "Renovação automática falhou (transiente). Nova tentativa no próximo tick."
    ctx.notifier.notify(_OAUTH_FINGERPRINT, "P0", "OAuth claude-worker expirado", body)


# ---------------------------------------------------------------------------
# V7 — pipeline pod health (P0)
# ---------------------------------------------------------------------------

def vigia_pipeline_health(ctx: MonitorContext) -> None:
    res = ctx.kubectl("get", "pod", "-l", "app=deile-pipeline", "-o", "json")
    data = _parse_json(res.out)
    if not data:
        return
    items = data.get("items", [])
    for pod in items:
        name = pod.get("metadata", {}).get("name", "?")
        status = pod.get("status", {})
        phase = status.get("phase", "Unknown")
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions", [])
        )
        restarts = sum(int(c.get("restartCount", 0)) for c in status.get("containerStatuses", []))
        unhealthy = phase != "Running" or not ready or restarts > 3
        fp = f"pipeline_unhealthy_{name}"
        if unhealthy:
            tail = ctx.kubectl("logs", name, "--tail=50").out[-400:]
            core.record_anomaly(ctx.state, fp, severity="P0", atype="pipeline_unhealthy",
                                now=ctx.now, pod=name, phase=phase, ready=ready, restarts=restarts)
            title = f"Pipeline {name} não-saudável"
            body = (f"phase={phase} ready={ready} restarts={restarts}\n"
                    f"Coração do sistema. Logs (tail):\n{tail}")
            ctx.notifier.notify(fp, "P0", title, body)
        else:
            ctx.emitter.emit(
                f"monitor.vigia.fix V=V7 kind=pipeline_health target={name} "
                f"elapsed_s=0"
            )


# ---------------------------------------------------------------------------
# V2 — error pods + autonomous cleanup (P1)
# ---------------------------------------------------------------------------

def _owner_kinds(pod: Dict[str, Any]) -> List[str]:
    return [o.get("kind", "") for o in pod.get("metadata", {}).get("ownerReferences", [])]


def vigia_error_pods(ctx: MonitorContext) -> None:
    res = ctx.kubectl("get", "pods", "-o", "json")
    data = _parse_json(res.out)
    if not data:
        return
    error_count = 0
    for pod in data.get("items", []):
        meta = pod.get("metadata", {})
        name = meta.get("name", "?")
        status = pod.get("status", {})
        phase = status.get("phase", "")
        reason = status.get("reason", "")
        cstatuses = status.get("containerStatuses", [])
        age = _age_seconds(meta.get("creationTimestamp"), ctx.now)

        terminated_reasons = [
            cs.get("state", {}).get("terminated", {}).get("reason", "")
            for cs in cstatuses
        ]
        waiting_reasons = [
            cs.get("state", {}).get("waiting", {}).get("reason", "")
            for cs in cstatuses
        ]
        is_error = (
            phase == "Failed"
            or "Error" in terminated_reasons
            or "OOMKilled" in terminated_reasons
        )
        if is_error:
            error_count += 1

        # Safe autonomous cure: delete abandoned Job pods (BackoffLimitExceeded > 1h).
        owned_by_job = "Job" in _owner_kinds(pod)
        if (
            owned_by_job
            and reason == "BackoffLimitExceeded"
            and age is not None
            and age > 3600
        ):
            _delete_abandoned_pod(ctx, name)
            continue

        # CrashLoopBackOff on a critical service (>3 restarts) → notify P1.
        crashloop = "CrashLoopBackOff" in waiting_reasons
        restarts = sum(int(cs.get("restartCount", 0)) for cs in cstatuses)
        critical = any(frag in name for frag in _CRITICAL_NAME_FRAGMENTS)
        if crashloop and restarts > 3 and critical:
            fp = f"pod_crashloop_{name}"
            tail = ctx.kubectl("logs", name, "--tail=50").out[-200:]
            core.record_anomaly(ctx.state, fp, severity="P1", atype="pod_crashloop",
                                now=ctx.now, pod=name, restarts=restarts)
            ctx.notifier.notify(fp, "P1", f"Pod {name} em CrashLoopBackOff",
                                f"restarts={restarts}\n{tail}")

    if error_count >= 5:
        fp = "pod_error_accumulation"
        core.record_anomaly(ctx.state, fp, severity="P1", atype="pod_error_accumulation",
                            now=ctx.now, count=error_count)
        ctx.notifier.notify(fp, "P1", "Pods em erro acumulando",
                            f"{error_count} pods em estado de erro no namespace {ctx.namespace}")


def _delete_abandoned_pod(ctx: MonitorContext, name: str) -> None:
    res = ctx.kubectl("delete", "pod", name)
    ok = res.rc == 0
    ctx.emitter.emit(
        f"monitor.action V=V2 kind=delete_pod target={name} "
        f"reason='BackoffLimitExceeded >1h' ok={'true' if ok else 'false'} elapsed_s=0"
    )
    ctx.flags.actions += 1
    if ok:
        ctx.emitter.emit(f"monitor.vigia.fix V=V2 kind=delete_pod target={name} elapsed_s=0")


# ---------------------------------------------------------------------------
# V6 — failed Jobs (P1, credentials-renew escalates to P0)
# ---------------------------------------------------------------------------

def vigia_failed_jobs(ctx: MonitorContext) -> None:
    res = ctx.kubectl("get", "jobs", "-o", "json")
    data = _parse_json(res.out)
    if not data:
        return
    for job in data.get("items", []):
        meta = job.get("metadata", {})
        name = meta.get("name", "?")
        failed = any(
            c.get("type") == "Failed" and c.get("status") == "True"
            for c in job.get("status", {}).get("conditions", [])
        )
        if not failed:
            continue
        age = _age_seconds(meta.get("creationTimestamp"), ctx.now)
        fp = f"job_backoff_{name}"
        if "credentials-renew" in name:
            core.record_anomaly(ctx.state, fp, severity="P0", atype="job_backoff",
                                now=ctx.now, job=name)
            ctx.notifier.notify(fp, "P0", f"Job {name} falhou (BackoffLimitExceeded)",
                                "claude-credentials-renew pode exigir OAuth manual")
        elif age is None or age > 1800:  # > 30min
            core.record_anomaly(ctx.state, fp, severity="P1", atype="job_backoff",
                                now=ctx.now, job=name)
            ctx.notifier.notify(fp, "P1", f"Job {name} falhou (BackoffLimitExceeded)",
                                f"parado há {_human_age(age)}")


# ---------------------------------------------------------------------------
# Forge helpers
# ---------------------------------------------------------------------------

def _gh_list(ctx: MonitorContext, *args: str) -> List[Dict[str, Any]]:
    res = ctx.gh("api", *args)
    data = _parse_json(res.out)
    return data if isinstance(data, list) else []


def _is_pull_request(item: Dict[str, Any]) -> bool:
    return "pull_request" in item


# ---------------------------------------------------------------------------
# V3 — orphan issues (P1, notify; 6h renotify)
# ---------------------------------------------------------------------------

def vigia_orphan_issues(ctx: MonitorContext) -> None:
    issues = _gh_list(
        ctx, f"repos/{ctx.repo}/issues",
        "-f", f"labels={_ORPHAN_LABELS}", "-f", "state=open", "-f", "per_page=100",
    )
    for issue in issues:
        if _is_pull_request(issue):
            continue
        number = issue.get("number")
        age = _age_seconds(issue.get("updated_at"), ctx.now)
        if number is None or age is None or age < ORPHAN_STALE_S:
            continue
        label = next((lb.get("name", "") for lb in issue.get("labels", [])
                      if lb.get("name", "").startswith("~workflow:")), "")
        fp = f"orphan_{number}"
        core.record_anomaly(ctx.state, fp, severity="P1", atype="orphan_issue",
                            now=ctx.now, issue=number, title=issue.get("title", ""),
                            stalled_hours=round(age / 3600, 1))
        ctx.notifier.notify(
            fp, "P1", f"Issue #{number} órfã em {label or 'workflow'}",
            f"{issue.get('title', '')} — parada há {_human_age(age)}",
            min_interval_s=ORPHAN_RENOTIFY_S,
        )


# ---------------------------------------------------------------------------
# V5 — awaiting stakeholder (P2, 4h)
# ---------------------------------------------------------------------------

def vigia_stakeholder(ctx: MonitorContext) -> None:
    issues = _gh_list(
        ctx, f"repos/{ctx.repo}/issues",
        "-f", "labels=~workflow:aguardando_stakeholder", "-f", "state=open", "-f", "per_page=100",
    )
    for issue in issues:
        if _is_pull_request(issue):
            continue
        number = issue.get("number")
        age = _age_seconds(issue.get("updated_at"), ctx.now)
        if number is None or age is None or age < STAKEHOLDER_WAIT_S:
            continue
        assignees = ",".join(a.get("login", "") for a in issue.get("assignees", []))
        fp = f"stakeholder_{number}"
        core.record_anomaly(ctx.state, fp, severity="P2", atype="aguardando_stakeholder",
                            now=ctx.now, issue=number, title=issue.get("title", ""),
                            assignee=assignees)
        ctx.notifier.notify(
            fp, "P2", f"Issue #{number} aguardando stakeholder",
            f"{issue.get('title', '')} — há {_human_age(age)} · assignees: {assignees or '—'}",
        )


# ---------------------------------------------------------------------------
# V4 — auto/* PRs with attempt N/3 (P1)
# ---------------------------------------------------------------------------

_ATTEMPT_RE = re.compile(r"attempt\s+([23])\s*/\s*3", re.IGNORECASE)


def vigia_pr_attempts(ctx: MonitorContext) -> None:
    pulls = _gh_list(ctx, f"repos/{ctx.repo}/pulls", "-f", "state=open", "-f", "per_page=100")
    for pr in pulls:
        ref = pr.get("head", {}).get("ref", "")
        if not ref.startswith("auto/"):
            continue
        m = re.search(r"issue-(\d+)", ref)
        if not m:
            continue
        issue_n = m.group(1)
        pr_n = pr.get("number")
        comments = _gh_list(ctx, f"repos/{ctx.repo}/issues/{issue_n}/comments")
        best_attempt = 0
        for c in comments[-5:]:
            am = _ATTEMPT_RE.search(c.get("body", ""))
            if am:
                best_attempt = max(best_attempt, int(am.group(1)))
        if best_attempt >= 2:
            fp = f"pr_attempt_{pr_n}_{best_attempt}"
            core.record_anomaly(ctx.state, fp, severity="P1", atype="pr_attempt",
                                now=ctx.now, pr=pr_n, issue=int(issue_n), attempt=best_attempt)
            ctx.notifier.notify(
                fp, "P1", f"PR #{pr_n} (issue #{issue_n}) em attempt {best_attempt}/3",
                f"{pr.get('title', '')} — branch {ref}",
            )


# ---------------------------------------------------------------------------
# V8 — follow-up collection + deterministic pre-filter → Phase B candidates
# ---------------------------------------------------------------------------

def _bot_login(login: str) -> bool:
    bot = os.environ.get("DEILE_BOT_LOGIN", "")
    return login.endswith("[bot]") or (bool(bot) and login == bot)


def _in_code_block(body: str, match_start: int) -> bool:
    """True if the match offset falls inside a fenced ``` block or a 4-space
    indented line (mechanical anti-false-positive filter)."""
    before = body[:match_start]
    if before.count("```") % 2 == 1:
        return True
    line_start = before.rfind("\n") + 1
    line = body[line_start:body.find("\n", match_start) if body.find("\n", match_start) != -1 else len(body)]
    return line.startswith("    ") or line.startswith("\t")


def _fu_matches(body: str) -> List[int]:
    """Return start offsets of follow-up promise matches not inside code blocks."""
    offsets: List[int] = []
    for pat in _FU_PATTERNS:
        for m in pat.finditer(body or ""):
            if not _in_code_block(body, m.start()):
                offsets.append(m.start())
    return offsets


def _collect_origin_comments(ctx: MonitorContext, kind: str) -> List[Dict[str, Any]]:
    """kind='issues' (closed) or 'pulls' (merged), within the last 24h."""
    items = _gh_list(
        ctx, f"repos/{ctx.repo}/{kind}",
        "-f", "state=closed", "-f", "sort=updated", "-f", "per_page=30",
    )
    out: List[Dict[str, Any]] = []
    for item in items:
        if kind == "issues" and _is_pull_request(item):
            continue
        if kind == "pulls" and not item.get("merged_at"):
            continue
        ts = item.get("merged_at") if kind == "pulls" else item.get("closed_at")
        age = _age_seconds(ts, ctx.now)
        if age is None or age > 86400:
            continue
        out.append(item)
    return out


def vigia_collect_followups(ctx: MonitorContext) -> List[Dict[str, Any]]:
    """Collect follow-up candidates and apply the DETERMINISTIC pre-filters
    (bot author, code block, fingerprint-seen). Survivors are returned for Phase
    B (semantic already-tracked judgement + issue creation). Emits ``monitor.v8.skip``
    for deterministic rejections. Does NOT create issues (that is Phase B)."""
    seen = set(ctx.state.get("fu_fingerprints", []))
    candidates: List[Dict[str, Any]] = []
    n_candidates = 0
    n_skipped = 0

    origins: List[tuple] = []
    for item in _collect_origin_comments(ctx, "issues"):
        origins.append(("issue", item))
    for item in _collect_origin_comments(ctx, "pulls"):
        origins.append(("pr", item))

    for kind, item in origins:
        number = item.get("number")
        # The body itself can carry a promise (comment_id sentinel "body").
        body_units = [("body", item.get("body", ""), item.get("user", {}).get("login", ""))]
        comments = _gh_list(ctx, f"repos/{ctx.repo}/issues/{number}/comments")
        for c in comments:
            body_units.append((c.get("id"), c.get("body", ""), c.get("user", {}).get("login", "")))

        for comment_id, body, author in body_units:
            offsets = _fu_matches(body)
            if not offsets:
                continue
            n_candidates += 1
            fp = f"fu_{number}_{comment_id}"
            if _bot_login(author):
                n_skipped += 1
                ctx.emitter.emit(f"monitor.v8.skip origin=#{number}/comment{comment_id} reason=bot_author")
                continue
            if fp in seen:
                n_skipped += 1
                ctx.emitter.emit(f"monitor.v8.skip origin=#{number}/comment{comment_id} reason=fingerprint_seen")
                continue
            snippet = body[offsets[0]:offsets[0] + 280].replace("\n", " ")
            candidates.append({
                "origin": number, "origin_kind": kind, "comment_id": comment_id,
                "author": author, "snippet": snippet, "fingerprint": fp,
            })

    # When nothing survives, Phase A owns the v8.scan summary (no Phase B will run).
    if not candidates:
        ctx.emitter.emit(
            f"monitor.v8.scan candidates={n_candidates} created=0 "
            f"skipped={n_skipped} capped=false"
        )
    return candidates
