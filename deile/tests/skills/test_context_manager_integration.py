"""Integration test: skills block flows through ``ContextManager``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deile.core.context_manager import ContextManager
from deile.parsers.base import ParseResult, ParseStatus
from deile.skills.base import Skill, SkillTrigger
from deile.skills.language_detector import LanguageDetector
from deile.skills.registry import get_skill_registry, reset_skill_registry
from deile.skills.router import SkillRouter


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_skill_registry()
    yield
    reset_skill_registry()


@pytest.fixture
def python_skill_router(monkeypatch: pytest.MonkeyPatch) -> SkillRouter:
    """Register a single python skill and patch ``bootstrap_skills`` to use it."""
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
    router = SkillRouter(
        registry, language_detector=LanguageDetector(), max_skills_per_turn=4
    )

    async def _fake_bootstrap(config=None, **kwargs):
        return router

    monkeypatch.setattr(
        "deile.core.context_manager.bootstrap_skills",
        _fake_bootstrap,
    )
    return router


@pytest.mark.integration
class TestSkillsInjectionViaContextManager:
    async def test_skills_block_present_when_file_reference_matches(
        self, python_skill_router: SkillRouter
    ) -> None:
        cm = ContextManager()  # no persona_manager → uses fallback path
        parse_result = ParseResult(
            status=ParseStatus.SUCCESS, file_references=["script.py"]
        )
        session = SimpleNamespace(
            conversation_history=[{"role": "user", "content": "fix script.py please"}],
            context_data={},
        )

        ctx = await cm.build_context(
            user_input="fix script.py please",
            parse_result=parse_result,
            session=session,
        )

        sys_instr = ctx["system_instruction"]
        assert "## Active Skills" in sys_instr
        assert "### Skill: python" in sys_instr
        assert "RULES FOR PYTHON" in sys_instr

    async def test_skills_block_absent_when_no_trigger_fires(
        self, python_skill_router: SkillRouter
    ) -> None:
        cm = ContextManager()
        parse_result = ParseResult(
            status=ParseStatus.SUCCESS, file_references=["README.md"]
        )
        session = SimpleNamespace(
            conversation_history=[{"role": "user", "content": "explain the readme"}],
            context_data={},
        )

        ctx = await cm.build_context(
            user_input="explain the readme",
            parse_result=parse_result,
            session=session,
        )

        assert "## Active Skills" not in ctx["system_instruction"]
        assert "RULES FOR PYTHON" not in ctx["system_instruction"]

    async def test_code_block_lang_triggers_skill_without_file_reference(
        self, python_skill_router: SkillRouter
    ) -> None:
        cm = ContextManager()
        message = "Review this:\n```python\nx = 1\n```"
        session = SimpleNamespace(
            conversation_history=[{"role": "user", "content": message}],
            context_data={},
        )

        ctx = await cm.build_context(user_input=message, session=session)

        assert "### Skill: python" in ctx["system_instruction"]
        assert "RULES FOR PYTHON" in ctx["system_instruction"]

    async def test_catalog_is_always_appended_when_skills_exist(
        self, python_skill_router: SkillRouter
    ) -> None:
        # Even when no trigger fires for the turn, the catalog ("## Available
        # Skills") must be in the system prompt so the LLM can call
        # ``invoke_skill`` on the python skill.
        cm = ContextManager()
        parse_result = ParseResult(
            status=ParseStatus.SUCCESS, file_references=["README.md"]
        )
        session = SimpleNamespace(
            conversation_history=[{"role": "user", "content": "no triggers here"}],
            context_data={},
        )

        ctx = await cm.build_context(
            user_input="no triggers here",
            parse_result=parse_result,
            session=session,
        )

        sys_instr = ctx["system_instruction"]
        assert "## Available Skills" in sys_instr
        assert "`python`" in sys_instr
        # Not auto-triggered → body is NOT injected.
        assert "RULES FOR PYTHON" not in sys_instr

    async def test_active_skill_is_omitted_from_catalog(
        self, python_skill_router: SkillRouter
    ) -> None:
        # When a skill auto-triggers AND is also in the catalog, the catalog
        # should skip it to avoid duplicating the description right above its
        # own injected body.
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
        assert "### Skill: python" in sys_instr  # active block present
        # With only one skill registered, the catalog excludes it → no
        # "## Available Skills" header in output.
        assert "## Available Skills" not in sys_instr

    async def test_disabled_subsystem_injects_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _disabled_bootstrap(config=None, **kwargs):
            return None

        monkeypatch.setattr(
            "deile.core.context_manager.bootstrap_skills",
            _disabled_bootstrap,
        )

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

        assert "## Active Skills" not in ctx["system_instruction"]
