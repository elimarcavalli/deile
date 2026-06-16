"""AC3b — ForgeClient on_label_change sink: no-op-safe + monitor wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from deile.orchestration.forge.base import ForgeConfig, ForgeKind
from deile.orchestration.forge.github_forge import GitHubForge
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor


def _github_forge() -> GitHubForge:
    cfg = ForgeConfig(
        project_path="owner/repo",
        kind=ForgeKind.GITHUB,
        host="github.com",
        cli_path="/usr/bin/gh",
    )
    return GitHubForge(cfg)


async def _fake_run_checked(*args):
    return ""


async def _fake_run(*args):
    return (0, "", "")


class TestForgeSinkNoopSafe:
    """on_label_change = None → all label mutations complete without exception."""

    async def test_add_labels_no_sink(self):
        gh = _github_forge()
        assert gh.on_label_change is None
        with patch.object(gh, "_run_checked", side_effect=_fake_run_checked):
            await gh.add_labels("issue", 1, ["~workflow:nova"])

    async def test_remove_labels_no_sink(self):
        gh = _github_forge()
        assert gh.on_label_change is None
        with patch.object(gh, "_run", side_effect=_fake_run):
            await gh.remove_labels("issue", 1, ["~workflow:nova"])

    async def test_transition_issue_no_sink(self):
        gh = _github_forge()
        assert gh.on_label_change is None
        with (
            patch.object(gh, "_run_checked", side_effect=_fake_run_checked),
            patch.object(gh, "_run", side_effect=_fake_run),
        ):
            await gh.transition_issue(
                1, from_label="~workflow:nova", to_label="~workflow:em_revisao"
            )


def _boom(kind, num, rem, add):
    raise RuntimeError("boom — sink must not propagate")


class TestForgeSinkExceptionSwallowed:
    """Sink raising RuntimeError → mutation completes (exception swallowed)."""

    async def test_add_labels_sink_raises_swallowed(self):
        gh = _github_forge()
        gh.on_label_change = _boom
        with patch.object(gh, "_run_checked", side_effect=_fake_run_checked):
            await gh.add_labels("issue", 1, ["~workflow:nova"])

    async def test_remove_labels_sink_raises_swallowed(self):
        gh = _github_forge()
        gh.on_label_change = _boom
        with patch.object(gh, "_run", side_effect=_fake_run):
            await gh.remove_labels("issue", 1, ["~workflow:nova"])

    async def test_transition_issue_sink_raises_swallowed(self):
        gh = _github_forge()
        gh.on_label_change = _boom
        with (
            patch.object(gh, "_run_checked", side_effect=_fake_run_checked),
            patch.object(gh, "_run", side_effect=_fake_run),
        ):
            await gh.transition_issue(
                1, from_label="~workflow:nova", to_label="~workflow:em_revisao"
            )


class TestMonitorWiresOnLabelChange:
    """PipelineMonitor.__init__ assigns forge.on_label_change (AC3b scenario iii)."""

    def test_monitor_wires_on_label_change_after_init(self):
        cfg = PipelineConfig(
            repo="owner/repo",
            base_repo_path=Path("/tmp/fake"),
            notify_user_id="42",
        )
        forge = MagicMock()
        forge.on_label_change = None
        monitor = PipelineMonitor(
            cfg,
            forge=forge,
            worktrees=MagicMock(),
            claude=MagicMock(),
        )
        assert (
            monitor.forge.on_label_change is not None
        ), "PipelineMonitor.__init__ must wire forge.on_label_change"
