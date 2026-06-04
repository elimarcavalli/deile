"""End-to-end: per-stage reasoning_effort flows through WorkerImplementer to
the dispatch payload (issue #441).

Companion to ``test_implementer_per_stage_model.py`` (which covers
``preferred_model``). This module focuses narrowly on the
*stage → preferred_reasoning* mapping, verifying that:

- Each pipeline stage forwards the resolved reasoning effort in the payload.
- ``DEILE_PIPELINE_REASONING_<STAGE>`` env var wins over the opinionated default.
- ``DEILE_REASONING_EFFORT`` global override propagates when no per-stage var is set.
- When no override is configured, the opinionated default for the stage is used
  (``implement=medium``, ``pr_review=high``, ``classify/refine/follow_ups=low``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from deile.config.settings import reset_settings
from deile.orchestration.pipeline.github_client import (CommentRef,
                                                        MentionTrigger, PrRef)
from deile.orchestration.pipeline.implementer import WorkerImplementer
from deile.orchestration.pipeline.model_resolver import PIPELINE_STAGES


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_REASONING_{stage.upper()}",
                           raising=False)
    monkeypatch.delenv("DEILE_REASONING_EFFORT", raising=False)
    reset_settings()
    yield
    reset_settings()


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.last_payload = None

    async def dispatch(self, payload, *, wait):
        self.last_payload = payload
        return self._response


def _make_monitor():
    monitor = SimpleNamespace()
    monitor.config = SimpleNamespace(
        repo="owner/name", main_branch="main", base_repo_path=Path("/tmp/x"),
        mention_handle="@deile-one",
    )
    monitor.branch_for_issue = lambda n: f"auto/issue-{n}"
    from deile.orchestration.forge.base import ForgeConfig, ForgeKind
    monitor.forge = SimpleNamespace(
        config=ForgeConfig(
            kind=ForgeKind.GITHUB,
            host="github.com",
            project_path="owner/name",
            cli_path="/usr/bin/gh",
        ),
    )
    return monitor


def _issue(number=1, labels=()):
    return SimpleNamespace(number=number, title="t", body="b", labels=labels)


def _pr(number=7):
    return SimpleNamespace(
        number=number, title="t", head_ref=f"auto/issue-{number}",
        url=f"https://github.com/owner/name/pull/{number}",
    )


def _mention_trigger_issue_comment(number=1):
    comment = CommentRef(
        comment_id=1,
        body="@deile-one fix",
        html_url=f"https://github.com/owner/name/issues/{number}#issuecomment-1",
        issue_url=f"https://api.github.com/repos/owner/name/issues/{number}",
        author="someone",
        kind="issue",
    )
    return MentionTrigger(trigger_type="comment", comment=comment)


def _mention_trigger_pr_assignee(number=7):
    pr = PrRef(
        number=number, title="t",
        url=f"https://github.com/owner/name/pull/{number}",
        head_ref=f"auto/issue-{number}", labels=(),
    )
    return MentionTrigger(trigger_type="assignee", pr=pr)


class TestStageReasoningPropagation:
    async def test_implement_per_stage_env_wins(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_REASONING_IMPLEMENT", "xhigh")
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert client.last_payload["preferred_reasoning"] == "xhigh"

    async def test_implement_uses_opinionated_default_when_no_env(self):
        """No override → resolver returns the opinionated default (medium)."""
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert client.last_payload["preferred_reasoning"] == "medium"

    async def test_review_per_stage_env_wins(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_REASONING_PR_REVIEW", "max")
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "merged"})
        await WorkerImplementer(client=client).review(_make_monitor(), _pr())
        assert client.last_payload["preferred_reasoning"] == "max"

    async def test_review_uses_opinionated_default_when_no_env(self):
        """No override → pr_review opinionated default is high."""
        client = _FakeClient({"ok": True, "summary": "merged"})
        await WorkerImplementer(client=client).review(_make_monitor(), _pr())
        assert client.last_payload["preferred_reasoning"] == "high"

    async def test_global_reasoning_effort_propagates_to_all_stages(
        self, monkeypatch,
    ):
        """DEILE_REASONING_EFFORT overrides opinionated defaults for every stage."""
        monkeypatch.setenv("DEILE_REASONING_EFFORT", "ultracode")
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert client.last_payload["preferred_reasoning"] == "ultracode"

        client2 = _FakeClient({"ok": True, "summary": "merged"})
        await WorkerImplementer(client=client2).review(_make_monitor(), _pr())
        assert client2.last_payload["preferred_reasoning"] == "ultracode"

    async def test_per_stage_env_takes_precedence_over_global(self, monkeypatch):
        monkeypatch.setenv("DEILE_REASONING_EFFORT", "low")
        monkeypatch.setenv("DEILE_PIPELINE_REASONING_IMPLEMENT", "high")
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert client.last_payload["preferred_reasoning"] == "high"

    async def test_critique_refine_decompose_use_refine_stage_default(self):
        """classify/refine/follow_ups opinionated default is low."""
        issue = _issue(labels=("intent",))
        client = _FakeClient({"ok": True, "summary": "VEREDITO: CLARO"})
        impl = WorkerImplementer(client=client)
        await impl.critique(_make_monitor(), issue)
        assert client.last_payload["preferred_reasoning"] == "low"

    async def test_mention_comment_on_issue_uses_follow_ups_default(self):
        """Issue comment mention → follow_ups stage → opinionated default low."""
        client = _FakeClient({"ok": True, "summary": "answered"})
        trigger = _mention_trigger_issue_comment()
        await WorkerImplementer(client=client).mention(
            _make_monitor(), trigger,
            trigger_types=["comment"], all_triggers=[trigger], mode="comment",
        )
        assert client.last_payload["preferred_reasoning"] == "low"

    async def test_preferred_reasoning_coexists_with_preferred_model(
        self, monkeypatch,
    ):
        """Both wire fields coexist on the same payload."""
        monkeypatch.setenv("DEILE_PIPELINE_REASONING_IMPLEMENT", "high")
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_IMPLEMENT", "anthropic:claude-opus-4-8")
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert client.last_payload["preferred_reasoning"] == "high"
        assert client.last_payload["preferred_model"] == "anthropic:claude-opus-4-8"
