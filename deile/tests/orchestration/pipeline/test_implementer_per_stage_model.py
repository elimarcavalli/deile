"""End-to-end: per-stage model override flows through WorkerImplementer to
the dispatch payload (issue #305).

Companion to ``test_implementer.py`` (which covers the broader behaviour).
This module focuses narrowly on the *stage → preferred_model* mapping:
- implement → ``implement`` stage
- review → ``pr_review`` stage
- critique/refine/decompose → ``refine`` stage
- mention (issue comment) → ``follow_ups`` stage
- mention (PR work_merge/review_only/address) → ``pr_review`` stage

Each test uses a fake client that captures the dispatched payload, sets the
relevant ``DEILE_PIPELINE_MODEL_<STAGE>`` env var, and asserts the payload
carries the expected ``preferred_model``.
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
        monkeypatch.delenv(f"DEILE_PIPELINE_MODEL_{stage.upper()}",
                           raising=False)
    monkeypatch.delenv("DEILE_PREFERRED_MODEL", raising=False)
    reset_settings()
    yield
    reset_settings()


class _FakeClient:
    """Captures the dispatch payload (mirrors the one in test_implementer.py
    but local so this module is self-contained)."""

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
    # Forge layer (PR #297) — implementer lê ``monitor.forge.config`` para
    # passar ao renderer de briefs. Stub mínimo com kind=GitHub default.
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


def _issue(number=242, labels=()):
    return SimpleNamespace(number=number, title="t", body="b", labels=labels)


def _pr(number=7):
    return SimpleNamespace(
        number=number, title="t", head_ref=f"auto/issue-{number}",
        url=f"https://github.com/owner/name/pull/{number}",
    )


def _mention_trigger_issue_comment(number=1):
    """Issue comment mention → follow_ups stage."""
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
    """PR assignee mention → pr_review stage (work_merge mode)."""
    pr = PrRef(
        number=number, title="t",
        url=f"https://github.com/owner/name/pull/{number}",
        head_ref=f"auto/issue-{number}", labels=(),
    )
    return MentionTrigger(trigger_type="assignee", pr=pr)


class TestStageModelPropagation:
    async def test_implement_sends_implement_stage_model(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_IMPLEMENT",
                           "anthropic:claude-opus-4-7")
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert client.last_payload["preferred_model"] == "anthropic:claude-opus-4-7"

    async def test_review_sends_pr_review_stage_model(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_PR_REVIEW",
                           "anthropic:claude-sonnet-4-6")
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "merged"})
        await WorkerImplementer(client=client).review(_make_monitor(), _pr())
        assert client.last_payload["preferred_model"] == \
            "anthropic:claude-sonnet-4-6"

    async def test_critique_refine_decompose_use_refine_stage(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_REFINE",
                           "deepseek:deepseek-v4-pro")
        reset_settings()
        issue = _issue(labels=("intent",))
        client = _FakeClient({"ok": True, "summary": "VEREDITO: CLARO"})
        impl = WorkerImplementer(client=client)
        await impl.critique(_make_monitor(), issue)
        assert client.last_payload["preferred_model"] == "deepseek:deepseek-v4-pro"
        await impl.refine(_make_monitor(), issue)
        assert client.last_payload["preferred_model"] == "deepseek:deepseek-v4-pro"
        await impl.decompose(_make_monitor(), issue)
        assert client.last_payload["preferred_model"] == "deepseek:deepseek-v4-pro"

    async def test_mention_comment_on_issue_uses_follow_ups_stage(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_FOLLOW_UPS",
                           "deepseek:deepseek-v3-small")
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "answered"})
        trigger = _mention_trigger_issue_comment()
        await WorkerImplementer(client=client).mention(
            _make_monitor(), trigger,
            trigger_types=["comment"], all_triggers=[trigger], mode="comment",
        )
        assert client.last_payload["preferred_model"] == "deepseek:deepseek-v3-small"

    async def test_mention_pr_unified_uses_pr_review_stage(self, monkeypatch):
        """Após o refactor "PR é o quadro", todo trigger sobre PR resolve para
        ``pr_unified`` (substitui ``work_merge``/``review_only``/``address``)
        e mapeia para o stage ``pr_review``."""
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_PR_REVIEW",
                           "anthropic:claude-opus-4-7")
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "merged"})
        trigger = _mention_trigger_pr_assignee(number=7)
        await WorkerImplementer(client=client).mention(
            _make_monitor(), trigger,
            trigger_types=["assignee"], all_triggers=[trigger], mode="pr_unified",
        )
        assert client.last_payload["preferred_model"] == "anthropic:claude-opus-4-7"

    async def test_unset_stage_omits_preferred_model_from_payload(self):
        """No env var → resolver returns None → builder omits the key.
        Keeps backward compat: worker falls back to its own
        ``DEILE_PREFERRED_MODEL`` / ``settings.preferred_model``."""
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert "preferred_model" not in client.last_payload

    async def test_payload_carries_resume_block_AND_preferred_model_together(
        self, monkeypatch,
    ):
        """The two additive wire fields (resume + preferred_model) coexist on
        the same payload — issue #305 must not break issue #254."""
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_IMPLEMENT",
                           "anthropic:claude-opus-4-7")
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(
            _make_monitor(), _issue(), resume=True,
        )
        assert client.last_payload["preferred_model"] == "anthropic:claude-opus-4-7"
        assert "resume" in client.last_payload
        assert client.last_payload["resume"]["mode"] == "resume"
