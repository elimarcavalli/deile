"""Integration test: auto-discovery wiring for `deile.tools.file_tools` package.

Verifica que converter o módulo em pacote não quebrou a auto-discovery:
`discover_tools_in_package` varre `dir(deile.tools.file_tools)` e registra as 5
tools exatamente uma vez cada (sem duplicata, sem omissão).

Fecha o gap de fiação identificado na issue #747 (GC #596): os ~135 testes de import
direto provam que o shim re-exporta as classes, mas não que `auto_discover()` as
descobre e registra pelos seus `name`.
"""

from __future__ import annotations

import pytest

from deile.tools.discovery import discover_tools_in_package
from deile.tools.registry import ToolRegistry


_EXPECTED_FILE_TOOL_NAMES = {
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
    "delete_file",
}


@pytest.mark.integration
def test_file_tools_discovery_registers_all_five() -> None:
    """discover_tools_in_package registra as 5 file-tools exatamente uma vez."""
    registry = ToolRegistry()
    count = discover_tools_in_package(registry, "deile.tools.file_tools")

    registered_names = set(registry._tools.keys()) if hasattr(registry, "_tools") else {
        t.name for t in registry
    }

    # Todas as 5 tools esperadas devem estar registradas.
    assert _EXPECTED_FILE_TOOL_NAMES.issubset(registered_names), (
        f"Missing tools: {_EXPECTED_FILE_TOOL_NAMES - registered_names}"
    )

    # Exatamente 5 tools registradas nesta chamada (sem duplicata, sem omissão).
    assert count == 5, (
        f"Expected 5 tools registered, got {count}. "
        f"Registered names: {registered_names}"
    )
