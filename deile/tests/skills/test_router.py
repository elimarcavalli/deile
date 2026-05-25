"""Tests for ``deile.skills.router`` — selection, triggers, catalog, traversal guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.skills.base import Skill, SkillTrigger
from deile.skills.registry import SkillRegistry
from deile.skills.router import (
    SkillRouter,
    SkillSelectionContext,
    _resolve_within,
)


def _skill(name: str, *, globs=None, langs=None, priority: int = 0, **trigger_kwargs) -> Skill:
    # Aliases: ``globs`` and ``file_globs`` both populate ``file_globs``;
    # likewise ``langs`` and ``code_block_langs``. Older tests used the short
    # form, newer ones used the field name — we accept either.
    if globs is not None:
        trigger_kwargs.setdefault("file_globs", list(globs))
    if langs is not None:
        trigger_kwargs.setdefault("code_block_langs", list(langs))
    return Skill(
        name=name,
        description=f"{name} desc",
        body=f"# {name}",
        triggers=SkillTrigger(**trigger_kwargs),
        priority=priority,
    )


def _registry(*skills: Skill) -> SkillRegistry:
    reg = SkillRegistry()
    for s in skills:
        reg.register(s)
    return reg


# ---------------------------------------------------------------------------
# Basic triggers: file globs + code-block langs
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Keyword trigger
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKeywordTrigger:
    def test_keyword_match_in_user_input(self) -> None:
        reg = _registry(_skill("tdd", keywords=["TDD", "test driven"]))
        router = SkillRouter(reg)
        out = router.select_skills(
            SkillSelectionContext(user_input="let's apply tdd here", file_references=())
        )
        assert [s.name for s in out] == ["tdd"]

    def test_keyword_is_case_insensitive(self) -> None:
        reg = _registry(_skill("rust", keywords=["Rust"]))
        router = SkillRouter(reg)
        out = router.select_skills(
            SkillSelectionContext(user_input="this should match RUST today", file_references=())
        )
        assert len(out) == 1

    def test_keyword_word_boundary_avoids_substring_hit(self) -> None:
        # The "rust" keyword must NOT match "trust" — word boundaries protect us.
        reg = _registry(_skill("rust", keywords=["rust"]))
        router = SkillRouter(reg)
        out = router.select_skills(
            SkillSelectionContext(user_input="I trust this approach", file_references=())
        )
        assert out == []

    def test_keyword_multiword_phrase_matches(self) -> None:
        reg = _registry(_skill("tdd", keywords=["test driven"]))
        router = SkillRouter(reg)
        out = router.select_skills(
            SkillSelectionContext(user_input="please write a test driven implementation", file_references=())
        )
        assert [s.name for s in out] == ["tdd"]

    def test_empty_keywords_does_not_trigger(self) -> None:
        reg = _registry(_skill("noop", keywords=[]))
        router = SkillRouter(reg)
        assert router.select_skills(SkillSelectionContext(user_input="anything")) == []


# ---------------------------------------------------------------------------
# File-content pattern trigger
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFileContentTrigger:
    def test_shebang_pattern_matches(self, tmp_path: Path) -> None:
        f = tmp_path / "run.sh"
        f.write_text("#!/usr/bin/env bash\nset -euo pipefail\n", encoding="utf-8")
        reg = _registry(_skill("bash-strict", file_content_patterns=[r"^#!/.*bash"]))
        router = SkillRouter(reg, project_root=tmp_path)
        out = router.select_skills(
            SkillSelectionContext(user_input="check run.sh", file_references=("run.sh",))
        )
        assert [s.name for s in out] == ["bash-strict"]

    def test_import_pattern_matches(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text("import requests\nimport asyncio\n", encoding="utf-8")
        reg = _registry(_skill("requests", file_content_patterns=[r"^import requests\b"]))
        router = SkillRouter(reg, project_root=tmp_path)
        out = router.select_skills(
            SkillSelectionContext(user_input="x.py", file_references=("x.py",))
        )
        assert [s.name for s in out] == ["requests"]

    def test_absolute_path_reference_works(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("import x\n", encoding="utf-8")
        reg = _registry(_skill("ximport", file_content_patterns=[r"^import x$"]))
        router = SkillRouter(reg, project_root=tmp_path)
        out = router.select_skills(
            SkillSelectionContext(user_input="", file_references=(str(f),))
        )
        assert [s.name for s in out] == ["ximport"]

    def test_missing_file_is_silently_skipped(self, tmp_path: Path) -> None:
        reg = _registry(_skill("never", file_content_patterns=[r".+"]))
        router = SkillRouter(reg, project_root=tmp_path)
        out = router.select_skills(
            SkillSelectionContext(user_input="", file_references=("does-not-exist.py",))
        )
        assert out == []

    def test_invalid_regex_is_skipped_without_crash(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("body", encoding="utf-8")
        reg = _registry(
            _skill("broken", file_content_patterns=["[unclosed"]),
            _skill("ok", file_content_patterns=[r"body"]),
        )
        router = SkillRouter(reg, project_root=tmp_path)
        out = router.select_skills(
            SkillSelectionContext(user_input="", file_references=("a.py",))
        )
        names = [s.name for s in out]
        assert "ok" in names
        assert "broken" not in names

    def test_file_content_cache_avoids_re_read(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("magic-marker\n", encoding="utf-8")
        reg = _registry(
            _skill("s1", file_content_patterns=[r"magic-marker"]),
            _skill("s2", file_content_patterns=[r"magic"]),
        )
        router = SkillRouter(reg, project_root=tmp_path)
        out = router.select_skills(
            SkillSelectionContext(user_input="", file_references=("a.py",))
        )
        assert sorted(s.name for s in out) == ["s1", "s2"]


# ---------------------------------------------------------------------------
# Path-traversal containment — security boundary
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPathTraversal:
    def test_resolve_within_accepts_in_root(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        out = _resolve_within("a.py", tmp_path)
        assert out is not None
        assert out == (tmp_path / "a.py").resolve()

    def test_resolve_within_rejects_dotdot_escape(self, tmp_path: Path) -> None:
        # A skill author cannot make us sample /etc/passwd via a crafted
        # reference like "../../etc/passwd".
        out = _resolve_within("../../etc/passwd", tmp_path)
        assert out is None

    def test_resolve_within_rejects_absolute_outside(self, tmp_path: Path) -> None:
        out = _resolve_within("/etc/hostname", tmp_path)
        assert out is None

    def test_resolve_within_accepts_absolute_inside(self, tmp_path: Path) -> None:
        target = tmp_path / "b.py"
        target.write_text("x", encoding="utf-8")
        out = _resolve_within(str(target), tmp_path)
        assert out == target.resolve()

    def test_file_content_trigger_does_not_leak_outside_root(self, tmp_path: Path) -> None:
        reg = _registry(_skill("probe", file_content_patterns=[r".+"]))
        router = SkillRouter(reg, project_root=tmp_path)
        out = router.select_skills(
            SkillSelectionContext(user_input="", file_references=("../../etc/passwd",))
        )
        assert out == []


# ---------------------------------------------------------------------------
# Priority, cap, render
# ---------------------------------------------------------------------------


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
            user_input="```python\nx\n```", file_references=("foo.py",),
        )
        assert [s.name for s in router.select_skills(ctx)] == ["python"]


@pytest.mark.unit
class TestRenderBlock:
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


@pytest.mark.unit
class TestRenderCatalog:
    def test_empty_registry_returns_empty_string(self) -> None:
        router = SkillRouter(SkillRegistry())
        assert router.render_catalog() == ""

    def test_lists_all_skills_with_descriptions(self) -> None:
        reg = _registry(
            _skill("python", file_globs=["*.py"]),
            _skill("tdd", keywords=["tdd"]),
        )
        router = SkillRouter(reg)
        out = router.render_catalog()
        assert "## Available Skills" in out
        assert "`python`" in out
        assert "`tdd`" in out

    def test_catalog_includes_invocation_directive(self) -> None:
        # The directive that tells the LLM to call invoke_skill BEFORE
        # answering — verified empirically (commit 25b2cd7) against
        # deepseek-v4-flash. Removing this is a regression.
        reg = _registry(_skill("python", file_globs=["*.py"]))
        router = SkillRouter(reg)
        out = router.render_catalog()
        assert "invoke_skill" in out
        assert "BEFORE" in out
        assert "OVERRIDE" in out or "override" in out
        # And a concrete example — the abstract directive alone wasn't enough.
        assert "Concrete example" in out

    def test_excludes_named_skills(self) -> None:
        reg = _registry(
            _skill("python", file_globs=["*.py"]),
            _skill("tdd", keywords=["tdd"]),
        )
        router = SkillRouter(reg)
        out = router.render_catalog(exclude_names={"python"})
        assert "`python`" not in out
        assert "`tdd`" in out

    def test_skill_with_no_triggers_shows_no_hint(self) -> None:
        reg = _registry(_skill("manual-only"))  # no triggers
        router = SkillRouter(reg)
        out = router.render_catalog()
        assert "`manual-only`" in out
        line = next(line for line in out.splitlines() if "manual-only" in line)
        assert "auto-active" not in line

    def test_skill_with_triggers_shows_hint(self) -> None:
        reg = _registry(_skill("python", file_globs=["*.py"], code_block_langs=["python"]))
        router = SkillRouter(reg)
        out = router.render_catalog()
        line = next(line for line in out.splitlines() if "python" in line)
        assert "auto-active when" in line
        assert "*.py" in line
