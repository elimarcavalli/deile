"""Tests for ``deile.skills.loader`` (unified lenient parser).

The unified parser mirrors the legacy ``deile/commands/skill_loader.py``
behavior: it returns ``None`` (with a logged warning) for recoverable
problems instead of raising — so one malformed file in a library directory
does not abort the entire scan. Use the ``caplog`` fixture to verify the
warning is emitted.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from deile.skills.base import Skill
from deile.skills.loader import (
    SkillLoader,
    SkillLoadError,
    normalize_name,
    parse_skill_text,
)


VALID_SKILL = """\
---
name: python
description: Python idioms
triggers:
  file_globs: ["*.py"]
  code_block_langs: [python, py]
priority: 50
---
# Body
Some content."""


@pytest.mark.unit
class TestParseSkillText:
    def test_valid_frontmatter_parses(self) -> None:
        skill = parse_skill_text(VALID_SKILL, Path("python.md"))
        assert skill is not None
        assert isinstance(skill, Skill)
        assert skill.name == "python"
        assert skill.description == "Python idioms"
        assert skill.priority == 50
        assert skill.triggers.file_globs == ["*.py"]
        assert skill.triggers.code_block_langs == ["python", "py"]
        assert skill.body == "# Body\nSome content."
        assert skill.content == skill.body
        assert skill.source_path == Path("python.md")

    def test_code_block_langs_are_lowercased(self) -> None:
        text = """\
---
name: ts
description: TS
triggers:
  code_block_langs: [TypeScript, TS]
---
body"""
        skill = parse_skill_text(text, Path("ts.md"))
        assert skill is not None
        assert skill.triggers.code_block_langs == ["typescript", "ts"]

    def test_no_frontmatter_uses_filename_stem_as_name(self) -> None:
        # Lenient: a file with just body text is accepted.
        skill = parse_skill_text("just a body", Path("my-skill.md"))
        assert skill is not None
        assert skill.name == "my-skill"
        assert skill.description == "Skill: my-skill"
        assert skill.body == "just a body"

    def test_empty_body_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # caplog gets clobbered by other tests that mutate global logging
        # config (propagate=False, root handler tweaks). Patch the loader's
        # logger directly — same pattern legacy skill_loader tests use.
        from deile.skills import loader as loader_mod

        warns: list = []
        monkeypatch.setattr(
            loader_mod.logger, "warning",
            lambda msg, *a, **kw: warns.append(msg % a if a else msg),
        )
        skill = parse_skill_text(
            "---\nname: foo\ndescription: bar\n---\n", Path("x.md")
        )
        assert skill is None
        assert any("empty body" in m for m in warns), warns

    def test_invalid_yaml_returns_none_with_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from deile.skills import loader as loader_mod

        warns: list = []
        monkeypatch.setattr(
            loader_mod.logger, "warning",
            lambda msg, *a, **kw: warns.append(msg % a if a else msg),
        )
        bad = "---\nname: 'unclosed\n---\nbody"
        skill = parse_skill_text(bad, Path("x.md"))
        assert skill is None
        assert any("invalid YAML" in m for m in warns), warns

    def test_non_string_name_falls_back_to_stem(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = "---\nname: 42\n---\nbody"
        with caplog.at_level(logging.WARNING, logger="deile.skills.loader"):
            skill = parse_skill_text(bad, Path("numeric.md"))
        assert skill is not None
        assert skill.name == "numeric"

    def test_list_description_is_rejected_default_used(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = "---\nname: x\ndescription:\n  - a\n  - b\n---\nBody"
        with caplog.at_level(logging.WARNING, logger="deile.skills.loader"):
            skill = parse_skill_text(bad, Path("x.md"))
        assert skill is not None
        assert skill.description == "Skill: x"

    def test_invalid_priority_defaults_to_zero(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = "---\nname: x\ndescription: y\npriority: high\n---\nbody"
        with caplog.at_level(logging.WARNING, logger="deile.skills.loader"):
            skill = parse_skill_text(bad, Path("x.md"))
        assert skill is not None
        assert skill.priority == 0

    def test_triggers_not_mapping_is_ignored(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = "---\nname: x\ndescription: y\ntriggers: not-a-mapping\n---\nbody"
        with caplog.at_level(logging.WARNING, logger="deile.skills.loader"):
            skill = parse_skill_text(bad, Path("x.md"))
        assert skill is not None
        assert skill.triggers.file_globs == []
        assert skill.triggers.code_block_langs == []

    def test_trigger_list_field_not_string_list_is_ignored(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = """\
