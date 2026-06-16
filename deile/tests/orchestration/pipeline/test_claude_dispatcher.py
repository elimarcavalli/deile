"""Unit tests for ClaudeDispatcher — uses real subprocess via /bin/sh stub."""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.orchestration.pipeline.claude_dispatcher import (
    ClaudeDispatcher,
    render_implement_prompt,
    render_review_prompt,
)


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
        slow.write_text("#!/bin/sh\nsleep 3\n")
        slow.chmod(0o755)
        d = ClaudeDispatcher(claude_path=str(slow), timeout_seconds=1)
        result = await d.run("anything", cwd=tmp_path)
        assert not result.ok
        assert result.returncode == 124
        assert result.duration_seconds < 5


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
        assert prompt.count("@") == 5000

    def test_implement_prompt_normal_issue_uses_closes(self):
        prompt = render_implement_prompt("foo/bar", 42, "Add widget", "do it")
        assert "Closes #42" in prompt
        assert "Refs #42" not in prompt

    def test_implement_prompt_spike_uses_refs(self):
        # Legacy local path mirrors the pipeline brief: a spike references,
        # never auto-closes, its issue.
        prompt = render_implement_prompt("foo/bar", 7, "[SPIKE] Provar X", "spike body")
        assert "Refs #7" in prompt
        assert "Closes #7" not in prompt

    def test_review_prompt_includes_repo_and_number(self):
        prompt = render_review_prompt("foo/bar", 17, "PR title")
        assert "foo/bar" in prompt
        assert "#17" in prompt
        assert "PR title" in prompt


class TestSubscriptionAuthStripping:
    """Verify ANTHROPIC_API_KEY is stripped from subprocess env by default."""

    def test_build_env_strips_api_key_by_default(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setenv("PATH", "/bin")
        d = ClaudeDispatcher()
        env = d._build_env(None)
        assert env is not None
        assert "ANTHROPIC_API_KEY" not in env
        assert env.get("PATH") == "/bin"  # other vars preserved

    def test_build_env_strips_all_anthropic_auth_vars(self, monkeypatch):
        for k in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BEARER_TOKEN",
        ):
            monkeypatch.setenv(k, "secret")
        d = ClaudeDispatcher()
        env = d._build_env(None)
        for k in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BEARER_TOKEN",
        ):
            assert k not in env

    def test_build_env_inherits_when_subscription_disabled(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        d = ClaudeDispatcher(prefer_subscription_auth=False)
        env = d._build_env(None)
        # None means "inherit parent env" — claude will see the API key.
        assert env is None

    def test_build_env_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        d = ClaudeDispatcher()  # subscription mode by default
        # Caller passes an explicit env — we don't second-guess them.
        env = d._build_env({"X": "1", "ANTHROPIC_API_KEY": "explicit-key"})
        assert env == {"X": "1", "ANTHROPIC_API_KEY": "explicit-key"}

    async def test_run_uses_stripped_env(self, monkeypatch, fake_claude, tmp_path):
        # Echo the env back so we can assert what claude actually saw.
        echoer = tmp_path / "echo-env"
        echoer.write_text(
            "#!/bin/sh\n" 'echo "key=${ANTHROPIC_API_KEY:-UNSET}"\n' "exit 0\n"
        )
        echoer.chmod(0o755)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leak")
        d = ClaudeDispatcher(claude_path=str(echoer), timeout_seconds=10)
        result = await d.run("anything", cwd=tmp_path)
        assert (
            "key=UNSET" in result.stdout
        ), f"expected ANTHROPIC_API_KEY to be stripped; got: {result.stdout!r}"
