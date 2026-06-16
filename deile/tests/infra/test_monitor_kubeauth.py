"""Regression for the #504 in-cluster kubectl auth bug.

The deterministic-tick refactor dropped the explicit ServiceAccount credentials
from its kubectl calls (`kubectl --server <url>` only), assuming kubectl would
reuse in-cluster config. It does NOT — proven in-pod: plain `kubectl` hits
localhost:8080 and `--server` without `--token` prompts for a username. Result:
every probe failed → K8S_API_UNREACHABLE → V1/V2/V6/V7 silently skipped → blind.

Fix: a 0600 kubeconfig with `tokenFile` (reads the SA token live, never copies
the secret) + the SA CA + the DNS server; kubectl calls authenticate via
`--kubeconfig` with no token in argv.
"""

from __future__ import annotations

import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

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


@pytest.fixture
def tick():
    import monitor_tick

    return monitor_tick


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _fake_sa(tmp_path):
    sa = tmp_path / "sa"
    sa.mkdir()
    (sa / "token").write_text("fake-sa-token")
    (sa / "ca.crt").write_text("fake-ca")
    return sa


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


# ---------------------------------------------------------------------------
# write_incluster_kubeconfig
# ---------------------------------------------------------------------------


def test_write_kubeconfig_uses_tokenfile_not_inline_token(core, tmp_path):
    sa = _fake_sa(tmp_path)
    kc = tmp_path / "kubeconfig"
    core.write_incluster_kubeconfig(
        str(kc), "https://kubernetes.default.svc:443", sa_dir=str(sa)
    )
    content = kc.read_text()
    # tokenFile reference, NOT the token value baked in (no secret duplication).
    assert f"tokenFile: {sa}/token" in content
    assert "fake-sa-token" not in content
    assert f"certificate-authority: {sa}/ca.crt" in content
    assert "server: https://kubernetes.default.svc:443" in content


def test_write_kubeconfig_is_0600(core, tmp_path):
    sa = _fake_sa(tmp_path)
    kc = tmp_path / "kubeconfig"
    core.write_incluster_kubeconfig(str(kc), "https://x:443", sa_dir=str(sa))
    mode = stat.S_IMODE(os.stat(kc).st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------------------
# resolve_incluster_kube (DNS-first, via kubeconfig)
# ---------------------------------------------------------------------------


def test_resolve_incluster_returns_first_working_server(core, tmp_path):
    sa = _fake_sa(tmp_path)
    kc = tmp_path / "kubeconfig"
    runner = FakeRunner({"version": (0, "ok")})  # probe succeeds
    server = core.resolve_incluster_kube(runner, str(kc), sa_dir=str(sa))
    assert server == "https://kubernetes.default.svc:443"
    assert kc.exists()  # kubeconfig left in place for the vigias
    # The probe must go through the kubeconfig, never with a bare --token in argv.
    assert any("--kubeconfig" in c and "version" in c for c in runner.calls)
    assert not any("--token" in c for c in runner.calls)


def test_resolve_incluster_none_when_all_probes_fail(core, tmp_path):
    sa = _fake_sa(tmp_path)
    kc = tmp_path / "kubeconfig"
    assert (
        core.resolve_incluster_kube(
            FakeRunner({"version": (1, "")}), str(kc), sa_dir=str(sa)
        )
        is None
    )


# ---------------------------------------------------------------------------
# MonitorContext.kubectl uses --kubeconfig (no --token, no --server)
# ---------------------------------------------------------------------------


def test_kubectl_uses_kubeconfig_when_set(core, vig):
    runner = FakeRunner()
    ctx = vig.MonitorContext(
        run=runner,
        emitter=core.Emitter("/dev/null", core.TickFlags()),
        notifier=None,
        state=core.default_state(),
        flags=core.TickFlags(),
        now=_utc(2026, 6, 2, 11, 0, 0),
        repo="r",
        namespace="deile",
        kube_api="https://kubernetes.default.svc:443",
        kubeconfig="/tmp/kc",
    )
    ctx.kubectl("get", "pods")
    call = runner.calls[-1]
    assert "--kubeconfig /tmp/kc" in call
    assert "--token" not in call


# ---------------------------------------------------------------------------
# run_tick: production path builds kubeconfig → kube vigias NOT skipped
# ---------------------------------------------------------------------------


async def _renew_ok():
    from types import SimpleNamespace

    return SimpleNamespace(
        ok=True, error=None, message="ok", seconds_until_new_expiry=3600
    )


async def test_run_tick_authenticates_via_kubeconfig_when_sa_present(
    tick, tmp_path, capsys
):
    sa = _fake_sa(tmp_path)
    sd = tmp_path / "state"
    (sd / "monitor-commands").mkdir(parents=True)
    runner = FakeRunner({"version": (0, "ok")})  # probe succeeds → kube reachable
    await tick.run_tick(
        str(sd),
        now=_utc(2026, 6, 2, 11, 0, 0),
        run=runner,
        renew=_renew_ok,
        repo="elimarcavalli/deile",
        namespace="deile",
        bot_endpoint="http://deilebot:8765",
        bot_token="t",
        user_id="",
        kube_probe=None,  # production path
        sa_dir=str(sa),
        kubeconfig_path=str(tmp_path / "kc"),
    )
    out = capsys.readouterr().out
    # Kube was reachable → NOT skipped as unreachable.
    assert "K8S_API_UNREACHABLE" not in out
    # The kubectl calls authenticated via the kubeconfig.
    assert any("--kubeconfig" in c for c in runner.calls)
    assert not any("--token" in c for c in runner.calls)
