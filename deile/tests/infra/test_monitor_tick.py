"""Tests for the tick orchestrator ``infra/k8s/monitor_tick.py`` (Phase A).

Covers the tick lifecycle: kill-switch + auto-resume, steer-command processing,
the tick summary emit, and the Phase-B escalation decision (judgment file written
only when V8 follow-up candidates survive). Vigias run against a fake runner.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_INFRA = str(_REPO / "infra" / "k8s")
if _INFRA not in sys.path:
    sys.path.insert(0, _INFRA)


@pytest.fixture
def tick():
    import monitor_tick
    return monitor_tick


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


class FakeRunner:
    def __init__(self, table=None):
        self.table = table or {}
        self.calls = []

    def __call__(self, args, **kwargs):
        from monitor_core import CmdResult
        joined = " ".join(args)
        self.calls.append(joined)
        for needle, (rc, out) in self.table.items():
            if needle in joined:
                return CmdResult(rc, out, "")
        return CmdResult(0, "[]", "")


async def _renew_ok():
    from types import SimpleNamespace
    return SimpleNamespace(ok=True, error=None, message="ok", seconds_until_new_expiry=3600)


def _state_dir(tmp_path):
    d = tmp_path / "state"
    (d / "monitor-commands").mkdir(parents=True)
    return d


async def _run(tick, state_dir, *, now, runner=None, **kw):
    runner = runner or FakeRunner()
    return await tick.run_tick(
        str(state_dir), now=now, run=runner, renew=_renew_ok,
        repo="elimarcavalli/deile", namespace="deile",
        bot_endpoint="http://deilebot:8765", bot_token="t", user_id="",
        kube_probe=lambda ep: 0, **kw,
    )


# ---------------------------------------------------------------------------
# Kill-switch / auto-resume
# ---------------------------------------------------------------------------

async def test_kill_switch_active_exits_early(tick, tmp_path):
    sd = _state_dir(tmp_path)
    (sd / "monitor-pause").write_text("")
    json.dump({"paused_until": "2026-06-02T23:00:00Z"}, open(sd / "monitor-state.json", "w"))
    runner = FakeRunner()
    res = await _run(tick, sd, now=_utc(2026, 6, 2, 11, 0, 0), runner=runner)
    assert res["paused"] is True
    # No vigia ran (no kubectl/gh calls).
    assert not any("get pods" in c or "gh api" in c for c in runner.calls)


async def test_auto_resume_when_pause_expired(tick, tmp_path, capsys):
    sd = _state_dir(tmp_path)
    (sd / "monitor-pause").write_text("")
    json.dump({"paused_until": "2026-06-02T10:00:00Z"}, open(sd / "monitor-state.json", "w"))
    res = await _run(tick, sd, now=_utc(2026, 6, 2, 11, 0, 0))
    assert res["paused"] is False
    assert not (sd / "monitor-pause").exists()
    out = capsys.readouterr().out
    assert "monitor.command from=auto kind=resume" in out


# ---------------------------------------------------------------------------
# Steer commands
# ---------------------------------------------------------------------------

async def test_steer_pause_creates_flag_and_consumes_file(tick, tmp_path, capsys):
    sd = _state_dir(tmp_path)
    (sd / "monitor-commands" / "cmd1").write_text("pause 30m")
    await _run(tick, sd, now=_utc(2026, 6, 2, 11, 0, 0))
    assert (sd / "monitor-pause").exists()
    assert not (sd / "monitor-commands" / "cmd1").exists()  # consumed
    state = json.load(open(sd / "monitor-state.json"))
    assert state["paused_until"] == "2026-06-02T11:30:00Z"
    assert "monitor.command from=bot kind=pause" in capsys.readouterr().out


async def test_steer_ack_sets_acked_until(tick, tmp_path):
    sd = _state_dir(tmp_path)
    json.dump({"known_anomalies": {"orphan_437": {"count": 3}}},
              open(sd / "monitor-state.json", "w"))
    (sd / "monitor-commands" / "c").write_text("ack orphan_437")
    await _run(tick, sd, now=_utc(2026, 6, 2, 11, 0, 0))
    state = json.load(open(sd / "monitor-state.json"))
    assert state["known_anomalies"]["orphan_437"]["acked_until"] == "2026-06-03T11:00:00Z"


async def test_steer_unknown_command_emits_unknown(tick, tmp_path, capsys):
    sd = _state_dir(tmp_path)
    (sd / "monitor-commands" / "c").write_text("frobnicate now")
    await _run(tick, sd, now=_utc(2026, 6, 2, 11, 0, 0))
    assert "monitor.command from=bot kind=unknown ok=false" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Tick summary + counter
# ---------------------------------------------------------------------------

async def test_tick_emits_summary_and_increments_counter(tick, tmp_path, capsys):
    sd = _state_dir(tmp_path)
    await _run(tick, sd, now=_utc(2026, 6, 2, 11, 0, 0))
    out = capsys.readouterr().out
    assert "monitor.tick #1 done in" in out
    state = json.load(open(sd / "monitor-state.json"))
    assert state["last_tick"] == 1


# ---------------------------------------------------------------------------
# Phase B escalation
# ---------------------------------------------------------------------------

async def test_no_phase_b_when_quiet(tick, tmp_path):
    sd = _state_dir(tmp_path)
    res = await _run(tick, sd, now=_utc(2026, 6, 2, 11, 0, 0))
    assert res["needs_phase_b"] is False
    assert not (sd / "monitor-judgment.json").exists()


async def test_phase_b_written_when_fu_candidate(tick, tmp_path):
    sd = _state_dir(tmp_path)
    closed = [{"number": 445, "title": "X", "body": "vou abrir uma issue para o resto",
               "closed_at": "2026-06-02T09:00:00Z", "user": {"login": "human"}}]
    runner = FakeRunner({
        "issues -f state=closed": (0, json.dumps(closed)),
        "issues/445/comments": (0, "[]"),
        "pulls -f state=closed": (0, "[]"),
    })
    res = await _run(tick, sd, now=_utc(2026, 6, 2, 11, 0, 0), runner=runner)
    assert res["needs_phase_b"] is True
    payload = json.load(open(sd / "monitor-judgment.json"))
    assert payload["fu_candidates"][0]["origin"] == 445


# ---------------------------------------------------------------------------
# parse_duration_s
# ---------------------------------------------------------------------------

async def test_kube_unreachable_skips_with_v_token(tick, tmp_path, capsys):
    sd = _state_dir(tmp_path)
    await tick.run_tick(
        str(sd), now=_utc(2026, 6, 2, 11, 0, 0), run=FakeRunner(), renew=_renew_ok,
        repo="elimarcavalli/deile", namespace="deile",
        bot_endpoint="http://deilebot:8765", bot_token="t", user_id="",
        kube_probe=lambda ep: 1,  # all endpoints unreachable
    )
    out = capsys.readouterr().out
    assert "monitor.vigia.skip V=V1 reason=K8S_API_UNREACHABLE" in out
    assert "monitor.vigia.skip V=V7 reason=K8S_API_UNREACHABLE" in out


@pytest.mark.parametrize("s,expected", [("30m", 1800), ("1h", 3600), ("2h", 7200)])
def test_parse_duration_s(tick, s, expected):
    assert tick.parse_duration_s(s) == expected


# ---------------------------------------------------------------------------
# Issue #612 (AC-6, monitor side) — the INJECTED repo reaches the real
# gh call-site (the vigias hit ``gh api repos/<repo>/...``). Proves the
# project-agnostic config flows through the monitor tick, not a hardcoded
# default. Exercises run_tick → MonitorContext → vigias (the real path).
# ---------------------------------------------------------------------------

async def test_injected_repo_reaches_gh_call_site(tick, tmp_path):
    """A tick run with a neutral repo must issue gh calls against THAT repo —
    never the hardcoded ``elimarcavalli/deile``."""
    sd = _state_dir(tmp_path)
    runner = FakeRunner()
    await tick.run_tick(
        str(sd), now=_utc(2026, 6, 2, 11, 0, 0), run=runner, renew=_renew_ok,
        repo="acme/neutral-project", namespace="deile",
        bot_endpoint="http://deilebot:8765", bot_token="t", user_id="",
        kube_probe=lambda ep: 0,  # kube reachable → kube-dependent vigias run
    )
    gh_calls = [c for c in runner.calls if "repos/" in c]
    assert gh_calls, "expected at least one gh api repos/<repo>/... call"
    assert all("repos/acme/neutral-project/" in c for c in gh_calls), gh_calls
    assert not any("elimarcavalli/deile" in c for c in runner.calls)


async def test_judgment_file_carries_injected_repo(tick, tmp_path, monkeypatch):
    """Phase-B judgment payload reports the injected repo (read from the
    DEILE_PIPELINE_REPO env that the manifest sources from the ConfigMap)."""
    monkeypatch.setenv("DEILE_PIPELINE_REPO", "acme/neutral-project")
    sd = _state_dir(tmp_path)
    closed = [{"number": 50, "title": "X", "body": "vou abrir uma issue para o resto",
               "closed_at": "2026-06-02T09:00:00Z", "user": {"login": "human"}}]
    runner = FakeRunner({
        "issues -f state=closed": (0, json.dumps(closed)),
        "issues/50/comments": (0, "[]"),
        "pulls -f state=closed": (0, "[]"),
    })
    await tick.run_tick(
        str(sd), now=_utc(2026, 6, 2, 11, 0, 0), run=runner, renew=_renew_ok,
        repo="acme/neutral-project", namespace="deile",
        bot_endpoint="http://deilebot:8765", bot_token="t", user_id="",
        kube_probe=lambda ep: 0,
    )
    payload = json.load(open(sd / "monitor-judgment.json"))
    assert payload["repo"] == "acme/neutral-project"
