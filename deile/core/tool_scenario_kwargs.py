"""Bridge between a tool call (name + args from the model) and the
``stage_messages`` rendering kwargs.

Centralizes the per-scenario logic that ``ToolLoopExecutor`` previously
inlined. Keeping it here lets ``tool_loop_executor.py`` stay focused on
the loop mechanics while still feeding rich stage labels to the cascade
renderer.
"""

from typing import Any, Dict, Mapping, Optional, Tuple

# Map tool names (as emitted by the model) to message-library scenario keys.
# Tools not in the map fall back to the generic "tool_executing" scenario.
TOOL_SCENARIO_MAP: Dict[str, str] = {
    "pip_install": "tool_pip_install",
    "run_tests": "tool_run_tests",
    "test_runner": "tool_run_tests",
    "find_in_files": "tool_find_files",
    "search_tool": "tool_find_files",
    "bash_execute": "tool_bash",
    "write_file": "tool_write_file",
    "file_write": "tool_write_file",
}


def build_tool_stage_kwargs(
    tc_name: str,
    tc_args: Optional[Mapping[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    """Return (scenario_key, kwargs) for ``stage_messages`` rendering.

    The kwargs always include ``tool``; per-scenario keys add the field
    each template expects (package, cmd, file, path, target, count, …).
    """
    tool_key = TOOL_SCENARIO_MAP.get(tc_name, "tool_executing")
    kwargs: Dict[str, Any] = {"tool": tc_name}
    args = tc_args or {}

    if tool_key == "tool_pip_install":
        kwargs["package"] = (
            str(next(iter(args.values()))) if args else tc_name
        )
    elif tool_key == "tool_bash":
        raw_cmd = ""
        if args:
            raw_cmd = str(
                args.get("command")
                or args.get("cmd")
                or args.get("script")
                or next(iter(args.values()))
            )
        kwargs["cmd"] = raw_cmd[:60] or tc_name
    elif tool_key == "tool_write_file":
        kwargs["file"] = (
            str(args.get("path") or args.get("file_path") or tc_name)
            if args
            else tc_name
        )
    elif tool_key == "tool_find_files":
        kwargs.setdefault(
            "path",
            str(args.get("path") or args.get("directory") or "workspace")
            if args
            else "workspace",
        )
        kwargs.setdefault("matches", 0)
        kwargs.setdefault("scanned", 0)
    elif tool_key == "tool_run_tests":
        kwargs["target"] = (
            str(args.get("target") or args.get("path") or tc_name)
            if args
            else tc_name
        )
        kwargs.setdefault("count", 0)

    return tool_key, kwargs
