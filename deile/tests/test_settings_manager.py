"""Tests: SettingsManager — issue #104."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deile.commands.settings_manager import SettingsManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(tmp_path: Path) -> SettingsManager:
    return SettingsManager(
        project_dir=tmp_path / "project",
        user_home=tmp_path / "home",
    )


# ---------------------------------------------------------------------------
# Path properties
# ---------------------------------------------------------------------------


class TestPaths:
    def test_global_settings_path_in_home(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.global_settings_path == tmp_path / "home" / ".deile" / "settings.json"

    def test_project_settings_path_in_project(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.project_settings_path == tmp_path / "project" / ".deile" / "settings.json"


# ---------------------------------------------------------------------------
# list_skills_paths
# ---------------------------------------------------------------------------


class TestListSkillsPaths:
    def test_global_empty_when_no_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.list_skills_paths("global") == []

    def test_project_empty_when_no_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.list_skills_paths("project") == []

    def test_global_returns_stored_paths(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.global_settings_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.global_settings_path.write_text(
            json.dumps({"skills_paths": ["/foo", "/bar"]}), encoding="utf-8"
        )
        assert mgr.list_skills_paths("global") == ["/foo", "/bar"]

    def test_project_returns_stored_paths(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.project_settings_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.project_settings_path.write_text(
            json.dumps({"skills_paths": ["/baz"]}), encoding="utf-8"
        )
        assert mgr.list_skills_paths("project") == ["/baz"]

    def test_invalid_json_returns_empty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.global_settings_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.global_settings_path.write_text("not valid json!!!", encoding="utf-8")
        assert mgr.list_skills_paths("global") == []

    def test_non_dict_json_returns_empty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.global_settings_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.global_settings_path.write_text("[1, 2, 3]", encoding="utf-8")
        assert mgr.list_skills_paths("global") == []

    def test_missing_skills_paths_key_returns_empty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.global_settings_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.global_settings_path.write_text(json.dumps({"other_key": "value"}), encoding="utf-8")
        assert mgr.list_skills_paths("global") == []

    def test_invalid_scope_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="Invalid scope"):
            mgr.list_skills_paths("invalid")

    def test_returns_copy_not_reference(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/a")
        paths = mgr.list_skills_paths("global")
        paths.append("/mutated")
        assert "/mutated" not in mgr.list_skills_paths("global")


# ---------------------------------------------------------------------------
# add_skills_path
# ---------------------------------------------------------------------------


class TestAddSkillsPath:
    def test_add_global_creates_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = mgr.add_skills_path("/my/skills", scope="global")
        assert result is True
        assert mgr.global_settings_path.exists()
        data = json.loads(mgr.global_settings_path.read_text())
        assert "/my/skills" in data["skills_paths"]

    def test_add_project_creates_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = mgr.add_skills_path("/team/skills", scope="project")
        assert result is True
        data = json.loads(mgr.project_settings_path.read_text())
        assert "/team/skills" in data["skills_paths"]

    def test_add_duplicate_returns_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/foo", scope="global")
        result = mgr.add_skills_path("/foo", scope="global")
        assert result is False
        assert mgr.list_skills_paths("global").count("/foo") == 1

    def test_add_multiple_paths(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/a")
        mgr.add_skills_path("/b")
        paths = mgr.list_skills_paths("global")
        assert "/a" in paths
        assert "/b" in paths

    def test_add_accepts_path_object(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path(Path("/path/obj"))
        assert str(Path("/path/obj")) in mgr.list_skills_paths("global")

    def test_add_invalid_scope_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="Invalid scope"):
            mgr.add_skills_path("/foo", scope="bad")

    def test_add_preserves_existing_paths(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/first")
        mgr.add_skills_path("/second")
        paths = mgr.list_skills_paths("global")
        assert "/first" in paths
        assert "/second" in paths

    def test_add_global_creates_parent_dirs(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert not mgr.global_settings_path.parent.exists()
        mgr.add_skills_path("/x")
        assert mgr.global_settings_path.parent.is_dir()


# ---------------------------------------------------------------------------
# remove_skills_path
# ---------------------------------------------------------------------------


class TestRemoveSkillsPath:
    def test_remove_existing_returns_true(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/x")
        result = mgr.remove_skills_path("/x")
        assert result is True
        assert "/x" not in mgr.list_skills_paths("global")

    def test_remove_nonexistent_returns_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = mgr.remove_skills_path("/not-there")
        assert result is False

    def test_remove_project_path(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/proj", scope="project")
        mgr.remove_skills_path("/proj", scope="project")
        assert "/proj" not in mgr.list_skills_paths("project")

    def test_remove_one_keeps_others(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/a")
        mgr.add_skills_path("/b")
        mgr.remove_skills_path("/a")
        paths = mgr.list_skills_paths("global")
        assert "/a" not in paths
        assert "/b" in paths

    def test_remove_invalid_scope_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="Invalid scope"):
            mgr.remove_skills_path("/foo", scope="nope")


# ---------------------------------------------------------------------------
# get_all_skills_paths (merge)
# ---------------------------------------------------------------------------


class TestGetAllSkillsPaths:
    def test_merge_global_and_project(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/global-skills", scope="global")
        mgr.add_skills_path("/project-skills", scope="project")
        str_paths = [str(p) for p in mgr.get_all_skills_paths()]
        assert any("global-skills" in p for p in str_paths)
        assert any("project-skills" in p for p in str_paths)

    def test_merge_deduplicates(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/same", scope="global")
        mgr.add_skills_path("/same", scope="project")
        paths = mgr.get_all_skills_paths()
        assert len(paths) == 1

    def test_merge_empty_when_none_configured(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.get_all_skills_paths() == []

    def test_merge_returns_path_objects(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/some/skills")
        paths = mgr.get_all_skills_paths()
        assert all(isinstance(p, Path) for p in paths)

    def test_merge_global_before_project(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/global-first", scope="global")
        mgr.add_skills_path("/project-second", scope="project")
        str_paths = [str(p) for p in mgr.get_all_skills_paths()]
        global_idx = next(i for i, p in enumerate(str_paths) if "global-first" in p)
        project_idx = next(i for i, p in enumerate(str_paths) if "project-second" in p)
        assert global_idx < project_idx

    def test_merge_only_global(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/g1")
        mgr.add_skills_path("/g2")
        paths = [str(p) for p in mgr.get_all_skills_paths()]
        assert "/g1" in paths
        assert "/g2" in paths

    def test_merge_only_project(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/p1", scope="project")
        paths = [str(p) for p in mgr.get_all_skills_paths()]
        assert "/p1" in paths
