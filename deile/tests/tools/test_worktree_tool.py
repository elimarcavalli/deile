"""Tests for WorktreeTool.

Design notes
------------
- No real git operations: WorktreeManager is monkeypatched / replaced with an
  AsyncMock at the class level so tests stay fast and hermetic.
- Schema regression: ``test_schema_is_json_schema_object`` mirrors the guard
  introduced after commit 5815725 — it checks both the raw ``parameters`` dict
  and the serialised ``to_openai_function()`` output.
- ``asyncio_mode = auto`` in pytest.ini — no ``@pytest.mark.asyncio`` needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.tools._pipeline_paths import resolve_base_path as _resolve_base_path
from deile.tools.base import ToolContext, ToolStatus
from deile.tools.worktree_tool import WorktreeTool

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ctx(**kwargs) -> ToolContext:
    return ToolContext(user_input="", parsed_args=kwargs, session_data={})


def _fake_worktree(path: str = "/repo/.worktrees/feat/my-branch", branch: str = "my-branch"):
    wt = MagicMock()
    wt.path = Path(path)
    wt.branch = branch
    wt.base_repo = Path("/repo")
    return wt


# ---------------------------------------------------------------------------
# 1. Schema validity — the regression guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_schema_is_json_schema_object():
    """parameters must be a JSON Schema object, not a raw property dict.

    Regression guard for the bug caught in commit 5815725: passing a raw
    ``{"action": {...}}`` dict instead of
    ``{"type": "object", "properties": {"action": {...}}}`` silently broke
    all providers that validate the schema.
    """
    tool = WorktreeTool()
    params = tool._schema.parameters
    assert params["type"] == "object", (
        "parameters must have top-level 'type': 'object'"
    )
    assert "properties" in params, "parameters must contain 'properties'"
    assert "action" in params["properties"]


@pytest.mark.unit
def test_to_openai_function_parameters_type_is_object():
    """to_openai_function() must emit 'type': 'object' in parameters."""
    tool = WorktreeTool()
    oai = tool.schema.to_openai_function()
    assert oai["function"]["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# 2. Invalid action
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_invalid_action_returns_error():
    tool = WorktreeTool()
    result = await tool.execute(_ctx(action="fly"))
    assert result.status == ToolStatus.ERROR
    assert result.metadata["error_code"] == "INVALID_ACTION"


@pytest.mark.unit
async def test_empty_action_returns_error():
    tool = WorktreeTool()
    result = await tool.execute(_ctx(action=""))
    assert result.status == ToolStatus.ERROR
    assert result.metadata["error_code"] == "INVALID_ACTION"


# ---------------------------------------------------------------------------
# 3. ensure_main happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_ensure_main_happy_path(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "deile.py").write_text("")

    mock_mgr = AsyncMock()
    mock_mgr.ensure_main = AsyncMock(return_value=tmp_path / ".worktrees" / "main")

    tool = WorktreeTool()
    with patch(
        "deile.tools.worktree_tool.WorktreeManager", return_value=mock_mgr
    ):
        result = await tool.execute(_ctx(action="ensure_main", base_path=str(tmp_path)))

    assert result.status == ToolStatus.SUCCESS
    assert "path" in result.data
    mock_mgr.ensure_main.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. create happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_create_happy_path(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "deile.py").write_text("")

    wt = _fake_worktree(str(tmp_path / ".worktrees" / "feat" / "my-branch"), "my-branch")
    mock_mgr = AsyncMock()
    mock_mgr.create_branch_worktree = AsyncMock(return_value=wt)

    tool = WorktreeTool()
    with patch(
        "deile.tools.worktree_tool.WorktreeManager", return_value=mock_mgr
    ):
        result = await tool.execute(
            _ctx(action="create", branch="my-branch", subdir="feat", base_path=str(tmp_path))
        )

    assert result.status == ToolStatus.SUCCESS
    assert result.data["branch"] == "my-branch"
    assert "path" in result.data
    mock_mgr.create_branch_worktree.assert_awaited_once_with("my-branch")


@pytest.mark.unit
async def test_create_missing_branch_returns_error(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "deile.py").write_text("")

    tool = WorktreeTool()
    result = await tool.execute(_ctx(action="create", base_path=str(tmp_path)))

    assert result.status == ToolStatus.ERROR
    assert result.metadata["error_code"] == "MISSING_BRANCH"


# ---------------------------------------------------------------------------
# 5. list happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_returns_worktrees(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "deile.py").write_text("")
    # Create fake worktree directories
    wt_dir = tmp_path / ".worktrees" / "feat" / "branch-a"
    wt_dir.mkdir(parents=True)
    (wt_dir / ".git").mkdir()

    tool = WorktreeTool()
    result = await tool.execute(_ctx(action="list", base_path=str(tmp_path)))

    assert result.status == ToolStatus.SUCCESS
    assert isinstance(result.data["worktrees"], list)
    assert len(result.data["worktrees"]) == 1
    assert result.data["worktrees"][0]["branch"] == "branch-a"


@pytest.mark.unit
async def test_list_no_worktrees_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "deile.py").write_text("")

    tool = WorktreeTool()
    result = await tool.execute(_ctx(action="list", base_path=str(tmp_path)))

    assert result.status == ToolStatus.SUCCESS
    assert result.data["worktrees"] == []


# ---------------------------------------------------------------------------
# 6. remove happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_remove_happy_path(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "deile.py").write_text("")
    branch_dir = tmp_path / ".worktrees" / "my-branch"
    branch_dir.mkdir(parents=True)

    tool = WorktreeTool()
    result = await tool.execute(
        _ctx(action="remove", branch="my-branch", base_path=str(tmp_path))
    )

    assert result.status == ToolStatus.SUCCESS
    assert not branch_dir.exists()


@pytest.mark.unit
async def test_remove_missing_branch_returns_error(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "deile.py").write_text("")

    tool = WorktreeTool()
    result = await tool.execute(_ctx(action="remove", base_path=str(tmp_path)))

    assert result.status == ToolStatus.ERROR
    assert result.metadata["error_code"] == "MISSING_BRANCH"


# ---------------------------------------------------------------------------
# 7. remove safety: refuse to remove 'main'
# ---------------------------------------------------------------------------


@pytest.mark.security
async def test_remove_refuses_main(tmp_path):
    """The shared clean main clone must never be removed."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "deile.py").write_text("")
    main_dir = tmp_path / ".worktrees" / "main"
    main_dir.mkdir(parents=True)

    tool = WorktreeTool()
    result = await tool.execute(
        _ctx(action="remove", branch="main", base_path=str(tmp_path))
    )

    assert result.status == ToolStatus.ERROR
    assert result.metadata["error_code"] == "REMOVE_PROTECTED"
    # Directory must still exist.
    assert main_dir.exists()


# ---------------------------------------------------------------------------
# 8. remove non-existent path returns NOT_FOUND
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_remove_nonexistent_returns_not_found(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "deile.py").write_text("")

    tool = WorktreeTool()
    result = await tool.execute(
        _ctx(action="remove", branch="ghost-branch", base_path=str(tmp_path))
    )

    assert result.status == ToolStatus.ERROR
    assert result.metadata["error_code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 9. Auto-discover registration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_registered_by_auto_discover():
    from deile.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.auto_discover()
    assert reg.get("worktree") is not None


# ---------------------------------------------------------------------------
# 10. _resolve_base_path utility
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_base_path_uses_override(tmp_path):
    result = _resolve_base_path(str(tmp_path))
    assert result == tmp_path.resolve()


@pytest.mark.unit
def test_resolve_base_path_uses_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("DEILE_PIPELINE_BASE_PATH", str(tmp_path))
    result = _resolve_base_path()
    assert result == tmp_path.resolve()


@pytest.mark.unit
def test_resolve_base_path_override_beats_env(tmp_path, monkeypatch, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    monkeypatch.setenv("DEILE_PIPELINE_BASE_PATH", str(other))
    result = _resolve_base_path(str(tmp_path))
    assert result == tmp_path.resolve()
