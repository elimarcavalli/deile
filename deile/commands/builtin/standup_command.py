"""Standup Command — narrativa do dia (commits + PRs + issues)."""

import asyncio
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from rich.panel import Panel
from rich.text import Text

from ...core.exceptions import CommandError
from ...core.models.base import ModelMessage
from ...core.models.router import get_model_router
from ...orchestration.pipeline.github_client import GitHubClient
from ..base import CommandContext, CommandResult, DirectCommand
from ._git_helpers import ensure_gh_authenticated, ensure_git_repo
from ._shared import emit_audit_event, wrap_command_errors


@dataclass
class StandupData:
    since_spec: str
    since_iso: str
    commits: List[Dict[str, str]] = field(default_factory=list)
    prs: List[Dict[str, Any]] = field(default_factory=list)
    issues: List[Dict[str, Any]] = field(default_factory=list)


def parse_since(duration: str) -> timedelta:
    if not duration:
        raise CommandError("Duração vazia.")
    match = re.match(r"^\s*(\d+)([hdwHDW])\s*$", duration)
    if not match:
        raise CommandError(f"Duração inválida: {duration}. Use formato como 24h, 3d, 1w.")
    val, unit = int(match.group(1)), match.group(2).lower()
    if val == 0:
        raise CommandError("Duração não pode ser zero.")
    if unit == "h":
        return timedelta(hours=val)
    elif unit == "d":
        return timedelta(days=val)
    elif unit == "w":
        return timedelta(weeks=val)
    raise CommandError(f"Unidade inválida: {unit}")


def parse_args(args: str) -> str:
    args = args.strip()
    if not args:
        return "24h"
    if args.startswith("--since="):
        return args.split("=", 1)[1].strip()
    if args.startswith("--since "):
        return args.split(" ", 1)[1].strip()
    if args.startswith("--"):
        raise CommandError(f"Flag desconhecida: {args}")
    return "24h"


# Reconhece tanto SSH (``git@github.com:owner/name(.git)?``) quanto HTTPS
# (``https://github.com/owner/name(.git)?``). Em ambos os casos captura o
# par ``owner/name`` — único shape aceito por :class:`GitHubClient`.
_REMOTE_RE = re.compile(
    r"(?:git@github\.com:|https?://github\.com/)"
    r"(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?$"
)


def _resolve_repo_from_git(cwd: Optional[str] = None) -> str:
    """Inferir ``owner/name`` a partir de ``git remote get-url origin``.

    O ``/standup`` precisa saber qual repo GitHub consultar via ``gh``; ao
    invés de exigir configuração explícita, derivamos do ``remote origin``
    do repositório git atual. Aceita os dois formatos canônicos (HTTPS e
    SSH). Levanta :class:`CommandError` em PT-BR quando o remote não
    existe ou não casa com um repo do GitHub — o caller decide se isso
    interrompe a execução.
    """
    try:
        res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise CommandError(
            "não consegui detectar o repo GitHub a partir do remote origin"
        ) from exc

    if res.returncode != 0:
        raise CommandError(
            "não consegui detectar o repo GitHub a partir do remote origin"
        )

    url = (res.stdout or "").strip()
    match = _REMOTE_RE.search(url)
    if not match:
        raise CommandError(
            "não consegui detectar o repo GitHub a partir do remote origin"
        )
    return match.group("repo")


