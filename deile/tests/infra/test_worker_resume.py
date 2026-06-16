"""Unit tests for the worker-side resume helpers (issue #254).

Covers ``infra/k8s/_worker_resume.py`` — the pure git/fingerprint/journal/
ground-truth logic — and the ``worker_server`` resume orchestration
(``_compute_resume_result`` + the structured result). The infra scripts live
outside the ``deile`` package, so the path is inserted on sys.path for the
import (same pattern as ``test_infra_tooling.py``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _worker_resume as resume  # noqa: E402

# --------------------------------------------------------------------------
# git fixture: a real throwaway repo with a feature branch + untracked file
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A workdir with a ./repo clone on a feature branch + a partial change.

    Layout mirrors the worker's PVC: ``<workdir>/repo`` is the git clone, and
    the state files (``.deile-progress.*``) live in ``<workdir>`` (one level
    above the clone).
    """
    workdir = tmp_path / "pipeline-issue-1"
    repo = workdir / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "base.py").write_text("x = 1\n")
    _git(repo, "add", "base.py")
    _git(repo, "commit", "-q", "-m", "base")
    # Feature branch with a committed change + an untracked file (partial work).
    _git(repo, "checkout", "-q", "-b", "auto/issue-1")
    (repo / "feature.py").write_text("def feat():\n    return 42\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-q", "-m", "wip feature")
    (repo / "untracked_note.py").write_text("# scratch\n")
    return workdir


# --------------------------------------------------------------------------
# fingerprint + substantive-diff filtering (item 4)
# --------------------------------------------------------------------------


