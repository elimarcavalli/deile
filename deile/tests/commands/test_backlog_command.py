"""Testes do comando /backlog (issue #419).

Cobre:
  * _parse_args — flags válidas, override, flags inválidas
  * _bucket_issue — todas as regras de precedência (bloqueada, aguardando_stakeholder,
    ordem canônica, sem rótulo)
  * _bucket_pr — bloqueada vence review, primeiro review encontrado, sem review
  * _extract_label_names — objetos dict e strings planas
  * collect_backlog_data — mock de subprocess.run, contagens corretas
  * BacklogCommand.execute — fluxo completo com mocks de git/gh/collect
  * _build_tables — saída Rich renderiza todos os buckets, totais corretos
"""

from __future__ import annotations

import json
from io import StringIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from deile.commands.base import CommandContext, CommandResult, CommandStatus
from deile.commands.builtin.backlog_command import (
    ISSUE_BUCKETS,
    PR_BUCKETS,
    BacklogCommand,
    BacklogData,
    _SEM_REVIEW,
    _SEM_WORKFLOW,
    _bucket_issue,
    _bucket_pr,
    _build_tables,
    _extract_label_names,
    _parse_args,
    _run_gh_list,
    collect_backlog_data,
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
    return CommandContext(user_input="/backlog", args=args)


def _completed(returncode: int = 0, stdout: str = "[]", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_args_empty_returns_none():
    assert _parse_args("") is None


@pytest.mark.unit
def test_parse_args_whitespace_returns_none():
    assert _parse_args("   ") is None


@pytest.mark.unit
def test_parse_args_repo_space_form():
    assert _parse_args("--repo owner/name") == "owner/name"


@pytest.mark.unit
def test_parse_args_repo_equals_form():
    assert _parse_args("--repo=owner/name") == "owner/name"


@pytest.mark.unit
def test_parse_args_repo_strips_whitespace():
    assert _parse_args("--repo  owner/name ") == "owner/name"


@pytest.mark.unit
def test_parse_args_repo_missing_slash_raises():
    with pytest.raises(CommandError, match="owner/name"):
        _parse_args("--repo onlyone")


@pytest.mark.unit
def test_parse_args_unknown_flag_raises():
    with pytest.raises(CommandError, match="desconhecida"):
        _parse_args("--unknown")


# ---------------------------------------------------------------------------
# _extract_label_names
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_label_names_dict_objects():
    raw = [{"id": 1, "name": "~workflow:nova"}, {"id": 2, "name": "~type:feature"}]
    assert _extract_label_names(raw) == ("~workflow:nova", "~type:feature")


@pytest.mark.unit
def test_extract_label_names_plain_strings():
    assert _extract_label_names(["~workflow:nova", "~type:feature"]) == (
        "~workflow:nova",
        "~type:feature",
    )


@pytest.mark.unit
def test_extract_label_names_empty():
    assert _extract_label_names([]) == ()


@pytest.mark.unit
def test_extract_label_names_none_input():
    assert _extract_label_names(None) == ()


@pytest.mark.unit
def test_extract_label_names_filters_empty_names():
    raw = [{"name": ""}, {"name": "~workflow:nova"}]
    assert _extract_label_names(raw) == ("~workflow:nova",)


# ---------------------------------------------------------------------------
# _bucket_issue — precedence rules
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bucket_issue_no_labels():
    assert _bucket_issue(()) == _SEM_WORKFLOW


@pytest.mark.unit
def test_bucket_issue_no_workflow_labels():
    assert _bucket_issue(("~type:feature", "~priority:high")) == _SEM_WORKFLOW


@pytest.mark.unit
def test_bucket_issue_nova():
    assert _bucket_issue(("~workflow:nova",)) == "nova"


@pytest.mark.unit
@pytest.mark.parametrize("state", list(ISSUE_BUCKETS))
def test_bucket_issue_each_canonical_bucket(state: str):
    assert _bucket_issue((f"~workflow:{state}",)) == state


@pytest.mark.unit
def test_bucket_issue_bloqueada_wins_over_any_other():
    assert _bucket_issue(("~workflow:em_implementacao", "~workflow:bloqueada")) == "bloqueada"


@pytest.mark.unit
def test_bucket_issue_bloqueada_alone():
    assert _bucket_issue(("~workflow:bloqueada",)) == "bloqueada"


@pytest.mark.unit
def test_bucket_issue_aguardando_wins_over_regular_state():
    # aguardando_stakeholder is an overlay that takes priority over the refine-state
    assert _bucket_issue(
        ("~workflow:em_arquitetura", "~workflow:aguardando_stakeholder")
    ) == "aguardando_stakeholder"


@pytest.mark.unit
def test_bucket_issue_bloqueada_beats_aguardando():
    assert _bucket_issue(
        ("~workflow:aguardando_stakeholder", "~workflow:bloqueada")
    ) == "bloqueada"


@pytest.mark.unit
def test_bucket_issue_first_canonical_order_wins():
    # em_revisao appears before em_implementacao in ISSUE_BUCKETS
    assert _bucket_issue(("~workflow:em_implementacao", "~workflow:em_revisao")) == "em_revisao"


# ---------------------------------------------------------------------------
# _bucket_pr — precedence rules
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bucket_pr_no_labels():
    assert _bucket_pr(()) == _SEM_REVIEW


@pytest.mark.unit
def test_bucket_pr_no_review_no_blocked():
    assert _bucket_pr(("~workflow:em_pr", "~type:feature")) == _SEM_REVIEW


@pytest.mark.unit
@pytest.mark.parametrize("state", list(PR_BUCKETS))
def test_bucket_pr_each_review_bucket(state: str):
    if state == "bloqueada":
        assert _bucket_pr(("~workflow:bloqueada",)) == "bloqueada"
    else:
        assert _bucket_pr((f"~review:{state}",)) == state


@pytest.mark.unit
def test_bucket_pr_bloqueada_wins_over_review():
    assert _bucket_pr(("~review:pendente", "~workflow:bloqueada")) == "bloqueada"


@pytest.mark.unit
def test_bucket_pr_first_review_wins():
    assert _bucket_pr(("~review:pendente",)) == "pendente"


@pytest.mark.unit
def test_bucket_pr_em_andamento():
    assert _bucket_pr(("~review:em_andamento",)) == "em_andamento"


@pytest.mark.unit
def test_bucket_pr_concluida():
    assert _bucket_pr(("~review:concluida",)) == "concluida"


# ---------------------------------------------------------------------------
# _run_gh_list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_gh_list_success():
    payload = [{"number": 1, "labels": []}]
    with patch("subprocess.run", return_value=_completed(stdout=json.dumps(payload))):
        result = _run_gh_list(["gh", "issue", "list"])
    assert result == payload


@pytest.mark.unit
def test_run_gh_list_nonzero_raises():
    with patch("subprocess.run", return_value=_completed(returncode=1, stderr="auth error")):
        with pytest.raises(CommandError, match="auth error"):
            _run_gh_list(["gh", "issue", "list"])


@pytest.mark.unit
def test_run_gh_list_gh_not_found():
    import subprocess as _subprocess
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(CommandError, match="gh CLI"):
            _run_gh_list(["gh", "issue", "list"])


@pytest.mark.unit
def test_run_gh_list_timeout():
    import subprocess as _subprocess
    with patch("subprocess.run", side_effect=_subprocess.TimeoutExpired("gh", 30)):
        with pytest.raises(CommandError, match="Timeout"):
            _run_gh_list(["gh", "issue", "list"])


@pytest.mark.unit
def test_run_gh_list_bad_json():
    with patch("subprocess.run", return_value=_completed(stdout="not json")):
        with pytest.raises(CommandError, match="inesperada"):
            _run_gh_list(["gh", "issue", "list"])


# ---------------------------------------------------------------------------
# collect_backlog_data
# ---------------------------------------------------------------------------


def _make_issues_json(*states: str) -> str:
    items = [
        {"number": i + 1, "labels": [{"name": f"~workflow:{s}"}]}
        for i, s in enumerate(states)
    ]
    return json.dumps(items)


def _make_prs_json(*reviews: str) -> str:
    items = []
    for i, r in enumerate(reviews):
        if r == "bloqueada":
            labels = [{"name": "~workflow:bloqueada"}]
        elif r == _SEM_REVIEW:
            labels = []
        else:
            labels = [{"name": f"~review:{r}"}]
        items.append({"number": i + 1, "labels": labels})
    return json.dumps(items)


@pytest.mark.asyncio
async def test_collect_backlog_data_basic_counts():
    issues_json = _make_issues_json("nova", "nova", "em_revisao", "bloqueada")
    prs_json = _make_prs_json("pendente", "em_andamento", "bloqueada")

    def fake_run(cmd, **_kwargs):
        if "issue" in cmd:
            return _completed(stdout=issues_json)
        return _completed(stdout=prs_json)

    with patch("shutil.which", return_value="/usr/bin/gh"), \
         patch("subprocess.run", side_effect=fake_run):
        data = await collect_backlog_data("owner/repo")

    assert data.repo == "owner/repo"
    assert data.issue_total == 4
    assert data.pr_total == 3
    assert data.issue_counts["nova"] == 2
    assert data.issue_counts["em_revisao"] == 1
    assert data.issue_counts["bloqueada"] == 1
    assert data.pr_counts["pendente"] == 1
    assert data.pr_counts["em_andamento"] == 1
    assert data.pr_counts["bloqueada"] == 1


@pytest.mark.asyncio
async def test_collect_backlog_data_sem_workflow():
    # Issue with no ~workflow:* label
    issues_json = json.dumps([{"number": 1, "labels": [{"name": "~type:feature"}]}])
    prs_json = json.dumps([])

    def fake_run(cmd, **_kwargs):
        if "issue" in cmd:
            return _completed(stdout=issues_json)
        return _completed(stdout=prs_json)

    with patch("shutil.which", return_value="/usr/bin/gh"), \
         patch("subprocess.run", side_effect=fake_run):
        data = await collect_backlog_data("owner/repo")

    assert data.issue_counts.get(_SEM_WORKFLOW, 0) == 1


@pytest.mark.asyncio
async def test_collect_backlog_data_sem_review():
    issues_json = json.dumps([])
    prs_json = json.dumps([{"number": 1, "labels": [{"name": "~workflow:em_pr"}]}])

    def fake_run(cmd, **_kwargs):
        if "issue" in cmd:
            return _completed(stdout=issues_json)
        return _completed(stdout=prs_json)

    with patch("shutil.which", return_value="/usr/bin/gh"), \
         patch("subprocess.run", side_effect=fake_run):
        data = await collect_backlog_data("owner/repo")

    assert data.pr_counts.get(_SEM_REVIEW, 0) == 1


@pytest.mark.asyncio
async def test_collect_backlog_data_gh_not_found():
    with patch("shutil.which", return_value=None):
        with pytest.raises(CommandError, match="gh CLI"):
            await collect_backlog_data("owner/repo")


@pytest.mark.asyncio
async def test_collect_backlog_data_empty_repo():
    def fake_run(cmd, **_kwargs):
        return _completed(stdout="[]")

    with patch("shutil.which", return_value="/usr/bin/gh"), \
         patch("subprocess.run", side_effect=fake_run):
        data = await collect_backlog_data("owner/repo")

    assert data.issue_total == 0
    assert data.pr_total == 0
    for b in ISSUE_BUCKETS:
        assert data.issue_counts[b] == 0
    for b in PR_BUCKETS:
        assert data.pr_counts[b] == 0


# ---------------------------------------------------------------------------
# _build_tables — Rich rendering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_tables_contains_all_issue_buckets():
    data = BacklogData(
        repo="owner/repo",
        issue_counts={b: i for i, b in enumerate(ISSUE_BUCKETS)},
        pr_counts={b: 0 for b in PR_BUCKETS},
        issue_total=sum(range(len(ISSUE_BUCKETS))),
        pr_total=0,
    )
    rendered = _render(_build_tables(data))
    for bucket in ISSUE_BUCKETS:
        assert bucket in rendered, f"bucket {bucket!r} missing from rendered output"


@pytest.mark.unit
def test_build_tables_contains_all_pr_buckets():
    data = BacklogData(
        repo="owner/repo",
        issue_counts={b: 0 for b in ISSUE_BUCKETS},
        pr_counts={b: i for i, b in enumerate(PR_BUCKETS)},
        issue_total=0,
        pr_total=sum(range(len(PR_BUCKETS))),
    )
    rendered = _render(_build_tables(data))
    for bucket in PR_BUCKETS:
        assert bucket in rendered, f"bucket {bucket!r} missing from rendered output"


@pytest.mark.unit
def test_build_tables_shows_totals():
    data = BacklogData(
        repo="owner/repo",
        issue_counts={"nova": 3, **{b: 0 for b in ISSUE_BUCKETS if b != "nova"}},
        pr_counts={"pendente": 2, **{b: 0 for b in PR_BUCKETS if b != "pendente"}},
        issue_total=3,
        pr_total=2,
    )
    rendered = _render(_build_tables(data))
    assert "3" in rendered
    assert "2" in rendered


@pytest.mark.unit
def test_build_tables_sem_workflow_hidden_when_zero():
    data = BacklogData(
        repo="owner/repo",
        issue_counts={b: 0 for b in ISSUE_BUCKETS},
        pr_counts={b: 0 for b in PR_BUCKETS},
        issue_total=0,
        pr_total=0,
    )
    rendered = _render(_build_tables(data))
    assert _SEM_WORKFLOW not in rendered


@pytest.mark.unit
def test_build_tables_sem_workflow_shown_when_nonzero():
    data = BacklogData(
        repo="owner/repo",
        issue_counts={**{b: 0 for b in ISSUE_BUCKETS}, _SEM_WORKFLOW: 5},
        pr_counts={b: 0 for b in PR_BUCKETS},
        issue_total=5,
        pr_total=0,
    )
    rendered = _render(_build_tables(data))
    assert _SEM_WORKFLOW in rendered


@pytest.mark.unit
def test_build_tables_sem_review_hidden_when_zero():
    data = BacklogData(
        repo="owner/repo",
        issue_counts={b: 0 for b in ISSUE_BUCKETS},
        pr_counts={b: 0 for b in PR_BUCKETS},
        issue_total=0,
        pr_total=0,
    )
    rendered = _render(_build_tables(data))
    assert _SEM_REVIEW not in rendered


@pytest.mark.unit
def test_build_tables_sem_review_shown_when_nonzero():
    data = BacklogData(
        repo="owner/repo",
        issue_counts={b: 0 for b in ISSUE_BUCKETS},
        pr_counts={**{b: 0 for b in PR_BUCKETS}, _SEM_REVIEW: 7},
        issue_total=0,
        pr_total=7,
    )
    rendered = _render(_build_tables(data))
    assert _SEM_REVIEW in rendered


# ---------------------------------------------------------------------------
# BacklogCommand.execute — integration with mocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_success():
    data = BacklogData(
        repo="owner/repo",
        issue_counts={b: 0 for b in ISSUE_BUCKETS},
        pr_counts={b: 0 for b in PR_BUCKETS},
        issue_total=0,
        pr_total=0,
    )
    cmd = BacklogCommand()
    ctx = _ctx("")

    with patch("deile.commands.builtin.backlog_command.ensure_git_repo"), \
         patch("deile.commands.builtin.backlog_command.ensure_gh_authenticated"), \
         patch("deile.commands.builtin.backlog_command._resolve_repo_from_git",
               return_value="owner/repo"), \
         patch("deile.commands.builtin.backlog_command.collect_backlog_data",
               new=AsyncMock(return_value=data)), \
         patch("deile.commands.builtin.backlog_command.emit_audit_event"):
        result = await cmd.execute(ctx)

    assert result.status == CommandStatus.SUCCESS
    assert result.content_type == "rich"
    assert result.metadata["repo"] == "owner/repo"
    assert result.metadata["issue_total"] == 0
    assert result.metadata["pr_total"] == 0


@pytest.mark.asyncio
async def test_execute_repo_override():
    data = BacklogData(
        repo="other/project",
        issue_counts={b: 0 for b in ISSUE_BUCKETS},
        pr_counts={b: 0 for b in PR_BUCKETS},
        issue_total=0,
        pr_total=0,
    )
    cmd = BacklogCommand()
    ctx = _ctx("--repo other/project")
    mock_collect = AsyncMock(return_value=data)

    with patch("deile.commands.builtin.backlog_command.ensure_git_repo"), \
         patch("deile.commands.builtin.backlog_command.ensure_gh_authenticated"), \
         patch("deile.commands.builtin.backlog_command._resolve_repo_from_git",
               return_value="should/not-be-used"), \
         patch("deile.commands.builtin.backlog_command.collect_backlog_data",
               new=mock_collect), \
         patch("deile.commands.builtin.backlog_command.emit_audit_event"):
        result = await cmd.execute(ctx)

    mock_collect.assert_called_once_with("other/project")
    assert result.metadata["repo"] == "other/project"


@pytest.mark.asyncio
async def test_execute_not_git_repo_raises():
    # CommandError propagates untouched through wrap_command_errors
    cmd = BacklogCommand()
    ctx = _ctx("")
    with patch("deile.commands.builtin.backlog_command.ensure_git_repo",
               side_effect=CommandError("não é um repositório git")), \
         patch("deile.commands.builtin.backlog_command.emit_audit_event"):
        with pytest.raises(CommandError, match="não é um repositório git"):
            await cmd.execute(ctx)


@pytest.mark.asyncio
async def test_execute_bad_flag_raises():
    # CommandError from _parse_args propagates untouched
    cmd = BacklogCommand()
    ctx = _ctx("--invalid-flag")
    with patch("deile.commands.builtin.backlog_command.ensure_git_repo"), \
         patch("deile.commands.builtin.backlog_command.emit_audit_event"):
        with pytest.raises(CommandError, match="desconhecida"):
            await cmd.execute(ctx)
