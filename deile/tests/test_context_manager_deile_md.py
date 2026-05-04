"""Integration tests for ContextManager + DEILEMDLoader (Issue #62 / Feature #64).

Garante que `_build_system_instruction` (path da persona) e
`_build_fallback_system_instruction` (path do fallback) prefixam corretamente
as três camadas DEILE.md à instrução base — e que falha de leitura não
derruba o turno.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core import context_manager as cm_module
from deile.core.context_manager import ContextManager


CORE_MARKER = "CORE_TEST_MARKER_alpha"
USER_MARKER = "USER_TEST_MARKER_beta"
CWD_MARKER = "CWD_TEST_MARKER_gamma"


@pytest.fixture
def deile_md_layout(tmp_path, monkeypatch):
    """Cria layout temporário para as três camadas e injeta paths via monkeypatch."""

    home_dir = tmp_path / "home"
    cwd_dir = tmp_path / "project"
    core_dir = tmp_path / "pkg" / "personas" / "instructions" / "core"
    home_dir.mkdir()
    cwd_dir.mkdir()
    core_dir.mkdir(parents=True)
    (home_dir / ".deile").mkdir()

    core_path = core_dir / "DEILE.md"
    user_path = home_dir / ".deile" / "DEILE.md"
    cwd_path = cwd_dir / "DEILE.md"

    core_path.write_text(CORE_MARKER, encoding="utf-8")
    user_path.write_text(USER_MARKER, encoding="utf-8")
    cwd_path.write_text(CWD_MARKER, encoding="utf-8")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_dir))

    # Redireciona o helper de path do core para o layout temporário.
    monkeypatch.setattr(
        "deile.core.deile_md_loader._core_deile_md_path",
        lambda: core_path,
    )

    return {
        "core": core_path,
        "user": user_path,
        "cwd": cwd_dir,
        "markers": (CORE_MARKER, USER_MARKER, CWD_MARKER),
    }


# ── _prepend_deile_md_layers ────────────────────────────────────────────────


def test_prepend_layers_includes_all_three(deile_md_layout):
    base = "PERSONA_BASE_INSTRUCTION"
    out = cm_module._prepend_deile_md_layers(base, str(deile_md_layout["cwd"]))

    assert CORE_MARKER in out
    assert USER_MARKER in out
    assert CWD_MARKER in out
    assert base in out
    # A persona vem DEPOIS do bloco DEILE.md
    assert out.index(CORE_MARKER) < out.index(base)
    assert out.index(USER_MARKER) < out.index(base)
    assert out.index(CWD_MARKER) < out.index(base)


def test_prepend_layers_preserves_base_when_no_deile_md(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
    (tmp_path / "h").mkdir()
    monkeypatch.setattr(
        "deile.core.deile_md_loader._core_deile_md_path",
        lambda: tmp_path / "missing-core" / "DEILE.md",
    )
    base = "ONLY_PERSONA"
    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    out = cm_module._prepend_deile_md_layers(base, str(empty_cwd))
    assert out == base


def test_prepend_layers_swallows_loader_failure_and_returns_base(monkeypatch):
    base = "FALLBACK_BASE"

    class _BoomLoader:
        def __init__(self, *_, **__):
            raise RuntimeError("boom")

    monkeypatch.setattr(cm_module, "DEILEMDLoader", _BoomLoader)
    out = cm_module._prepend_deile_md_layers(base, "/tmp/whatever")
    assert out == base


# ── _build_system_instruction (persona path) ────────────────────────────────


@pytest.mark.asyncio
async def test_build_system_instruction_prefixes_layers_when_persona_active(
    deile_md_layout,
):
    persona = MagicMock()
    persona.name = "test_persona"
    persona.build_system_instruction = AsyncMock(return_value="PERSONA_PROMPT_BODY")

    persona_manager = MagicMock()
    persona_manager.get_active_persona = MagicMock(return_value=persona)

    ctx_manager = ContextManager(persona_manager=persona_manager)

    out = await ctx_manager._build_system_instruction(
        parse_result=None,
        session=None,
        working_directory=str(deile_md_layout["cwd"]),
    )

    assert "PERSONA_PROMPT_BODY" in out
    assert CORE_MARKER in out
    assert USER_MARKER in out
    assert CWD_MARKER in out
    # Camadas precedem o corpo da persona
    assert out.index(CORE_MARKER) < out.index("PERSONA_PROMPT_BODY")


# ── _build_fallback_system_instruction (no persona) ─────────────────────────


@pytest.mark.asyncio
async def test_fallback_system_instruction_prefixes_layers(deile_md_layout):
    ctx_manager = ContextManager(persona_manager=None)
    ctx_manager.instruction_loader = MagicMock()
    ctx_manager.instruction_loader.load_fallback_instruction = MagicMock(
        return_value="FALLBACK_BODY"
    )

    out = await ctx_manager._build_fallback_system_instruction(
        session=None,
        working_directory=str(deile_md_layout["cwd"]),
    )

    assert "FALLBACK_BODY" in out
    assert CORE_MARKER in out
    assert USER_MARKER in out
    assert CWD_MARKER in out
    assert out.index(CORE_MARKER) < out.index("FALLBACK_BODY")


@pytest.mark.asyncio
async def test_fallback_system_instruction_when_no_layers_returns_only_base(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
    (tmp_path / "h").mkdir()
    monkeypatch.setattr(
        "deile.core.deile_md_loader._core_deile_md_path",
        lambda: tmp_path / "no-core" / "DEILE.md",
    )
    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()

    ctx_manager = ContextManager(persona_manager=None)
    ctx_manager.instruction_loader = MagicMock()
    ctx_manager.instruction_loader.load_fallback_instruction = MagicMock(
        return_value="JUST_FALLBACK"
    )

    out = await ctx_manager._build_fallback_system_instruction(
        session=None,
        working_directory=str(empty_cwd),
    )

    assert "JUST_FALLBACK" in out
    assert "FIM DAS CAMADAS DEILE.md" not in out
