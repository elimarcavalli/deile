"""Tests for issue #447: panel.activity_sources settings key.

Coverage:
  1.  _to_activity_sources: empty list → []  (sentinel for "use default V1").
  2.  _to_activity_sources: valid list of dicts → validated list.
  3.  _to_activity_sources: non-list → TypeError.
  4.  _to_activity_sources: item is not a dict → TypeError.
  5.  _to_activity_sources: deployment missing → ValueError.
  6.  _to_activity_sources: deployment fails DNS-1123 regex → ValueError.
  7.  _to_activity_sources: role missing/empty → ValueError.
  8.  _to_activity_sources: color missing/empty → ValueError.
  9.  _to_activity_sources: duplicate deployment → ValueError.
  10. _to_activity_sources: duplicate role → allowed (cosmetic).
  11. apply_overrides: valid panel.activity_sources loaded correctly.
  12. apply_overrides: invalid panel.activity_sources → field stays at [].
  13. Layered loading: user layer applies panel.activity_sources.
  14. Layered loading: project layer overrides user layer (3 > 5, D3).
  15. Layered loading: missing key → field stays at default [].
  16. panel_activity_sources NOT in _LIST_ATTRS.
  17. panel.activity_sources in _OVERRIDE_HANDLERS.
  18. panel.activity_sources NOT in _JSON_FIELD_MAP (strict-only).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from deile.config.settings import (
    Settings,
    _OVERRIDE_HANDLERS,
    _JSON_FIELD_MAP,
    _LIST_ATTRS,
    _to_activity_sources,
    get_settings,
    reset_settings,
)


# ---------------------------------------------------------------------------
# 1–10: _to_activity_sources unit tests
# ---------------------------------------------------------------------------

class TestToActivitySources:
    def test_empty_list_is_valid_sentinel(self):
        result = _to_activity_sources([])
        assert result == []

    def test_valid_list(self):
        raw = [
            {"deployment": "deile-pipeline", "role": "pipeline", "color": "bright_black"},
            {"deployment": "deile-worker",   "role": "worker",   "color": "cyan"},
        ]
        result = _to_activity_sources(raw)
        assert len(result) == 2
        assert result[0] == {"deployment": "deile-pipeline", "role": "pipeline", "color": "bright_black"}
        assert result[1] == {"deployment": "deile-worker", "role": "worker", "color": "cyan"}

    def test_non_list_raises_type_error(self):
        with pytest.raises(TypeError, match="expected list"):
            _to_activity_sources("not a list")

    def test_item_not_dict_raises_type_error(self):
        with pytest.raises(TypeError, match="expected dict"):
            _to_activity_sources(["not a dict"])

    def test_deployment_missing_raises_value_error(self):
        with pytest.raises(ValueError, match="deployment.*missing or empty"):
            _to_activity_sources([{"role": "worker", "color": "cyan"}])

    def test_deployment_invalid_dns_raises_value_error(self):
        with pytest.raises(ValueError, match="DNS-1123"):
            _to_activity_sources([
                {"deployment": "Invalid_NAME", "role": "worker", "color": "cyan"}
            ])

    def test_deployment_with_uppercase_fails(self):
        with pytest.raises(ValueError, match="DNS-1123"):
            _to_activity_sources([
                {"deployment": "MyDeployment", "role": "w", "color": "c"}
            ])

    def test_deployment_starting_with_hyphen_fails(self):
        with pytest.raises(ValueError, match="DNS-1123"):
            _to_activity_sources([
                {"deployment": "-invalid", "role": "w", "color": "c"}
            ])

    def test_role_missing_raises_value_error(self):
        with pytest.raises(ValueError, match="role.*missing or empty"):
            _to_activity_sources([
                {"deployment": "my-deploy", "role": "", "color": "cyan"}
            ])

    def test_color_missing_raises_value_error(self):
        with pytest.raises(ValueError, match="color.*missing or empty"):
            _to_activity_sources([
                {"deployment": "my-deploy", "role": "worker", "color": ""}
            ])

    def test_duplicate_deployment_raises_value_error(self):
        with pytest.raises(ValueError, match="duplicate"):
            _to_activity_sources([
                {"deployment": "deile-worker", "role": "w1", "color": "c1"},
                {"deployment": "deile-worker", "role": "w2", "color": "c2"},
            ])

    def test_duplicate_role_is_allowed(self):
        raw = [
            {"deployment": "deile-worker",   "role": "worker", "color": "cyan"},
            {"deployment": "deile-pipeline",  "role": "worker", "color": "blue"},
        ]
        result = _to_activity_sources(raw)
        assert len(result) == 2

    def test_seven_sources_accepted(self):
        """AC: 7 fontes incluindo nome novo são aceitas."""
        raw = [
            {"deployment": f"deploy-{i}", "role": f"role-{i}", "color": "cyan"}
            for i in range(7)
        ]
        result = _to_activity_sources(raw)
        assert len(result) == 7

    def test_single_char_deployment_valid(self):
        """DNS-1123: single char is valid."""
        result = _to_activity_sources([{"deployment": "x", "role": "r", "color": "c"}])
        assert result[0]["deployment"] == "x"


# ---------------------------------------------------------------------------
# 11–12: apply_overrides integration
# ---------------------------------------------------------------------------

class TestApplyOverridesActivitySources:
    def test_valid_sources_loaded(self):
        s = Settings()
        s.apply_overrides({
            "panel": {
                "activity_sources": [
                    {"deployment": "deile-pipeline", "role": "pipeline", "color": "bright_black"},
                    {"deployment": "deile-worker",   "role": "worker",   "color": "cyan"},
                    {"deployment": "deile-monitor",  "role": "monitor",  "color": "blue"},
                ]
            }
        })
        assert len(s.panel_activity_sources) == 3
        assert s.panel_activity_sources[0]["deployment"] == "deile-pipeline"

    def test_invalid_sources_keeps_default(self):
        s = Settings()
        s.apply_overrides({
            "panel": {
                "activity_sources": [
                    {"deployment": "INVALID-UPPER", "role": "r", "color": "c"},
                ]
            }
        })
        assert s.panel_activity_sources == []

    def test_empty_sources_sets_empty_list(self):
        s = Settings()
        s.panel_activity_sources = [
            {"deployment": "x", "role": "r", "color": "c"}
        ]
        s.apply_overrides({"panel": {"activity_sources": []}})
        assert s.panel_activity_sources == []

    def test_non_list_value_keeps_previous(self):
        s = Settings()
        s.apply_overrides({"panel": {"activity_sources": "not-a-list"}})
        assert s.panel_activity_sources == []


# ---------------------------------------------------------------------------
# 13–15: Layered loading via settings files
# ---------------------------------------------------------------------------

class TestLayeredLoadingActivitySources:
    def test_user_layer_applies_sources(self, tmp_path: Path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({
            "panel": {
                "activity_sources": [
                    {"deployment": "my-pod", "role": "myrole", "color": "green"},
                ]
            }
        }))
        import deile.config.settings as _sm
        orig_resolve = _sm._resolve_global_settings_path
        _sm._resolve_global_settings_path = lambda: settings_file
        reset_settings()
        try:
            s = get_settings()
            assert len(s.panel_activity_sources) == 1
            assert s.panel_activity_sources[0]["deployment"] == "my-pod"
        finally:
            _sm._resolve_global_settings_path = orig_resolve
            reset_settings()

    def test_project_layer_overrides_user_layer(self, tmp_path: Path):
        """D3: last layer wins — 3-source project layer overrides 5-source user layer."""
        user_file = tmp_path / "user_settings.json"
        user_file.write_text(json.dumps({
            "panel": {
                "activity_sources": [
                    {"deployment": f"deploy-{i}", "role": f"r{i}", "color": "c"}
                    for i in range(5)
                ]
            }
        }))
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_settings = project_dir / ".deile" / "settings.json"
        project_settings.parent.mkdir(parents=True)
        project_settings.write_text(json.dumps({
            "panel": {
                "activity_sources": [
                    {"deployment": "proj-pod-0", "role": "pr0", "color": "blue"},
                    {"deployment": "proj-pod-1", "role": "pr1", "color": "red"},
                    {"deployment": "proj-pod-2", "role": "pr2", "color": "green"},
                ]
            },
            "trust": {"project_layer_dirs": [str(project_dir)]},
        }))
        import deile.config.settings as _sm
        orig_resolve = _sm._resolve_global_settings_path
        orig_cwd = os.getcwd()
        _sm._resolve_global_settings_path = lambda: user_file
        reset_settings()
        os.chdir(project_dir)
        try:
            s = get_settings()
            # Project layer (3 sources) must win over user layer (5 sources).
            assert len(s.panel_activity_sources) == 3
            assert s.panel_activity_sources[0]["deployment"] == "proj-pod-0"
        finally:
            os.chdir(orig_cwd)
            _sm._resolve_global_settings_path = orig_resolve
            reset_settings()

    def test_missing_key_keeps_default(self, tmp_path: Path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"logging": {"level": "INFO"}}))
        import deile.config.settings as _sm
        orig_resolve = _sm._resolve_global_settings_path
        _sm._resolve_global_settings_path = lambda: settings_file
        reset_settings()
        try:
            s = get_settings()
            assert s.panel_activity_sources == []
        finally:
            _sm._resolve_global_settings_path = orig_resolve
            reset_settings()


# ---------------------------------------------------------------------------
# 16–18: Schema / map membership invariants
# ---------------------------------------------------------------------------

class TestSchemaInvariants:
    def test_panel_activity_sources_not_in_list_attrs(self):
        assert "panel_activity_sources" not in _LIST_ATTRS

    def test_panel_activity_sources_in_override_handlers(self):
        assert "panel.activity_sources" in _OVERRIDE_HANDLERS

    def test_panel_activity_sources_not_in_json_field_map(self):
        """Strict-only: _apply_nested_dict must not process it directly."""
        assert "panel.activity_sources" not in _JSON_FIELD_MAP
