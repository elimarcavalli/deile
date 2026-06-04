"""Testes do comando /backlog (issue #419).

Cobre:
  * _parse_args — flags válidas, override, flags inválidas
  * _bucket_issue — todas as regras de precedência (bloqueada,
    aguardando_stakeholder, ordem canônica, sem rótulo)
  * _bucket_pr — bloqueada vence review, primeiro review encontrado, sem review
  * bucketize_issues / bucketize_prs — agregação funcional pura
  * collect_backlog_data — mock do ForgeClient (sem subprocess), contagens
    corretas
  * BacklogCommand.execute — fluxo completo com mocks de git/gh/collect
  * _build_tables — saída Rich renderiza todos os buckets, totais corretos
"""

from __future__ import annotations

from io import StringIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from deile.commands.base import CommandContext, CommandStatus
from deile.commands.builtin._backlog_collectors import (ISSUE_BUCKETS,
                                                        PR_BUCKETS, BacklogData,
                                                        _SEM_REVIEW,
                                                        _SEM_WORKFLOW,
                                                        _bucket_issue,
                                                        _bucket_pr,
                                                        bucketize_issues,
                                                        bucketize_prs,
                                                        collect_backlog_data)
from deile.commands.builtin.backlog_command import (BacklogCommand,
                                                    _build_tables, _parse_args)
from deile.core.exceptions import CommandError
from deile.orchestration.forge.refs import IssueRef, PrRef

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


def _issue(number: int, *labels: str) -> IssueRef:
    return IssueRef(
        number=number,
        title=f"issue {number}",
        url=f"https://github.com/owner/repo/issues/{number}",
        labels=tuple(labels),
    )


def _pr(number: int, *labels: str) -> PrRef:
    return PrRef(
        number=number,
        title=f"pr {number}",
        url=f"https://github.com/owner/repo/pull/{number}",
        labels=tuple(labels),
        head_ref="feat",
        base_ref="main",
    )


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
# Bucket constants — derivam de labels.py (Critério de aceite issue #419)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_issue_buckets_derived_from_workflow_labels():
    """ISSUE_BUCKETS é derivado de WORKFLOW_LABELS — mudança em labels.py
    deve propagar automaticamente para /backlog (critério explícito da issue)."""
    from deile.orchestration.pipeline.labels import WORKFLOW_LABELS
    expected = tuple(lb[len("~workflow:"):] for lb in WORKFLOW_LABELS)
    assert ISSUE_BUCKETS == expected


@pytest.mark.unit
def test_pr_buckets_derived_from_review_labels_plus_bloqueada():
    """PR_BUCKETS = REVIEW_LABELS (sem prefixo) + ``bloqueada`` (do WORKFLOW_BLOCKED)."""
    from deile.orchestration.pipeline.labels import REVIEW_LABELS
    review_buckets = tuple(lb[len("~review:"):] for lb in REVIEW_LABELS)
    assert PR_BUCKETS == review_buckets + ("bloqueada",)


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
    # em_revisao appears before em_implementacao in WORKFLOW_LABELS
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
# bucketize_issues / bucketize_prs (pure functional aggregation)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bucketize_issues_empty_input():
    counts = bucketize_issues([])
    assert all(counts[b] == 0 for b in ISSUE_BUCKETS)


@pytest.mark.unit
def test_bucketize_issues_basic():
    counts = bucketize_issues([
        _issue(1, "~workflow:nova"),
        _issue(2, "~workflow:nova"),
        _issue(3, "~workflow:em_revisao"),
        _issue(4, "~workflow:bloqueada"),
    ])
    assert counts["nova"] == 2
    assert counts["em_revisao"] == 1
    assert counts["bloqueada"] == 1


@pytest.mark.unit
def test_bucketize_issues_sem_workflow_collected():
    counts = bucketize_issues([
        _issue(1, "~type:feature"),
        _issue(2),
    ])
    assert counts.get(_SEM_WORKFLOW, 0) == 2


