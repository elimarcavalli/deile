"""Tests for ``infra/k8s/monitor_core.py`` — the deterministic engine of the
DEILE-Monitor tick (Phase A).

Covers the reusable primitives: emit-line sanitization, the structured Emitter
(stdout-first + audit-log append + audit_pvc_fail guard), per-severity cooldown,
anti-flood predicates (ack suppression, per-fingerprint cooldown, hourly cap),
state load/save, notification composition and the DNS-first kube-api resolver.

Loaded via importlib (same pattern as ``test_wrapper_monitor.py``) because
``infra/k8s`` is not an importable package.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def core():
    repo_root = Path(__file__).resolve().parents[3]
    mod_path = repo_root / "infra" / "k8s" / "monitor_core.py"
    spec = importlib.util.spec_from_file_location("monitor_core_under_test", str(mod_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["monitor_core_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# sanitize_emit_line
# ---------------------------------------------------------------------------

def test_sanitize_strips_control_chars(core):
    assert core.sanitize_emit_line("a\nb\rc\td") == "a b c d"


def test_sanitize_truncates_to_500(core):
    out = core.sanitize_emit_line("x" * 600)
    assert len(out) == 500


def test_sanitize_truncates_before_stripping(core):
    # 500-char cap applied first, then control-char strip (matches bash _emit).
    line = "y" * 499 + "\n" + "z" * 50
    out = core.sanitize_emit_line(line)
    assert len(out) == 500
    assert "\n" not in out


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------

def test_emitter_writes_stdout_and_audit(core, tmp_path, capsys):
    audit = tmp_path / "monitor-audit.log"
    flags = core.TickFlags()
    em = core.Emitter(str(audit), flags, clock=lambda: _utc(2026, 6, 2, 11, 0, 0), tick_n=7)
    em.emit("monitor.action V=V2 kind=delete_pod ok=true elapsed_s=1")
    out = capsys.readouterr().out
    assert "monitor.action V=V2 kind=delete_pod ok=true elapsed_s=1" in out
    content = audit.read_text()
    assert content == "2026-06-02T11:00:00Z monitor.action V=V2 kind=delete_pod ok=true elapsed_s=1\n"


def test_emitter_audit_pvc_fail_once_per_tick(core, tmp_path, capsys):
    # Unwritable audit path (parent dir does not exist) → OSError → fallback emit.
    audit = tmp_path / "nope" / "monitor-audit.log"
    flags = core.TickFlags()
    em = core.Emitter(str(audit), flags, clock=lambda: _utc(2026, 6, 2, 11, 0, 0), tick_n=9)
    em.emit("monitor.tick #9 done in 1s: actions=0 notify=0 skipped=[] anomalias=0")
    em.emit("monitor.action V=V1 kind=oauth_check ok=false elapsed_s=0")
    out = capsys.readouterr().out
    # Both lines still reach stdout (source of truth)
    assert "monitor.tick #9" in out
    assert "monitor.action V=V1" in out
    # audit_pvc_fail emitted exactly once, carrying the tick number
    assert out.count("monitor.audit_pvc_fail") == 1
    assert "tick=#9" in out
    assert flags.pvc_fail_emitted is True


# ---------------------------------------------------------------------------
# cooldown_seconds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sev,expected", [("P0", 900), ("P1", 7200), ("P2", 14400)])
def test_cooldown_seconds(core, sev, expected):
    assert core.cooldown_seconds(sev) == expected


# ---------------------------------------------------------------------------
# anti-flood predicates
# ---------------------------------------------------------------------------

def test_is_acked_true_when_future(core):
    anomaly = {"acked_until": "2026-06-02T12:00:00Z"}
    assert core.is_acked(anomaly, _utc(2026, 6, 2, 11, 0, 0)) is True


def test_is_acked_false_when_expired(core):
    anomaly = {"acked_until": "2026-06-02T10:00:00Z"}
    assert core.is_acked(anomaly, _utc(2026, 6, 2, 11, 0, 0)) is False


def test_is_acked_false_when_absent(core):
    assert core.is_acked({}, _utc(2026, 6, 2, 11, 0, 0)) is False


def test_in_cooldown_p1_true_within_2h(core):
    anomaly = {"last_notified": "2026-06-02T10:00:00Z"}
    assert core.in_cooldown(anomaly, "P1", _utc(2026, 6, 2, 11, 0, 0)) is True


def test_in_cooldown_p1_false_after_2h(core):
    anomaly = {"last_notified": "2026-06-02T08:00:00Z"}
    assert core.in_cooldown(anomaly, "P1", _utc(2026, 6, 2, 11, 0, 0)) is False


def test_in_cooldown_false_when_never_notified(core):
    assert core.in_cooldown({}, "P0", _utc(2026, 6, 2, 11, 0, 0)) is False


def test_hour_slot_format(core):
    assert core.hour_slot(_utc(2026, 6, 2, 11, 37, 9)) == "2026-06-02T11:00:00"


def test_hourly_cap_reached_resets_on_new_hour(core):
    state = {"notifications_this_hour": 8, "hour_slot": "2026-06-02T10:00:00"}
    # New hour → counter logically resets → cap NOT reached
    reached = core.hourly_cap_reached(state, _utc(2026, 6, 2, 11, 5, 0), cap=8)
    assert reached is False


def test_hourly_cap_reached_true_same_hour(core):
    state = {"notifications_this_hour": 8, "hour_slot": "2026-06-02T11:00:00"}
    reached = core.hourly_cap_reached(state, _utc(2026, 6, 2, 11, 5, 0), cap=8)
    assert reached is True


# ---------------------------------------------------------------------------
# state load/save
# ---------------------------------------------------------------------------

def test_default_state_has_required_keys(core):
    st = core.default_state()
    for key in (
        "last_tick", "known_anomalies", "notifications_this_hour",
        "hour_slot", "fu_fingerprints", "fu_created_today", "fu_day_slot",
    ):
        assert key in st


def test_load_state_returns_defaults_when_missing(core, tmp_path):
    st = core.load_state(str(tmp_path / "absent.json"))
    assert st["last_tick"] == 0
    assert st["known_anomalies"] == {}


def test_load_state_returns_defaults_when_corrupt(core, tmp_path):
    p = tmp_path / "monitor-state.json"
    p.write_text("{ this is not json")
    st = core.load_state(str(p))
    assert st["known_anomalies"] == {}


def test_save_then_load_roundtrip(core, tmp_path):
    p = tmp_path / "monitor-state.json"
    st = core.default_state()
    st["last_tick"] = 42
    st["known_anomalies"]["orphan_437"] = {"count": 3}
    core.save_state(str(p), st)
    back = core.load_state(str(p))
    assert back["last_tick"] == 42
    assert back["known_anomalies"]["orphan_437"]["count"] == 3


def test_save_state_is_atomic_no_tmp_left(core, tmp_path):
    p = tmp_path / "monitor-state.json"
    core.save_state(str(p), core.default_state())
    leftovers = [x.name for x in tmp_path.iterdir() if x.name != "monitor-state.json"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# record_anomaly
# ---------------------------------------------------------------------------

def test_record_anomaly_creates_then_increments(core):
    state = core.default_state()
    now = _utc(2026, 6, 2, 11, 0, 0)
    core.record_anomaly(state, "orphan_437", severity="P1", atype="orphan_issue", now=now, issue=437)
    a = state["known_anomalies"]["orphan_437"]
    assert a["count"] == 1
    assert a["first_seen"] == "2026-06-02T11:00:00Z"
    assert a["severity"] == "P1"
    assert a["issue"] == 437
    core.record_anomaly(state, "orphan_437", severity="P1", atype="orphan_issue",
                        now=_utc(2026, 6, 2, 11, 10, 0), issue=437)
    a = state["known_anomalies"]["orphan_437"]
    assert a["count"] == 2
    assert a["first_seen"] == "2026-06-02T11:00:00Z"  # preserved


# ---------------------------------------------------------------------------
# compose_notification
# ---------------------------------------------------------------------------

def test_compose_notification_emoji_and_header(core):
    msg = core.compose_notification("P0", "OAuth claude-worker expirado", "sem credential file em 3/3 pods")
    assert msg.startswith("🔴 [DEILE-MONITOR] P0: OAuth claude-worker expirado")
    assert "sem credential file em 3/3 pods" in msg
    assert "/monitor pause 30m" in msg  # comandos rápidos footer


@pytest.mark.parametrize("sev,emoji", [("P0", "🔴"), ("P1", "🟡"), ("P2", "🔵")])
def test_compose_notification_emoji_by_severity(core, sev, emoji):
    msg = core.compose_notification(sev, "t", "d")
    assert msg.startswith(emoji)


# ---------------------------------------------------------------------------
# resolve_kube_api (DNS-first)
# ---------------------------------------------------------------------------

def test_resolve_kube_api_prefers_first_endpoint(core):
    tried = []

    def runner(endpoint):
        tried.append(endpoint)
        return 0  # first one succeeds

    ep = core.resolve_kube_api(runner)
    assert ep == "https://kubernetes.default.svc:443"
    assert tried == ["https://kubernetes.default.svc:443"]


def test_resolve_kube_api_falls_through(core):
    def runner(endpoint):
        return 0 if "cluster.local" in endpoint else 1

    ep = core.resolve_kube_api(runner)
    assert ep == "https://kubernetes.default.svc.cluster.local:443"


def test_resolve_kube_api_returns_none_when_all_fail(core):
    assert core.resolve_kube_api(lambda ep: 1) is None


# ---------------------------------------------------------------------------
# run_cmd
# ---------------------------------------------------------------------------

def test_run_cmd_success(core):
    res = core.run_cmd(["printf", "hello"])
    assert res.rc == 0
    assert res.out == "hello"


def test_run_cmd_nonzero(core):
    res = core.run_cmd(["sh", "-c", "exit 3"])
    assert res.rc == 3


def test_run_cmd_missing_binary_is_graceful(core):
    # P4 graceful: a missing binary must NOT raise — returns rc != 0.
    res = core.run_cmd(["this-binary-does-not-exist-xyz"])
    assert res.rc != 0


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

def _make_notifier(core, tmp_path, *, state, user_id="", run=None, clock=None):
    flags = core.TickFlags()
    emitter = core.Emitter(str(tmp_path / "audit.log"), flags,
                           clock=clock or (lambda: _utc(2026, 6, 2, 11, 0, 0)))
    return core.Notifier(
        state=state,
        emitter=emitter,
        flags=flags,
        run=run or (lambda args, **k: core.CmdResult(0, "", "")),
        bot_endpoint="http://deilebot:8765",
        bot_token="tok",
        user_id=user_id,
        notif_log_path=str(tmp_path / "notif.log"),
        clock=clock or (lambda: _utc(2026, 6, 2, 11, 0, 0)),
    ), emitter, flags


def test_notifier_suppressed_when_acked(core, tmp_path, capsys):
    state = core.default_state()
    state["known_anomalies"]["fp"] = {"acked_until": "2026-06-02T23:00:00Z"}
    n, _, _ = _make_notifier(core, tmp_path, state=state)
    sent = n.notify("fp", "P0", "title", "body")
    assert sent is False
    assert "monitor.notify" not in capsys.readouterr().out


def test_notifier_suppressed_in_cooldown(core, tmp_path, capsys):
    state = core.default_state()
    state["known_anomalies"]["fp"] = {"last_notified": "2026-06-02T10:30:00Z"}  # 30m ago, P1=2h
    n, _, _ = _make_notifier(core, tmp_path, state=state)
    assert n.notify("fp", "P1", "t", "b") is False
    assert "monitor.notify" not in capsys.readouterr().out


def test_notifier_log_only_when_no_user_id(core, tmp_path, capsys):
    state = core.default_state()
    state["known_anomalies"]["fp"] = {}
    n, _, _ = _make_notifier(core, tmp_path, state=state, user_id="")
    assert n.notify("fp", "P1", "Issue órfã", "stuck 12h") is True
    out = capsys.readouterr().out
    assert "monitor.notify fingerprint=fp severity=P1 channel=log-only ok=true" in out
    assert state["known_anomalies"]["fp"]["last_notified"] == "2026-06-02T11:00:00Z"
    assert state["notifications_this_hour"] == 1


def test_notifier_dm_when_user_id_and_curl_ok(core, tmp_path, capsys):
    state = core.default_state()
    state["known_anomalies"]["fp"] = {}
    calls = []

    def run(args, **k):
        calls.append(args)
        return core.CmdResult(0, "", "")

    n, _, _ = _make_notifier(core, tmp_path, state=state, user_id="123", run=run)
    assert n.notify("fp", "P0", "Pipeline down", "not ready") is True
    out = capsys.readouterr().out
    assert "channel=dm ok=true" in out
    assert any("curl" in a[0] for a in calls)


def test_notifier_hourly_cap_emits_flood_cap_once(core, tmp_path, capsys):
    state = core.default_state()
    state["notifications_this_hour"] = 8
    state["hour_slot"] = "2026-06-02T11:00:00"
    state["known_anomalies"]["a"] = {}
    state["known_anomalies"]["b"] = {}
    n, _, flags = _make_notifier(core, tmp_path, state=state)
    assert n.notify("a", "P1", "t", "b") is False
    assert n.notify("b", "P1", "t", "b") is False
    out = capsys.readouterr().out
    assert out.count("monitor.flood_cap kind=notify") == 1
    assert flags.flood_cap_notify_emitted is True


def test_notifier_counter_resets_on_new_hour(core, tmp_path, capsys):
    state = core.default_state()
    state["notifications_this_hour"] = 8
    state["hour_slot"] = "2026-06-02T10:00:00"  # previous hour
    state["known_anomalies"]["fp"] = {}
    n, _, _ = _make_notifier(core, tmp_path, state=state)
    assert n.notify("fp", "P1", "t", "b") is True  # new hour → counter reset → not capped
    assert state["hour_slot"] == "2026-06-02T11:00:00"
    assert state["notifications_this_hour"] == 1


def test_notifier_min_interval_override_suppresses(core, tmp_path, capsys):
    # Orphan issues renotify every 6h even though P1 default cooldown is 2h.
    state = core.default_state()
    state["known_anomalies"]["orphan_1"] = {"last_notified": "2026-06-02T08:00:00Z"}  # 3h ago
    n, _, _ = _make_notifier(core, tmp_path, state=state)
    sent = n.notify("orphan_1", "P1", "t", "b", min_interval_s=21600)  # 6h
    assert sent is False  # within 6h window → suppressed even though > 2h
