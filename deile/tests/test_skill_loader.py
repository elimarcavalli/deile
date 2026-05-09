"""Tests: SkillLoader — issue #41."""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.commands.skill_loader import (SkillLoader, _normalize_name,
                                         _parse_skill_file)

# ---------------------------------------------------------------------------
# _normalize_name
# ---------------------------------------------------------------------------


class TestNormalizeName:
    def test_lowercases(self):
        assert _normalize_name("MySkill") == "myskill"

    def test_replaces_spaces_with_hyphens(self):
        assert _normalize_name("my skill") == "my-skill"

    def test_replaces_underscores_with_hyphens(self):
        assert _normalize_name("my_skill") == "my-skill"

    def test_strips_leading_trailing_hyphens(self):
        assert _normalize_name("-my-skill-") == "my-skill"

    def test_removes_invalid_chars(self):
        assert _normalize_name("my@skill!") == "myskill"


# ---------------------------------------------------------------------------
# _parse_skill_file
# ---------------------------------------------------------------------------


class TestParseSkillFile:
    def test_full_frontmatter(self, tmp_path):
        md = tmp_path / "review.md"
        md.write_text(
            "---\nname: code-review\ndescription: Review the code\n---\nPlease review this code.",
            encoding="utf-8",
        )
        skill = _parse_skill_file(md, source="user")
        assert skill is not None
        assert skill.name == "code-review"
        assert skill.description == "Review the code"
        assert skill.body == "Please review this code."
        assert skill.source == "user"

    def test_name_falls_back_to_stem(self, tmp_path):
        md = tmp_path / "my-skill.md"
        md.write_text(
            "---\ndescription: A skill\n---\nDo something.",
            encoding="utf-8",
        )
        skill = _parse_skill_file(md, source="user")
        assert skill is not None
        assert skill.name == "my-skill"

    def test_no_frontmatter_uses_filename(self, tmp_path):
        md = tmp_path / "quick-fix.md"
        md.write_text("Fix the most obvious bug.", encoding="utf-8")
        skill = _parse_skill_file(md, source="project")
        assert skill is not None
        assert skill.name == "quick-fix"
        assert skill.body == "Fix the most obvious bug."

    def test_empty_body_returns_none(self, tmp_path):
        md = tmp_path / "empty.md"
        md.write_text("---\nname: empty\n---\n", encoding="utf-8")
        skill = _parse_skill_file(md, source="user")
        assert skill is None

    def test_invalid_name_returns_none(self, tmp_path):
        md = tmp_path / "!!.md"
        md.write_text("---\nname: !!invalid!!\n---\nDo something.", encoding="utf-8")
        skill = _parse_skill_file(md, source="user")
        assert skill is None

    def test_source_tagged_correctly(self, tmp_path):
        md = tmp_path / "s.md"
        md.write_text("Do it.", encoding="utf-8")
        skill = _parse_skill_file(md, source="project")
        assert skill is not None
        assert skill.source == "project"

    def test_missing_file_returns_none(self, tmp_path):
        missing = tmp_path / "does_not_exist.md"
        skill = _parse_skill_file(missing, source="user")
        assert skill is None


# ---------------------------------------------------------------------------
# SkillLoader.load_skills
# ---------------------------------------------------------------------------


