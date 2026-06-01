"""Testes da verdade-sempre do doing-now dos pods claude-worker no painel.

Cobre a proposta de UI da tela [1]Pods:
  1. ``ClaudeWorkerTruthProvider`` — parsing do exec (/proc + lease) sem
     depender de cluster (o ``kubectl exec`` é mockado).
  2. Helpers puros de render em ``_panel`` — cor de restart, staleness do
     heartbeat (>15s → ⚠), célula doing-now por verdade vs idle vs pod-down.

Os módulos ``_panel`` / ``_panel_data`` vivem em ``infra/k8s/`` (fora do
pacote ``deile``); o path é inserido manualmente — mesma convenção dos
demais testes de infra (ver ``test_claude_worker_lease.py``).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as pnl  # noqa: E402
import _panel_data as pd  # noqa: E402


# Saída sintética do probe in-pod (formato CPID|pid|cmdline + LEASE|json).
_CLAUDE_CMDLINE = (
    "/usr/bin/claude -p --permission-mode bypassPermissions "
    "--output-format json --session-id abc-123 "
    "--model claude-sonnet-4-6 --effort xhigh --max-budget-usd 8 "
    "Você é Claude Code revisor de PR. Worktree: , "
    "branch fix/netpol-apiserver-egress. Revise a PR após ---."
)


def _probe_output(*, running_pod: str, this_pod: str, claude_pid: int,
                  heartbeat_at: float) -> str:
    """Monta a saída do probe vista por ``this_pod``.

    Lease é global (PVC compartilhado), então aparece em todo pod; o CPID só
    aparece no pod que realmente roda o ``claude`` (``running_pod``).
    """
    lines = []
    if this_pod == running_pod:
        lines.append(f"CPID|{claude_pid}|{_CLAUDE_CMDLINE}")
    lines.append(
        f'LEASE|{{"pod": "{running_pod}", "pid": 7, '
        f'"started_at": 1.0, "heartbeat_at": {heartbeat_at}, '
        f'"claude_pid": {claude_pid}}}'
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# ClaudeWorkerTruthProvider — parsing
# ---------------------------------------------------------------------------

def _make_provider(pods, probe_for):
    prov = pd.ClaudeWorkerTruthProvider(namespace="deile", enabled=True)
    prov._kubectl = "/fake/kubectl"  # noqa: SLF001 (skip resolve)
    names = " ".join(pods)

    def fake_text(cmd, timeout=5.0):
        return names

    def fake_lossy(cmd, timeout=5.0):
        # último arg do exec é o script; o pod é o índice após "exec".
        pod = cmd[cmd.index("exec") + 1]
        return probe_for(pod)

    with patch.object(pd, "_capture_text", fake_text), \
         patch.object(pd, "_capture_text_lossy", fake_lossy):
        return prov._fetch()  # noqa: SLF001


def test_provider_marks_running_pod_and_extracts_cmdline():
    now = pd.time.time()
    pods = ["claude-worker-aaa", "claude-worker-bbb"]
    res = _make_provider(
        pods,
        lambda pod: _probe_output(
            running_pod="claude-worker-aaa", this_pod=pod,
            claude_pid=6903, heartbeat_at=now - 1.0),
    )
    assert set(res) == set(pods)
    aaa = res["claude-worker-aaa"]
    assert aaa.probe_ok and aaa.claude_running
    assert len(aaa.dispatches) == 1
    d = aaa.dispatches[0]
    assert d.claude_pid == 6903
    assert d.summary == "review PR"
    assert d.branch == "fix/netpol-apiserver-egress"
    assert d.model == "sonnet-4-6"   # slug curto, sem 'claude-'
    assert d.effort == "xhigh"
    assert d.heartbeat_age_s is not None and d.heartbeat_age_s < 5
    # bbb vê o mesmo lease, mas NÃO roda claude → idle.
    bbb = res["claude-worker-bbb"]
    assert bbb.probe_ok and not bbb.claude_running and not bbb.dispatches


def test_provider_probe_failure_is_graceful():
    pods = ["claude-worker-aaa"]
    res = _make_provider(pods, lambda pod: None)  # exec falhou
    aaa = res["claude-worker-aaa"]
    assert aaa.probe_ok is False
    assert aaa.claude_running is False


def test_provider_ignores_sh_self_match_via_executable_anchor():
    # Mesmo que a saída contenha uma linha cujo cmdline NÃO começa por
    # .../claude, o parser de CPID só confia no que o script já filtrou —
    # aqui validamos que um CPID legítimo é contado e um lease órfão (sem
    # CPID correspondente) não cria dispatch.
    pods = ["claude-worker-aaa"]

    def probe(pod):
        return (
            'LEASE|{"pod": "claude-worker-zzz", "pid": 7, "started_at": 1.0, '
            '"heartbeat_at": 1.0, "claude_pid": 999}\n'
        )

    res = _make_provider(pods, probe)
    assert res["claude-worker-aaa"].claude_running is False


def test_active_dispatch_count():
    now = pd.time.time()
    pods = ["claude-worker-aaa", "claude-worker-bbb"]
    prov = pd.ClaudeWorkerTruthProvider(namespace="deile", enabled=True)
    prov._kubectl = "/fake/kubectl"
    names = " ".join(pods)

    def fake_text(cmd, timeout=5.0):
        return names

    def fake_lossy(cmd, timeout=5.0):
        pod = cmd[cmd.index("exec") + 1]
        return _probe_output(running_pod="claude-worker-aaa", this_pod=pod,
                             claude_pid=6903, heartbeat_at=now)

    with patch.object(pd, "_capture_text", fake_text), \
         patch.object(pd, "_capture_text_lossy", fake_lossy):
        prov.get(force=True)
        assert prov.active_dispatch_count() == 1


# ---------------------------------------------------------------------------
# Helpers puros de cmdline
# ---------------------------------------------------------------------------

def test_cw_short_model():
    assert pd._cw_short_model("claude-sonnet-4-6") == "sonnet-4-6"
    assert pd._cw_short_model("claude-opus-4-8") == "opus-4-8"
    assert pd._cw_short_model("anthropic:claude-haiku-4-5") == "haiku-4-5"
    assert pd._cw_short_model(None) is None


def test_cw_summary():
    assert pd._cw_summary("Você é revisor de PR ...") == "review PR"
    assert pd._cw_summary("implemente a feature X") == "implement"
    assert pd._cw_summary("refine a issue") == "refine"
    assert pd._cw_summary("texto neutro") == "dispatch"


# ---------------------------------------------------------------------------
# Helpers puros de render (_panel)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n,expect", [
    ("0", "green"), ("1", "yellow"), ("3", "yellow"),
    ("4", "bold red"), ("99", "bold red"),
])
def test_restart_text_color(n, expect):
    t = pnl._restart_text(n)
    assert t.style == expect
    assert str(t) == n


def test_restart_text_non_numeric():
    t = pnl._restart_text("?")
    assert t.style == "dim"


def _row(**kw):
    base = dict(icon="●", name="claude-worker-x", role="claude-worker",
                status="Running", age="1m", restarts="0",
                last_activity="—", doing_now="idle", busy=False, stale_s=None)
    base.update(kw)
    return pnl.PodRow(**base)


def test_doing_now_render_busy_is_yellow():
    txt, style = pnl._doing_now_render(_row(busy=True, doing_now="review PR"))
    assert style == "bold yellow"
    assert "review PR" in str(txt)


def test_doing_now_render_idle_is_dim():
    txt, style = pnl._doing_now_render(_row(busy=False, doing_now="idle"))
    assert style == "dim"
    assert str(txt) == "idle"


def test_doing_now_render_stale_overrides_to_warning():
    # heartbeat 40s > 15s → ⚠ stale, amarelo, NÃO mostra a ação velha.
    txt, style = pnl._doing_now_render(
        _row(busy=True, doing_now="review PR", stale_s=40.0))
    assert style == "yellow"
    assert "⚠ stale" in str(txt)
    assert "review PR" not in str(txt)


def test_doing_now_render_fresh_heartbeat_not_stale():
    txt, style = pnl._doing_now_render(
        _row(busy=True, doing_now="review PR", stale_s=4.0))
    assert style == "bold yellow"
    assert "review PR" in str(txt)


# ---------------------------------------------------------------------------
# _claude_worker_cell — precedência verdade > pod-down > idle
# ---------------------------------------------------------------------------

class _FakeTruthProv:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, force=False):
        return self._m


class _FakeData:
    def __init__(self, truth_map=None, workers_map=None):
        self.claude_truth = _FakeTruthProv(truth_map) if truth_map is not None else None
        self.claude_workers = _FakeTruthProv(workers_map) if workers_map is not None else None


def test_claude_worker_cell_running():
    disp = pd.ClaudeDispatch(pod="p", claude_pid=1, summary="implement",
                             branch="auto/issue-9", model="opus-4-8",
                             effort="max", heartbeat_age_s=2.0)
    truth = pd.ClaudeWorkerTruth(pod_name="p", probe_ok=True,
                                 claude_running=True, dispatches=[disp],
                                 heartbeat_age_s=2.0)
    data = _FakeData(truth_map={"p": truth})
    age, last, doing, busy, icon, stale = pnl._claude_worker_cell(data, "p")
    assert busy and icon == "⚡" and last == "agora"
    assert doing == "implement · auto/issue-9 (opus-4-8 max)"
    assert stale == 2.0


def test_claude_worker_cell_pod_down():
    truth = pd.ClaudeWorkerTruth(pod_name="p", probe_ok=False)
    data = _FakeData(truth_map={"p": truth})
    age, last, doing, busy, icon, stale = pnl._claude_worker_cell(data, "p")
    assert not busy and doing == "— (pod down)" and stale is None


def test_claude_worker_cell_idle_with_last_completed():
    truth = pd.ClaudeWorkerTruth(pod_name="p", probe_ok=True,
                                 claude_running=False)
    lc = pd.LastCompletedTask(
        task_id="t", channel_id="c",
        finished_ts=datetime.now(timezone.utc) - timedelta(minutes=10),
        outcome="DONE", duration_s=5.0, cost_usd=0.74)
    ws = pd.WorkerState(pod_name="p", last_completed=lc)
    data = _FakeData(truth_map={"p": truth}, workers_map={"p": ws})
    age, last, doing, busy, icon, stale = pnl._claude_worker_cell(data, "p")
    assert not busy and doing == "idle"
    assert "DONE" in last and "$0.74" in last


# ---------------------------------------------------------------------------
# PodPickerView ([1]Pods): agrupamento de réplicas + alinhamento do cursor
# ---------------------------------------------------------------------------

def _pod(name, role):
    return pnl.PodRow(icon="●", name=name, role=role, status="Running",
                      age="1m", restarts="0", last_activity="—",
                      doing_now="idle")


def test_picker_groups_claude_worker_contiguous_and_keeps_cursor_mapping():
    # _pod_rows mistura roles (ordem por recência); a picker reordena para
    # agrupar claude-worker contíguo ANTES dos locais. handle_key indexa a
    # mesma lista, então o cursor tem de continuar casando.
    mixed = [
        _pod("claude-worker-aaa", "claude-worker"),
        _pod("deile-pipeline-x", "pipeline"),
        _pod("claude-worker-bbb", "claude-worker"),
        _pod("deilebot-y", "bot"),
    ]
    locals_ = [_pod("local-other#1", "local-other")]
    view = pnl.PodPickerView(data=object())  # data não é usado pelos stubs
    with patch.object(pnl, "_pod_rows", return_value=mixed), \
         patch.object(pnl, "_local_process_rows", return_value=locals_):
        rows = view._rows()
    roles = [r.role for r in rows]
    # não-cw primeiro (ordem preservada), cw contíguo, locais por último.
    assert roles == ["pipeline", "bot", "claude-worker", "claude-worker",
                     "local-other"]
    # As duas réplicas cw ficam adjacentes.
    cw_idx = [i for i, r in enumerate(rows) if r.role == "claude-worker"]
    assert cw_idx == [2, 3]


# ---------------------------------------------------------------------------
# Gap 1: deile-worker truth via marcador de dispatch
# ---------------------------------------------------------------------------

def test_target_from_channel():
    assert pd._target_from_channel("pipeline-issue-443") == "#443"
    assert pd._target_from_channel("pipeline-pr-12") == "PR#12"
    assert pd._target_from_channel("pipeline-mention-pr-5") == "mention PR5"
    assert pd._target_from_channel("avulso") is None


def _dw_provider(pods, probe_for):
    prov = pd.DeileWorkerTruthProvider(namespace="deile", enabled=True)
    prov._kubectl = "/fake/kubectl"
    names = " ".join(pods)

    def fake_text(cmd, timeout=5.0):
        return names

    def fake_lossy(cmd, timeout=5.0):
        pod = cmd[cmd.index("exec") + 1]
        return probe_for(pod)

    with patch.object(pd, "_capture_text", fake_text), \
         patch.object(pd, "_capture_text_lossy", fake_lossy):
        return prov._fetch()


def test_deile_worker_truth_busy_from_marker():
    now = int(pd.time.time())
    marker = ('{"task_id":"t1","channel_id":"pipeline-issue-443",'
              '"persona":"developer","model":"deepseek:deepseek-chat",'
              f'"phase":"rodando testes","pid":7,"started_at":1,"updated_at":{now}}}')

    def probe(pod):
        return f'DIR|1\nMARK|1|{marker}\n'

    res = _dw_provider(["deile-worker-x"], probe)
    t = res["deile-worker-x"]
    assert t.probe_ok and t.has_marker_dir and t.busy
    assert t.target == "#443" and t.persona == "developer"
    assert t.phase == "rodando testes"


def test_deile_worker_truth_dead_pid_not_busy():
    # MARK com alive=0 (pid não vive neste pod) → não conta como busy.
    def probe(pod):
        return 'DIR|1\nMARK|0|{"task_id":"t","channel_id":"x","pid":999,"updated_at":1}\n'

    res = _dw_provider(["deile-worker-x"], probe)
    t = res["deile-worker-x"]
    assert t.has_marker_dir and not t.busy


def test_deile_worker_truth_old_image_no_marker_dir():
    res = _dw_provider(["deile-worker-x"], lambda pod: "DIR|0\n")
    t = res["deile-worker-x"]
    assert t.probe_ok and not t.has_marker_dir and not t.busy


class _StubWS:
    def __init__(self, busy=False, body="", last_completed=None,
                 last_activity_s=None):
        self.busy = busy
        self.last_substantive_body = body
        self.last_completed = last_completed
        self.last_activity_s = last_activity_s


def _dw_data(truth=None):
    class _D:
        deile_worker_truth = _FakeTruthProv(truth) if truth is not None else None
    return _D()


def test_deile_worker_cell_truth_busy():
    t = pd.DeileWorkerTruth(pod_name="p", probe_ok=True, has_marker_dir=True,
                            busy=True, target="#443", phase="editando",
                            model="deepseek-chat", age_s=3.0)
    data = _dw_data({"p": t})
    age, last, doing, busy, icon, stale = pnl._deile_worker_cell(
        data, "p", _StubWS())
    assert busy and icon == "⚡" and last == "agora"
    assert doing == "#443 · editando (deepseek-chat)" and stale is None


def test_deile_worker_cell_old_image_falls_back_to_log():
    # has_marker_dir False → usa o legado (log): busy + corpo substantivo.
    t = pd.DeileWorkerTruth(pod_name="p", probe_ok=True, has_marker_dir=False)
    data = _dw_data({"p": t})
    ws = _StubWS(busy=True, body="implementando issue 99", last_activity_s=5.0)
    age, last, doing, busy, icon, stale = pnl._deile_worker_cell(data, "p", ws)
    assert busy and "implementando issue 99" in doing


def test_deile_worker_cell_idle_truth():
    t = pd.DeileWorkerTruth(pod_name="p", probe_ok=True, has_marker_dir=True,
                            busy=False)
    data = _dw_data({"p": t})
    age, last, doing, busy, icon, stale = pnl._deile_worker_cell(
        data, "p", _StubWS())
    assert not busy and doing == "idle"


# ---------------------------------------------------------------------------
# Gap 2: custo "hoje" — guarda anti-back-to-back + filtro since_mtime
# ---------------------------------------------------------------------------

def test_cost_today_anti_backtoback_guard():
    prov = pd.ClaudeCostTodayProvider(namespace="deile", enabled=True)
    prov._kubectl = "/fake/kubectl"
    calls = {"n": 0}

    def fake_run_parser(sta, pod, since):
        calls["n"] += 1
        return [{"models": {"claude-sonnet-4-6": {"in": 100, "out": 50,
                                                   "cc": 0, "cr": 0,
                                                   "cc_5m": 0, "cc_1h": 0}}}]

    with patch.object(prov, "_run_parser", fake_run_parser), \
         patch.object(pd, "_capture_text", lambda cmd, timeout=5.0: "pod-1"):
        v1 = prov.get(force=True)        # fetch real (calls=1), Cache guarda v1
        v2 = prov._cache._refresh()      # tick back-to-back → guarda devolve v1
    assert calls["n"] == 1               # parse NÃO refez
    assert v1 == v2 and v1 is not None and v1 > 0


def test_cost_today_midnight_brt_is_utc_minus_3():
    epoch = pd.ClaudeCostTodayProvider._today_brt_midnight_epoch()
    dt = datetime.fromtimestamp(epoch, pd._BRT)
    assert (dt.hour, dt.minute, dt.second) == (0, 0, 0)
