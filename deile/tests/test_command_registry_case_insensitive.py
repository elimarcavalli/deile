"""CommandRegistry — resolução de slash commands case-insensitive."""

from __future__ import annotations

import pytest

from deile.commands.base import CommandContext, CommandResult, CommandStatus, SlashCommand
from deile.commands.registry import CommandRegistry
from deile.config.manager import CommandConfig


@pytest.mark.unit
class TestCommandRegistryCaseInsensitive:
    def _register_stub(self, registry: CommandRegistry, name: str) -> None:
        class _Stub(SlashCommand):
            def __init__(self) -> None:
                super().__init__(CommandConfig(name=name, description="stub"))

            async def execute(self, ctx: CommandContext) -> CommandResult:
                return CommandResult(
                    success=True,
                    content="ok",
                    status=CommandStatus.SUCCESS,
                )

        registry.register_command(_Stub())

    def test_get_command_matches_any_case(self) -> None:
        r = CommandRegistry()
        self._register_stub(r, "DOC-HYGIENE")
        assert r.get_command("DOC-HYGIENE") is not None
        assert r.get_command("doc-hygiene") is not None
        assert r.get_command("Doc-HyGiEnE") is not None

    def test_has_command(self) -> None:
        r = CommandRegistry()
        self._register_stub(r, "BEGIN-GIT")
        assert r.has_command("begin-git") is True
        assert r.has_command("BEGIN-GIT") is True
        assert r.has_command("missing") is False

    def test_contains_operator(self) -> None:
        r = CommandRegistry()
        self._register_stub(r, "EVOLVE")
        assert "evolve" in r
        assert "EVOLVE" in r

    def test_get_command_suggestions_case_insensitive_prefix(self) -> None:
        r = CommandRegistry()
        self._register_stub(r, "MY-SKILL")
        names = [s["name"] for s in r.get_command_suggestions("my-s")]
        assert "MY-SKILL" in names
