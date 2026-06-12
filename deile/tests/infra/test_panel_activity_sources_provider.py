"""Tests for issue #447: MultiSourceActivityProvider with configurable sources.

Coverage:
  1.  ActivitySource dataclass is immutable (frozen=True).
  2.  MultiSourceActivityProvider defaults to 5 V1 sources when sources=None.
  3.  MultiSourceActivityProvider accepts custom sources list.
  4.  _fetch iterates self._sources (not the module-level constant).
  5.  Seven sources produce seven concurrent tail calls.
  6.  Empty sources list → falls back to default V1.
  7.  Custom sources update _ROLE_COLOR_MAP in-place.
  8.  _sources_from_settings returns None when settings has empty list.
  9.  _sources_from_settings converts dicts to ActivitySource objects.
  10. _sources_from_settings returns None on import failure (graceful).
  11. Default V1 deployment names match _MULTI_SOURCE_DEFS exactly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from subprocess import CompletedProcess
from typing import List
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest

import _panel_data as pd  # noqa: E402


def _make_source(deployment: str = "my-deploy",
                 role: str = "my-role",
                 color: str = "cyan") -> pd.ActivitySource:
    return pd.ActivitySource(deployment=deployment, role=role, color=color)


# ---------------------------------------------------------------------------
# 1: ActivitySource dataclass
# ---------------------------------------------------------------------------

class TestActivitySourceDataclass:
    def test_immutable(self):
        src = _make_source()
        with pytest.raises(Exception):  # frozen=True → FrozenInstanceError
            src.deployment = "other"  # type: ignore[misc]

    def test_fields(self):
        src = pd.ActivitySource(deployment="deile-worker", role="worker", color="cyan")
        assert src.deployment == "deile-worker"
        assert src.role == "worker"
        assert src.color == "cyan"


# ---------------------------------------------------------------------------
# 2–6: MultiSourceActivityProvider construction
# ---------------------------------------------------------------------------

class TestProviderConstruction:
    def test_default_sources_is_five(self):
        prov = pd.MultiSourceActivityProvider(enabled=False)
        assert len(prov._sources) == 5

    def test_default_source_names_match_defs(self):
        prov = pd.MultiSourceActivityProvider(enabled=False)
        expected = {d for d, _, _ in pd._MULTI_SOURCE_DEFS}
        actual = {s.deployment for s in prov._sources}
        assert actual == expected

    def test_custom_sources_override_defaults(self):
        custom = [
            _make_source("my-pod-1", "role1", "cyan"),
            _make_source("my-pod-2", "role2", "blue"),
        ]
        prov = pd.MultiSourceActivityProvider(enabled=False, sources=custom)
        assert len(prov._sources) == 2
        assert prov._sources[0].deployment == "my-pod-1"

    def test_empty_sources_falls_back_to_default(self):
        prov = pd.MultiSourceActivityProvider(enabled=False, sources=[])
        assert len(prov._sources) == 5

    def test_seven_sources_accepted(self):
        """AC: 7 fontes incluindo nome novo → provider stores all 7."""
        sources = [
            _make_source(f"my-pod-{i}", f"role-{i}", "cyan")
            for i in range(7)
        ]
        prov = pd.MultiSourceActivityProvider(enabled=False, sources=sources)
        assert len(prov._sources) == 7


# ---------------------------------------------------------------------------
# 7: Color map update
# ---------------------------------------------------------------------------

class TestColorMapUpdate:
    def test_custom_sources_update_role_color_map(self):
        sources = [
            pd.ActivitySource(deployment="special-pod", role="special-role", color="yellow"),
        ]
        pd.MultiSourceActivityProvider(enabled=False, sources=sources)
        assert pd._ROLE_COLOR_MAP.get("special-role") == "yellow"

    def test_default_colors_preserved(self):
        prov = pd.MultiSourceActivityProvider(enabled=False)
        for _, role, color in pd._MULTI_SOURCE_DEFS:
            assert pd._ROLE_COLOR_MAP.get(role) == color


# ---------------------------------------------------------------------------
# 4–5: _fetch uses self._sources
# ---------------------------------------------------------------------------

class TestFetchUsesCustomSources:
    def _empty_kubectl(self) -> CompletedProcess:
        return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def test_fetch_calls_each_source(self):
        sources = [
            pd.ActivitySource(deployment="pod-a", role="ra", color="cyan"),
            pd.ActivitySource(deployment="pod-b", role="rb", color="blue"),
            pd.ActivitySource(deployment="pod-c", role="rc", color="red"),
        ]
        prov = pd.MultiSourceActivityProvider(enabled=True, sources=sources)
        prov._kubectl = "/usr/bin/kubectl"

        called_deploys = []

        def fake_run(cmd, **kwargs):
            # Extract deployment name from "deploy/<name>" argument
            for arg in cmd:
                if arg.startswith("deploy/"):
                    called_deploys.append(arg[len("deploy/"):])
                    break
            return self._empty_kubectl()

        with patch("subprocess.run", side_effect=fake_run):
            prov._fetch()

        assert set(called_deploys) == {"pod-a", "pod-b", "pod-c"}

    def test_fetch_seven_sources(self):
        """AC: 7 fontes → 7 tail calls."""
        sources = [
            pd.ActivitySource(deployment=f"deploy-{i}", role=f"r{i}", color="cyan")
            for i in range(7)
        ]
        prov = pd.MultiSourceActivityProvider(enabled=True, sources=sources)
        prov._kubectl = "/usr/bin/kubectl"

        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            return self._empty_kubectl()

        with patch("subprocess.run", side_effect=fake_run):
            prov._fetch()

        assert call_count[0] == 7


# ---------------------------------------------------------------------------
# 8–10: _sources_from_settings helper
# ---------------------------------------------------------------------------

class TestSourcesFromSettings:
    def test_returns_none_when_settings_empty(self):
        mock_settings = MagicMock()
        mock_settings.panel_activity_sources = []
        with patch("deile.config.settings.get_settings", return_value=mock_settings):
            result = pd._sources_from_settings()
        assert result is None

    def test_converts_dicts_to_activity_sources(self):
        mock_settings = MagicMock()
        mock_settings.panel_activity_sources = [
            {"deployment": "my-pod", "role": "my-role", "color": "cyan"},
        ]
        with patch("deile.config.settings.get_settings", return_value=mock_settings):
            result = pd._sources_from_settings()
        assert result is not None
        assert len(result) == 1
        assert isinstance(result[0], pd.ActivitySource)
        assert result[0].deployment == "my-pod"
        assert result[0].role == "my-role"
        assert result[0].color == "cyan"

    def test_returns_none_on_import_error(self):
        original = sys.modules.get("deile.config.settings")
        sys.modules["deile.config.settings"] = None  # type: ignore[assignment]
        try:
            result = pd._sources_from_settings()
            assert result is None
        finally:
            if original is None:
                sys.modules.pop("deile.config.settings", None)
            else:
                sys.modules["deile.config.settings"] = original


# ---------------------------------------------------------------------------
# 11: V1 defaults
# ---------------------------------------------------------------------------

class TestDefaultV1:
    def test_default_deployments(self):
        prov = pd.MultiSourceActivityProvider(enabled=False)
        deployments = [s.deployment for s in prov._sources]
        expected = ["deile-pipeline", "deile-worker", "claude-worker",
                    "deilebot", "deile-monitor"]
        assert sorted(deployments) == sorted(expected)
