"""Unit tests for ``deile/tools/discovery``.

Pinpoint behavior contracts of the extracted auto-discovery module so that
regressions (silent ImportError, double-registration, abstract-class pickup)
fail fast.
"""
from __future__ import annotations

import sys
import types
from abc import abstractmethod

from deile.tools.base import Tool, ToolContext, ToolResult, ToolStatus
from deile.tools.discovery import discover_tools_in_package
from deile.tools.registry import ToolRegistry


class _ConcreteTool(Tool):
    """Concrete Tool stub used across discovery tests."""

    @property
    def name(self) -> str:
        return "_concrete_tool"

    @property
    def description(self) -> str:
        return "concrete tool"

    @property
    def category(self) -> str:
        return "other"

    async def execute(self, context: ToolContext) -> ToolResult:
        return ToolResult(status=ToolStatus.SUCCESS, message="ok")


class _AbstractTool(Tool):
    """Abstract Tool — must be skipped by discovery."""

    @property
    def name(self) -> str:
        return "_abstract_tool"

    @abstractmethod
    async def execute(self, context: ToolContext) -> ToolResult:  # pragma: no cover
        ...


def _install_fake_module(name: str, **attrs) -> None:
    """Install a synthetic module into ``sys.modules`` for importlib lookup."""
    mod = types.ModuleType(name)
    for attr_name, attr_value in attrs.items():
        setattr(mod, attr_name, attr_value)
    sys.modules[name] = mod


def test_missing_package_returns_zero_silently():
    registry = ToolRegistry()
    # A package name guaranteed not to exist.
    count = discover_tools_in_package(
        registry, "deile.tools._definitely_not_a_real_package_xyz"
    )
    assert count == 0
    assert len(registry) == 0


def test_duplicate_tool_not_reregistered():
    registry = ToolRegistry()
    pkg = "deile.tools._fake_discovery_pkg_duplicate"
    _install_fake_module(pkg, ConcreteTool=_ConcreteTool)

    try:
        first = discover_tools_in_package(registry, pkg)
        second = discover_tools_in_package(registry, pkg)
    finally:
        sys.modules.pop(pkg, None)

    assert first == 1
    assert second == 0
    assert len(registry) == 1


def test_abstract_class_is_ignored():
    registry = ToolRegistry()
    pkg = "deile.tools._fake_discovery_pkg_abstract"
    _install_fake_module(
        pkg, AbstractTool=_AbstractTool, ConcreteTool=_ConcreteTool
    )

    try:
        count = discover_tools_in_package(registry, pkg)
    finally:
        sys.modules.pop(pkg, None)

    # Only the concrete tool should be registered; abstract is filtered out.
    assert count == 1
    assert "_concrete_tool" in registry
    assert "_abstract_tool" not in registry