class TestSkillLoaderLoadSkills:
    def _make_loader(self, tmp_path: Path) -> SkillLoader:
        return SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )

    def _write_skill(self, directory: Path, filename: str, name: str, body: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(
            f"---\nname: {name}\ndescription: {name} skill\n---\n{body}",
            encoding="utf-8",
        )

    def test_returns_empty_when_no_skills(self, tmp_path):
        loader = self._make_loader(tmp_path)
        skills = loader.load_skills()
        assert skills == []

    def test_user_skills_loaded(self, tmp_path):
        loader = self._make_loader(tmp_path)
        self._write_skill(loader.user_skills_dir, "foo.md", "foo", "Do foo.")
        skills = loader.load_skills()
        assert len(skills) == 1
        assert skills[0].name == "foo"
        assert skills[0].source == "user"

    def test_project_skills_loaded(self, tmp_path):
        loader = self._make_loader(tmp_path)
        self._write_skill(loader.project_skills_dir, "bar.md", "bar", "Do bar.")
        skills = loader.load_skills()
        assert len(skills) == 1
        assert skills[0].name == "bar"
        assert skills[0].source == "project"

    def test_project_overrides_user_on_name_conflict(self, tmp_path):
        loader = self._make_loader(tmp_path)
        self._write_skill(loader.user_skills_dir, "skill.md", "my-skill", "User body.")
        self._write_skill(loader.project_skills_dir, "skill.md", "my-skill", "Project body.")
        skills = loader.load_skills()
        assert len(skills) == 1
        assert skills[0].source == "project"
        assert skills[0].body == "Project body."

    def test_user_dir_created_automatically(self, tmp_path):
        loader = self._make_loader(tmp_path)
        assert not loader.user_skills_dir.exists()
        loader.load_skills()
        assert loader.user_skills_dir.is_dir()

    def test_both_sources_merged(self, tmp_path):
        loader = self._make_loader(tmp_path)
        self._write_skill(loader.user_skills_dir, "a.md", "skill-a", "Body A.")
        self._write_skill(loader.project_skills_dir, "b.md", "skill-b", "Body B.")
        skills = loader.load_skills()
        names = {s.name for s in skills}
        assert names == {"skill-a", "skill-b"}


# ---------------------------------------------------------------------------
# SkillLoader + SettingsManager extra paths
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("allow_settings_writes")
class TestSkillLoaderExtraPaths:
    """SkillLoader reads extra directories from SettingsManager.

    Issue #125 made ``add_skills_path`` permission-gated (fail-closed by
    default), so the tests in this class need the centralized permissive
    rule installed for the duration of each test.
    """

    def _write_skill(self, directory: Path, filename: str, name: str, body: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(
            f"---\nname: {name}\ndescription: {name} skill\n---\n{body}",
            encoding="utf-8",
        )

    def test_extra_path_skills_loaded(self, tmp_path):
        from deile.commands.settings_manager import SettingsManager

        extra_dir = tmp_path / "extra_skills"
        self._write_skill(extra_dir, "extra.md", "extra-skill", "Extra body.")

        mgr = SettingsManager(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        mgr.add_skills_path(str(extra_dir), scope="global")

        loader = SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
            settings_manager=mgr,
        )
        skills = loader.load_skills()
        names = {s.name for s in skills}
        assert "extra-skill" in names

    def test_extra_path_overrides_default_on_collision(self, tmp_path):
        from deile.commands.settings_manager import SettingsManager

        # Write same skill name in user_skills_dir and in extra dir
        loader_base = SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        user_dir = loader_base.user_skills_dir
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "clash.md").write_text(
            "---\nname: clash\ndescription: user ver\n---\nUser body.", encoding="utf-8"
        )

        extra_dir = tmp_path / "extra_skills"
        self._write_skill(extra_dir, "clash.md", "clash", "Extra body.")

        mgr = SettingsManager(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        mgr.add_skills_path(str(extra_dir), scope="global")

        loader = SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
            settings_manager=mgr,
        )
        skills = loader.load_skills()
        assert len(skills) == 1
        assert skills[0].body == "Extra body."

    def test_no_settings_manager_loads_defaults_only(self, tmp_path):
        loader = SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        # No extra paths — should not crash and should return normal scan result
        skills = loader.load_skills()
        assert isinstance(skills, list)

    def test_extra_nonexistent_dir_skipped_gracefully(self, tmp_path):
        from deile.commands.settings_manager import SettingsManager

        mgr = SettingsManager(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        mgr.add_skills_path("/nonexistent/path/does/not/exist", scope="global")

        loader = SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
            settings_manager=mgr,
        )
        skills = loader.load_skills()
        assert isinstance(skills, list)

    def test_multiple_extra_paths(self, tmp_path):
        from deile.commands.settings_manager import SettingsManager

        extra1 = tmp_path / "extra1"
        extra2 = tmp_path / "extra2"
        self._write_skill(extra1, "a.md", "skill-extra-a", "Body A.")
        self._write_skill(extra2, "b.md", "skill-extra-b", "Body B.")

        mgr = SettingsManager(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        mgr.add_skills_path(str(extra1), scope="global")
        mgr.add_skills_path(str(extra2), scope="global")

        loader = SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
            settings_manager=mgr,
        )
        names = {s.name for s in loader.load_skills()}
        assert "skill-extra-a" in names
        assert "skill-extra-b" in names


# ---------------------------------------------------------------------------
# SkillLoader.load_into_registry
# ---------------------------------------------------------------------------


class TestLoadIntoRegistry:
    def _make_loader(self, tmp_path: Path) -> SkillLoader:
        return SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )

    def _write_skill(self, directory: Path, filename: str, name: str, body: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(
            f"---\nname: {name}\ndescription: A skill\n---\n{body}",
            encoding="utf-8",
        )

    def test_registers_skill_in_registry(self, tmp_path):
        from deile.commands.registry import CommandRegistry

        loader = self._make_loader(tmp_path)
        self._write_skill(loader.user_skills_dir, "foo.md", "foo-skill", "Do foo.")
        registry = CommandRegistry()
        count = loader.load_into_registry(registry)
        assert count == 1
        cmd = registry.get_command("foo-skill")
        assert cmd is not None
        assert cmd.name == "foo-skill"

    def test_returns_count_of_registered_skills(self, tmp_path):
        from deile.commands.registry import CommandRegistry

        loader = self._make_loader(tmp_path)
        self._write_skill(loader.user_skills_dir, "a.md", "skill-a", "Body A.")
        self._write_skill(loader.user_skills_dir, "b.md", "skill-b", "Body B.")
        registry = CommandRegistry()
        count = loader.load_into_registry(registry)
        assert count == 2

    def test_zero_skills_returns_zero(self, tmp_path):
        from deile.commands.registry import CommandRegistry

        loader = self._make_loader(tmp_path)
        registry = CommandRegistry()
        assert loader.load_into_registry(registry) == 0


# ---------------------------------------------------------------------------
# SkillCommand.execute
# ---------------------------------------------------------------------------


class TestSkillCommandExecute:
    def _make_skill_command(self, tmp_path: Path, body: str = "Do something."):
        from deile.commands.registry import CommandRegistry
        from deile.commands.skill_loader import SkillLoader

        loader = SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        skills_dir = loader.user_skills_dir
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "test-skill.md").write_text(
            f"---\nname: test-skill\ndescription: Test\n---\n{body}",
            encoding="utf-8",
        )
        registry = CommandRegistry()
        loader.load_into_registry(registry)
        return registry.get_command("test-skill")

    async def test_execute_returns_llm_prompt(self, tmp_path):
        from deile.commands.base import CommandContext

        cmd = self._make_skill_command(tmp_path)
        assert cmd is not None
        ctx = CommandContext(user_input="/test-skill", args="")
        result = await cmd.execute(ctx)
        assert result.success is True
        assert result.content_type == "llm_prompt"
        assert result.content == "Do something."

    async def test_execute_appends_args(self, tmp_path):
        from deile.commands.base import CommandContext

        cmd = self._make_skill_command(tmp_path, body="Review this.")
        ctx = CommandContext(user_input="/test-skill src/main.py", args="src/main.py")
        result = await cmd.execute(ctx)
        assert "Arguments: src/main.py" in result.content
        assert result.content.startswith("Review this.")

    async def test_execute_no_args_no_suffix(self, tmp_path):
        from deile.commands.base import CommandContext

        cmd = self._make_skill_command(tmp_path, body="List issues.")
        ctx = CommandContext(user_input="/test-skill", args="   ")
        result = await cmd.execute(ctx)
        assert result.content == "List issues."

    def test_skill_appears_in_suggestions(self, tmp_path):
        from deile.commands.registry import CommandRegistry

        loader = SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        loader.user_skills_dir.mkdir(parents=True, exist_ok=True)
        (loader.user_skills_dir / "rev.md").write_text(
            "---\nname: revisar\ndescription: Revisa código\n---\nRevise o código.",
            encoding="utf-8",
        )
        registry = CommandRegistry()
        loader.load_into_registry(registry)
        suggestions = registry.get_command_suggestions("rev")
        names = [s["name"] for s in suggestions]
        assert "revisar" in names