class TestFingerprint:
    def test_detects_substantive_work(self, workspace: Path):
        repo = resume.repo_dir(workspace)
        assert resume.has_substantive_work(repo, main_branch="main") is True

    def test_fingerprint_stable_across_calls(self, workspace: Path):
        repo = resume.repo_dir(workspace)
        fp1 = resume.compute_fingerprint(repo, main_branch="main")
        fp2 = resume.compute_fingerprint(repo, main_branch="main")
        assert fp1 == fp2 and fp1

    def test_fingerprint_changes_with_substantive_edit(self, workspace: Path):
        repo = resume.repo_dir(workspace)
        before = resume.compute_fingerprint(repo, main_branch="main")
        (repo / "feature.py").write_text("def feat():\n    return 99\n")
        after = resume.compute_fingerprint(repo, main_branch="main")
        assert before != after

    def test_progress_md_change_does_not_move_fingerprint(self, workspace: Path):
        # A change confined to a meta file is NOT substantive (item 4).
        repo = resume.repo_dir(workspace)
        before = resume.compute_fingerprint(repo, main_branch="main")
        # Write the journal *inside* the clone (worst case) and stage it.
        (repo / resume.PROGRESS_MD).write_text("# journal v1\n")
        after = resume.compute_fingerprint(repo, main_branch="main")
        assert before == after

    def test_no_substantive_work_on_clean_main(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t.t")
        _git(repo, "config", "user.name", "t")
        _git(repo, "checkout", "-q", "-b", "main")
        (repo / "a.py").write_text("a = 1\n")
        _git(repo, "add", "a.py")
        _git(repo, "commit", "-q", "-m", "base")
        assert resume.has_substantive_work(repo, main_branch="main") is False


class TestSubstantiveDiffFilter:
    def test_excludes_meta_file_hunks(self):
        diff = (
            "diff --git a/.deile-progress.md b/.deile-progress.md\n"
            "--- a/.deile-progress.md\n+++ b/.deile-progress.md\n"
            "@@ -1 +1 @@\n-old\n+new\n"
            "diff --git a/src/x.py b/src/x.py\n"
            "--- a/src/x.py\n+++ b/src/x.py\n"
            "@@ -1 +1 @@\n+real change\n"
        )
        lines = resume._substantive_diff_lines(diff)
        assert lines == ["+real change"]

    def test_drops_headers_and_hunks(self):
        diff = (
            "diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n"
            "@@ -1,2 +1,2 @@\n context\n-removed\n+added\n"
        )
        lines = resume._substantive_diff_lines(diff)
        assert "-removed" in lines and "+added" in lines
        assert all(not ln.startswith("@@") for ln in lines)


# --------------------------------------------------------------------------
# ground-truth end detection (item 5)
# --------------------------------------------------------------------------


class TestEndDetection:
    def test_concluido_when_pr_present(self, workspace: Path):
        repo = resume.repo_dir(workspace)
        end = resume.detect_end_state(
            repo,
            "Tudo certo.\nhttps://github.com/o/r/pull/9",
            main_branch="main",
            loop_ended=resume.LOOP_NATURAL,
        )
        assert end["ended"] == resume.ENDED_CONCLUIDO
        assert end["pr_url"] == "https://github.com/o/r/pull/9"

    def test_incompleto_when_diff_but_no_pr(self, workspace: Path):
        repo = resume.repo_dir(workspace)
        end = resume.detect_end_state(
            repo,
            "fiz parte mas estourou o limite",
            main_branch="main",
            loop_ended=resume.LOOP_CAP,
        )
        assert end["ended"] == resume.ENDED_INCOMPLETO
        assert end["motivo_fim_loop"] == resume.LOOP_CAP
        assert end["pr_url"] == ""

    def test_bloqueado_when_agent_declares(self, workspace: Path):
        repo = resume.repo_dir(workspace)
        end = resume.detect_end_state(
            repo,
            "tentei mas\nBLOQUEADO: falta a credencial X no ambiente",
            main_branch="main",
        )
        assert end["ended"] == resume.ENDED_BLOQUEADO
        assert "credencial X" in end["motivo_bloqueio"]

    def test_blocked_wins_even_with_pr(self, workspace: Path):
        # A declared block beats a PR URL — the agent knows best about a hard stop.
        repo = resume.repo_dir(workspace)
        end = resume.detect_end_state(
            repo,
            "https://github.com/o/r/pull/9\nBLOQUEADO: revisão humana necessária",
            main_branch="main",
        )
        assert end["ended"] == resume.ENDED_BLOQUEADO

    def test_review_needs_merge_not_just_pr(self, workspace: Path):
        # On the review/merge stage, a PR URL without MERGED is still incomplete.
        repo = resume.repo_dir(workspace)
        end = resume.detect_end_state(
            repo,
            "https://github.com/o/r/pull/9 (ainda não mergeei)",
            main_branch="main",
            expect_merge=True,
        )
        assert end["ended"] == resume.ENDED_INCOMPLETO
        end2 = resume.detect_end_state(
            repo,
            "https://github.com/o/r/pull/9 MERGED",
            main_branch="main",
            expect_merge=True,
        )
        assert end2["ended"] == resume.ENDED_CONCLUIDO


# --------------------------------------------------------------------------
# journal: agent writes vs worker auto-summary fallback (item 3)
# --------------------------------------------------------------------------


class TestJournal:
    def test_agent_wrote_progress_detected(self, workspace: Path):
        assert resume.agent_wrote_progress(workspace) is False
        resume.write_progress_md(workspace, "# fiz X\n")
        assert resume.agent_wrote_progress(workspace) is True

    def test_fallback_summary_from_transcript(self):
        text = resume.summarize_transcript_fallback(
            "linha 1\nlinha 2\nestado final aqui",
            ended=resume.ENDED_INCOMPLETO,
            motivo_fim_loop=resume.LOOP_TIMEOUT,
            attempt=3,
        )
        assert "estado final aqui" in text
        assert "tentativa" in text.lower()
        assert resume.ENDED_INCOMPLETO in text

    def test_fallback_caps_transcript(self):
        huge = "Z" * 50000
        text = resume.summarize_transcript_fallback(
            huge, ended=resume.ENDED_INCOMPLETO, motivo_fim_loop="x", max_chars=100
        )
        assert ("Z" * 100) in text
        assert ("Z" * 101) not in text


# --------------------------------------------------------------------------
# progress.json state (attempt + fingerprint + budget) — item 4 + 6
# --------------------------------------------------------------------------


class TestProgressState:
    def test_roundtrip(self, workspace: Path):
        resume.write_progress_state(
            workspace, attempt=2, fingerprint="abc", budget_acumulado_s=12.5
        )
        state = resume.read_progress_state(workspace)
        assert state["tentativa"] == 2
        assert state["fingerprint"] == "abc"
        assert state["budget_acumulado_s"] == 12.5

    def test_missing_returns_empty(self, workspace: Path):
        assert resume.read_progress_state(workspace) == {}

    def test_corrupt_returns_empty(self, workspace: Path):
        resume.progress_json_path(workspace).write_text("{not json")
        assert resume.read_progress_state(workspace) == {}


# --------------------------------------------------------------------------
# state files never enter the PR (gitignore + un-stage) — item: PR cleanliness
# --------------------------------------------------------------------------


class TestStateFilesNeverCommitted:
    def test_ensure_ignored_adds_exclude_entries(self, workspace: Path):
        repo = resume.repo_dir(workspace)
        resume.ensure_state_files_ignored(repo)
        exclude = (repo / resume.WORKSPACE_GITIGNORE).read_text()
        assert resume.PROGRESS_MD in exclude
        assert resume.PROGRESS_JSON in exclude

    def test_ensure_ignored_is_idempotent(self, workspace: Path):
        repo = resume.repo_dir(workspace)
        resume.ensure_state_files_ignored(repo)
        resume.ensure_state_files_ignored(repo)
        exclude = (repo / resume.WORKSPACE_GITIGNORE).read_text()
        # Each entry appears at most once.
        assert exclude.count(resume.PROGRESS_MD + "\n") == 1

    def test_ignored_progress_md_is_not_listed_as_untracked(self, workspace: Path):
        repo = resume.repo_dir(workspace)
        resume.ensure_state_files_ignored(repo)
        (repo / resume.PROGRESS_MD).write_text("# journal in clone\n")
        untracked = resume.list_untracked(repo)
        assert resume.PROGRESS_MD not in untracked

    def test_strip_from_index_unstages_forced_add(self, workspace: Path):
        repo = resume.repo_dir(workspace)
        # Force-add the progress file (simulating an agent that did `git add -f`).
        (repo / resume.PROGRESS_MD).write_text("# journal\n")
        subprocess.run(
            ["git", "-C", str(repo), "add", "-f", resume.PROGRESS_MD],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        resume.strip_state_files_from_index(repo)
        rc = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--error-unmatch", resume.PROGRESS_MD],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).returncode
        assert rc != 0  # no longer tracked


# --------------------------------------------------------------------------
# worker_server orchestration: _compute_resume_result + structured result
# --------------------------------------------------------------------------


class TestComputeResumeResult:
    def test_incompleto_writes_state_and_fallback_journal(self, workspace: Path):
        pytest.importorskip("aiohttp")
        import worker_server

        ctx = {
            "repo": "o/r",
            "branch": "auto/issue-1",
            "main_branch": "main",
            "expect_merge": False,
            "elapsed_s": 5.0,
        }
        res = worker_server._compute_resume_result(
            workspace, "fiz parte, estourei o limite", resume.LOOP_CAP, ctx
        )
        assert res["ended"] == resume.ENDED_INCOMPLETO
        assert res["tentativa"] == 1
        assert res["fingerprint"]
        # Budget surfaces in the structured result (item 6 ceiling input).
        assert res["budget_acumulado_s"] == 5.0
        # State persisted + fallback journal written (agent didn't write one).
        state = resume.read_progress_state(workspace)
        assert state["tentativa"] == 1
        assert state["budget_acumulado_s"] == 5.0
        assert resume.agent_wrote_progress(workspace)

    def test_attempt_counter_increments_across_calls(self, workspace: Path):
        pytest.importorskip("aiohttp")
        import worker_server

        ctx = {
            "repo": "o/r",
            "branch": "auto/issue-1",
            "main_branch": "main",
            "expect_merge": False,
            "elapsed_s": 3.0,
        }
        r1 = worker_server._compute_resume_result(
            workspace, "wip", resume.LOOP_CAP, ctx
        )
        r2 = worker_server._compute_resume_result(
            workspace, "wip more", resume.LOOP_CAP, ctx
        )
        assert r1["tentativa"] == 1
        assert r2["tentativa"] == 2
        # Budget accumulates across attempts.
        assert resume.read_progress_state(workspace)["budget_acumulado_s"] == 6.0

    def test_agent_journal_is_kept_not_overwritten(self, workspace: Path):
        pytest.importorskip("aiohttp")
        import worker_server

        resume.write_progress_md(workspace, "# JOURNAL ESCRITO PELO AGENTE\n")
        ctx = {
            "repo": "o/r",
            "branch": "auto/issue-1",
            "main_branch": "main",
            "expect_merge": False,
            "elapsed_s": 1.0,
        }
        worker_server._compute_resume_result(
            workspace, "transcript", resume.LOOP_NATURAL, ctx
        )
        # The agent's journal must survive (worker only writes fallback if absent).
        assert "ESCRITO PELO AGENTE" in resume.read_progress_md(workspace)

    def test_concluido_when_pr_in_transcript(self, workspace: Path):
        pytest.importorskip("aiohttp")
        import worker_server

        ctx = {
            "repo": "o/r",
            "branch": "auto/issue-1",
            "main_branch": "main",
            "expect_merge": False,
            "elapsed_s": 2.0,
        }
        res = worker_server._compute_resume_result(
            workspace, "pronto https://github.com/o/r/pull/5", resume.LOOP_NATURAL, ctx
        )
        assert res["ended"] == resume.ENDED_CONCLUIDO
        assert res["pr_url"].endswith("/pull/5")


class TestParseResumeCtx:
    def test_pipeline_dispatch_parsed(self):
        pytest.importorskip("aiohttp")
        import worker_server

        ctx = worker_server._parse_resume_ctx(
            {
                "resume": {
                    "mode": "resume",
                    "repo": "o/r",
                    "branch": "b",
                    "main_branch": "develop",
                    "expect_merge": True,
                }
            }
        )
        assert ctx is not None
        assert ctx["mode"] == "resume"
        assert ctx["main_branch"] == "develop"
        assert ctx["expect_merge"] is True

    def test_non_pipeline_dispatch_is_none(self):
        pytest.importorskip("aiohttp")
        import worker_server

        assert worker_server._parse_resume_ctx({}) is None
        assert worker_server._parse_resume_ctx({"resume": {}}) is None
        assert worker_server._parse_resume_ctx({"resume": "nope"}) is None


class TestRunTaskEmbedsResume:
    """``_run_task`` on the pipeline path embeds the structured ``resume`` block."""

    async def test_run_task_returns_structured_resume(self, workspace, monkeypatch):
        pytest.importorskip("aiohttp")
        import worker_server

        # WORK_ROOT → the test workspace's parent so the per-channel workdir is
        # our prepared ``pipeline-issue-1`` (which already has ./repo).
        monkeypatch.setattr(worker_server, "WORK_ROOT", workspace.parent)

        # Mock the agent: returns a transcript with a confirmed PR URL.
        class _Resp:
            content = "feito\nhttps://github.com/o/r/pull/7"

        fake_agent = MagicMock()
        fake_agent.get_or_create_session = AsyncMock()
        fake_agent.process_input = AsyncMock(return_value=_Resp())
        # Force the non-streaming path (simpler, deterministic).
        fake_agent.process_input_stream = None
        monkeypatch.setattr(
            worker_server, "_get_agent", AsyncMock(return_value=fake_agent)
        )
        # Silence the status-message UI.
        monkeypatch.setattr(
            worker_server, "_post_status_message", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            worker_server, "_edit_status_message", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(worker_server, "_react", AsyncMock(return_value=True))

        resume_ctx = {
            "mode": "resume",
            "repo": "o/r",
            "branch": "auto/issue-1",
            "main_branch": "main",
            "expect_merge": False,
            "pr_url_hint": "",
        }
        result = await worker_server._run_task(
            "task1",
            "continue a issue",
            "pipeline-issue-1",
            None,
            "developer",
            resume_ctx=resume_ctx,
        )
        assert "resume" in result
        rb = result["resume"]
        assert rb["ended"] == resume.ENDED_CONCLUIDO
        assert rb["pr_url"].endswith("/pull/7")
        assert rb["tentativa"] == 1
        assert rb["fingerprint"]

    async def test_run_task_without_resume_ctx_has_no_resume_block(
        self, workspace, monkeypatch
    ):
        pytest.importorskip("aiohttp")
        import worker_server

        monkeypatch.setattr(worker_server, "WORK_ROOT", workspace.parent)

        class _Resp:
            content = "ok"

        fake_agent = MagicMock()
        fake_agent.get_or_create_session = AsyncMock()
        fake_agent.process_input = AsyncMock(return_value=_Resp())
        fake_agent.process_input_stream = None
        monkeypatch.setattr(
            worker_server, "_get_agent", AsyncMock(return_value=fake_agent)
        )
        monkeypatch.setattr(
            worker_server, "_post_status_message", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            worker_server, "_edit_status_message", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(worker_server, "_react", AsyncMock(return_value=True))

        # Non-pipeline dispatch (the /deile passthrough): no resume_ctx.
        result = await worker_server._run_task(
            "task2",
            "faça algo",
            "pipeline-issue-1",
            None,
            "developer",
        )
        assert "resume" not in result
