"""Unit tests for ClaudeDispatcher — uses real subprocess via /bin/sh stub."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from deile.orchestration.pipeline.claude_dispatcher import (
    ClaudeDispatcher, render_implement_prompt, render_review_prompt)


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    """Make a tiny shell script that mimics `claude -p "prompt"`."""
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\n"
        # echo back the prompt for stdout assertions
        "shift\n"  # drop -p
        'echo "$@"\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return script


class TestRunBasic:
    async def test_returns_ok_on_success(self, fake_claude, tmp_path):
        d = ClaudeDispatcher(claude_path=str(fake_claude), timeout_seconds=10)
        result = await d.run("hello world", cwd=tmp_path)
        assert result.ok
        assert result.returncode == 0
        assert "hello world" in result.stdout

    async def test_rejects_empty_prompt(self, fake_claude, tmp_path):
        d = ClaudeDispatcher(claude_path=str(fake_claude))
        with pytest.raises(ValueError):
            await d.run("", cwd=tmp_path)

    async def test_rejects_missing_cwd(self, fake_claude, tmp_path):
        d = ClaudeDispatcher(claude_path=str(fake_claude))
        with pytest.raises(FileNotFoundError):
            await d.run("foo", cwd=tmp_path / "does_not_exist")


class TestRunFailure:
    async def test_returncode_propagated_on_nonzero(self, tmp_path):
        failing = tmp_path / "fail-claude"
        failing.write_text("#!/bin/sh\necho boom 1>&2\nexit 7\n")
        failing.chmod(0o755)
        d = ClaudeDispatcher(claude_path=str(failing))
        result = await d.run("anything", cwd=tmp_path)
        assert not result.ok
        assert result.returncode == 7
        assert "boom" in result.stderr


class TestTimeout:
    async def test_timeout_terminates(self, tmp_path):
        slow = tmp_path / "slow-claude"
        slow.write_text("#!/bin/sh\nsleep 30\n")
        slow.chmod(0o755)
        d = ClaudeDispatcher(claude_path=str(slow), timeout_seconds=1)
        result = await d.run("anything", cwd=tmp_path)
        assert not result.ok
        assert result.returncode == 124


class TestPrompts:
    def test_implement_prompt_includes_repo_and_number(self):
        prompt = render_implement_prompt("foo/bar", 42, "title", "body")
        assert "foo/bar" in prompt
        assert "#42" in prompt
        assert "title" in prompt
        assert "body" in prompt

    def test_implement_prompt_truncates_long_body(self):
        body = "@" * 10000
        prompt = render_implement_prompt("foo/bar", 1, "t", body)
        # The '@' marker isolates the body: the template contains none.
        assert prompt.count("@") == 6000

    def test_review_prompt_includes_repo_and_number(self):
        prompt = render_review_prompt("foo/bar", 17, "PR title")
        assert "foo/bar" in prompt
        assert "#17" in prompt
        assert "PR title" in prompt
