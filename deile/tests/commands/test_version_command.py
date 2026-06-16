"""Testes do comando /version (issue #173).

Cobre a matriz de testes obrigatória:
  - test_version_matches_version_module
  - test_metrics_match_version_module_metrics
  - test_python_version_matches_sys_version
  - test_platform_matches_platform_module
  - test_feature_flags_match_version_module
  - test_feature_flags_have_descriptions
  - test_install_info_graceful_on_failure
  - test_links_are_not_placeholders
  - test_response_under_2s
"""

from __future__ import annotations

import platform
import sys
import time
from io import StringIO
from unittest.mock import MagicMock, patch

from rich.console import Console

import deile.__version__ as version_mod
from deile.commands.base import CommandContext
from deile.commands.builtin.version_command import (
    _LINKS,
    VersionCommand,
    _detect_install_info,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(content) -> str:
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=200)
    console.print(content)
    return buf.getvalue()


def _ctx() -> CommandContext:
    return CommandContext(user_input="/version", args="")


def _cmd() -> VersionCommand:
    return VersionCommand()


# ---------------------------------------------------------------------------
# Testes de correctude dos dados
# ---------------------------------------------------------------------------


class TestVersionMatchesModule:
    async def test_version_matches_version_module(self):
        result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        assert version_mod.__version__ in rendered

    async def test_metadata_contains_version(self):
        result = await _cmd().execute(_ctx())
        assert result.metadata["version"] == version_mod.__version__

    async def test_metadata_build_date(self):
        result = await _cmd().execute(_ctx())
        assert result.metadata["build_date"] == version_mod.__build_date__

    async def test_metadata_build_number(self):
        result = await _cmd().execute(_ctx())
        assert result.metadata["build_number"] == version_mod.__build_number__


class TestMetricsMatchVersionModule:
    async def test_metrics_match_version_module_metrics(self):
        result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        total_files = str(version_mod.METRICS.get("total_files", ""))
        if total_files:
            assert total_files in rendered

    async def test_metrics_no_extra_hardcode(self):
        with patch.object(
            version_mod, "METRICS", {"total_files": 9999, "coverage": "77%"}
        ):
            result = await _cmd().execute(_ctx())
            rendered = _render(result.content)
            assert "9999" in rendered
            assert "77%" in rendered

    async def test_metrics_missing_graceful(self):
        with patch.object(version_mod, "METRICS", {}):
            result = await _cmd().execute(_ctx())
            assert result.success


class TestEnvironmentInfo:
    async def test_python_version_matches_sys_version(self):
        result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        py_ver = sys.version.split()[0]
        assert py_ver in rendered

    async def test_platform_matches_platform_module(self):
        result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        plat = platform.platform()
        assert plat in rendered


class TestFeatureFlags:
    async def test_feature_flags_match_version_module(self):
        result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        active = [k for k, v in version_mod.FEATURES.items() if v]
        for flag in active:
            assert flag in rendered

    async def test_feature_flags_have_descriptions(self):
        from deile.commands.builtin.version_command import _FLAG_DESCRICOES

        active = [k for k, v in version_mod.FEATURES.items() if v]
        for flag in active:
            assert (
                flag in _FLAG_DESCRICOES
            ), f"Flag '{flag}' sem descrição em _FLAG_DESCRICOES"

    async def test_inactive_flags_not_shown(self):
        patched = {k: False for k in version_mod.FEATURES}
        with patch.object(version_mod, "FEATURES", patched):
            result = await _cmd().execute(_ctx())
            assert result.success
            rendered = _render(result.content)
            assert "nenhuma flag ativa" in rendered


class TestInstallInfo:
    async def test_install_info_graceful_on_failure(self):
        import importlib.metadata as im

        with patch.object(im, "distribution", side_effect=Exception("not found")):
            info = _detect_install_info()
            assert info["modo"] == "indisponível"
            assert info["diretorio"] == "indisponível"

    async def test_install_info_graceful_renders(self):
        import importlib.metadata as im

        with patch.object(im, "distribution", side_effect=Exception("not found")):
            result = await _cmd().execute(_ctx())
            assert result.success

    async def test_install_info_malformed_direct_url_json(self):
        """Verifica graceful fallback quando direct_url.json contém JSON inválido."""
        import importlib.metadata as im

        mock_dist = MagicMock()
        mock_dist.metadata = {"Version": "1.0.0"}
        mock_dist.locate_file.return_value = MagicMock(
            __str__=lambda self: "/fake/path"
        )
        mock_dist.read_text.return_value = "invalid json {"
        with patch.object(im, "distribution", return_value=mock_dist):
            info = _detect_install_info()
            assert info["modo"] == "indisponível"
            assert info["versao_pkg"] == "indisponível"
            assert info["diretorio"] == "indisponível"


class TestLinks:
    async def test_links_are_not_placeholders(self):
        for name, url in _LINKS.items():
            assert "example.com" not in url, f"Link '{name}' aponta para placeholder"
            assert url.strip(), f"Link '{name}' está vazio"

    async def test_links_rendered(self):
        result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        assert "github.com/elimarcavalli/deile" in rendered
        assert "docs/system_design/00-VISAO-GERAL.md" in rendered


class TestPerformance:
    async def test_response_under_2s(self):
        cmd = _cmd()
        t0 = time.monotonic()
        result = await cmd.execute(_ctx())
        elapsed = time.monotonic() - t0
        assert result.success
        assert elapsed < 2.0, f"/version demorou {elapsed:.2f}s (limite 2s)"

    async def test_elapsed_in_metadata(self):
        result = await _cmd().execute(_ctx())
        assert "elapsed_s" in result.metadata
        assert result.metadata["elapsed_s"] < 2.0


class TestContentType:
    async def test_content_type_is_rich(self):
        result = await _cmd().execute(_ctx())
        assert result.content_type == "rich"

    async def test_content_not_string(self):
        result = await _cmd().execute(_ctx())
        assert not isinstance(result.content, str)

    async def test_renders_without_errors(self):
        result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        assert len(rendered) > 50


class TestIssue412:
    """Acceptance criteria for issue #412 — /version shows current DEILE version.

    Verifies the three checkable criteria:
    1. Output contains the product name (DEILE) followed by the version read
       from deile.__version__ — no hardcoded duplicate.
    2. Command lives in deile/commands/builtin/ and follows the SlashCommand pattern.
    3. Tests in deile/tests/commands/ confirm the behavior (this class).
    """

    async def test_output_shows_deile_and_version(self):
        """Rendered output contains 'DEILE' and the version from __version__.py."""
        result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        assert "DEILE" in rendered
        assert version_mod.__version__ in rendered

    async def test_version_and_title_appear_together(self):
        """'DEILE v<X.Y.Z>' appears together in the output — matches desired format."""
        result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        assert f"DEILE v{version_mod.__version__}" in rendered

    async def test_version_not_hardcoded_in_command(self):
        """The command module must not hardcode the version string."""
        import inspect

        from deile.commands.builtin import version_command

        source = inspect.getsource(version_command)
        assert version_mod.__version__ not in source, (
            f"Version {version_mod.__version__!r} is hardcoded in version_command.py — "
            "it must be read from deile.__version__ only."
        )

    async def test_command_is_slash_command_subclass(self):
        """VersionCommand follows the SlashCommand/DirectCommand pattern."""
        from deile.commands.base import DirectCommand

        cmd = _cmd()
        assert isinstance(cmd, DirectCommand)

    async def test_command_registered_as_version(self):
        """Command name is 'version' so users invoke it as /version."""
        cmd = _cmd()
        assert cmd.name == "version"