@pytest.mark.unit
def test_bucketize_prs_basic():
    counts = bucketize_prs([
        _pr(1, "~review:pendente"),
        _pr(2, "~review:em_andamento"),
        _pr(3, "~workflow:bloqueada"),
    ])
    assert counts["pendente"] == 1
    assert counts["em_andamento"] == 1
    assert counts["bloqueada"] == 1


@pytest.mark.unit
def test_bucketize_prs_sem_review_collected():
    counts = bucketize_prs([_pr(1, "~workflow:em_pr")])
    assert counts.get(_SEM_REVIEW, 0) == 1


# ---------------------------------------------------------------------------
# collect_backlog_data — uses ForgeClient (no subprocess)
# ---------------------------------------------------------------------------


def _fake_forge(issues, prs) -> MagicMock:
    forge = MagicMock()
    forge.list_open_issues = AsyncMock(return_value=issues)
    forge.list_open_prs = AsyncMock(return_value=prs)
    return forge


@pytest.mark.asyncio
async def test_collect_backlog_data_basic_counts():
    issues = [
        _issue(1, "~workflow:nova"),
        _issue(2, "~workflow:nova"),
        _issue(3, "~workflow:em_revisao"),
        _issue(4, "~workflow:bloqueada"),
    ]
    prs = [
        _pr(1, "~review:pendente"),
        _pr(2, "~review:em_andamento"),
        _pr(3, "~workflow:bloqueada"),
    ]
    forge = _fake_forge(issues, prs)
    router = MagicMock()
    router.route = MagicMock(return_value=forge)

    with patch(
        "deile.commands.builtin._backlog_collectors.get_forge_router",
        return_value=router,
    ):
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
    router.route.assert_called_once_with(project_path="owner/repo")
    forge.list_open_issues.assert_awaited_once_with(limit=1000)
    forge.list_open_prs.assert_awaited_once_with(limit=1000)


@pytest.mark.asyncio
async def test_collect_backlog_data_sem_workflow():
    forge = _fake_forge([_issue(1, "~type:feature")], [])
    router = MagicMock(route=MagicMock(return_value=forge))
    with patch(
        "deile.commands.builtin._backlog_collectors.get_forge_router",
        return_value=router,
    ):
        data = await collect_backlog_data("owner/repo")
    assert data.issue_counts.get(_SEM_WORKFLOW, 0) == 1


@pytest.mark.asyncio
async def test_collect_backlog_data_sem_review():
    forge = _fake_forge([], [_pr(1, "~workflow:em_pr")])
    router = MagicMock(route=MagicMock(return_value=forge))
    with patch(
        "deile.commands.builtin._backlog_collectors.get_forge_router",
        return_value=router,
    ):
        data = await collect_backlog_data("owner/repo")
    assert data.pr_counts.get(_SEM_REVIEW, 0) == 1


@pytest.mark.asyncio
async def test_collect_backlog_data_empty_repo():
    forge = _fake_forge([], [])
    router = MagicMock(route=MagicMock(return_value=forge))
    with patch(
        "deile.commands.builtin._backlog_collectors.get_forge_router",
        return_value=router,
    ):
        data = await collect_backlog_data("owner/repo")
    assert data.issue_total == 0
    assert data.pr_total == 0
    for b in ISSUE_BUCKETS:
        assert data.issue_counts[b] == 0
    for b in PR_BUCKETS:
        assert data.pr_counts[b] == 0


@pytest.mark.asyncio
async def test_collect_backlog_data_forge_error_propagates():
    """A ForgeCommandError from the adapter propagates so the command
    decorator can map it to an error panel (no silent swallow)."""
    from deile.orchestration.forge.base import ForgeCommandError
    forge = MagicMock()
    forge.list_open_issues = AsyncMock(
        side_effect=ForgeCommandError(("gh",), 1, "", "auth error")
    )
    forge.list_open_prs = AsyncMock(return_value=[])
    router = MagicMock(route=MagicMock(return_value=forge))
    with patch(
        "deile.commands.builtin._backlog_collectors.get_forge_router",
        return_value=router,
    ):
        with pytest.raises(ForgeCommandError):
            await collect_backlog_data("owner/repo")


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


