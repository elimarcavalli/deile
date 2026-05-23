"""Testes do comando /standup (issue #286).

Cobre:
  * Parser de duração (24h / 3d / 1w / inválido / vazio / zero)
  * Parser de argumentos da linha de comando do slash
  * build_prompt() — contém todos os dados coletados (assert do "o prompt
    do LLM inclui os dados coletados")
  * execute() — fluxo completo com mocks de git/gh + ModelRouter
  * Falhas claras quando o repo não é git ou gh não está autenticado
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any, List, Optional

import pytest
from rich.console import Console

from deile.commands.base import CommandContext, CommandResult
from deile.commands.builtin import standup_command as sc
from deile.commands.builtin.standup_command import (
    StandupCommand,
    StandupData,
    build_prompt,
    collect_commits,
    collect_issues,
    collect_prs,
    parse_args,
    parse_since,
)
from deile.core.exceptions import CommandError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(content: Any) -> str:
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=200)
    console.print(content)
    return buf.getvalue()


def _ctx(args: str = "") -> CommandContext:
    return CommandContext(user_input="/standup", args=args)


class _cd:
    """Context manager para mudar temporariamente de diretório."""

    def __init__(self, path: Path):
        self.path = path
        self._prev: Optional[str] = None

    def __enter__(self):
        self._prev = os.getcwd()
        os.chdir(str(self.path))
        return self

    def __exit__(self, *args: Any) -> None:
        if self._prev is not None:
            os.chdir(self._prev)


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Standup Tester",
            "GIT_AUTHOR_EMAIL": "standup@example.com",
            "GIT_COMMITTER_NAME": "Standup Tester",
            "GIT_COMMITTER_EMAIL": "standup@example.com",
        },
    )


# ---------------------------------------------------------------------------
# parse_since
# ---------------------------------------------------------------------------


class TestParseSince:
    def test_hours(self):
        assert parse_since("24h") == timedelta(hours=24)

    def test_hours_single_digit(self):
        assert parse_since("1h") == timedelta(hours=1)

    def test_days(self):
        assert parse_since("3d") == timedelta(days=3)

    def test_weeks(self):
        assert parse_since("1w") == timedelta(weeks=1)

    def test_uppercase_unit(self):
        assert parse_since("24H") == timedelta(hours=24)

    def test_extra_whitespace(self):
        assert parse_since("  3d  ") == timedelta(days=3)

    def test_invalid_unit(self):
        with pytest.raises(CommandError):
            parse_since("24m")

    def test_no_unit(self):
        with pytest.raises(CommandError):
            parse_since("24")

    def test_empty(self):
        with pytest.raises(CommandError):
            parse_since("")

    def test_none(self):
        with pytest.raises(CommandError):
            parse_since(None)  # type: ignore[arg-type]

    def test_zero(self):
        with pytest.raises(CommandError):
            parse_since("0h")

    def test_negative_is_invalid_format(self):
        # O regex não aceita o sinal '-' → erro de formato
        with pytest.raises(CommandError):
            parse_since("-24h")


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_empty_default(self):
        assert parse_args("") == "24h"

    def test_whitespace_default(self):
        assert parse_args("   ") == "24h"

    def test_equal_form(self):
        assert parse_args("--since=3d") == "3d"

    def test_space_form(self):
        assert parse_args("--since 1w") == "1w"

    def test_unknown_flag_raises(self):
        with pytest.raises(CommandError):
            parse_args("--foo=1d")

    def test_missing_value_raises(self):
        with pytest.raises(CommandError):
            parse_args("--since")


# ---------------------------------------------------------------------------
# build_prompt — dados coletados aparecem no prompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def _data(self) -> StandupData:
        return StandupData(
            since_spec="24h",
            since_iso="2025-05-22T05:00:00Z",
            commits=[
                {"hash": "abc1234", "author": "alice", "title": "fix pipeline retry"},
                {"hash": "def5678", "author": "bob", "title": "docs(readme)"},
            ],
            prs=[
                {
                    "number": 284,
                    "title": "retry backoff",
                    "state": "MERGED",
                    "author": "alice",
                    "url": "",
                    "updated_at": "",
                },
                {
                    "number": 277,
                    "title": "tier router",
                    "state": "OPEN",
                    "author": "carol",
                    "url": "",
                    "updated_at": "",
                },
            ],
            issues=[
                {
                    "number": 286,
                    "title": "/standup",
                    "state": "OPEN",
                    "author": "elimar",
                    "url": "",
                    "updated_at": "",
                },
                {
                    "number": 279,
                    "title": "loop fix",
                    "state": "CLOSED",
                    "author": "dan",
                    "url": "",
                    "updated_at": "",
                },
            ],
        )

    def test_contains_window_spec(self):
        prompt = build_prompt(self._data())
        assert "24h" in prompt
        assert "2025-05-22T05:00:00Z" in prompt

    def test_contains_all_commits(self):
        prompt = build_prompt(self._data())
        for commit in self._data().commits:
            assert commit["hash"] in prompt
            assert commit["author"] in prompt
            assert commit["title"] in prompt

    def test_contains_all_prs(self):
        prompt = build_prompt(self._data())
        for pr in self._data().prs:
            assert f"#{pr['number']}" in prompt
            assert pr["title"] in prompt
            assert pr["state"] in prompt

    def test_contains_all_issues(self):
        prompt = build_prompt(self._data())
        for issue in self._data().issues:
            assert f"#{issue['number']}" in prompt
            assert issue["title"] in prompt
            assert issue["state"] in prompt

    def test_empty_data_is_marked_explicitly(self):
        empty = StandupData(since_spec="24h", since_iso="2025-05-22T05:00:00Z")
        prompt = build_prompt(empty)
        assert "Commits (0)" in prompt
        assert "Pull Requests (0)" in prompt
        assert "Issues (0)" in prompt
        assert "(nenhum" in prompt  # bullet "(nenhum)" ou "(nenhuma)"

    def test_format_instructions_present(self):
        prompt = build_prompt(self._data())
        assert "PT-BR" in prompt
        assert "8 linhas" in prompt
        assert "Destaques" in prompt


# ---------------------------------------------------------------------------
# Coleta com mock de subprocess
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestCollectCommits:
    def test_parses_git_log_output(self, monkeypatch):
        fake_out = "\n".join(
            [
                "abc1234\x1falice\x1ffix pipeline retry",
                "def5678\x1fbob\x1fdocs(readme)",
            ]
        )
        calls: List[List[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _FakeCompleted(0, fake_out)

        monkeypatch.setattr(sc.subprocess, "run", fake_run)
        commits = collect_commits("2025-05-22T05:00:00Z")
        assert len(commits) == 2
        assert commits[0] == {"hash": "abc1234", "author": "alice", "title": "fix pipeline retry"}
        assert commits[1]["author"] == "bob"
        # Sanity: usamos git log com --since
        assert calls[0][0:2] == ["git", "log"]
        assert any("--since=2025-05-22T05:00:00Z" in tok for tok in calls[0])

    def test_empty_output(self, monkeypatch):
        monkeypatch.setattr(sc.subprocess, "run", lambda *a, **kw: _FakeCompleted(0, ""))
        assert collect_commits("2025-05-22T05:00:00Z") == []

    def test_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            sc.subprocess,
            "run",
            lambda *a, **kw: _FakeCompleted(128, "", "fatal: not a repo"),
        )
        assert collect_commits("2025-05-22T05:00:00Z") == []


class TestCollectPRs:
    def test_parses_gh_json(self, monkeypatch):
        payload = json.dumps(
            [
                {
                    "number": 284,
                    "title": "retry backoff",
                    "state": "MERGED",
                    "author": {"login": "alice"},
                    "url": "https://github.com/x/y/pull/284",
                    "updatedAt": "2025-05-22T10:00:00Z",
                },
                {
                    "number": 277,
                    "title": "tier router",
                    "state": "OPEN",
                    "author": {"login": "carol"},
                    "url": "https://github.com/x/y/pull/277",
                    "updatedAt": "2025-05-22T11:00:00Z",
                },
            ]
        )
        calls: List[List[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _FakeCompleted(0, payload)

        monkeypatch.setattr(sc.subprocess, "run", fake_run)
        prs = collect_prs("2025-05-22T05:00:00Z")
        assert len(prs) == 2
        assert prs[0]["number"] == 284
        assert prs[0]["author"] == "alice"
        assert prs[0]["state"] == "MERGED"
        # Sanity: gh pr list --state all --search updated:>=...
        assert calls[0][0:3] == ["gh", "pr", "list"]
        assert "--state" in calls[0]
        joined = " ".join(calls[0])
        assert "updated:>=2025-05-22T05:00:00Z" in joined

    def test_empty_output(self, monkeypatch):
        monkeypatch.setattr(sc.subprocess, "run", lambda *a, **kw: _FakeCompleted(0, ""))
        assert collect_prs("2025-05-22T05:00:00Z") == []

    def test_invalid_json_returns_empty(self, monkeypatch):
        monkeypatch.setattr(sc.subprocess, "run", lambda *a, **kw: _FakeCompleted(0, "not json"))
        assert collect_prs("2025-05-22T05:00:00Z") == []

    def test_author_none_safe(self, monkeypatch):
        payload = json.dumps(
            [
                {
                    "number": 1,
                    "title": "x",
                    "state": "OPEN",
                    "author": None,
                    "url": "",
                    "updatedAt": "",
                }
            ]
        )
        monkeypatch.setattr(sc.subprocess, "run", lambda *a, **kw: _FakeCompleted(0, payload))
        prs = collect_prs("2025-05-22T05:00:00Z")
        assert prs[0]["author"] == "?"


class TestCollectIssues:
    def test_parses_gh_json(self, monkeypatch):
        payload = json.dumps(
            [
                {
                    "number": 286,
                    "title": "/standup",
                    "state": "OPEN",
                    "author": {"login": "elimar"},
                    "url": "",
                    "updatedAt": "2025-05-22T05:00:00Z",
                }
            ]
        )
        calls: List[List[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _FakeCompleted(0, payload)

        monkeypatch.setattr(sc.subprocess, "run", fake_run)
        issues = collect_issues("2025-05-22T05:00:00Z")
        assert len(issues) == 1
        assert issues[0]["number"] == 286
        assert calls[0][0:3] == ["gh", "issue", "list"]


# ---------------------------------------------------------------------------
# Guard checks (não-git / sem gh / gh não autenticado)
# ---------------------------------------------------------------------------


class TestEnsureChecks:
    def test_not_git_raises(self, monkeypatch, tmp_path: Path):
        # shutil.which("git") presente, mas rev-parse retorna != 0
        monkeypatch.setattr(sc.shutil, "which", lambda exe: f"/usr/bin/{exe}")
        monkeypatch.setattr(
            sc.subprocess,
            "run",
            lambda *a, **kw: _FakeCompleted(128, "", "fatal: not a git repo"),
        )
        with pytest.raises(CommandError) as exc_info:
            sc._ensure_git_repo()
        assert "git" in str(exc_info.value).lower()

    def test_git_not_installed(self, monkeypatch):
        monkeypatch.setattr(sc.shutil, "which", lambda exe: None)
        with pytest.raises(CommandError) as exc_info:
            sc._ensure_git_repo()
        assert "git" in str(exc_info.value).lower()

    def test_gh_not_installed(self, monkeypatch):
        monkeypatch.setattr(sc.shutil, "which", lambda exe: None if exe == "gh" else f"/usr/bin/{exe}")
        with pytest.raises(CommandError) as exc_info:
            sc._ensure_gh_available()
        assert "gh" in str(exc_info.value).lower() or "github cli" in str(exc_info.value).lower()

    def test_gh_not_authenticated(self, monkeypatch):
        monkeypatch.setattr(sc.shutil, "which", lambda exe: f"/usr/bin/{exe}")
        monkeypatch.setattr(
            sc.subprocess,
            "run",
            lambda *a, **kw: _FakeCompleted(1, "", "You are not logged into any GitHub hosts."),
        )
        with pytest.raises(CommandError) as exc_info:
            sc._ensure_gh_available()
        assert "auten" in str(exc_info.value).lower() or "auth" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# execute() — fluxo completo
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_data():
    return StandupData(
        since_spec="24h",
        since_iso="2025-05-22T05:00:00Z",
        commits=[
            {"hash": "abc1234", "author": "alice", "title": "fix pipeline retry"},
        ],
        prs=[
            {
                "number": 284,
                "title": "retry backoff",
                "state": "MERGED",
                "author": "alice",
                "url": "",
                "updated_at": "",
            }
        ],
        issues=[
            {
                "number": 286,
                "title": "/standup",
                "state": "OPEN",
                "author": "elimar",
                "url": "",
                "updated_at": "",
            }
        ],
    )


class TestExecute:
    async def test_full_flow_calls_router(self, monkeypatch, fake_data):
        """Mock de coleta + ModelRouter; valida que o prompt do LLM
        contém os dados coletados e que a tool (router.select_provider +
        provider.generate) é chamada."""
        monkeypatch.setattr(sc, "collect_standup_data", lambda spec, **kw: fake_data)

        captured_prompts: List[str] = []

        async def fake_generate(prompt: str) -> str:
            captured_prompts.append(prompt)
            return "📰 Resumo gerado pelo LLM. Foi 1 commit, 1 PR e 1 issue."

        monkeypatch.setattr(sc, "generate_narrative", fake_generate)

        cmd = StandupCommand()
        result = await cmd.execute(_ctx("--since=24h"))
        assert isinstance(result, CommandResult)
        assert result.success
        assert result.content_type == "rich"
        # Prompt do LLM foi chamado uma vez e inclui os dados coletados
        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        assert "abc1234" in prompt
        assert "fix pipeline retry" in prompt
        assert "#284" in prompt
        assert "#286" in prompt
        assert "MERGED" in prompt
        # Metadata bate
        assert result.metadata["commit_count"] == 1
        assert result.metadata["pr_count"] == 1
        assert result.metadata["issue_count"] == 1
        assert result.metadata["since_spec"] == "24h"
        # Narrativa do LLM aparece no painel renderizado
        rendered = _render(result.content)
        assert "Resumo gerado pelo LLM" in rendered

    async def test_calls_real_router_via_generate_narrative(self, monkeypatch, fake_data):
        """Garante que generate_narrative() de fato bate em router/provider."""
        monkeypatch.setattr(sc, "collect_standup_data", lambda spec, **kw: fake_data)

        # Mock do router + provider
        class FakeResponse:
            def __init__(self, content: str):
                self.content = content

        captured: dict = {}

        class FakeProvider:
            async def generate(self, messages, system_instruction=None, **kw):
                captured["messages"] = messages
                captured["system_instruction"] = system_instruction
                return FakeResponse("Narrativa real do provedor.")

        class FakeRouter:
            async def select_provider(self, **kwargs):
                captured["select_kwargs"] = kwargs
                return FakeProvider()

        monkeypatch.setattr(sc, "get_model_router", lambda: FakeRouter())

        cmd = StandupCommand()
        result = await cmd.execute(_ctx(""))
        assert result.success
        # generate foi chamado com o prompt PT-BR + system_instruction PT-BR
        assert "messages" in captured
        assert len(captured["messages"]) == 1
        assert captured["messages"][0].role == "user"
        assert "abc1234" in captured["messages"][0].content
        assert "PT-BR" in (captured["system_instruction"] or "")
        # Narrativa do "provedor" aparece
        rendered = _render(result.content)
        assert "Narrativa real do provedor" in rendered

    async def test_invalid_since_returns_error(self, monkeypatch, fake_data):
        # Nem chega a coletar (falha no parse_since dentro de collect_standup_data)
        monkeypatch.setattr(sc.subprocess, "run", lambda *a, **kw: _FakeCompleted(0, ""))
        cmd = StandupCommand()
        with pytest.raises(CommandError):
            await cmd.execute(_ctx("--since=24m"))

    async def test_not_in_git_repo(self, monkeypatch, tmp_path: Path):
        # shutil.which retorna paths, mas rev-parse falha
        monkeypatch.setattr(sc.shutil, "which", lambda exe: f"/usr/bin/{exe}")
        monkeypatch.setattr(
            sc.subprocess,
            "run",
            lambda *a, **kw: _FakeCompleted(128, "", "fatal: not a git repo"),
        )

        async def must_not_be_called(prompt):
            pytest.fail("LLM não devia ser chamado quando o repo não é git")

        monkeypatch.setattr(sc, "generate_narrative", must_not_be_called)

        cmd = StandupCommand()
        with pytest.raises(CommandError) as exc_info:
            await cmd.execute(_ctx(""))
        assert "git" in str(exc_info.value).lower()

    async def test_gh_not_authenticated(self, monkeypatch):
        # git ok (true), gh auth status falha
        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
                return _FakeCompleted(0, "true\n")
            if cmd[:3] == ["gh", "auth", "status"]:
                return _FakeCompleted(1, "", "not logged in")
            return _FakeCompleted(0, "")

        monkeypatch.setattr(sc.shutil, "which", lambda exe: f"/usr/bin/{exe}")
        monkeypatch.setattr(sc.subprocess, "run", fake_run)

        cmd = StandupCommand()
        with pytest.raises(CommandError) as exc_info:
            await cmd.execute(_ctx(""))
        msg = str(exc_info.value).lower()
        assert "gh" in msg or "github" in msg

    async def test_default_since_is_24h(self, monkeypatch, fake_data):
        captured_specs: List[str] = []

        def fake_collect(spec, **kw):
            captured_specs.append(spec)
            return StandupData(since_spec=spec, since_iso="2025-05-22T05:00:00Z")

        async def fake_gen(prompt):
            return "ok"

        monkeypatch.setattr(sc, "collect_standup_data", fake_collect)
        monkeypatch.setattr(sc, "generate_narrative", fake_gen)
        cmd = StandupCommand()
        result = await cmd.execute(_ctx(""))
        assert result.metadata["since_spec"] == "24h"
        assert captured_specs == ["24h"]


# ---------------------------------------------------------------------------
# Smoke: auto-discovery registra o comando
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_standup_is_discoverable(self):
        from deile.commands.registry import CommandRegistry

        registry = CommandRegistry()
        registry.auto_discover_builtin_commands()
        assert "standup" in registry._commands

    def test_class_metadata(self):
        cmd = StandupCommand()
        assert cmd.name == "standup"
        assert cmd.cli_flag == "--standup"
        assert cmd.cli_requires_provider is True
