"""Testes para ApplyCommand — foco no bug de repr Rich (issue #691).

O bug original: _show_applicable_patches() passava f"{table}\n\n{usage_panel}"
como content com content_type="rich". str(Table()) retorna o repr do objeto,
não o conteúdo renderizado. A correção usa rich.console.Group.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console, Group

from deile.commands.base import CommandContext
from deile.commands.builtin.apply_command import ApplyCommand


def _make_context(args: str = "") -> CommandContext:
    return CommandContext(user_input=f"/apply {args}", args=args)


@pytest.mark.unit
async def test_show_patches_returns_rich_group_not_str(tmp_path):
    """Quando há patches, result.content deve ser um renderable Rich, não uma str com repr."""
    patch_file = tmp_path / "test.patch"
    patch_file.write_text("--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n")

    cmd = ApplyCommand()

    with patch("deile.commands.builtin.apply_command.PATCHES_DIR", tmp_path), \
         patch("deile.commands.builtin._shared.PATCHES_DIR", tmp_path):
        result = await cmd.execute(_make_context())

    assert result.success
    content = result.content

    # Deve ser um renderable Rich — Group implementa __rich_console__
    assert hasattr(content, "__rich_console__") or hasattr(content, "__rich__"), (
        f"result.content deve ser um renderable Rich, mas é {type(content)!r}"
    )

    # Não deve ser uma str (a falha original era isso)
    assert not isinstance(content, str), (
        "result.content não deve ser str — f-string de objeto Rich retorna repr"
    )

    # Não deve conter o repr do objeto Table
    if isinstance(content, str):
        assert "<rich.table.Table object" not in content
        assert "<rich.panel.Panel object" not in content


@pytest.mark.unit
async def test_show_patches_renders_filename(tmp_path):
    """O output renderizado deve conter o nome do arquivo de patch."""
    patch_file = tmp_path / "my_feature.patch"
    patch_file.write_text("--- a\n+++ b\n")

    cmd = ApplyCommand()

    with patch("deile.commands.builtin.apply_command.PATCHES_DIR", tmp_path), \
         patch("deile.commands.builtin._shared.PATCHES_DIR", tmp_path):
        result = await cmd.execute(_make_context())

    assert result.success

    # Renderiza via Console de captura (sem terminal)
    console = Console(width=120)
    with console.capture() as cap:
        console.print(result.content)
    rendered = cap.get()

    assert "my_feature.patch" in rendered, (
        f"Nome do patch deve aparecer no output renderizado, mas não está em:\n{rendered}"
    )


@pytest.mark.unit
async def test_no_patches_returns_panel_not_group(tmp_path):
    """Quando não há patches, deve retornar um Panel direto (caminho sem bug)."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    cmd = ApplyCommand()

    with patch("deile.commands.builtin.apply_command.PATCHES_DIR", empty_dir), \
         patch("deile.commands.builtin._shared.PATCHES_DIR", empty_dir):
        result = await cmd.execute(_make_context())

    assert result.success
    # O ramo sem patches já estava correto — retorna Panel diretamente
    from rich.panel import Panel
    assert isinstance(result.content, Panel)
