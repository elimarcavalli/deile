"""Regression tests for the three integration findings:

1. ``ContextManager._build_skills_block`` must pass the session's
   ``working_directory`` through to ``bootstrap_skills`` so the
   ``SkillRouter``'s ``project_root`` matches the agent's project — the
   security boundary of ``file_content_patterns`` triggers depends on it.
2. ``_build_skills_block`` must stash the active skill names on
   ``session.context_data["_active_skills"]`` so the streaming layer can
   emit a STAGE event ("Skill ativa: <names>") — auto-injection is
   invisible to the user otherwise.
3. ``STAGE_MESSAGES["skills_active"]`` must exist with a ``{names}`` slot
   so the agent's stream emit can format it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from deile.core.context_manager import ContextManager
from deile.parsers.base import ParseResult, ParseStatus
from deile.skills.base import Skill, SkillTrigger
from deile.skills.language_detector import LanguageDetector
from deile.skills.registry import get_skill_registry, reset_skill_registry
from deile.skills.router import SkillRouter
from deile.ui.stage_messages import STAGE_MESSAGES, get_stage_message


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_skill_registry()
    yield
    reset_skill_registry()


@pytest.fixture
def python_skill_router(monkeypatch: pytest.MonkeyPatch):
    """Pre-populate the registry and patch bootstrap_skills to use it."""
    registry = get_skill_registry()
    registry.register(
        Skill(
            name="python",
            description="Python",
            body="RULES",
            triggers=SkillTrigger(file_globs=["*.py"]),
        )
    )
    captured: dict = {}
    router = SkillRouter(
        registry, language_detector=LanguageDetector(), max_skills_per_turn=4
    )

    async def _fake_bootstrap(config=None, *, project_dir=None, **kwargs):
        captured["project_dir"] = project_dir
        return router

    monkeypatch.setattr("deile.core.context_manager.bootstrap_skills", _fake_bootstrap)
    return captured


@pytest.mark.integration
class TestProjectDirPropagation:
    async def test_session_working_directory_reaches_bootstrap(
        self, python_skill_router, tmp_path: Path
    ) -> None:
        # The session knows the project root — that's the value the router
        # must use for path-traversal containment, NOT the process CWD.
        cm = ContextManager()
        parse_result = ParseResult(status=ParseStatus.SUCCESS, file_references=["x.py"])
        session = SimpleNamespace(
            conversation_history=[{"role": "user", "content": "fix x.py"}],
            context_data={},
            working_directory=tmp_path,
        )
        await cm.build_context(
            user_input="fix x.py",
            parse_result=parse_result,
            session=session,
        )
        assert python_skill_router["project_dir"] == tmp_path

    async def test_kwarg_working_directory_used_when_session_missing(
        self, python_skill_router, tmp_path: Path
    ) -> None:
        # If session has no working_directory, fall back to the caller's
        # working_directory kwarg (the persona path passes it).
        cm = ContextManager()
        parse_result = ParseResult(status=ParseStatus.SUCCESS, file_references=["x.py"])
        session = SimpleNamespace(
            conversation_history=[{"role": "user", "content": "fix x.py"}],
            context_data={},
        )
        await cm.build_context(
            user_input="fix x.py",
            parse_result=parse_result,
            session=session,
            working_directory=str(tmp_path),
        )
        assert python_skill_router["project_dir"] == Path(str(tmp_path))


@pytest.mark.integration
class TestActiveSkillsStashOnSession:
    async def test_active_skills_recorded_when_trigger_fires(
        self, python_skill_router
    ) -> None:
        cm = ContextManager()
        parse_result = ParseResult(
            status=ParseStatus.SUCCESS, file_references=["script.py"]
        )
        session = SimpleNamespace(
            conversation_history=[{"role": "user", "content": "fix script.py"}],
            context_data={},
        )
        await cm.build_context(
            user_input="fix script.py",
            parse_result=parse_result,
            session=session,
        )
        # The streaming layer reads this exact key to emit the STAGE event.
        assert session.context_data.get("_active_skills") == ["python"]

    async def test_active_skills_empty_when_no_trigger_fires(
        self, python_skill_router
    ) -> None:
        cm = ContextManager()
        parse_result = ParseResult(
            status=ParseStatus.SUCCESS, file_references=["README.md"]
        )
        session = SimpleNamespace(
            conversation_history=[{"role": "user", "content": "explain readme"}],
            context_data={},
        )
        await cm.build_context(
            user_input="explain readme",
            parse_result=parse_result,
            session=session,
        )
        # Always written (even when empty) so the streaming layer doesn't
        # carry stale values from a previous turn.
        assert session.context_data.get("_active_skills") == []


@pytest.mark.unit
class TestSkillsActiveStageMessage:
    def test_message_key_exists(self) -> None:
        # The agent at agent.py uses ``get_stage_message("skills_active",
        # "initial", names=...)`` to format the spinner label; the key
        # must be registered or the lookup falls back to a generic.
        assert "skills_active" in STAGE_MESSAGES

    def test_message_formats_names_argument(self) -> None:
        msg = get_stage_message("skills_active", "initial", names="python, tdd")
        assert "python" in msg
        assert "tdd" in msg

    def test_message_has_visible_emoji_marker(self) -> None:
        # The 🧩 prefix makes the skill stage stand out from the other
        # generic stages in the spinner cascade.
        msg = get_stage_message("skills_active", "initial", names="x")
        assert "🧩" in msg
