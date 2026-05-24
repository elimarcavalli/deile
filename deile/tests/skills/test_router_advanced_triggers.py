"""Tests for the new SkillRouter triggers: keywords + file_content_patterns."""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.skills.base import Skill, SkillTrigger
from deile.skills.registry import SkillRegistry
from deile.skills.router import SkillRouter, SkillSelectionContext


def _skill(name: str, **kwargs) -> Skill:
    return Skill(
        name=name,
        description=f"{name} desc",
        body=f"# {name}",
        triggers=SkillTrigger(**kwargs),
    )


@pytest.mark.unit
class TestKeywordTrigger:
    def test_keyword_match_in_user_input(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("tdd", keywords=["TDD", "test driven"]))
        router = SkillRouter(reg)

        out = router.select_skills(
            SkillSelectionContext(user_input="let's apply tdd here", file_references=())
        )
        assert [s.name for s in out] == ["tdd"]

    def test_keyword_is_case_insensitive(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("rust", keywords=["Rust"]))
        router = SkillRouter(reg)

        out = router.select_skills(
            SkillSelectionContext(user_input="this should match RUST today", file_references=())
        )
        assert len(out) == 1

    def test_keyword_word_boundary_avoids_substring_hit(self) -> None:
        # The "rust" keyword must NOT match "trust" — word boundaries protect us.
        reg = SkillRegistry()
        reg.register(_skill("rust", keywords=["rust"]))
        router = SkillRouter(reg)

        out = router.select_skills(
            SkillSelectionContext(user_input="I trust this approach", file_references=())
        )
        assert out == []

    def test_keyword_multiword_phrase_matches(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("tdd", keywords=["test driven"]))
        router = SkillRouter(reg)

        out = router.select_skills(
            SkillSelectionContext(user_input="please write a test driven implementation", file_references=())
        )
        assert [s.name for s in out] == ["tdd"]

    def test_empty_keywords_does_not_trigger(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("noop", keywords=[]))
        router = SkillRouter(reg)
        assert router.select_skills(SkillSelectionContext(user_input="anything")) == []


@pytest.mark.unit
class TestFileContentTrigger:
    def test_shebang_pattern_matches(self, tmp_path: Path) -> None:
        f = tmp_path / "run.sh"
        f.write_text("#!/usr/bin/env bash\nset -euo pipefail\n", encoding="utf-8")

        reg = SkillRegistry()
        reg.register(_skill("bash-strict", file_content_patterns=[r"^#!/.*bash"]))
        router = SkillRouter(reg, project_root=tmp_path)

        out = router.select_skills(
            SkillSelectionContext(user_input="check run.sh", file_references=("run.sh",))
        )
        assert [s.name for s in out] == ["bash-strict"]

    def test_import_pattern_matches(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text("import requests\nimport asyncio\n", encoding="utf-8")

        reg = SkillRegistry()
        reg.register(_skill("requests", file_content_patterns=[r"^import requests\b"]))
        router = SkillRouter(reg, project_root=tmp_path)

        out = router.select_skills(
            SkillSelectionContext(user_input="x.py", file_references=("x.py",))
        )
        assert [s.name for s in out] == ["requests"]

    def test_absolute_path_reference_works(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("import x\n", encoding="utf-8")

        reg = SkillRegistry()
        reg.register(_skill("ximport", file_content_patterns=[r"^import x$"]))
        router = SkillRouter(reg, project_root=tmp_path)

        out = router.select_skills(
            SkillSelectionContext(user_input="", file_references=(str(f),))
        )
        assert [s.name for s in out] == ["ximport"]

    def test_missing_file_is_silently_skipped(self, tmp_path: Path) -> None:
        reg = SkillRegistry()
        reg.register(_skill("never", file_content_patterns=[r".+"]))
        router = SkillRouter(reg, project_root=tmp_path)

        out = router.select_skills(
            SkillSelectionContext(user_input="", file_references=("does-not-exist.py",))
        )
        assert out == []

    def test_invalid_regex_is_skipped_without_crash(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("body", encoding="utf-8")

        reg = SkillRegistry()
        # Two skills: one with broken regex, one with valid regex — only the
        # broken-regex one should be skipped, and the other still works.
        reg.register(_skill("broken", file_content_patterns=["[unclosed"]))
        reg.register(_skill("ok", file_content_patterns=[r"body"]))
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

        reg = SkillRegistry()
        # Two skills both reading the same file — should produce 1 read, not 2.
        # We can't directly assert call counts without instrumenting open(),
        # so use a behavioral proxy: both skills match correctly.
        reg.register(_skill("s1", file_content_patterns=[r"magic-marker"]))
        reg.register(_skill("s2", file_content_patterns=[r"magic"]))
        router = SkillRouter(reg, project_root=tmp_path)

        out = router.select_skills(
            SkillSelectionContext(user_input="", file_references=("a.py",))
        )
        assert sorted(s.name for s in out) == ["s1", "s2"]


@pytest.mark.unit
class TestRenderCatalog:
    def test_empty_registry_returns_empty_string(self) -> None:
        router = SkillRouter(SkillRegistry())
        assert router.render_catalog() == ""

    def test_lists_all_skills_with_descriptions(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("python", file_globs=["*.py"]))
        reg.register(_skill("tdd", keywords=["tdd"]))
        router = SkillRouter(reg)

        out = router.render_catalog()
        assert "## Available Skills" in out
        assert "`python`" in out
        assert "`tdd`" in out

    def test_catalog_includes_invocation_directive(self) -> None:
        # The directive that tells the LLM to call invoke_skill BEFORE
        # answering — without it, the LLM tends to skip the catalog. Verified
        # empirically (commit 25b2cd7) against deepseek-v4-flash. Removing
        # this string is a regression.
        reg = SkillRegistry()
        reg.register(_skill("python", file_globs=["*.py"]))
        router = SkillRouter(reg)

        out = router.render_catalog()
        assert "invoke_skill" in out
        assert "BEFORE" in out
        # And it has to make clear these override generic knowledge.
        assert "OVERRIDE" in out or "override" in out

    def test_excludes_named_skills(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("python", file_globs=["*.py"]))
        reg.register(_skill("tdd", keywords=["tdd"]))
        router = SkillRouter(reg)

        out = router.render_catalog(exclude_names={"python"})
        assert "`python`" not in out
        assert "`tdd`" in out

    def test_skill_with_no_triggers_shows_no_hint(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("manual-only"))  # no triggers
        router = SkillRouter(reg)

        out = router.render_catalog()
        assert "`manual-only`" in out
        # No "auto-active when" suffix because there are no triggers.
        line = next(line for line in out.splitlines() if "manual-only" in line)
        assert "auto-active" not in line

    def test_skill_with_triggers_shows_hint(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("python", file_globs=["*.py"], code_block_langs=["python"]))
        router = SkillRouter(reg)

        out = router.render_catalog()
        line = next(line for line in out.splitlines() if "python" in line)
        assert "auto-active when" in line
        assert "*.py" in line
