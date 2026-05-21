"""Tests for build_tool_stage_kwargs — the bridge that maps a tool call
(``name`` + ``args`` from the model) into the kwargs consumed by the
cascade stage-message renderer.
"""

from __future__ import annotations

import pytest

from deile.core.tool_scenario_kwargs import (TOOL_SCENARIO_MAP,
                                             build_tool_stage_kwargs)


@pytest.mark.unit
class TestBuildToolStageKwargs:
    def test_unknown_tool_falls_back_to_generic(self):
        scenario, kwargs = build_tool_stage_kwargs("totally_made_up", {"x": 1})
        assert scenario == "tool_executing"
        assert kwargs == {"tool": "totally_made_up"}

    def test_empty_args_does_not_crash(self):
        scenario, kwargs = build_tool_stage_kwargs("bash_execute", None)
        assert scenario == "tool_bash"
        assert kwargs["tool"] == "bash_execute"
        # No args → cmd falls back to the tool name (never empty/missing).
        assert kwargs["cmd"] == "bash_execute"

    def test_empty_args_dict_treated_as_no_args(self):
        scenario, kwargs = build_tool_stage_kwargs("pip_install", {})
        assert scenario == "tool_pip_install"
        # Empty mapping → package falls back to tool name.
        assert kwargs["package"] == "pip_install"

    def test_pip_install_picks_first_arg_value(self):
        scenario, kwargs = build_tool_stage_kwargs(
            "pip_install", {"package": "httpx"}
        )
        assert scenario == "tool_pip_install"
        assert kwargs == {"tool": "pip_install", "package": "httpx"}

    def test_bash_truncates_cmd_to_60_chars(self):
        long_cmd = "echo " + ("x" * 200)
        scenario, kwargs = build_tool_stage_kwargs(
            "bash_execute", {"command": long_cmd}
        )
        assert scenario == "tool_bash"
        assert len(kwargs["cmd"]) == 60
        assert kwargs["cmd"] == long_cmd[:60]

    def test_bash_accepts_alt_arg_names(self):
        # The mapper checks command → cmd → script → first value.
        for key in ("cmd", "script"):
            _, kwargs = build_tool_stage_kwargs("bash_execute", {key: "ls -la"})
            assert kwargs["cmd"] == "ls -la"

    def test_write_file_prefers_path_then_file_path(self):
        _, kwargs = build_tool_stage_kwargs(
            "write_file", {"path": "/tmp/a.py", "file_path": "/tmp/b.py"}
        )
        assert kwargs["file"] == "/tmp/a.py"

        _, kwargs = build_tool_stage_kwargs(
            "write_file", {"file_path": "/tmp/b.py"}
        )
        assert kwargs["file"] == "/tmp/b.py"

    def test_write_file_alias_file_write(self):
        scenario, kwargs = build_tool_stage_kwargs(
            "file_write", {"path": "x.py"}
        )
        assert scenario == "tool_write_file"
        assert kwargs["file"] == "x.py"

    def test_find_files_defaults_path_and_counters(self):
        scenario, kwargs = build_tool_stage_kwargs("find_in_files", None)
        assert scenario == "tool_find_files"
        assert kwargs["path"] == "workspace"
        assert kwargs["matches"] == 0
        assert kwargs["scanned"] == 0

    def test_find_files_uses_directory_when_path_missing(self):
        _, kwargs = build_tool_stage_kwargs(
            "search_tool", {"directory": "src/"}
        )
        assert kwargs["path"] == "src/"

    def test_run_tests_target_and_count_defaults(self):
        scenario, kwargs = build_tool_stage_kwargs(
            "run_tests", {"target": "deile/tests/"}
        )
        assert scenario == "tool_run_tests"
        assert kwargs["target"] == "deile/tests/"
        assert kwargs["count"] == 0

    def test_run_tests_alias_test_runner(self):
        scenario, kwargs = build_tool_stage_kwargs("test_runner", {"path": "p/"})
        assert scenario == "tool_run_tests"
        assert kwargs["target"] == "p/"

    def test_every_mapped_scenario_returns_tool_key(self):
        # Smoke: every entry in the scenario map must include ``tool`` in
        # the returned kwargs so the renderer can show what was called.
        for tool_name in TOOL_SCENARIO_MAP:
            _, kwargs = build_tool_stage_kwargs(tool_name, {})
            assert kwargs["tool"] == tool_name
