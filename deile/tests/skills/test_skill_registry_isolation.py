"""Reprodutor determinístico da regressão ordering-pollution do SkillRegistry.

Este módulo prova que:
  1. Um teste que chama ``_build_fallback_system_instruction`` / ``bootstrap_skills``
     sem patchear o bootstrap popula o singleton com as skills bundled.
  2. Sem o reset hermético central (``_reset_global_singletons`` no conftest raiz),
     esse estado vaza para ``test_active_skill_is_omitted_from_catalog``, que falha
     porque o catálogo exibe typescript + tdd residuais.
  3. COM o fix (``reset_skill_registry`` em ``_reset_global_singletons``), o teste
     vítima passa mesmo após o poluidor ter rodado.

Recorrência #4 do padrão ordering-pollution (#432/#471/#499/#728).
Os leakers confirmados são ``test_context_manager_deile_md.py`` e
``test_preference_injection.py``: chamam métodos de ``ContextManager`` sem
patchear ``bootstrap_skills`` e sem ``_reset_registry`` próprio.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core.context_manager import ContextManager
from deile.parsers.base import ParseResult, ParseStatus
from deile.skills.base import Skill, SkillTrigger
from deile.skills.language_detector import LanguageDetector
from deile.skills.registry import SkillRegistry, get_skill_registry, reset_skill_registry
from deile.skills.router import SkillRouter


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_skill_registry()
    yield
    reset_skill_registry()


@pytest.fixture
def single_python_skill_router(monkeypatch: pytest.MonkeyPatch) -> SkillRouter:
    """Regista apenas a skill 'python' e patcha ``bootstrap_skills``."""
    registry = get_skill_registry()
    registry.clear()
    registry.register(
        Skill(
            name="python",
            description="Python",
            body="RULES FOR PYTHON",
            triggers=SkillTrigger(
                file_globs=["*.py"],
                code_block_langs=["python"],
            ),
        )
    )
    router = SkillRouter(registry, language_detector=LanguageDetector(), max_skills_per_turn=4)

    async def _fake_bootstrap(config=None, **kwargs):
        return router

    monkeypatch.setattr("deile.core.context_manager.bootstrap_skills", _fake_bootstrap)
    return router


@pytest.mark.integration
class TestSourceLevelIsolationFix:
    """Prova determinística de que o fix na fonte funciona.

    O fix em ``test_context_manager_deile_md.py`` e
    ``test_preference_injection.py`` adiciona um fixture autouse que patcha
    ``deile.core.context_manager.bootstrap_skills`` para um router vazio
    isolado (não usa o singleton global). Este teste reproduz exatamente
    esse padrão e verifica que o singleton global permanece com 0 skills
    após a chamada — ou seja, a contaminação foi eliminada na fonte.

    Este teste é determinístico: não depende de ordenação de coleta nem de
    pytest-randomly. Roda na mesma JVM; se o patch funciona, o registry
    global fica limpo; se não funciona, o assert falha com a lista das skills
    que vazaram.
    """

    async def test_patched_polluter_does_not_leak_into_global_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Com o patch fonte, bootstrap_skills usa registry isolado → singleton global
        permanece com 0 skills após chamada ao padrão poluidor."""
        # Pré-condição: registry global vazio (garantido pelo autouse _reset_registry)
        assert len(get_skill_registry()) == 0, "pre-condition: global registry must start empty"

        # Configura o patch exatamente como o fixture _isolate_bootstrap_skills faz
        _isolated_registry = SkillRegistry()
        _isolated_router = SkillRouter(_isolated_registry, language_detector=LanguageDetector())

        async def _fake_bootstrap(config=None, **kwargs):
            return _isolated_router

        monkeypatch.setattr("deile.core.context_manager.bootstrap_skills", _fake_bootstrap)

        # Executa o padrão poluidor: ContextManager._build_fallback_system_instruction
        ctx = ContextManager(persona_manager=None)
        ctx.instruction_loader = MagicMock()
        ctx.instruction_loader.load_fallback_instruction = MagicMock(
            return_value="FALLBACK_BODY"
        )
        await ctx._build_fallback_system_instruction(
            session=None,
            working_directory="/tmp/test_isolation",
        )

        # Pós-condição: singleton global ainda tem 0 skills — o bootstrap isolado
        # nunca tocou no singleton global.
        leaked = get_skill_registry().list_names()
        assert len(leaked) == 0, (
            f"Vazamento detectado! O padrão poluidor populou o singleton global com: {leaked}. "
            "O fixture _isolate_bootstrap_skills NÃO está sendo aplicado corretamente."
        )

    async def test_patched_polluter_does_not_leak_via_persona_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mesmo teste pelo caminho da persona (_build_system_instruction com PersonaManager)."""
        assert len(get_skill_registry()) == 0

        _isolated_registry = SkillRegistry()
        _isolated_router = SkillRouter(_isolated_registry, language_detector=LanguageDetector())

        async def _fake_bootstrap(config=None, **kwargs):
            return _isolated_router

        monkeypatch.setattr("deile.core.context_manager.bootstrap_skills", _fake_bootstrap)

        persona = MagicMock()
        persona.name = "test_persona"
        persona.build_system_instruction = MagicMock(return_value="PERSONA_BODY")

        # build_system_instruction é async e chamada com await — usar AsyncMock
        persona.build_system_instruction = AsyncMock(return_value="PERSONA_BODY")

        persona_manager = MagicMock()
        persona_manager.get_active_persona = MagicMock(return_value=persona)

        ctx = ContextManager(persona_manager=persona_manager)
        await ctx._build_system_instruction(
            parse_result=None,
            session=None,
            working_directory="/tmp/test_isolation_persona",
        )

        leaked = get_skill_registry().list_names()
        assert len(leaked) == 0, (
            f"Vazamento via persona path: {leaked}"
        )


@pytest.mark.integration
class TestSkillRegistryIsolationRepro:
    """Reprodução determinística do bug de ordering pollution."""

    async def test_pollutant_populates_registry_via_unpatched_bootstrap(self) -> None:
        """Simula o que test_context_manager_deile_md.py faz: chama
        _build_fallback_system_instruction sem patchear bootstrap_skills.

        Verifica que o registry fica populado com as skills bundled (> 1 skill).
        """
        ctx = ContextManager(persona_manager=None)
        ctx.instruction_loader = MagicMock()
        ctx.instruction_loader.load_fallback_instruction = MagicMock(
            return_value="FALLBACK_BODY"
        )
        # bootstrap_skills NÃO está patchado → vai buscar skills bundled reais
        await ctx._build_fallback_system_instruction(
            session=None,
            working_directory="/tmp/test",
        )
        # Depois do bootstrap real, o registry tem python + typescript + tdd (mínimo 3)
        registered = get_skill_registry().list_names()
        assert len(registered) >= 3, (
            f"bootstrap deve carregar as bundled skills; registry tem só {registered}"
        )
        # Entre elas, deve estar 'python' (bundled library skill)
        assert "python" in registered

    async def test_victim_passes_after_pollution_when_reset_is_active(
        self, single_python_skill_router: SkillRouter
    ) -> None:
        """Vítima do bug: passa quando o reset hermético do conftest limpa o registry
        antes deste teste, independente do que o poluidor deixou no estado global.

        Se ``reset_skill_registry`` NÃO estiver em ``_reset_global_singletons``,
        este teste FALHA quando o poluidor roda primeiro (porque a fixture
        ``_reset_registry`` deste arquivo limpa, mas o singleton podia ter sido
        re-populado pelo bootstrap assíncrono do poluidor depois da limpeza).

        Com o fix no conftest raiz, o registry é sempre limpo antes de cada teste.
        """
        cm = ContextManager()
        parse_result = ParseResult(status=ParseStatus.SUCCESS, file_references=["x.py"])
        session = SimpleNamespace(
            conversation_history=[{"role": "user", "content": "x.py"}],
            context_data={},
        )

        ctx = await cm.build_context(
            user_input="x.py",
            parse_result=parse_result,
            session=session,
        )

        sys_instr = ctx["system_instruction"]
        assert "### Skill: python" in sys_instr  # skill ativa presente
        # Com apenas 1 skill registrada e ela excluída do catálogo (está ativa),
        # o catálogo deve estar vazio → sem header "## Available Skills".
        assert "## Available Skills" not in sys_instr, (
            "Catálogo apareceu com skills residuais do poluidor. "
            "O reset hermético do conftest não limpou o SkillRegistry antes deste teste. "
            f"Registry tinha: {get_skill_registry().list_names()!r}"
        )
