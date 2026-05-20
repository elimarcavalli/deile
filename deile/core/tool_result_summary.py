"""One-line ``ToolResult`` summaries for inline UI rendering.

Renderer-agnostic formatters for ToolResult — given a :class:`ToolResult`
(and, where useful, the originating tool name pulled from
``metadata.function_name``), produce a short single-line preview that the
cascade renderer drops into stage messages. No I/O, no orchestration, no
executor state.

Lives outside ``tool_loop_executor`` because the loop owns iteration and
registry execution; the formatters only read fields off ``ToolResult``
and return strings (feature envy that did not belong on the executor).
"""

from __future__ import annotations

from typing import Optional

from deile.tools.base import ToolResult, ToolStatus

SUMMARY_MAX_CHARS = 200


def summarize(result: ToolResult, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """Build a short, single-line preview suitable for inline UI rendering.

    Tool-name aware: for known tools, render a semantic summary
    (``exit 0 • 23ms``, ``46 bytes • 2 lines``, ``3 entries: a, b, c``)
    instead of dumping ``str(result.data)`` which would surface Python repr
    of dicts/lists in the terminal.
    """
    if result.status == ToolStatus.ERROR:
        prefix = "error: "
        body = result.message or (str(result.error) if result.error else "(no message)")
        body = body.replace("\n", " ").replace("\r", " ").strip()
        text = prefix + body
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        return text

    # Success path — try a tool-specific renderer first.
    meta = result.metadata or {}
    tool_name = str(meta.get("function_name") or "")
    semantic = semantic_summary(tool_name, result)
    if semantic is not None:
        body = semantic
    elif result.data is not None:
        body = str(result.data)
    else:
        body = result.message or "ok"
    body = body.replace("\n", " ").replace("\r", " ").strip()
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"
    return body


def semantic_summary(tool_name: str, result: ToolResult) -> Optional[str]:
    """Tool-specific one-line summaries — return ``None`` to fall back.

    Strict on shape: we read from ``metadata``/``data`` defensively so
    odd providers can't crash the renderer. Anything weird → return None
    and let the generic path handle it.
    """
    meta = result.metadata or {}
    data = result.data

    if tool_name in ("bash_execute", "python_execute"):
        # bash_tool.py packs data as dict; execution_tools.py packs string in data + dict in metadata.
        exit_code = None
        exec_time = None
        stdout = ""
        stderr = ""
        if isinstance(data, dict):
            exit_code = data.get("exit_code")
            exec_time = data.get("execution_time")
            stdout = str(data.get("stdout") or "")
            stderr = str(data.get("stderr") or "")
        else:
            exit_code = meta.get("exit_code")
            exec_time = meta.get("execution_time")
            stdout = str(meta.get("stdout") or (data if isinstance(data, str) else ""))
            stderr = str(meta.get("stderr") or "")
        parts = []
        if exit_code is not None:
            parts.append(f"exit {exit_code}")
        if isinstance(exec_time, (int, float)):
            parts.append(f"{int(exec_time * 1000)}ms")
        head = " • ".join(parts) or "ok"
        # Append first line of stdout (or stderr if errored) for context.
        trailer = ""
        snippet_source = stderr if (isinstance(exit_code, int) and exit_code != 0 and stderr) else stdout
        first_line = next(
            (ln.strip() for ln in snippet_source.splitlines() if ln.strip()),
            "",
        )
        if first_line:
            if len(first_line) > 80:
                first_line = first_line[:77] + "…"
            trailer = f" • {first_line}"
        return head + trailer

    if tool_name == "read_file":
        size = meta.get("file_size")
        if size is None and isinstance(data, str):
            size = len(data)
        lines = None
        if isinstance(data, str):
            lines = data.count("\n") + (0 if data.endswith("\n") else 1) if data else 0
        if size is not None and lines is not None:
            return f"{size} bytes • {lines} line" + ("" if lines == 1 else "s")
        if size is not None:
            return f"{size} bytes"
        return None

    if tool_name == "write_file":
        length = meta.get("content_length")
        rel = meta.get("project_relative_path") or meta.get("input_path") or ""
        if isinstance(length, int) and rel:
            return f"{length} bytes written → {rel}"
        if isinstance(length, int):
            return f"{length} bytes written"
        return None

    if tool_name == "edit_file":
        rel = meta.get("project_relative_path") or meta.get("input_path") or ""
        patches = meta.get("patches_applied") or meta.get("patch_count")
        if isinstance(patches, int) and rel:
            return f"{patches} patch" + ("" if patches == 1 else "es") + f" → {rel}"
        if rel:
            return f"updated → {rel}"
        return None

    if tool_name == "list_files":
        total = meta.get("total_items")
        if total is None and isinstance(data, list):
            total = len(data)
        names: list = []
        if isinstance(data, list):
            for entry in data[:5]:
                if isinstance(entry, dict):
                    n = entry.get("name") or entry.get("path") or ""
                    if entry.get("type") == "directory":
                        n = f"{n}/"
                    if n:
                        names.append(str(n))
                elif isinstance(entry, str):
                    names.append(entry)
        if total is not None and names:
            preview = ", ".join(names[:3])
            if total > 3:
                preview += f", … +{total - 3}"
            return f"{total} entr{'y' if total == 1 else 'ies'}: {preview}"
        if total is not None:
            return f"{total} entr{'y' if total == 1 else 'ies'}"
        return None

    if tool_name == "delete_file":
        if result.message:
            msg = result.message.replace("Successfully deleted directory: ", "deleted dir ")
            msg = msg.replace("Successfully deleted file: ", "deleted ")
            msg = msg.replace("Successfully deleted: ", "deleted ")
            return msg
        return "deleted"

    return None
