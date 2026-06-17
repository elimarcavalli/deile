"""Pacote deile.tools.file_tools — re-export shim estável.

Mantém a superfície pública original de `deile/tools/file_tools.py` após a divisão
em subpacotes por-tool. Todo import canônico `from deile.tools.file_tools import X`
continua válido; `auto_discover` encontra as 5 tools via `dir()` deste pacote.
"""

from .delete_tool import DeleteFileTool
from .edit_tool import EditFileTool
from .list_tool import ListFilesTool
from .read_tool import ReadFileTool
from .write_tool import WriteFileTool
from .._path_resolution import (
    LocalFileAccessViolation,
    ResolvedPath,
    _looks_like_outside_project,
    _post_write_validation_hint,
    _resolve_project_path,
    _validate_path_within_working_directory,
)

__all__ = [
    # Tool classes.
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "ListFilesTool",
    "DeleteFileTool",
    # Re-exports from `_path_resolution` (kept stable for tests/callers).
    "LocalFileAccessViolation",
    "ResolvedPath",
    "_looks_like_outside_project",
    "_post_write_validation_hint",
    "_resolve_project_path",
    "_validate_path_within_working_directory",
]