# ---------------------------------------------------------------------------
# F1 — Refuse to override built-in / existing commands (PR #51 review)
# ---------------------------------------------------------------------------


class TestNoOverrideExistingCommand:
    """Skills must NOT silently hijack existing slash commands."""

    def _registry_with_existing_command(self, name: str = "help"):
        from deile.commands.base import (CommandContext, CommandResult,
                                         CommandStatus, SlashCommand)
        from deile.commands.registry import CommandRegistry
        from deile.config.manager import CommandConfig

        registry = CommandRegistry()

        class _Builtin(SlashCommand):
            def __init__(self) -> None:
                cfg = CommandConfig(name=name, description="REAL builtin")
                super().__init__(cfg)
                self.category = "system"

            async def execute(self, ctx: CommandContext) -> CommandResult:
                return CommandResult(
                    success=True,
                    content="from builtin",
                    status=CommandStatus.SUCCESS,
                )

        registry.register_command(_Builtin())
        return registry

    def test_skill_does_not_override_existing_builtin(self, tmp_path, monkeypatch):
        registry = self._registry_with_existing_command("help")

        loader = SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        loader.user_skills_dir.mkdir(parents=True, exist_ok=True)
        (loader.user_skills_dir / "help.md").write_text(
            "---\nname: help\ndescription: HIJACKED\n---\nHijack body.",
            encoding="utf-8",
        )

        # Patch the logger directly. caplog can be defeated by other tests in
        # the suite that mutate the global logging config (propagate=False, etc).
        from deile.commands import skill_loader as sl_mod

        warn_calls: list[str] = []

        def _capture(msg, *args, **kwargs):
            warn_calls.append(msg % args if args else msg)

        monkeypatch.setattr(sl_mod.logger, "warning", _capture)

        registered = loader.load_into_registry(registry)

        assert registered == 0
        assert registry.get_command("help").description == "REAL builtin"
        assert any("collides with existing command" in m for m in warn_calls), warn_calls
        assert any("Skipped 1 skill(s) due to name collision" in m for m in warn_calls), warn_calls

    def test_two_distinct_skills_register_when_no_collision(self, tmp_path):
        from deile.commands.registry import CommandRegistry

        registry = CommandRegistry()
        loader = SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        loader.user_skills_dir.mkdir(parents=True, exist_ok=True)
        (loader.user_skills_dir / "alpha.md").write_text("---\nname: alpha\n---\nA.", encoding="utf-8")
        (loader.user_skills_dir / "beta.md").write_text("---\nname: beta\n---\nB.", encoding="utf-8")

        registered = loader.load_into_registry(registry)

        assert registered == 2
        assert registry.get_command("alpha") is not None
        assert registry.get_command("beta") is not None