# ---------------------------------------------------------------------------
# Architecture invariants — Pilar 03 §2 (Hexagonal) + issue #419 critérios
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_command_module_has_no_subprocess_import():
    """Pilar 03 §2: o command path NÃO chama ``gh`` em subprocess — toda a
    coleta passa pelo :class:`ForgeClient`. Garantia estrutural — a inspeção
    do source code do módulo do command falha se ``import subprocess``
    reaparecer."""
    import inspect
    from deile.commands.builtin import backlog_command
    src = inspect.getsource(backlog_command)
    assert "import subprocess" not in src, (
        "backlog_command.py reintroduziu 'import subprocess' — Pilar 03 §2 "
        "exige que toda chamada a gh/glab passe pelo ForgeClient."
    )
    assert "subprocess.run" not in src, (
        "backlog_command.py reintroduziu 'subprocess.run' — toda transporte "
        "para o forge deve passar pelo adapter (GitHubForge/GitLabForge)."
    )


@pytest.mark.unit
def test_collectors_module_has_no_subprocess_call():
    """Coletores também não falam ``gh``/``glab`` diretamente — só via
    :func:`get_forge_router`."""
    import inspect
    from deile.commands.builtin import _backlog_collectors
    src = inspect.getsource(_backlog_collectors)
    assert "subprocess.run" not in src
    assert "subprocess.Popen" not in src


def _runtime_string_constants(module) -> list:
    """Return every string constant *evaluated at runtime* in *module*.

    Skips docstrings (Module/ClassDef/FunctionDef ``body[0]`` when it is a
    bare string expression). Documentation is allowed to mention label
    names; what the issue critério forbids is runtime literals.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    docstring_nodes = set()

    def _mark_doc(node):
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docstring_nodes.add(id(body[0].value))

    _mark_doc(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _mark_doc(node)

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstring_nodes:
            out.append(node.value)
    return out


@pytest.mark.unit
def test_no_workflow_label_literals_in_command_runtime():
    """Critério explícito #419: nenhum literal ``"~workflow:<state>"`` /
    ``"~review:<state>"`` no código novo. Strings de help/UI usando o
    wildcard ``~workflow:*`` / ``~review:*`` (mero documental, sem nome de
    estado) são permitidas; o que o critério proíbe é hard-code de um
    nome de estado específico em runtime — esse deve vir de ``labels.py``."""
    import re
    from deile.commands.builtin import backlog_command
    runtime_strings = _runtime_string_constants(backlog_command)
    state_pattern = re.compile(r"~(?:workflow|review):[a-z_]+")
    offenders = [s for s in runtime_strings if state_pattern.search(s)]
    assert not offenders, (
        f"Runtime literals com nomes de estado em backlog_command.py: "
        f"{offenders!r}. Importe de WORKFLOW_LABELS / REVIEW_LABELS em "
        f"deile/orchestration/pipeline/labels.py."
    )


@pytest.mark.unit
def test_no_workflow_label_literals_in_collectors_runtime():
    """Mesmo critério para o módulo de coletores — só os prefixes
    ``~workflow:`` / ``~review:`` (sem nome de estado) são permitidos como
    constantes de stripping."""
    from deile.commands.builtin import _backlog_collectors
    runtime_strings = _runtime_string_constants(_backlog_collectors)
    # Allowed: bare prefixes used by `_strip_prefix`. Forbidden: any string
    # that pairs the prefix with a *state name*.
    import re
    state_pattern = re.compile(r"~(?:workflow|review):[a-z_]+")
    offenders = [s for s in runtime_strings if state_pattern.search(s)]
    assert not offenders, (
        f"Runtime literals com nomes de estado em _backlog_collectors.py: "
        f"{offenders!r}. Importe de WORKFLOW_LABELS / REVIEW_LABELS / "
        f"WORKFLOW_BLOCKED / WORKFLOW_WAITING."
    )


@pytest.mark.unit
def test_command_registered_in_registry():
    """BacklogCommand expõe ``cli_flag`` para registro pelo registry de
    builtins (mesmo padrão que /status, /standup, etc.)."""
    cmd = BacklogCommand()
    assert cmd.cli_flag == "--backlog"
    assert cmd.config.name == "backlog"
