"""WriteFileTool — escrita atômica de arquivos com read-back validation."""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...core.exceptions import ValidationError
from .._path_resolution import (
    _PATH_ARG_KEYS_WRITE,
    LocalFileAccessViolation,
    ResolvedPath,
    _apply_post_write_hint,
    _extract_path_arg,
    _resolve_project_path,
)
from ..base import SyncTool, ToolContext, ToolResult, ToolStatus

logger = logging.getLogger(__name__)


class WriteFileTool(SyncTool):
    """Ferramenta para escrita de arquivos"""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Writes content to a file"

    @property
    def category(self) -> str:
        return "file"

    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa escrita de arquivo"""
        # DEBUG: Log dos argumentos recebidos
        logger.debug(f"WriteFileTool - parsed_args: {context.parsed_args}")
        logger.debug(f"WriteFileTool - user_input: {context.user_input}")

        # Extração robusta dos argumentos com fallbacks múltiplos
        file_path = None
        content = None
        # Default permissivo: quando o usuário pede para alterar/criar um
        # arquivo, a tool não pergunta — apenas garante fidelidade total via
        # escrita atômica + read-back (ver _atomic_write_text abaixo).
        overwrite = context.parsed_args.get("overwrite", True)

        # 1. Tenta argumentos nomeados primeiro. WriteFileTool's original
        # precedence is ``file_path > filename > path > file > filepath`` —
        # pinned via ``_PATH_ARG_KEYS_WRITE`` so the centralized helper does
        # not silently reshuffle the synonym order.
        file_path = _extract_path_arg(context.parsed_args, keys=_PATH_ARG_KEYS_WRITE)

        if 'content' in context.parsed_args:
            content = context.parsed_args['content']
        elif 'text' in context.parsed_args:
            content = context.parsed_args['text']
        elif 'data' in context.parsed_args:
            content = context.parsed_args['data']

        # 2. Fallback para argumentos posicionais
        if not file_path and len(context.parsed_args) >= 2:
            args_values = list(context.parsed_args.values())
            file_path = args_values[0]
            content = args_values[1]

        # 3. Fallback para parsing do user_input
        if not file_path or content is None:
            user_input = context.user_input.lower()

            # Padrões para extrair file_path
            path_patterns = [
                r"write\s+(?:file\s+)?['\"]?([^'\"]+?)['\"]?\s+with",
                r"create\s+(?:file\s+)?['\"]?([^'\"]+?)['\"]?\s+with",
                r"(?:file|arquivo)\s+['\"]?([^'\"]+?)['\"]?",
                r"['\"]([^'\"]+?\.\w+)['\"]"
            ]

            for pattern in path_patterns:
                match = re.search(pattern, user_input)
                if match and not file_path:
                    file_path = match.group(1).strip()
                    logger.debug(f"WriteFileTool: Extracted file_path from user_input: {file_path}")
                    break

            # Padrões para extrair content
            content_patterns = [
                r"with\s+content\s+['\"]([^'\"]*)['\"]",
                r"with\s+['\"]([^'\"]*)['\"]",
                r"content\s+['\"]([^'\"]*)['\"]"
            ]

            for pattern in content_patterns:
                match = re.search(pattern, context.user_input)
                if match and content is None:
                    content = match.group(1)
                    logger.debug(f"WriteFileTool: Extracted content from user_input: {content[:50] if content else 'None'}...")
                    break

        # Valida que content foi fornecido
        if content is None:
            return ToolResult.error_result(
                message="No content provided",
                error=ValidationError("content is required")
            )

        if not file_path:
            return ToolResult.error_result(
                message="No file path provided. Please specify a file to write.",
                error=ValidationError("file_path is required")
            )

        try:
            # Resolve and validate the path. The resolver normalizes common
            # LLM-confusion patterns (leading '/', '@', '~', backslashes,
            # Windows drives) and returns a structured result we surface to
            # the caller so the model can never alucinar where the file went.
            resolved = _resolve_project_path(file_path, context.working_directory)
            full_path = Path(resolved.absolute)

            # Verifica se arquivo já existe
            existed_before = full_path.exists()
            if existed_before and not overwrite:
                return ToolResult.error_result(
                    message=(
                        f"File already exists: {resolved.relative_to_cwd}. "
                        f"Use overwrite=True to replace."
                    ),
                    error=FileExistsError(
                        f"File '{resolved.relative_to_cwd}' already exists"
                    ),
                )

            # Cria diretório pai se necessário
            full_path.parent.mkdir(parents=True, exist_ok=True)

            # Escreve atomicamente: garante que o destino só é substituído
            # depois que o conteúdo está completo no disco e que os bytes
            # gravados batem byte-a-byte com o que o caller pediu. Em caso
            # de qualquer falha no caminho, o arquivo original (se existia)
            # permanece íntegro.
            self._atomic_write_text(full_path, content)

            # Prepara display rico
            lines = content.splitlines()
            line_count = len(lines)

            # Preview do arquivo criado (primeiras 10 linhas)
            preview_lines = []
            for i, line in enumerate(lines[:10]):
                line_num = str(i + 1)
                preview_lines.append(f"        {line_num}     {line}")

            status_color = "green" if not existed_before else "yellow"
            action = "Created" if not existed_before else "Updated"
            rich_display = (
                f"● write_file({resolved.relative_to_cwd})\n"
                f"  ⎿ {action} [{status_color}]{resolved.relative_to_cwd}[/{status_color}] "
                f"with {line_count} lines\n" + "\n".join(preview_lines)
            )
            if line_count > 10:
                rich_display += "\n        ..."

            message_parts = [
                f"Wrote {len(content)} characters ({line_count} lines) to:",
                f"  file_path: {resolved.absolute}",
                f"  project_relative: {resolved.relative_to_cwd}",
                f"  input_given: {resolved.input}",
            ]
            if resolved.note:
                message_parts.append(
                    f"  ⚠️  PATH_NORMALIZED: {resolved.note} "
                    f"The file is at the resolved_path above — use THAT path "
                    f"in subsequent tool calls (read_file, bash_execute, …), "
                    f"NOT the input you originally sent."
                )

            metadata: Dict[str, Any] = {
                "function_name": "write_file",
                "file_path": resolved.absolute,
                "project_relative_path": resolved.relative_to_cwd,
                "input_path": resolved.input,
                "path_normalization_note": resolved.note,
                "content_length": len(content),
                "overwrite": overwrite,
                "encoding": "utf-8",
                "rich_display": rich_display,
            }

            _apply_post_write_hint(resolved.relative_to_cwd, metadata, message_parts)

            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=resolved.absolute,
                message="\n".join(message_parts),
                metadata=metadata,
            )

        except LocalFileAccessViolation as e:
            return ToolResult.error_result(
                message=str(e),
                error=e
            )
        except Exception as e:
            return ToolResult.error_result(
                message=f"Error writing file {file_path}: {str(e)}",
                error=e
            )

    @staticmethod
    def _atomic_write_text(target: Path, content: str) -> None:
        """Escreve ``content`` em ``target`` de forma atômica e fiel.

        Garantias:

        * **Atomicidade**: o conteúdo é gravado primeiro num arquivo temporário
          no mesmo diretório (mesmo filesystem) e só então renomeado para o
          destino via ``os.replace``. Se algo falhar no meio, o arquivo
          original permanece intacto e o temporário é removido.

        * **Fidelidade**: o ``content`` é codificado como UTF-8 sem BOM e sem
          tradução de newlines, e os bytes do arquivo final são re-lidos e
          comparados byte-a-byte com o que foi pedido. Qualquer divergência
          aborta a operação com :class:`IOError` antes do arquivo final ser
          publicado.

        * **Durabilidade**: ``fsync`` é chamado no temporário antes do rename,
          então um crash de SO pós-rename não deixa o arquivo zero-byte.
        """
        import os
        import tempfile

        payload = content.encode("utf-8")
        target_dir = target.parent
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target_dir), prefix=f".{target.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            # Read-back validation antes do replace: o temporário é descartável
            # e o original ainda existe se falharmos aqui.
            with open(tmp_path, "rb") as fh:
                written = fh.read()
            if written != payload:
                raise IOError(
                    f"Atomic write integrity check failed for {target}: "
                    f"requested {len(payload)} bytes, found {len(written)} on disk"
                )
            os.replace(str(tmp_path), str(target))
        except BaseException:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise
