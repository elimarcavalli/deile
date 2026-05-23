"""Tests for ``deile.skills.router``."""

from __future__ import annotations

import pytest

from deile.skills.base import Skill, SkillTrigger
from deile.skills.registry import SkillRegistry
from deile.skills.router import SkillRouter, SkillSelectionContext


def _skill(name: str, *, globs=None, langs=None, priority: int = 0) -> Skill:
    return Skill(
        name=name,
        description=f"{name} desc",
        body=f"# {name}",
        triggers=SkillTrigger(
            file_globs=list(globs or []),
            code_block_langs=list(langs or []),
        ),
        priority=priority,
    )


def _registry(*skills: Skill) -> SkillRegistry:
    reg = SkillRegistry()
    for s in skills:
        reg.register(s)
    return reg


@pytest.mark.unit
class TestFileGlobMatching:
    def test_file_glob_triggers_skill(self) -> None:
        reg = _registry(_skill("python", globs=["*.py"]))
        router = SkillRouter(reg)
        ctx = SkillSelectionContext(file_references=("src/foo.py",))
        assert [s.name for s in router.select_skills(ctx)] == ["python"]

    def test_glob_matches_basename_when_pattern_has_no_slash(self) -> None:
        reg = _registry(_skill("py", globs=["*.py"]))
        router = SkillRouter(reg)
        ctx = SkillSelectionContext(file_references=("/deep/nested/path/x.py",))
        assert [s.name for s in router.select_skills(ctx)] == ["py"]

    def test_glob_matches_full_path_when_pattern_includes_slash(self) -> None:
        reg = _registry(_skill("tests", globs=["tests/*.py"]))
        router = SkillRouter(reg)
        ctx = SkillSelectionContext(file_references=("tests/foo.py",))
        assert [s.name for s in router.select_skills(ctx)] == ["tests"]

    def test_no_match_returns_empty(self) -> None:
        reg = _registry(_skill("rust", globs=["*.rs"]))
        router = SkillRouter(reg)
        ctx = SkillSelectionContext(file_references=("a.py",))
        assert router.select_skills(ctx) == []


@pytest.mark.unit
class TestCodeBlockMatching:
    def test_code_block_lang_triggers_skill(self) -> None:
        reg = _registry(_skill("python", langs=["python", "py"]))
        router = SkillRouter(reg)
        ctx = SkillSelectionContext(user_input="```python\nx=1\n```")
        assert [s.name for s in router.select_skills(ctx)] == ["python"]

    def test_file_extension_promotes_to_code_block_lang(self) -> None:
        # A skill that only declares code_block_langs still fires when a file
        # reference resolves to that language via the extension map.
        reg = _registry(_skill("python", langs=["python"]))
        router = SkillRouter(reg)
        ctx = SkillSelectionContext(file_references=("foo.py",))
        assert [s.name for s in router.select_skills(ctx)] == ["python"]

    def test_unrelated_code_block_does_not_trigger(self) -> None:
        reg = _registry(_skill("python", langs=["python"]))
        router = SkillRouter(reg)
        ctx = SkillSelectionContext(user_input="```rust\nfn main(){}\n```")
        assert router.select_skills(ctx) == []


@pytest.mark.unit
class TestPriorityAndCap:
    def test_priority_desc_then_name_asc(self) -> None:
        reg = _registry(
            _skill("zebra", langs=["python"], priority=10),
            _skill("apple", langs=["python"], priority=10),
            _skill("higher", langs=["python"], priority=50),
        )
        router = SkillRouter(reg)
        ctx = SkillSelectionContext(user_input="```python\n```")
        names = [s.name for s in router.select_skills(ctx)]
        assert names == ["higher", "apple", "zebra"]

    def test_max_skills_per_turn_caps_result(self) -> None:
        reg = _registry(
            *[_skill(f"s{i}", langs=["python"], priority=i) for i in range(10)]
        )
        router = SkillRouter(reg, max_skills_per_turn=3)
        ctx = SkillSelectionContext(user_input="```python\n```")
        out = router.select_skills(ctx)
        assert len(out) == 3
        assert [s.name for s in out] == ["s9", "s8", "s7"]

    def test_empty_registry_returns_empty(self) -> None:
        router = SkillRouter(SkillRegistry())
        ctx = SkillSelectionContext(user_input="```python\n```", file_references=("a.py",))
        assert router.select_skills(ctx) == []

    def test_skill_matched_only_once_even_when_two_triggers_fire(self) -> None:
        # A skill fires when EITHER trigger matches; matching both must not
        # double-list it in the output.
        reg = _registry(_skill("python", globs=["*.py"], langs=["python"]))
        router = SkillRouter(reg)
        ctx = SkillSelectionContext(
            user_input="```python\nx\n```",
            file_references=("foo.py",),
        )
        assert [s.name for s in router.select_skills(ctx)] == ["python"]


@pytest.mark.unit
class TestRender:
    def test_render_block_assembles_named_sections(self) -> None:
        skills = [
            Skill(name="a", description="A", body="body-a"),
            Skill(name="b", description="B", body="body-b"),
        ]
        router = SkillRouter(SkillRegistry())
        out = router.render_block(skills)
        assert "## Active Skills" in out
        assert "### Skill: a" in out
        assert "body-a" in out
        assert "### Skill: b" in out
        assert "body-b" in out

    def test_render_block_returns_empty_for_no_skills(self) -> None:
        router = SkillRouter(SkillRegistry())
        assert router.render_block([]) == ""