def collect_commits(since_iso: str) -> List[Dict[str, str]]:
    res = subprocess.run(
        ["git", "log", f"--since={since_iso}", "--format=%h\x1f%an\x1f%s"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return []
    commits = []
    for line in res.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) >= 3:
            commits.append({"hash": parts[0], "author": parts[1], "title": parts[2]})
    return commits


async def collect_prs(client: GitHubClient, since_iso: str) -> List[Dict[str, Any]]:
    """Lista PRs atualizados desde ``since_iso`` via :class:`GitHubClient`.

    A integração com ``gh`` (subprocess, JSON, normalização de autor) vive
    no adapter da camada de pipeline — o comando só consome a forma já
    normalizada. Falha do ``gh`` é logada pelo adapter e devolvida como
    lista vazia (preservando o comportamento original do command).
    """
    return await client.list_prs_updated_since(since_iso)


async def collect_issues(client: GitHubClient, since_iso: str) -> List[Dict[str, Any]]:
    """Lista issues atualizadas desde ``since_iso`` via :class:`GitHubClient`."""
    return await client.list_issues_updated_since(since_iso)


async def collect_standup_data(since_spec: str) -> StandupData:
    ensure_git_repo()
    ensure_gh_authenticated()

    delta = parse_since(since_spec)
    since_date = datetime.now(timezone.utc) - delta
    since_iso = since_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    repo = _resolve_repo_from_git()
    client = GitHubClient(repo)

    # ``collect_commits`` ainda usa ``git`` síncrono — mantém em thread para
    # não bloquear o loop; os métodos do GitHubClient já são nativamente async.
    commits = await asyncio.to_thread(collect_commits, since_iso)
    prs = await collect_prs(client, since_iso)
    issues = await collect_issues(client, since_iso)

    return StandupData(
        since_spec=since_spec,
        since_iso=since_iso,
        commits=commits,
        prs=prs,
        issues=issues,
    )


def build_prompt(data: StandupData) -> str:
    prompt = f"Gere um resumo de standup em PT-BR para as últimas {data.since_spec} (desde {data.since_iso}).\n"
    prompt += "O resumo deve ter no máximo 8 linhas no corpo principal, seguido de bullets de Destaques.\n\n"

    prompt += f"Commits ({len(data.commits)}):\n"
    if not data.commits:
        prompt += "- (nenhum)\n"
    for c in data.commits:
        prompt += f"- {c['hash']} por {c['author']}: {c['title']}\n"

    prompt += f"\nPull Requests ({len(data.prs)}):\n"
    if not data.prs:
        prompt += "- (nenhuma)\n"
    for pr in data.prs:
        prompt += f"- #{pr['number']} [{pr['state']}] por {pr['author']}: {pr['title']}\n"

    prompt += f"\nIssues ({len(data.issues)}):\n"
    if not data.issues:
        prompt += "- (nenhuma)\n"
    for issue in data.issues:
        prompt += f"- #{issue['number']} [{issue['state']}] por {issue['author']}: {issue['title']}\n"

    return prompt


async def generate_narrative(prompt: str) -> str:
    router = get_model_router()
    provider = await router.select_provider()
    if not provider:
        raise CommandError("Nenhum provedor de IA disponível para gerar o standup.")

    messages = [ModelMessage(role="user", content=prompt)]
    response = await provider.generate(messages, system_instruction="Você é um assistente técnico que gera resumos de standup em PT-BR.")
    return response.content


class StandupCommand(DirectCommand):
    """Gera um standup diário com base em commits, PRs e issues."""

    cli_flag = "--standup"
    cli_help = "Gera um standup diário (commits, PRs, issues)."
    cli_requires_provider = True

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="standup",
            description="Gera um standup diário com base em commits, PRs e issues.",
        )
        super().__init__(config)

    @wrap_command_errors("standup", message_template="Falha ao executar /{name}: {exc}")
    async def execute(self, context: CommandContext) -> CommandResult:
        self._emit_audit_event(context)

        since_spec = parse_args(context.args)
        # collect_standup_data é async: chama GitHubClient diretamente
        # e isola o git (síncrono) em asyncio.to_thread internamente.
        data = await collect_standup_data(since_spec)
        prompt = build_prompt(data)

        narrative = await generate_narrative(prompt)

        metadata = {
            "since_spec": data.since_spec,
            "commit_count": len(data.commits),
            "pr_count": len(data.prs),
            "issue_count": len(data.issues),
        }

        return CommandResult.success_result(
            Panel(Text(narrative), title=f"📰 DEILE — Standup das últimas {since_spec}", border_style="blue"),
            "rich",
            **metadata,
        )

    def _emit_audit_event(self, context: CommandContext) -> None:
        from ...security.audit_logger import AuditEventType, SeverityLevel
        emit_audit_event(
            event_type=AuditEventType.COMMAND_EXECUTED,
            severity=SeverityLevel.INFO,
            resource="/standup",
            action="execute",
            details={"args": context.args},
        )

    def get_help(self) -> str:
        return "Gera um standup diário (commits, PRs, issues).\\nUso: /standup [--since=24h|3d|1w]"
