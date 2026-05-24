"""Mapa do argumento "primário" de cada tool — usado por renderizadores que
mostram só o valor dominante (ex.: ``bash_execute`` mostra o ``command`` cru,
não ``command='ls -la'``).

Centralizado em ``deile/common/`` para evitar drift entre o renderer do
stream principal (``deile/ui/streaming_renderer.py``) e o painel de
sub-agentes (``deile/orchestration/subagents/runner._format_tool_inline``).
"""

from __future__ import annotations

from typing import Dict

TOOL_PRIMARY_ARG_KEYS: Dict[str, str] = {
    "bash_execute": "command",
    "python_execute": "code",
    "read_file": "file_path",
    "write_file": "file_path",
    "list_files": "path",
    "delete_file": "file_path",
    "edit_file": "file_path",
}
