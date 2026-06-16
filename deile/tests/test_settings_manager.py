"""Tests: SettingsManager — issue #104.

These tests cover the JSON-persistence semantics of ``SettingsManager``;
the permission-gate / audit emission added by issue #125 is exercised in
``test_settings_manager_audit.py``. Apply ``allow_settings_writes`` (root
conftest) module-wide so the legacy happy-path tests below run regardless
of the fail-closed default introduced by #125 — without depending on test
order in the broader suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deile.commands.settings_manager import SettingsManager

pytestmark = pytest.mark.usefixtures("allow_settings_writes")

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
        assert (
            mgr.global_settings_path == tmp_path / "home" / ".deile" / "settings.json"
        )

    def test_project_settings_path_in_project(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert (
            mgr.project_settings_path
            == tmp_path / "project" / ".deile" / "settings.json"
        )


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
        mgr.global_settings_path.write_text(
            json.dumps({"other_key": "value"}), encoding="utf-8"
        )
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


# ---------------------------------------------------------------------------
# get_layer / get_merged / get_setting / set_setting (issue #111)
# ---------------------------------------------------------------------------


def _write_layer(mgr: SettingsManager, scope: str, data: dict) -> None:
    path = mgr.global_settings_path if scope == "global" else mgr.project_settings_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestGetLayer:
    def test_empty_when_no_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.get_layer("global") == {}
        assert mgr.get_layer("project") == {}

    def test_returns_full_dict(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(
            mgr, "global", {"logging": {"level": "INFO"}, "skills_paths": ["/x"]}
        )
        layer = mgr.get_layer("global")
        assert layer == {"logging": {"level": "INFO"}, "skills_paths": ["/x"]}

    def test_returns_deep_copy_not_reference(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"logging": {"level": "INFO"}})
        layer = mgr.get_layer("global")
        layer["logging"]["level"] = "DEBUG"
        assert mgr.get_layer("global") == {"logging": {"level": "INFO"}}

    def test_invalid_scope_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="Invalid scope"):
            mgr.get_layer("nope")


class TestGetMerged:
    def test_empty_when_no_files(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.get_merged() == {}

    def test_only_user_layer(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"logging": {"level": "INFO"}})
        assert mgr.get_merged() == {"logging": {"level": "INFO"}}

    def test_only_project_layer(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "project", {"ui": {"streaming_enabled": False}})
        assert mgr.get_merged() == {"ui": {"streaming_enabled": False}}

    def test_project_wins_at_leaf(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"logging": {"level": "INFO"}})
        _write_layer(mgr, "project", {"logging": {"level": "DEBUG"}})
        assert mgr.get_merged() == {"logging": {"level": "DEBUG"}}

    def test_deep_merge_preserves_sibling_keys(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"logging": {"level": "INFO", "to_file": True}})
        _write_layer(mgr, "project", {"logging": {"level": "DEBUG"}})
        merged = mgr.get_merged()
        assert merged == {"logging": {"level": "DEBUG", "to_file": True}}

    def test_lists_replace_no_concat(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"file_safety": {"blocked": ["a", "b"]}})
        _write_layer(mgr, "project", {"file_safety": {"blocked": ["c"]}})
        assert mgr.get_merged() == {"file_safety": {"blocked": ["c"]}}

    def test_independent_top_level_keys(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"logging": {"level": "INFO"}})
        _write_layer(mgr, "project", {"ui": {"streaming_enabled": False}})
        merged = mgr.get_merged()
        assert merged["logging"] == {"level": "INFO"}
        assert merged["ui"] == {"streaming_enabled": False}

    def test_dict_replaces_scalar(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"logging": "INFO"})
        _write_layer(mgr, "project", {"logging": {"level": "DEBUG"}})
        assert mgr.get_merged() == {"logging": {"level": "DEBUG"}}

    def test_scalar_replaces_dict(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"logging": {"level": "DEBUG"}})
        _write_layer(mgr, "project", {"logging": "INFO"})
        assert mgr.get_merged() == {"logging": "INFO"}


class TestGetSetting:
    def test_default_when_missing(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.get_setting("logging.level", default="WARN") == "WARN"

    def test_returns_value_from_user_layer(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"logging": {"level": "INFO"}})
        assert mgr.get_setting("logging.level") == "INFO"

    def test_returns_value_from_project_layer(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "project", {"ui": {"streaming_enabled": False}})
        assert mgr.get_setting("ui.streaming_enabled") is False

    def test_project_wins_over_user(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"logging": {"level": "INFO"}})
        _write_layer(mgr, "project", {"logging": {"level": "DEBUG"}})
        assert mgr.get_setting("logging.level") == "DEBUG"

    def test_top_level_key(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"environment": "production"})
        assert mgr.get_setting("environment") == "production"

    def test_intermediate_non_dict_returns_default(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(mgr, "global", {"logging": "INFO"})
        assert mgr.get_setting("logging.level", default="missing") == "missing"

    def test_empty_key_path_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError):
            mgr.get_setting("")

    def test_key_path_with_empty_segment_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError):
            mgr.get_setting("logging..level")


class TestSetSetting:
    def test_creates_file_with_nested_value(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.set_setting("logging.level", "DEBUG", scope="global") is True
        data = json.loads(mgr.global_settings_path.read_text())
        assert data == {"logging": {"level": "DEBUG"}}

    def test_writes_to_project_scope(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("ui.streaming_enabled", False, scope="project")
        data = json.loads(mgr.project_settings_path.read_text())
        assert data == {"ui": {"streaming_enabled": False}}

    def test_preserves_unrelated_existing_keys(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _write_layer(
            mgr, "global", {"skills_paths": ["/x"], "logging": {"to_file": True}}
        )
        mgr.set_setting("logging.level", "DEBUG", scope="global")
        data = json.loads(mgr.global_settings_path.read_text())
        assert data == {
            "skills_paths": ["/x"],
            "logging": {"to_file": True, "level": "DEBUG"},
        }

    def test_overwrites_existing_value(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("logging.level", "INFO")
        mgr.set_setting("logging.level", "DEBUG")
        assert mgr.get_setting("logging.level") == "DEBUG"

    def test_top_level_scalar(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("environment", "production")
        data = json.loads(mgr.global_settings_path.read_text())
        assert data == {"environment": "production"}

    def test_intermediate_non_dict_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("logging", "INFO")
        with pytest.raises(ValueError, match="not dict"):
            mgr.set_setting("logging.level", "DEBUG")

    def test_invalid_scope_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="Invalid scope"):
            mgr.set_setting("logging.level", "INFO", scope="bad")

    def test_empty_key_path_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError):
            mgr.set_setting("", "value")

    def test_creates_parent_directories(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert not mgr.global_settings_path.parent.exists()
        mgr.set_setting("a.b", 1)
        assert mgr.global_settings_path.parent.is_dir()
