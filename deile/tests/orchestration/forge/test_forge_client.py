"""Tests para ForgeClient._run() timeout — fix #779."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.orchestration.forge.base import ForgeConfig, ForgeKind


def _make_forge():
    """Retorna um GitHubForge com config mínima para testar _run()."""
    from deile.orchestration.forge.github_forge import GitHubForge

    cfg = ForgeConfig(
        kind=ForgeKind.GITHUB,
        host="github.com",
        project_path="owner/repo",
        cli_path="gh",
    )
    forge = GitHubForge.__new__(GitHubForge)
    forge._config = cfg
    return forge


@pytest.mark.unit
class TestForgeClientRunTimeout:
    """ForgeClient._run() lança TimeoutError e termina o filho quando subprocess trava."""

    async def test_run_kills_subprocess_on_timeout(self):
        """AC-6a/6b: _run() com subprocess bloqueado lança TimeoutError e mata filho."""
        forge = _make_forge()
        never_done = asyncio.Event()

        async def _blocking_communicate():
            await never_done.wait()
            return (b"", b"")

        mock_proc = MagicMock()
        mock_proc.communicate = _blocking_communicate
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=None)
        mock_proc.returncode = 0

        original_timeout = forge._RUN_TIMEOUT_S
        forge._RUN_TIMEOUT_S = 0.2

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            try:
                with pytest.raises(asyncio.TimeoutError):
                    await forge._run("pr", "list")
            finally:
                forge._RUN_TIMEOUT_S = original_timeout
                never_done.set()

        mock_proc.kill.assert_called_once()

    async def test_run_returns_normally_when_subprocess_completes(self):
        """Subprocesso que completa normalmente retorna rc/stdout/stderr."""
        forge = _make_forge()
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"output\n", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            rc, stdout, stderr = await forge._run("pr", "list")

        assert rc == 0
        assert stdout == "output\n"
        assert stderr == ""
