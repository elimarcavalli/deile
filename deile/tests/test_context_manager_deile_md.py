"""Integration tests for ContextManager + DEILEMDLoader (Issue #62 / Feature #64).

Garante que `_build_system_instruction` (path da persona) e
`_build_fallback_system_instruction` (path do fallback) prefixam corretamente
as três camadas DEILE.md à instrução base — e que falha de leitura não
derruba o turno.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.core import context_manager as cm_module
from deile.core import deile_md_loader as loader_module
from deile.core.context_manager import ContextManager
from deile.skills.registry import SkillRegistry
from deile.skills.router import SkillRouter


@pytest.fixture(autouse=True)
def _isolate_loader_cache():
    loader_module.clear_cache()
    yield
    loader_module.clear_cache()


@pytest.fixture(autouse=True)
def _isolate_bootstrap_skills(monkeypatch):
    """Patch bootstrap_skills to an empty router so this file never populates
    the global SkillRegistry with bundled/operator skills.

    Estes testes afirmam apenas camadas DEILE.md — não precisam de skills
    reais. Sem este patch, ``_build_skills_block`` chama o bootstrap real e
    popula o singleton com python/typescript/tdd (+ skills do operador),
    contaminando testes subsequentes que dependem de um registry vazio.
    """
    _empty_registry = SkillRegistry()  # isolado — nunca afeta o singleton global
    _empty_router = SkillRouter(_empty_registry)

    async def _fake_bootstrap(config=None, **kwargs):
        return _empty_router

    monkeypatch.setattr("deile.core.context_manager.bootstrap_skills", _fake_bootstrap)


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


@pytest.mark.asyncio
async def test_prepend_layers_includes_all_three(deile_md_layout):
    base = "PERSONA_BASE_INSTRUCTION"
    out = await cm_module._prepend_deile_md_layers(base, str(deile_md_layout["cwd"]))

    assert CORE_MARKER in out
    assert USER_MARKER in out
    assert CWD_MARKER in out
    assert base in out
    # A persona vem DEPOIS do bloco DEILE.md
    assert out.index(CORE_MARKER) < out.index(base)
    assert out.index(USER_MARKER) < out.index(base)
    assert out.index(CWD_MARKER) < out.index(base)


@pytest.mark.asyncio
async def test_prepend_layers_preserves_base_when_no_deile_md(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
    (tmp_path / "h").mkdir()
    monkeypatch.setattr(
        "deile.core.deile_md_loader._core_deile_md_path",
        lambda: tmp_path / "missing-core" / "DEILE.md",
    )
    base = "ONLY_PERSONA"
    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    out = await cm_module._prepend_deile_md_layers(base, str(empty_cwd))
    assert out == base


@pytest.mark.asyncio
async def test_prepend_layers_swallows_loader_failure_and_returns_base(monkeypatch):
    base = "FALLBACK_BASE"

    class _BoomLoader:
        def __init__(self, *_, **__):
            raise RuntimeError("boom")

    monkeypatch.setattr(cm_module, "DEILEMDLoader", _BoomLoader)
    out = await cm_module._prepend_deile_md_layers(base, "/tmp/whatever")
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


# ── End-to-end: build_context() public API ──────────────────────────────────


@pytest.mark.asyncio
async def test_build_context_end_to_end_includes_deile_md_layers(deile_md_layout):
    """Garante que o caminho público `build_context` ainda passa pelo
    `_prepend_deile_md_layers`. Catches regression if someone moves the
    injection point.
    """
    persona = MagicMock()
    persona.name = "test_persona"
    persona.build_system_instruction = AsyncMock(return_value="PERSONA_PROMPT_BODY")

    persona_manager = MagicMock()
    persona_manager.get_active_persona = MagicMock(return_value=persona)

    ctx_manager = ContextManager(persona_manager=persona_manager)

    ctx = await ctx_manager.build_context(
        user_input="oi",
        working_directory=str(deile_md_layout["cwd"]),
    )

    sys_instr = ctx["system_instruction"]
    assert CORE_MARKER in sys_instr
    assert USER_MARKER in sys_instr
    assert CWD_MARKER in sys_instr
    assert "PERSONA_PROMPT_BODY" in sys_instr
    # A ordem fixa CORE → USER → CWD → persona é preservada
    assert sys_instr.index(CORE_MARKER) < sys_instr.index(USER_MARKER)
    assert sys_instr.index(USER_MARKER) < sys_instr.index(CWD_MARKER)
    assert sys_instr.index(CWD_MARKER) < sys_instr.index("PERSONA_PROMPT_BODY")
