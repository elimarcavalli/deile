"""AC11 — label.change log lines emitted by forge label mutations via pipeline_logger."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl
from deile.orchestration.forge.base import ForgeConfig, ForgeKind
from deile.orchestration.forge.github_forge import GitHubForge


@pytest.fixture(autouse=True)
def _reset_dedup(monkeypatch):
    monkeypatch.setattr(pl, "_DEDUP", pl._DedupCache())


async def _fake_run_checked(*args):
    return ""


async def _fake_run(*args):
    return (0, "", "")


def _forge_wired() -> GitHubForge:
    cfg = ForgeConfig(
        project_path="owner/repo",
        kind=ForgeKind.GITHUB,
        host="github.com",
        cli_path="/usr/bin/gh",
    )
    gh = GitHubForge(cfg)
    gh.on_label_change = lambda kind, num, rem, add: pl.log_label_change(
        target_kind=kind,
        target=num,
        removed=rem,
        added=add,
    )
    return gh


class TestLabelChangeAC11:
    """AC11 — forge label mutations emit label.change lines to the events logger."""

    @pytest.mark.asyncio
    @patch.object(GitHubForge, "_run_checked", _fake_run_checked)
    @patch.object(GitHubForge, "_run", _fake_run)
    async def test_transition_issue_emits_exactly_one_label_change(self, caplog):
        """AC11-A: transition_issue emits exactly one label.change line."""
        gh = _forge_wired()
        with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
            await gh.transition_issue(
                123,
                from_label="~workflow:em_implementacao",
                to_label="~workflow:em_pr",
            )
        label_change_lines = [
            r for r in caplog.records if r.message.startswith("label.change")
        ]
        assert (
            len(label_change_lines) == 1
        ), f"Expected exactly 1 label.change line, got: {[r.message for r in label_change_lines]}"
        assert (
            "removed=[~workflow:em_implementacao]" in label_change_lines[0].message
        ), f"Expected removed label in message: {label_change_lines[0].message!r}"
        assert (
            "added=[~workflow:em_pr]" in label_change_lines[0].message
        ), f"Expected added label in message: {label_change_lines[0].message!r}"

    @pytest.mark.asyncio
    @patch.object(GitHubForge, "_run_checked", _fake_run_checked)
    @patch.object(GitHubForge, "_run", _fake_run)
    async def test_separate_add_remove_emits_two_label_changes(self, caplog):
        """AC11-B: separate add_labels + remove_labels emit two label.change lines."""
        gh = _forge_wired()
        with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
            await gh.add_labels("issue", 123, ["a", "b"])
            await gh.remove_labels("issue", 123, ["c"])
        label_change_lines = [
            r for r in caplog.records if r.message.startswith("label.change")
        ]
        assert (
            len(label_change_lines) == 2
        ), f"Expected exactly 2 label.change lines, got: {[r.message for r in label_change_lines]}"
        assert (
            "removed=[]" in label_change_lines[0].message
        ), f"Expected removed=[] in add line: {label_change_lines[0].message!r}"
        assert (
            "added=[a,b]" in label_change_lines[0].message
        ), f"Expected added=[a,b] in add line: {label_change_lines[0].message!r}"
        assert (
            "removed=[c]" in label_change_lines[1].message
        ), f"Expected removed=[c] in remove line: {label_change_lines[1].message!r}"
        assert (
            "added=[]" in label_change_lines[1].message
        ), f"Expected added=[] in remove line: {label_change_lines[1].message!r}"

    @pytest.mark.asyncio
    @patch.object(GitHubForge, "_run_checked", _fake_run_checked)
    @patch.object(GitHubForge, "_run", _fake_run)
    async def test_transition_issue_without_from_label_emits_one_label_change(
        self, caplog
    ):
        """AC11-C: transition_issue with from_label=None emits one label.change line with removed=[]."""
        gh = _forge_wired()
        with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
            await gh.transition_issue(
                123,
                from_label=None,
                to_label="~workflow:em_pr",
            )
        label_change_lines = [
            r for r in caplog.records if r.message.startswith("label.change")
        ]
        assert (
            len(label_change_lines) == 1
        ), f"Expected exactly 1 label.change line, got: {[r.message for r in label_change_lines]}"
        assert (
            "removed=[]" in label_change_lines[0].message
        ), f"Expected removed=[] in message: {label_change_lines[0].message!r}"
        assert (
            "added=[~workflow:em_pr]" in label_change_lines[0].message
        ), f"Expected added label in message: {label_change_lines[0].message!r}"
