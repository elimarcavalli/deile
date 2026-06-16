"""Backlog Command — contagens agregadas de issues/PRs abertas por estado de workflow.

Slash command ``/backlog`` que imprime, em duas tabelas Rich, contagens
agregadas das issues e PRs abertas agrupadas pelos rótulos de estado do
pipeline autônomo (``~workflow:*`` e ``~review:*``).

Uso:
    /backlog                    # repositório resolvido via git remote origin
    /backlog --repo owner/name  # override explícito

Responsabilidade única: parsing de argumentos e renderização Rich. A
coleta/bucketização vive em :mod:`._backlog_collectors` (delegação via
:class:`ForgeClient` — Pilar 03 §2 Hexagonal).
"""

from __future__ import annotations

import asyncio
from typing import Optional

from rich.console import Group
from rich.table import Table

from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand
from ._backlog_collectors import (
    _SEM_REVIEW,
    _SEM_WORKFLOW,
    ISSUE_BUCKETS,
    PR_BUCKETS,
    BacklogData,
    collect_backlog_data,
)
from ._git_helpers import ensure_gh_authenticated, ensure_git_repo
from ._shared import emit_audit_event, wrap_command_errors
from ._standup_collectors import _resolve_repo_from_git

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(args: str) -> Optional[str]:
    """Parse ``/backlog [--repo owner/name]``.

    Returns the explicit repo override (``owner/name``) or ``None`` when the
    repo should be resolved from the git remote.
    """
    args = args.strip()
    if not args:
        return None
    for prefix in ("--repo=", "--repo "):
        if args.startswith(prefix):
            repo = args[len(prefix) :].strip()
            if not repo or "/" not in repo:
                raise CommandError(
                    f"Repo inválido: {repo!r}. Use o formato owner/name."
                )
            return repo
    raise CommandError(
        f"Flag desconhecida: {args!r}. Uso: /backlog [--repo owner/name]"
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _build_tables(data: BacklogData) -> Group:
    """Build the two Rich tables from *data* and return them as a Group.

    Column widths are intentionally NOT set (no ``width=N``) so Rich adapts
    to the terminal — Pilar 03 §15 (UI adaptativa).  Buckets with count 0
    are included for shape visibility; the ``_SEM_*`` rows appear only when
    count > 0 so they don't pollute empty backlogs.
    """
    # Table 1 — Issues by ~workflow:*
    t1 = Table(
        title=f"Issues abertas — {data.repo}",
        show_header=True,
        header_style="bold cyan",
    )
    t1.add_column("Estado")
    t1.add_column("Count", justify="right")

    for bucket in ISSUE_BUCKETS:
        count = data.issue_counts.get(bucket, 0)
        t1.add_row(bucket, str(count))

    sem_issues = data.issue_counts.get(_SEM_WORKFLOW, 0)
    if sem_issues > 0:
        t1.add_row(_SEM_WORKFLOW, str(sem_issues), style="dim")

    t1.add_row("[bold]Total aberto[/bold]", f"[bold]{data.issue_total}[/bold]")

    # Table 2 — PRs by ~review:* + bloqueada
    t2 = Table(
        title=f"PRs abertas — {data.repo}",
        show_header=True,
        header_style="bold cyan",
    )
    t2.add_column("Estado")
    t2.add_column("Count", justify="right")

    for bucket in PR_BUCKETS:
        count = data.pr_counts.get(bucket, 0)
        t2.add_row(bucket, str(count))

    sem_prs = data.pr_counts.get(_SEM_REVIEW, 0)
    if sem_prs > 0:
        t2.add_row(_SEM_REVIEW, str(sem_prs), style="dim")

    t2.add_row("[bold]Total aberto[/bold]", f"[bold]{data.pr_total}[/bold]")

    return Group(t1, t2)


# ---------------------------------------------------------------------------
# Command class
# ---------------------------------------------------------------------------


class BacklogCommand(DirectCommand):
    """Exibe contagens de issues e PRs abertas por estado de workflow."""

    cli_flag = "--backlog"
    cli_help = "Contagens de issues/PRs abertas por estado de workflow do pipeline."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig

        config = CommandConfig(
            name="backlog",
            description=(
                "Contagens agregadas de issues e PRs abertas agrupadas por "
                "rótulos ~workflow:* e ~review:*. "
                "Uso: /backlog [--repo owner/name]"
            ),
        )
        super().__init__(config)

    @wrap_command_errors(
        "backlog", message_template="Falha ao executar /backlog: {exc}"
    )
    async def execute(self, context: CommandContext) -> CommandResult:
        from ...security.audit_logger import AuditEventType, SeverityLevel

        emit_audit_event(
            event_type=AuditEventType.COMMAND_EXECUTED,
            severity=SeverityLevel.INFO,
            resource="/backlog",
            action="execute",
            details={"args": context.args},
        )

        repo_override = _parse_args(context.args)
        # Pilar 03 §1: os gates e a resolução do remote disparam ``git``/``gh``
        # via subprocess síncrono — isolados em ``to_thread`` para não bloquear
        # o event loop.
        await asyncio.to_thread(ensure_git_repo)
        await asyncio.to_thread(ensure_gh_authenticated)

        if repo_override is not None:
            repo = repo_override
        else:
            repo = await asyncio.to_thread(_resolve_repo_from_git)
        data = await collect_backlog_data(repo)

        return CommandResult.success_result(
            _build_tables(data),
            "rich",
            repo=repo,
            issue_total=data.issue_total,
            pr_total=data.pr_total,
        )

    def get_help(self) -> str:
        return (
            "Exibe contagens de issues e PRs abertas por estado de workflow.\n"
            "Uso: /backlog [--repo owner/name]"
        )
