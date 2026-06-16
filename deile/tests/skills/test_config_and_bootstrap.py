"""Tests for ``deile.skills.config`` and ``deile.skills.bootstrap``."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from deile.skills.bootstrap import (
    BootstrapResult,
    bootstrap_skills,
    bootstrap_skills_with_handle,
)
from deile.skills.config import SkillsConfig, load_skills_config
from deile.skills.registry import get_skill_registry, reset_skill_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_skill_registry()
    yield
    reset_skill_registry()


@pytest.mark.unit
class TestLoadSkillsConfig:
    def test_missing_file_yields_enabled_defaults(self, tmp_path: Path) -> None:
        # Bullet-proofing fix: a missing skills.yaml no longer silently
        # disables the whole subsystem. Defaults take over. See
        # ``test_bulletproofing.TestConfigDefaults`` for the full contract.
        cfg = load_skills_config(tmp_path / "absent.yaml")
        assert cfg.enabled is True

    def test_valid_yaml_round_trips_all_fields(self, tmp_path: Path) -> None:
        f = tmp_path / "skills.yaml"
        f.write_text(
            "enabled: true\n"
            "max_per_turn: 7\n"
            "library_paths:\n"
            "  - /abs/lib1\n"
            "  - relative/lib2\n"
            "extension_map:\n"
            "  .zig: zig\n"
            "basename_map:\n"
            "  buildfile: bazel\n",
            encoding="utf-8",
        )
        cfg = load_skills_config(f)
        assert cfg.enabled is True
        assert cfg.max_per_turn == 7
        assert Path("/abs/lib1") in cfg.library_paths
        assert any(str(p).endswith("relative/lib2") for p in cfg.library_paths)
        assert cfg.extension_map == {".zig": "zig"}
        assert cfg.basename_map == {"buildfile": "bazel"}

    def test_malformed_yaml_disables(self, tmp_path: Path) -> None:
        f = tmp_path / "skills.yaml"
        f.write_text("enabled: true\n  bad: : indent", encoding="utf-8")
        assert load_skills_config(f).enabled is False

    def test_non_mapping_root_disables(self, tmp_path: Path) -> None:
        f = tmp_path / "skills.yaml"
        f.write_text("- not\n- a mapping\n", encoding="utf-8")
        assert load_skills_config(f).enabled is False

    def test_invalid_max_per_turn_falls_back_to_default(self, tmp_path: Path) -> None:
        f = tmp_path / "skills.yaml"
        f.write_text("max_per_turn: not-a-number\n", encoding="utf-8")
        assert load_skills_config(f).max_per_turn == 4

    def test_max_per_turn_minimum_is_one(self, tmp_path: Path) -> None:
        f = tmp_path / "skills.yaml"
        f.write_text("max_per_turn: 0\n", encoding="utf-8")
        assert load_skills_config(f).max_per_turn == 1

    def test_unreadable_file_falls_back_to_enabled(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An unreadable file behaves like a missing one (defaults + WARNING).
        # Different from malformed YAML, which disables to surface the mistake.
        f = tmp_path / "skills.yaml"
        f.write_text("enabled: true", encoding="utf-8")
        f.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger="deile.skills.config"):
                cfg = load_skills_config(f)
            assert cfg.enabled is True
        finally:
            f.chmod(0o644)


@pytest.mark.unit
class TestBootstrapSkills:
    async def test_disabled_config_returns_none(self) -> None:
        cfg = SkillsConfig(enabled=False)
        assert await bootstrap_skills(cfg) is None

    async def test_bootstrap_uses_isolated_paths(self, tmp_path: Path) -> None:
        # Isolate the scan from the developer's real ~/.deile/skills/ etc.
        # by pointing project_dir and user_home at empty tmp directories.
        # The bundled library still ships the python/typescript/tdd skills.
        cfg = SkillsConfig(enabled=True, max_per_turn=2)
        router = await bootstrap_skills(
            cfg,
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        assert router is not None
        names = set(get_skill_registry().list_names())
        assert {"python", "typescript", "tdd"}.issubset(names)

    async def test_extra_path_adds_skills(self, tmp_path: Path) -> None:
        extra = tmp_path / "extra"
        extra.mkdir()
        (extra / "mythril.md").write_text(
            "---\n"
            "name: mythril\n"
            "description: Mythril\n"
            "triggers:\n"
            "  file_globs: ['*.myth']\n"
            "---\n"
            "body",
            encoding="utf-8",
        )
        cfg = SkillsConfig(enabled=True)
        router = await bootstrap_skills(
            cfg,
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
            extra_paths=[extra],
        )
        assert router is not None
        assert "mythril" in get_skill_registry().list_names()

    async def test_bundled_library_loads_via_default_config(
        self, tmp_path: Path
    ) -> None:
        # Default skills.yaml is enabled; isolate user/project to verify the
        # bundled library is auto-included.
        router = await bootstrap_skills(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "home",
        )
        assert router is not None
        names = set(get_skill_registry().list_names())
        assert {"python", "typescript", "tdd"}.issubset(names)


@pytest.mark.unit
class TestBootstrapHandle:
    async def test_with_handle_returns_typed_dataclass(self, tmp_path: Path) -> None:
        result = await bootstrap_skills_with_handle(
            project_dir=tmp_path / "p",
            user_home=tmp_path / "h",
        )
        assert isinstance(result, BootstrapResult)
        assert result.router is not None
        # Without hot_reload=True, watcher is None.
        assert result.watcher is None

    async def test_with_handle_returns_none_when_disabled(self) -> None:
        result = await bootstrap_skills_with_handle(SkillsConfig(enabled=False))
        assert result is None

    async def test_legacy_bootstrap_skills_returns_just_router(
        self, tmp_path: Path
    ) -> None:
        router = await bootstrap_skills(
            project_dir=tmp_path / "p",
            user_home=tmp_path / "h",
        )
        assert router is not None
        # watcher attribute exists on the router for direct access.
        assert router.watcher is None
