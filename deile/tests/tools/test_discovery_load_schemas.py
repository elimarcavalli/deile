"""Unit tests for ``deile.tools.discovery.load_schemas_from_directory``.

Complements ``test_discovery.py`` (which covers ``discover_tools_in_package``).
Pinpoints the contracts of the newly extracted schema-loader: only uses
the registry's public API, returns the count of matched schemas, skips
malformed files without aborting the batch, and tolerates schemas for
unregistered tools (logged, not raised).
"""
from __future__ import annotations

import json
from pathlib import Path

from deile.tools.base import (SecurityLevel, Tool, ToolCategory, ToolContext,
                              ToolResult, ToolStatus)
from deile.tools.discovery import load_schemas_from_directory
from deile.tools.registry import ToolRegistry


class _DummyTool(Tool):
    """Concrete Tool stub matching the schema name 'dummy_one'."""

    @property
    def name(self) -> str:
        return "dummy_one"

    @property
    def description(self) -> str:
        return "stub for schema loading tests"

    @property
    def category(self) -> str:
        return "other"

    async def execute(self, context: ToolContext) -> ToolResult:
        return ToolResult(status=ToolStatus.SUCCESS, message="ok")


def _write_schema_file(directory: Path, name: str) -> Path:
    path = directory / f"{name}.json"
    payload = {
        "name": name,
        "description": "loaded from disk",
        "parameters": {"type": "object", "properties": {}},
        "required": [],
        "category": ToolCategory.OTHER.value,
        "security_level": SecurityLevel.SAFE.value,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_missing_directory_returns_zero(tmp_path):
    registry = ToolRegistry()
    missing = tmp_path / "does-not-exist"
    assert load_schemas_from_directory(registry, missing) == 0


def test_loads_schema_into_registered_tool(tmp_path):
    registry = ToolRegistry()
    tool = _DummyTool()
    # Ensure no schema set so we can assert mutation downstream.
    tool._schema = None
    registry.register(tool)

    _write_schema_file(tmp_path, "dummy_one")
    count = load_schemas_from_directory(registry, tmp_path)
    assert count == 1
    loaded = registry.get("dummy_one")
    assert loaded is not None
    assert loaded.schema is not None
    assert loaded.schema.description == "loaded from disk"


def test_skips_schema_for_unregistered_tool(tmp_path):
    import logging

    from deile.tools import discovery as discovery_mod

    registry = ToolRegistry()  # nothing registered
    _write_schema_file(tmp_path, "ghost_tool")

    # Attach a private handler to the discovery logger so we don't rely
    # on caplog's propagation semantics — other modules in the suite
    # silence or redirect the root logger, which makes caplog flaky in
    # the full pytest run.
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture(level=logging.WARNING)
    discovery_mod.logger.addHandler(handler)
    original_level = discovery_mod.logger.level
    discovery_mod.logger.setLevel(logging.WARNING)
    try:
        count = load_schemas_from_directory(registry, tmp_path)
    finally:
        discovery_mod.logger.removeHandler(handler)
        discovery_mod.logger.setLevel(original_level)

    assert count == 0
    assert any(
        "unregistered tool" in rec.getMessage()
        and "ghost_tool" in rec.getMessage()
        for rec in captured
    )


def test_malformed_file_does_not_abort_batch(tmp_path):
    registry = ToolRegistry()
    tool = _DummyTool()
    tool._schema = None
    registry.register(tool)

    # One valid, one malformed JSON file.
    _write_schema_file(tmp_path, "dummy_one")
    (tmp_path / "broken.json").write_text("{not-json", encoding="utf-8")

    count = load_schemas_from_directory(registry, tmp_path)
    assert count == 1  # broken file did not abort the loop


def test_uses_public_registry_api(monkeypatch, tmp_path):
    """Regression: must not touch ``registry._tools`` directly."""
    registry = ToolRegistry()
    tool = _DummyTool()
    tool._schema = None
    registry.register(tool)

    # Sentinel: poison ``_tools`` so any private access fails the test.
    sentinel = object()
    original = registry._tools
    registry._tools = sentinel  # type: ignore[assignment]
    try:
        # __contains__ and get must remain public — re-route them to
        # the original dict so the test still exercises the loader.
        monkeypatch.setattr(
            ToolRegistry,
            "__contains__",
            lambda self, n: n in original,
        )
        monkeypatch.setattr(
            ToolRegistry, "get", lambda self, n: original.get(n)
        )
        _write_schema_file(tmp_path, "dummy_one")
        count = load_schemas_from_directory(registry, tmp_path)
        assert count == 1
    finally:
        registry._tools = original  # type: ignore[assignment]