# ---------------------------------------------------------------------------
# F3 — Strict frontmatter validation (PR #51 review)
# ---------------------------------------------------------------------------


class TestFrontmatterValidation:
    def test_null_name_falls_back_to_stem(self, tmp_path):
        f = tmp_path / "my_skill.md"
        f.write_text("---\nname: null\n---\nBody here.", encoding="utf-8")
        skill = _parse_skill_file(f, source="user")
        assert skill is not None
        # null -> ignored -> falls back to stem (normalized)
        assert skill.name == "my-skill"

    def test_list_description_is_rejected_and_default_used(self, tmp_path):
        f = tmp_path / "x.md"
        f.write_text(
            "---\nname: x\ndescription:\n  - a\n  - b\n---\nBody.",
            encoding="utf-8",
        )
        skill = _parse_skill_file(f, source="user")
        assert skill is not None
        # description rejected -> default ("Skill: x") used, NOT "['a', 'b']"
        assert skill.description == "Skill: x"
        assert "[" not in skill.description

    def test_int_name_is_rejected_falls_back_to_stem(self, tmp_path):
        f = tmp_path / "numeric.md"
        f.write_text("---\nname: 42\n---\nBody.", encoding="utf-8")
        skill = _parse_skill_file(f, source="user")
        assert skill is not None
        assert skill.name == "numeric"

    def test_malformed_yaml_rejects_file_loudly(self, tmp_path, monkeypatch):
        f = tmp_path / "broken.md"
        # Unclosed quote -> YAML parse error
        f.write_text(
            "---\nname: 'unclosed\ndescription: real\n---\nBody.",
            encoding="utf-8",
        )

        # Patch the logger directly (caplog vs suite-wide logging config).
        from deile.commands import skill_loader as sl_mod

        warn_calls: list[str] = []

        def _capture(msg, *args, **kwargs):
            warn_calls.append(msg % args if args else msg)

        monkeypatch.setattr(sl_mod.logger, "warning", _capture)

        skill = _parse_skill_file(f, source="user")

        assert skill is None
        assert any("invalid YAML front-matter" in m for m in warn_calls), warn_calls

    def test_valid_frontmatter_still_works(self, tmp_path):
        f = tmp_path / "ok.md"
        f.write_text(
            "---\nname: ok\ndescription: Works fine\n---\nBody.",
            encoding="utf-8",
        )
        skill = _parse_skill_file(f, source="user")
        assert skill is not None
        assert skill.name == "ok"
        assert skill.description == "Works fine"


