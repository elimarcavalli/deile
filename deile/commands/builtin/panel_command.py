"""Panel Command — live observability panel (issue #347).

Slash command ``/panel`` que entra no painel TUI ao-vivo. Hotkeys 1/2/3
alternam entre 3 telas (Cluster / Live Session / History). [q] sai.

Implementação delgada: instancia ``ClusterObservabilityClient`` apontando
para o ``pipeline_status_server`` + ``claude_worker_server``, e roda o loop
de tela do subpacote ``deile.ui.panel.observability``.
"""

from __future__ import annotations

import os

from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import emit_audit_event, wrap_command_errors


class PanelCommand(DirectCommand):
    """Open the live observability panel (issue #347)."""

    cli_flag = "--panel"
    cli_help = "Abre o painel TUI ao-vivo (3 telas: Cluster / Live / History)."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig

        config = CommandConfig(
            name="panel",
            description=(
                "Live observability panel — 3 telas (Cluster / Live Session / "
                "History). Hotkeys: 1/2/3 alternam, [q] sai."
            ),
        )
        super().__init__(config)

    @wrap_command_errors("panel", message_template="Falha ao executar /panel: {exc}")
    async def execute(self, context: CommandContext) -> CommandResult:
        from ...security.audit_logger import AuditEventType, SeverityLevel

        emit_audit_event(
            event_type=AuditEventType.COMMAND_EXECUTED,
            severity=SeverityLevel.INFO,
            resource="/panel",
            action="execute",
            details={"args": context.args},
        )
        try:
            from ...ui.panel.observability import client as obs_client
            from ...ui.panel.observability import screens as obs_screens
        except ImportError as exc:
            return CommandResult.error_result(
                f"painel não disponível (módulo ausente: {exc})",
            )

        pipeline_endpoint = os.environ.get(
            "DEILE_PIPELINE_STATUS_ENDPOINT",
            "http://deile-pipeline-status:8768",
        )
        claude_worker_endpoint = os.environ.get(
            "DEILE_CLAUDE_WORKER_ENDPOINT",
            "http://claude-worker:8767",
        )
        pipeline_token = (
            os.environ.get(
                "DEILE_PIPELINE_STATUS_AUTH_TOKEN",
                "",
            ).strip()
            or None
        )
        claude_worker_token = (
            os.environ.get(
                "DEILE_CLAUDE_WORKER_AUTH_TOKEN",
                "",
            ).strip()
            or None
        )

        try:
            cli = obs_client.ClusterObservabilityClient.from_endpoints(
                pipeline_url=pipeline_endpoint,
                pipeline_token=pipeline_token,
                claude_worker_url=claude_worker_endpoint,
                claude_worker_token=claude_worker_token,
            )
        except Exception as exc:  # noqa: BLE001
            return CommandResult.error_result(
                f"falha ao criar ClusterObservabilityClient: {exc}",
            )

        try:
            runner = getattr(obs_screens, "run_panel", None)
            if runner is None:
                return CommandResult.error_result(
                    "deile.ui.panel.observability.screens.run_panel "
                    "não existe — implementação parcial (issue #347 follow-up)",
                )
            await runner(cli)
        except KeyboardInterrupt:
            pass
        except Exception as exc:  # noqa: BLE001
            return CommandResult.error_result(f"painel crashou: {exc}")

        return CommandResult.success_result(
            content="painel encerrado",
            content_type="text",
        )