---
name: x
description: y
triggers:
  file_globs: [1, 2, 3]
---
body"""
        with caplog.at_level(logging.WARNING, logger="deile.skills.loader"):
            skill = parse_skill_text(bad, Path("x.md"))
        assert skill is not None
        assert skill.triggers.file_globs == []

    def test_priority_defaults_to_zero_when_absent(self) -> None:
        text = "---\nname: foo\ndescription: bar\n---\nbody"
        skill = parse_skill_text(text, Path("x.md"))
        assert skill is not None
        assert skill.priority == 0

    def test_missing_triggers_defaults_to_empty(self) -> None:
        text = "---\nname: foo\ndescription: bar\n---\nbody"
        skill = parse_skill_text(text, Path("x.md"))
        assert skill is not None
        assert skill.triggers.is_empty()

    def test_force_uppercase_name_applies(self) -> None:
        skill = parse_skill_text(
            "---\nname: my-command\n---\nbody",
            Path("ignored.md"),
            force_uppercase_name=True,
        )
        assert skill is not None
        assert skill.name == "MY-COMMAND"

    def test_invalid_name_after_normalize_returns_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Body present but stem normalizes to empty string.
        with caplog.at_level(logging.WARNING, logger="deile.skills.loader"):
            skill = parse_skill_text("body here", Path("___.md"))
        assert skill is None


@pytest.mark.unit
class TestNormalizeName:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("MySkill", "myskill"),
            ("hello world", "hello-world"),
            ("foo_bar", "foo-bar"),
            ("--leading", "leading"),
            ("trailing--", "trailing"),
            ("snake_case_name", "snake-case-name"),
        ],
    )
    def test_normalization(self, raw: str, expected: str) -> None:
        assert normalize_name(raw) == expected


@pytest.mark.unit
class TestLoadFile:
    async def test_load_file_returns_skill(self, tmp_path: Path) -> None:
        f = tmp_path / "python.md"
        f.write_text(VALID_SKILL, encoding="utf-8")
        loader = SkillLoader()
        skill = await loader.load_file(f)
        assert skill.name == "python"

    async def test_load_file_raises_on_unrecoverable(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.md"
        f.write_text("---\nname: x\n---\n", encoding="utf-8")
        loader = SkillLoader()
        with pytest.raises(SkillLoadError):
            await loader.load_file(f)


@pytest.mark.unit
class TestLoadDirectory:
    async def test_loads_all_valid_skills_recursively(self, tmp_path: Path) -> None:
        (tmp_path / "languages").mkdir()
        (tmp_path / "practices").mkdir()
        (tmp_path / "languages" / "python.md").write_text(VALID_SKILL, encoding="utf-8")
        (tmp_path / "practices" / "tdd.md").write_text(
            "---\nname: tdd\ndescription: TDD\n---\nbody", encoding="utf-8"
        )
        loader = SkillLoader()
        skills = await loader.load_directory(tmp_path)
        names = sorted(s.name for s in skills)
        assert names == ["python", "tdd"]

    async def test_empty_body_file_is_skipped_not_fatal(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "good.md").write_text(VALID_SKILL, encoding="utf-8")
        (tmp_path / "bad.md").write_text("---\nname: bad\n---\n", encoding="utf-8")
        loader = SkillLoader()
        with caplog.at_level(logging.WARNING, logger="deile.skills.loader"):
            skills = await loader.load_directory(tmp_path)
        assert [s.name for s in skills] == ["python"]

    async def test_missing_directory_returns_empty(self, tmp_path: Path) -> None:
        loader = SkillLoader()
        skills = await loader.load_directory(tmp_path / "does-not-exist")
        assert skills == []

    async def test_file_path_argument_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "a.md"
        f.write_text(VALID_SKILL, encoding="utf-8")
        loader = SkillLoader()
        skills = await loader.load_directory(f)
        assert skills == []
