"""Tests for deile.commands.builtin.env_command."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_context(args: str = "") -> MagicMock:
    ctx = MagicMock()
    ctx.args = args
    ctx.agent = None
    ctx.ui_manager = None
    ctx.config_manager = None
    return ctx


def _write_settings(home: Path, data: dict) -> None:
    d = home / ".deile"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps(data), encoding="utf-8")

@pytest.mark.unit
class TestEnvCommandSet:
    async def test_set_key_equals_value(self, tmp_path, monkeypatch):
        import deile.config.env_store as es
        monkeypatch.setattr(es, "_settings_path", lambda home=None: tmp_path / ".deile" / "settings.json")
        monkeypatch.delenv("MY_ENV_CMD_VAR", raising=False)

        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("set MY_ENV_CMD_VAR=hello")
        result = await cmd.execute(ctx)
        assert result.success
        assert "MY_ENV_CMD_VAR" in result.content

    async def test_set_key_space_value(self, tmp_path, monkeypatch):
        import deile.config.env_store as es
        monkeypatch.setattr(es, "_settings_path", lambda home=None: tmp_path / ".deile" / "settings.json")
        monkeypatch.delenv("SPACE_VAR", raising=False)

        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("set SPACE_VAR world")
        result = await cmd.execute(ctx)
        assert result.success

    async def test_set_invalid_key(self, tmp_path, monkeypatch):
        import deile.config.env_store as es
        monkeypatch.setattr(es, "_settings_path", lambda home=None: tmp_path / ".deile" / "settings.json")

        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("set MY-INVALID=value")
        result = await cmd.execute(ctx)
        assert not result.success

    async def test_set_missing_value(self, tmp_path):
        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("set ONLY_KEY")
        result = await cmd.execute(ctx)
        assert not result.success

    async def test_set_sensitive_key_masked_in_output(self, tmp_path, monkeypatch):
        import deile.config.env_store as es
        monkeypatch.setattr(es, "_settings_path", lambda home=None: tmp_path / ".deile" / "settings.json")
        monkeypatch.delenv("MY_API_KEY", raising=False)

        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("set MY_API_KEY=sk-secret-value")
        result = await cmd.execute(ctx)
        assert result.success
        assert "sk-secret-value" not in str(result.content)
        assert "<masked>" in str(result.content)

@pytest.mark.unit
class TestEnvCommandList:
    async def test_list_empty(self, tmp_path, monkeypatch):
        import deile.config.env_store as es
        monkeypatch.setattr(es, "_settings_path", lambda home=None: tmp_path / ".deile" / "settings.json")

        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("list")
        result = await cmd.execute(ctx)
        assert result.success

    async def test_list_shows_vars(self, tmp_path, monkeypatch):
        import deile.config.env_store as es
        monkeypatch.setattr(es, "_settings_path", lambda home=None: tmp_path / ".deile" / "settings.json")
        monkeypatch.delenv("LIST_VAR", raising=False)
        _write_settings(tmp_path, {"env": {"exports": {"LIST_VAR": "val"}}})

        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("list")
        result = await cmd.execute(ctx)
        assert result.success


@pytest.mark.unit
class TestEnvCommandUnset:
    async def test_unset_existing(self, tmp_path, monkeypatch):
        import deile.config.env_store as es
        monkeypatch.setattr(es, "_settings_path", lambda home=None: tmp_path / ".deile" / "settings.json")
        monkeypatch.delenv("UNSET_ME", raising=False)
        _write_settings(tmp_path, {"env": {"exports": {"UNSET_ME": "v"}}})

        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("unset UNSET_ME")
        result = await cmd.execute(ctx)
        assert result.success
        assert "Removed" in str(result.content)

    async def test_unset_nonexistent(self, tmp_path, monkeypatch):
        import deile.config.env_store as es
        monkeypatch.setattr(es, "_settings_path", lambda home=None: tmp_path / ".deile" / "settings.json")

        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("unset GHOST_VAR")
        result = await cmd.execute(ctx)
        assert result.success
        assert "not in" in str(result.content)

    async def test_unset_empty_key(self, tmp_path):
        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("unset")
        result = await cmd.execute(ctx)
        assert not result.success


@pytest.mark.unit
class TestEnvCommandMisc:
    async def test_no_args_shows_help(self, tmp_path):
        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("")
        result = await cmd.execute(ctx)
        assert result.success

    async def test_unknown_subcommand(self, tmp_path):
        from deile.commands.builtin.env_command import EnvCommand
        cmd = EnvCommand()
        ctx = _make_context("frobnicate FOO")
        result = await cmd.execute(ctx)
        assert not result.success
