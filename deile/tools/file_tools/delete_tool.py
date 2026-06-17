"""DeleteFileTool — deleção de arquivos e diretórios com precauções."""

import logging
from pathlib import Path

from ...core.exceptions import ValidationError
from .._path_resolution import (
    _PATH_ARG_KEYS_FALLBACK,
    _PATH_ARG_KEYS_PRIMARY,
    LocalFileAccessViolation,
    _extract_path_arg,
    _not_found_message,
    _resolve_project_path,
)
from ..base import SyncTool, ToolContext, ToolResult, ToolStatus

logger = logging.getLogger(__name__)


class DeleteFileTool(SyncTool):
    """Ferramenta para deletar arquivos (com precauções)"""

    @property
    def name(self) -> str:
        return "delete_file"

    @property
    def description(self) -> str:
        return "Deletes a file or directory (use with caution)"

    @property
    def category(self) -> str:
        return "file"

    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa deleção de arquivo"""
        # DeleteFileTool's historical precedence is two-stage: ``file_path``/
        # ``path`` first, then ``file``/``filename``/``filepath`` as fallback.
        # We preserve that ordering by calling the helper twice with the
        # per-tier keys, matching the pre-DRY behavior in commit 0ea6af1.
        file_path = _extract_path_arg(context.parsed_args, keys=_PATH_ARG_KEYS_PRIMARY)
        if not file_path:
            file_path = _extract_path_arg(context.parsed_args, keys=_PATH_ARG_KEYS_FALLBACK)
        force = context.parsed_args.get("force", False)

        if not file_path:
            return ToolResult.error_result(
                message="No file path provided. Please specify a file to delete.",
                error=ValidationError("file_path is required")
            )

        # Medidas de segurança
        if not force:
            # Verifica se não é um arquivo crítico
            dangerous_patterns = ['.env', 'config', '.git', '__pycache__']
            if any(pattern in file_path.lower() for pattern in dangerous_patterns):
                return ToolResult.error_result(
                    message=f"Refusing to delete potentially important file: {file_path}. Use force=True if needed.",
                    error=PermissionError("Safety check failed")
                )

        try:
            resolved = _resolve_project_path(file_path, context.working_directory)
            full_path = Path(resolved.absolute)

            if not full_path.exists():
                return ToolResult.error_result(
                    message=_not_found_message(
                        resolved,
                        file_path,
                        include_bash_hint=True,
                        bash_verb="rm",
                    ),
                    error=FileNotFoundError(f"File '{resolved.relative_to_cwd}' not found"),
                )

            # Registra informações antes de deletar
            was_directory = full_path.is_dir()
            size = full_path.stat().st_size if full_path.is_file() else 0

            # Deleta
            if was_directory:
                # Remove diretório recursivamente
                import shutil
                shutil.rmtree(full_path)
            else:
                full_path.unlink()

            # Prepara display rico
            item_type = "directory" if was_directory else "file"
            rich_display = f"● delete_file({file_path})\n  ⎿ Deleted [red]{file_path}[/red] ({item_type})"

            return ToolResult(
                status=ToolStatus.SUCCESS,
                message=f"Successfully deleted {'directory' if was_directory else 'file'}: {file_path}",
                metadata={
                    "deleted_path": str(full_path),
                    "was_directory": was_directory,
                    "size": size,
                    "force": force,
                    "rich_display": rich_display
                }
            )

        except LocalFileAccessViolation as e:
            return ToolResult.error_result(
                message=str(e),
                error=e
            )
        except Exception as e:
            return ToolResult.error_result(
                message=f"Error deleting {file_path}: {str(e)}",
                error=e
            )
