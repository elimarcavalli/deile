"""Tests for V1 (OAuth) of ``infra/k8s/monitor_vigias.py``.

V1 is the headline fix: the old persona ran ``claude auth login`` (interactive,
impossible headless) and hammered it ~100×/day. V1 calls an injected ``renew``
coroutine, emits ONE structured action, and on a fatal/unrenewable result
notifies P0 ONCE (cooldown-gated) instead of retrying forever. Since issue #603
(setup-token, ~1-year ``CLAUDE_CODE_OAUTH_TOKEN``) the production ``renew``
reports ``ok=False`` — there is no headless refresh — so V1 always degrades to
the notify path when the token actually expires.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[3]
_INFRA = str(_REPO / "infra" / "k8s")
if _INFRA not in sys.path:
    sys.path.insert(0, _INFRA)


@pytest.fixture
def core():
    import monitor_core

    return monitor_core


@pytest.fixture
def vig():
    import monitor_vigias

    return monitor_vigias


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


class FakeRunner:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, args, **kwargs):
        from monitor_core import CmdResult

        joined = " ".join(args)
        self.calls.append(joined)
        for needle, (rc, out) in self.table.items():
            if needle in joined:
                return CmdResult(rc, out, "")
        return CmdResult(0, "", "")


def _ctx(core, vig, runner, now):
    state = core.default_state()
    flags = core.TickFlags()
    emitter = core.Emitter("/dev/null", flags, clock=lambda: now)
    sent = []

    notifier = SimpleNamespace(
        sent=sent,
        notify=lambda fp, sev, title, body: (sent.append((fp, sev, title)) or True),
    )
    ctx = vig.MonitorContext(
        run=runner,
        emitter=emitter,
        notifier=notifier,
        state=state,
        flags=flags,
        now=now,
        repo="elimarcavalli/deile",
        namespace="deile",
        kube_api="https://kubernetes.default.svc:443",
    )
    return ctx, sent, state


def _creds(now, *, plus_seconds):
    exp_ms = int((now.timestamp() + plus_seconds) * 1000)
    return '{"expiresAt": %d}' % exp_ms


async def _ok_renew():
    return SimpleNamespace(
        ok=True, message="renewed", error=None, seconds_until_new_expiry=3600
    )


async def _fatal_renew():
    return SimpleNamespace(
        ok=False,
        message="fail",
        error="refresh_token also expired",
        seconds_until_new_expiry=None,
    )


async def test_v1_healthy_no_renew_no_notify(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    runner = FakeRunner(
        {
            "-l app=claude-worker": (0, "claude-worker-1"),
            "exec": (0, _creds(now, plus_seconds=7200)),  # 2h left
            "logs": (0, ""),
        }
    )
    ctx, sent, state = _ctx(core, vig, runner, now)
    called = {"renew": 0}

    async def renew():
        called["renew"] += 1
        return await _ok_renew()

    await vig.vigia_oauth(ctx, renew)
    assert called["renew"] == 0
    assert sent == []


async def test_v1_expired_triggers_renew_ok(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    runner = FakeRunner(
        {
            "-l app=claude-worker": (0, "claude-worker-1"),
            "exec": (0, _creds(now, plus_seconds=600)),  # 10 min left (< 30 min)
            "logs": (0, ""),
        }
    )
    ctx, sent, state = _ctx(core, vig, runner, now)
    await vig.vigia_oauth(ctx, _ok_renew)
    assert sent == []  # successful renew is silent


async def test_v1_fatal_renew_notifies_p0_once(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    runner = FakeRunner(
        {
            "-l app=claude-worker": (0, "claude-worker-1"),
            "exec": (1, ""),  # no credential file
            "logs": (0, ""),
        }
    )
    ctx, sent, state = _ctx(core, vig, runner, now)
    await vig.vigia_oauth(ctx, _fatal_renew)
    assert any(sev == "P0" and fp.startswith("oauth_expired") for fp, sev, _ in sent)


async def test_v1b_reactive_log_detection_triggers_renew(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    runner = FakeRunner(
        {
            "-l app=claude-worker": (0, "claude-worker-1"),
            "exec": (0, _creds(now, plus_seconds=7200)),  # creds look fine
            "logs": (
                0,
                "WORKER_AUTH_EXPIRED\nWORKER_AUTH_EXPIRED",
            ),  # but pipeline is failing
        }
    )
    ctx, sent, state = _ctx(core, vig, runner, now)
    called = {"renew": 0}

    async def renew():
        called["renew"] += 1
        return await _ok_renew()

    await vig.vigia_oauth(ctx, renew)
    assert called["renew"] == 1  # reactive detection forces a renew despite healthy TTL


async def test_v1_never_passes_claude_auth_login(core, vig):
    """Regression guard: V1 must NOT shell out to the interactive login."""
    now = _utc(2026, 6, 2, 11, 0, 0)
    runner = FakeRunner(
        {
            "-l app=claude-worker": (0, "claude-worker-1"),
            "exec": (1, ""),
            "logs": (0, ""),
        }
    )
    ctx, sent, state = _ctx(core, vig, runner, now)
    await vig.vigia_oauth(ctx, _fatal_renew)
    assert not any("auth login" in c for c in runner.calls)
