"""Backlog Command — contagens agregadas de issues/PRs abertas por estado de workflow.

Slash command ``/backlog`` que imprime, em duas tabelas Rich, contagens
agregadas das issues e PRs abertas agrupadas pelos rótulos de estado do
pipeline autônomo (``~workflow:*`` e ``~review:*``).

Uso:
    /backlog                    # repositório resolvido via git remote origin
    /backlog --repo owner/name  # override explícito

Responsabilidade única: parsing de argumentos, coleta via ``gh`` CLI e
renderização Rich.  A coleta é delegada para :func:`collect_backlog_data`
(importável/mockável nos testes).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from rich.console import Group
from rich.table import Table

from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand
from ._git_helpers import ensure_gh_authenticated, ensure_git_repo
from ._shared import emit_audit_event, wrap_command_errors
from ._standup_collectors import _resolve_repo_from_git

# ---------------------------------------------------------------------------
# Constants — bucket definitions
# ---------------------------------------------------------------------------

_WORKFLOW_PREFIX = "~workflow:"
_REVIEW_PREFIX = "~review:"
_BLOCKED_LABEL = "~workflow:bloqueada"

# Canonical ordered buckets for issues (mirrors WORKFLOW_LABELS in labels.py)
ISSUE_BUCKETS: Tuple[str, ...] = (
    "nova",
    "em_revisao",
    "em_refinamento",
    "em_arquitetura",
    "aguardando_stakeholder",
    "revisada",
    "em_implementacao",
    "em_pr",
    "decomposta",
    "bloqueada",
)

# Canonical ordered buckets for PRs
PR_BUCKETS: Tuple[str, ...] = (
    "pendente",
    "em_andamento",
    "concluida",
    "bloqueada",
)

_SEM_WORKFLOW = "(sem ~workflow:*)"
_SEM_REVIEW = "(sem ~review:* e sem bloqueada)"


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
            repo = args[len(prefix):].strip()
            if not repo or "/" not in repo:
                raise CommandError(
                    f"Repo inválido: {repo!r}. Use o formato owner/name."
                )
            return repo
    raise CommandError(
        f"Flag desconhecida: {args!r}. Uso: /backlog [--repo owner/name]"
    )


# ---------------------------------------------------------------------------
# Bucket assignment — precedence rules
# ---------------------------------------------------------------------------

def _bucket_issue(labels: Tuple[str, ...]) -> str:
    """Assign an open issue to its backlog bucket.

    Precedence (mirrors ``_derive_workflow`` in infra/k8s/_panel_data.py plus
    the ``aguardando_stakeholder`` overlay rule from the issue spec):

    1. ``~workflow:bloqueada`` → **bloqueada** (terminal, overrides everything)
    2. ``~workflow:aguardando_stakeholder`` → **aguardando_stakeholder** (overlay)
    3. First ``~workflow:*`` in canonical ``ISSUE_BUCKETS`` order → nominal bucket
    4. No ``~workflow:*`` present → ``_SEM_WORKFLOW``
    """
    workflow_states = [
        lbl[len(_WORKFLOW_PREFIX):]
        for lbl in labels
        if lbl.startswith(_WORKFLOW_PREFIX)
    ]
    if not workflow_states:
        return _SEM_WORKFLOW
    if "bloqueada" in workflow_states:
        return "bloqueada"
    if "aguardando_stakeholder" in workflow_states:
        return "aguardando_stakeholder"
    for bucket in ISSUE_BUCKETS:
        if bucket in workflow_states:
            return bucket
    return workflow_states[0]


def _bucket_pr(labels: Tuple[str, ...]) -> str:
    """Assign an open PR to its backlog bucket.

    Precedence:
    1. ``~workflow:bloqueada`` → **bloqueada** (overrides any ``~review:*``)
    2. First ``~review:*`` → nominal bucket (e.g. ``pendente``, ``em_andamento``)
    3. Neither → ``_SEM_REVIEW``
    """
    if _BLOCKED_LABEL in labels:
        return "bloqueada"
    for lbl in labels:
        if lbl.startswith(_REVIEW_PREFIX):
            return lbl[len(_REVIEW_PREFIX):]
    return _SEM_REVIEW


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

@dataclass
class BacklogData:
    """Aggregated counts for the two backlog tables."""
    repo: str
    issue_counts: Dict[str, int] = field(default_factory=dict)
    pr_counts: Dict[str, int] = field(default_factory=dict)
    issue_total: int = 0
    pr_total: int = 0


def _run_gh_list(cmd: List[str], *, timeout: int = 30) -> List[dict]:
    """Run a ``gh`` list command and return parsed JSON.

    Raises :class:`CommandError` on subprocess failure, timeout, or malformed
    output so the caller never needs to inspect raw subprocess state.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CommandError(
            f"Timeout ({timeout}s) ao consultar o GitHub. Tente novamente."
        )
    except FileNotFoundError:
        raise CommandError("gh CLI não encontrado. Instale o GitHub CLI.")
    if result.returncode != 0:
        raise CommandError(f"Falha no gh CLI: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError(f"Resposta inesperada do gh CLI: {exc}") from exc


def _extract_label_names(raw_labels: list) -> Tuple[str, ...]:
    """Normalise the two label shapes ``gh`` may return.

    ``gh issue list --json labels`` returns objects ``{"id":…,"name":"…"}``;
    ``gh pr list --json labels`` may return plain strings in some versions.
    Both are normalised to a tuple of name strings.
    """
    names = []
    for lbl in raw_labels or []:
        if isinstance(lbl, dict):
            names.append(lbl.get("name", ""))
        else:
            names.append(str(lbl))
    return tuple(n for n in names if n)


async def collect_backlog_data(repo: str) -> BacklogData:
    """Fetch all open issues/PRs for *repo* and aggregate by workflow bucket.

    Makes two parallel ``gh`` CLI calls (issues + PRs) each with ``--limit
    1000`` which is sufficient for any active project.  Repositories with
    more than 1 000 open items will produce a count capped at 1 000 — an
    edge case documented here rather than silently truncated.
    """
    gh = shutil.which("gh")
    if not gh:
        raise CommandError("gh CLI não encontrado.")

    issues_cmd = [
        gh, "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--limit", "1000",
        "--json", "number,labels",
    ]
    prs_cmd = [
        gh, "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--limit", "1000",
        "--json", "number,labels",
    ]

    issues_raw, prs_raw = await asyncio.gather(
        asyncio.to_thread(_run_gh_list, issues_cmd),
        asyncio.to_thread(_run_gh_list, prs_cmd),
    )

    # Bucketize issues
    issue_counts: Dict[str, int] = {b: 0 for b in ISSUE_BUCKETS}
    for item in issues_raw:
        labels = _extract_label_names(item.get("labels", []))
        bucket = _bucket_issue(labels)
        issue_counts[bucket] = issue_counts.get(bucket, 0) + 1

    # Bucketize PRs
    pr_counts: Dict[str, int] = {b: 0 for b in PR_BUCKETS}
    for item in prs_raw:
        labels = _extract_label_names(item.get("labels", []))
        bucket = _bucket_pr(labels)
        pr_counts[bucket] = pr_counts.get(bucket, 0) + 1

    return BacklogData(
        repo=repo,
        issue_counts=issue_counts,
        pr_counts=pr_counts,
        issue_total=len(issues_raw),
        pr_total=len(prs_raw),
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

    @wrap_command_errors("backlog", message_template="Falha ao executar /backlog: {exc}")
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
        ensure_git_repo()
        ensure_gh_authenticated()

        repo = repo_override if repo_override is not None else _resolve_repo_from_git()
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
