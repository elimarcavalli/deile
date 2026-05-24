"""Standup Command — narrativa do dia (commits + PRs + issues).

Responsabilidade única deste módulo: dispatch + render Rich + LLM call.
Parsing de CLI e coleta de git/gh foram extraídos para
``_standup_collectors`` (Pilar 03 §1 — coleta I/O síncrona isolada;
Pilar 03 §8 — single responsibility por unidade).

Re-exportamos os símbolos coletores no namespace deste módulo para
preservar a superfície pública que ``test_standup_command.py`` importa
diretamente — assim a refatoração é puramente interna.
"""

import asyncio
import shutil  # noqa: F401 — re-exportado para `monkeypatch.setattr(sc.shutil, ...)`
import subprocess  # noqa: F401 — re-exportado para `monkeypatch.setattr(sc.subprocess, ...)`

from rich.panel import Panel
from rich.text import Text

from ...core.exceptions import CommandError
from ...core.models.base import ModelMessage
from ...core.models.router import get_model_router
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import emit_audit_event, wrap_command_errors
from ._standup_collectors import (StandupData, _ensure_gh_available,
                                  _ensure_git_repo, build_prompt,
                                  collect_commits, collect_issues, collect_prs,
                                  collect_standup_data, parse_args,
                                  parse_since)

__all__ = [
    "StandupCommand",
    "StandupData",
    "_ensure_gh_available",
    "_ensure_git_repo",
    "build_prompt",
    "collect_commits",
    "collect_issues",
    "collect_prs",
    "collect_standup_data",
    "generate_narrative",
    "get_model_router",
    "parse_args",
    "parse_since",
]


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
        # `collect_standup_data` runs `git rev-parse`, `gh auth status`,
        # `git log` e duas chamadas `gh <verb> list` em sequência — todo
        # subprocess síncrono. Off-loading para uma worker thread evita
        # bloquear o event loop (pilar 03 §1).
        data = await asyncio.to_thread(collect_standup_data, since_spec)
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
