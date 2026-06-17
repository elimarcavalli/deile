"""ListFilesTool — listagem de arquivos e diretórios."""
import logging
import re
from pathlib import Path
from typing import Optional

from .._file_listing import _collect_entries, _render_tree
from .._path_resolution import (LocalFileAccessViolation, ResolvedPath, _looks_like_outside_project, _resolve_project_path)
from ..base import SyncTool, ToolContext, ToolResult, ToolStatus

logger = logging.getLogger(__name__)


class ListFilesTool(SyncTool):
    """Ferramenta para listar arquivos"""

    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return "Lists files and directories in a given path"

    @property
    def category(self) -> str:
        return "file"

    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa listagem de arquivos"""
        # Extração robusta dos argumentos com fallbacks múltiplos
        target_path = "."
        recursive = False
        show_hidden = False
        pattern = None

        # 1. Tenta argumentos nomeados primeiro
        if 'path' in context.parsed_args:
            target_path = context.parsed_args['path']
        elif 'directory' in context.parsed_args:
            target_path = context.parsed_args['directory']
        elif 'folder' in context.parsed_args:
            target_path = context.parsed_args['folder']
        elif 'dir' in context.parsed_args:
            target_path = context.parsed_args['dir']

        recursive = context.parsed_args.get("recursive", False)
        show_hidden = context.parsed_args.get("show_hidden", False)
        pattern = context.parsed_args.get("pattern")

        # 2. Fallback para argumentos posicionais
        if target_path == "." and len(context.parsed_args) >= 1:
            args_values = list(context.parsed_args.values())
            potential_path = args_values[0]
            if isinstance(potential_path, str) and potential_path != "True" and potential_path != "False":
                target_path = potential_path

        # 3. Fallback para parsing do user_input
        if target_path == ".":
            user_input = context.user_input.lower()

            # Padrões para extrair path
            path_patterns = [
                r"list\s+files?\s+in\s+['\"]?([^'\"]+?)['\"]?",
                r"list\s+['\"]?([^'\"]+?)['\"]?",
                r"files?\s+in\s+['\"]?([^'\"]+?)['\"]?",
                r"directory\s+['\"]?([^'\"]+?)['\"]?",
                r"folder\s+['\"]?([^'\"]+?)['\"]?"
            ]

            for pattern_regex in path_patterns:
                match = re.search(pattern_regex, user_input)
                if match:
                    extracted_path = match.group(1).strip()
                    # Reject regex-capture artifacts: lazy `[^'"]+?` can match a
                    # single letter from a downstream word (e.g. "f" from "for").
                    # Real paths are either explicit (./, /, ~) or have length>1.
                    is_explicit_root = extracted_path in {".", "/", "~"}
                    is_too_short = len(extracted_path) < 2 and not is_explicit_root
                    is_filler_word = extracted_path.lower() in {"files", "file", "directory", "folder"}
                    if not (is_too_short or is_filler_word):
                        target_path = extracted_path
                        logger.debug(f"ListFilesTool: Extracted path from user_input: {target_path}")
                        break

        # Resolve the LLM-supplied target_path through the canonical resolver
        # so we capture the normalization ``note`` (e.g. "leading '/' stripped"
        # or "@ prefix stripped") AND surface sandbox violations cleanly.
        #
        # IMPORTANT: when the LLM EXPLICITLY supplies a path that violates the
        # sandbox (``../parent_repo/.github`` or system-absolute outside CWD),
        # we MUST surface that violation — never silently fall back to CWD,
        # which would list unrelated content and mislead the model into a
        # tighter loop. Fallbacks are only useful when the LLM omitted the
        # argument entirely (target_path stayed at default "." which always
        # resolves cleanly).
        full_path = None
        resolved_obj: Optional[ResolvedPath] = None
        working_dir = Path(context.working_directory).resolve()

        logger.debug(f"ListFilesTool - target_path: {target_path}, working_directory: {context.working_directory}")

        try:
            resolved_obj = _resolve_project_path(
                target_path, context.working_directory
            )
            full_path = Path(resolved_obj.absolute)
            logger.debug(f"ListFilesTool - validation successful: {full_path}")
        except LocalFileAccessViolation as e:
            logger.debug(f"ListFilesTool - sandbox violation for {target_path!r}: {e}")
            return ToolResult.error_result(
                message=str(e),
                error=e,
            )
        except Exception as e:
            logger.debug(f"ListFilesTool - unexpected resolver error for {target_path!r}: {e}")
            # Fall through to the working_directory fallback below — this
            # branch should be unreachable in normal operation, but keeping
            # the safety net avoids a hard 500 on resolver bugs.
            full_path = working_dir

        try:

            if not full_path.exists():
                # Surface the normalization note (e.g. "leading '/' stripped")
                # AND a bash-execute hint when the user clearly asked for a
                # system-absolute or parent-relative path. Without this, the
                # LLM gets only "Path not found: <garbage>" and loops on the
                # same broken call (observed in the second-run trace).
                hint = ""
                if resolved_obj is not None and resolved_obj.note:
                    hint += (
                        f" (input was {resolved_obj.input!r} → "
                        f"{resolved_obj.note})"
                    )
                if _looks_like_outside_project(target_path):
                    hint += (
                        ". For paths OUTSIDE the project working directory "
                        "(parent repo, sibling project, /etc/, ~/...), use "
                        "`bash_execute` (e.g. `ls <abs_path>` or "
                        "`cat <abs_path>`) — bash_execute has no "
                        "working-directory sandbox."
                    )
                return ToolResult.error_result(
                    message=f"Path not found: {target_path}{hint}",
                    error=FileNotFoundError(f"Path '{target_path}' not found")
                )

            files_info = _collect_entries(
                full_path,
                working_dir,
                recursive=recursive,
                show_hidden=show_hidden,
                pattern=pattern,
            )

            rich_display = _render_tree(target_path, files_info)

            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=files_info,
                message=f"Found {len(files_info)} items in {target_path}",
                metadata={
                    "target_path": str(full_path),
                    "recursive": recursive,
                    "show_hidden": show_hidden,
                    "pattern": pattern,
                    "total_items": len(files_info),
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
                message=f"Error listing files in {target_path}: {str(e)}",
                error=e
            )
