"""Tests for the gh-based vigias (V3 orphans, V4 PR attempts, V5 stakeholder)
and V8 follow-up collection/pre-filter of ``infra/k8s/monitor_vigias.py``."""
from __future__ import annotations

import json
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
        return CmdResult(0, "[]", "")


def _ctx(core, vig, runner, now):
    state = core.default_state()
    flags = core.TickFlags()
    emitter = core.Emitter("/dev/null", flags, clock=lambda: now)
    sent = []
    notifier = SimpleNamespace(
        sent=sent,
        notify=lambda fp, sev, title, body, **kw: (sent.append((fp, sev, title)) or True),
    )
    ctx = vig.MonitorContext(
        run=runner, emitter=emitter, notifier=notifier, state=state,
        flags=flags, now=now, repo="elimarcavalli/deile", namespace="deile",
        kube_api=None,
    )
    return ctx, sent, state


# ----- V3 orphans ----------------------------------------------------------

def test_v3_stale_orphan_notifies_p1(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    issues = [{"number": 437, "title": "Logging expandido", "updated_at": "2026-05-31T00:00:00Z",
               "labels": [{"name": "~workflow:em_revisao"}]}]
    runner = FakeRunner({"em_revisao": (0, json.dumps(issues))})
    ctx, sent, state = _ctx(core, vig, runner, now)
    vig.vigia_orphan_issues(ctx)
    assert any(fp == "orphan_437" and sev == "P1" for fp, sev, _ in sent)


def test_v3_fresh_issue_not_flagged(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    issues = [{"number": 999, "title": "Recente", "updated_at": "2026-06-02T08:00:00Z",
               "labels": [{"name": "~workflow:em_revisao"}]}]
    runner = FakeRunner({"em_revisao": (0, json.dumps(issues))})
    ctx, sent, state = _ctx(core, vig, runner, now)
    vig.vigia_orphan_issues(ctx)
    assert sent == []


def test_v3_skips_pull_requests(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    issues = [{"number": 500, "title": "PR não é issue", "updated_at": "2026-05-30T00:00:00Z",
               "labels": [{"name": "~workflow:em_revisao"}], "pull_request": {"url": "x"}}]
    runner = FakeRunner({"em_revisao": (0, json.dumps(issues))})
    ctx, sent, state = _ctx(core, vig, runner, now)
    vig.vigia_orphan_issues(ctx)
    assert sent == []


# ----- V5 stakeholder ------------------------------------------------------

def test_v5_stakeholder_waiting_notifies_p2(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    issues = [{"number": 418, "title": "Rollout restart", "updated_at": "2026-06-02T05:00:00Z",
               "labels": [{"name": "~workflow:aguardando_stakeholder"}], "assignees": [{"login": "x"}]}]
    runner = FakeRunner({"aguardando_stakeholder": (0, json.dumps(issues))})
    ctx, sent, state = _ctx(core, vig, runner, now)
    vig.vigia_stakeholder(ctx)
    assert any(fp == "stakeholder_418" and sev == "P2" for fp, sev, _ in sent)


# ----- V4 PR attempts ------------------------------------------------------

def test_v4_attempt_2of3_notifies(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    pulls = [{"number": 501, "title": "auto pr", "head": {"ref": "auto/issue-440"},
              "updated_at": "2026-06-02T10:00:00Z"}]
    comments = [{"body": "tentativa attempt 2/3 falhou", "created_at": "2026-06-02T10:30:00Z"}]
    runner = FakeRunner({
        "pulls": (0, json.dumps(pulls)),
        "issues/440/comments": (0, json.dumps(comments)),
    })
    ctx, sent, state = _ctx(core, vig, runner, now)
    vig.vigia_pr_attempts(ctx)
    assert any(fp.startswith("pr_attempt_501") for fp, _, _ in sent)


def test_v4_ignores_non_auto_branches(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    pulls = [{"number": 502, "title": "human pr", "head": {"ref": "feature/x"},
              "updated_at": "2026-06-02T10:00:00Z"}]
    runner = FakeRunner({"pulls": (0, json.dumps(pulls))})
    ctx, sent, state = _ctx(core, vig, runner, now)
    vig.vigia_pr_attempts(ctx)
    assert sent == []


# ----- V8 follow-up collection + pre-filter --------------------------------

def test_v8_collects_human_promise_as_candidate(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    closed = [{"number": 445, "title": "X", "body": "", "closed_at": "2026-06-02T09:00:00Z"}]
    comments = [{"id": 700, "body": "vou abrir uma issue para tratar o resto",
                 "user": {"login": "elimarcavalli"}, "created_at": "2026-06-02T09:30:00Z"}]
    runner = FakeRunner({
        "state=closed sort=updated per_page=30": (0, "[]"),  # default for pulls
        "issues -f state=closed": (0, json.dumps(closed)),
        "issues/445/comments": (0, json.dumps(comments)),
        "pulls -f state=closed": (0, "[]"),
    })
    ctx, sent, state = _ctx(core, vig, runner, now)
    cands = vig.vigia_collect_followups(ctx)
    assert len(cands) == 1
    assert cands[0]["origin"] == 445
    assert cands[0]["comment_id"] == 700


def test_v8_skips_bot_author(core, vig, capsys):
    now = _utc(2026, 6, 2, 11, 0, 0)
    closed = [{"number": 446, "title": "X", "body": "", "closed_at": "2026-06-02T09:00:00Z"}]
    comments = [{"id": 701, "body": "vou abrir uma issue", "user": {"login": "deile-one[bot]"},
                 "created_at": "2026-06-02T09:30:00Z"}]
    runner = FakeRunner({
        "issues -f state=closed": (0, json.dumps(closed)),
        "issues/446/comments": (0, json.dumps(comments)),
        "pulls -f state=closed": (0, "[]"),
    })
    ctx, sent, state = _ctx(core, vig, runner, now)
    cands = vig.vigia_collect_followups(ctx)
    assert cands == []


def test_v8_skips_already_fingerprinted(core, vig):
    now = _utc(2026, 6, 2, 11, 0, 0)
    closed = [{"number": 447, "title": "X", "body": "", "closed_at": "2026-06-02T09:00:00Z"}]
    comments = [{"id": 702, "body": "follow-up: tratar isso", "user": {"login": "human"},
                 "created_at": "2026-06-02T09:30:00Z"}]
    runner = FakeRunner({
        "issues -f state=closed": (0, json.dumps(closed)),
        "issues/447/comments": (0, json.dumps(comments)),
        "pulls -f state=closed": (0, "[]"),
    })
    ctx, sent, state = _ctx(core, vig, runner, now)
    state["fu_fingerprints"].append("fu_447_702")
    cands = vig.vigia_collect_followups(ctx)
    assert cands == []