# ---------------------------------------------------------------------------
# reload_into_registry (hot-reload — bug fix)
# ---------------------------------------------------------------------------


class TestReloadIntoRegistry:
    def _make_loader(self, tmp_path: Path) -> SkillLoader:
        return SkillLoader(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )

    def _write_skill(self, directory: Path, filename: str, name: str, body: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(
            f"---\nname: {name}\ndescription: A skill\n---\n{body}",
            encoding="utf-8",
        )

    def test_reload_removes_stale_skill_and_adds_new_one(self, tmp_path):
        """After reload the old skill is gone and the new one is registered."""
        from deile.commands.registry import CommandRegistry

        loader = self._make_loader(tmp_path)
        skills_dir = loader.user_skills_dir

        self._write_skill(skills_dir, "old.md", "old-skill", "Old body.")
        registry = CommandRegistry()
        loader.load_into_registry(registry)
        assert registry.get_command("old-skill") is not None

        # Remove old skill file, add a new one
        (skills_dir / "old.md").unlink()
        self._write_skill(skills_dir, "new.md", "new-skill", "New body.")

        loader.reload_into_registry(registry)

        assert registry.get_command("old-skill") is None, "stale skill must be removed"
        assert registry.get_command("new-skill") is not None, "new skill must be present"

    def test_reload_does_not_touch_builtin_commands(self, tmp_path):
        """Built-in commands (no _is_skill_command marker) survive reload."""
        from deile.commands.base import (CommandContext, CommandResult,
                                         CommandStatus, SlashCommand)
        from deile.commands.registry import CommandRegistry
        from deile.config.manager import CommandConfig

        class _Builtin(SlashCommand):
            def __init__(self):
                super().__init__(CommandConfig(name="builtin", description="built-in"))

            async def execute(self, ctx: CommandContext) -> CommandResult:
                return CommandResult(success=True, content="", status=CommandStatus.SUCCESS)

        registry = CommandRegistry()
        registry.register_command(_Builtin())

        loader = self._make_loader(tmp_path)
        self._write_skill(loader.user_skills_dir, "s.md", "skill-x", "Body.")
        loader.load_into_registry(registry)
        loader.reload_into_registry(registry)

        assert registry.get_command("builtin") is not None, "built-in must survive reload"

    async def test_reload_registers_updated_skill_body(self, tmp_path):
        """A skill whose file was modified gets the updated body after reload."""
        from deile.commands.base import CommandContext
        from deile.commands.registry import CommandRegistry

        loader = self._make_loader(tmp_path)
        skills_dir = loader.user_skills_dir
        self._write_skill(skills_dir, "s.md", "my-skill", "Original body.")
        registry = CommandRegistry()
        loader.load_into_registry(registry)

        # Overwrite with new body
        (skills_dir / "s.md").write_text(
            "---\nname: my-skill\ndescription: Updated\n---\nUpdated body.",
            encoding="utf-8",
        )
        loader.reload_into_registry(registry)

        cmd = registry.get_command("my-skill")
        assert cmd is not None
        ctx = CommandContext(user_input="/my-skill", args="")
        result = await cmd.execute(ctx)
        assert "Updated body." in result.content

    def test_reload_marks_commands_with_is_skill_command(self, tmp_path):
        """Skills registered via load_into_registry carry the _is_skill_command marker."""
        from deile.commands.registry import CommandRegistry

        loader = self._make_loader(tmp_path)
        self._write_skill(loader.user_skills_dir, "x.md", "marked-skill", "Body.")
        registry = CommandRegistry()
        loader.load_into_registry(registry)
        cmd = registry.get_command("marked-skill")
        assert getattr(cmd, "_is_skill_command", False) is True
