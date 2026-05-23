"""Tests for the SkillsWatcher hot-reload pipeline.

These tests exercise ``reload_registry`` directly (deterministic, no I/O
timing) and use a low-level integration test on the watcher with a tiny
debounce window. They avoid sleeping for long periods by polling.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from deile.skills.registry import get_skill_registry, reset_skill_registry
from deile.skills.watcher import SkillsWatcher, reload_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_skill_registry()
    yield
    reset_skill_registry()


def _isolated(tmp_path: Path) -> dict:
    """Return a dict of paths suitable for passing to the reload/watcher APIs.

    Isolates project_dir + user_home so the developer's real
    ``~/.deile/skills/`` does not leak into the test.
    """
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    return {"project_dir": project, "user_home": home}


@pytest.mark.unit
class TestReloadRegistry:
    def test_new_file_appears_after_reload(self, tmp_path: Path) -> None:
        paths = _isolated(tmp_path)
        # Pre-state: only the bundled skills are loaded.
        reload_registry(**paths)
        before = set(get_skill_registry().list_names())
        assert "extra-one" not in before

        # Drop a file into the user dir and re-reload.
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        (user_skills_dir / "extra-one.md").write_text(
            "---\nname: extra-one\ndescription: Extra\n---\nbody", encoding="utf-8"
        )

        count = reload_registry(**paths)
        assert count == len(before) + 1
        assert "extra-one" in get_skill_registry().list_names()

    def test_deleted_file_disappears_after_reload(self, tmp_path: Path) -> None:
        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        target = user_skills_dir / "ephemeral.md"
        target.write_text(
            "---\nname: ephemeral\ndescription: Will be deleted\n---\nbody", encoding="utf-8"
        )

        reload_registry(**paths)
        assert "ephemeral" in get_skill_registry().list_names()

        target.unlink()
        reload_registry(**paths)
        assert "ephemeral" not in get_skill_registry().list_names()

    def test_edited_body_takes_effect_after_reload(self, tmp_path: Path) -> None:
        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        target = user_skills_dir / "rewritable.md"
        target.write_text(
            "---\nname: rewritable\n---\nVERSION ONE", encoding="utf-8"
        )

        reload_registry(**paths)
        assert get_skill_registry().get("rewritable").body == "VERSION ONE"

        target.write_text(
            "---\nname: rewritable\n---\nVERSION TWO", encoding="utf-8"
        )
        reload_registry(**paths)
        assert get_skill_registry().get("rewritable").body == "VERSION TWO"

    def test_command_registry_refresh(self, tmp_path: Path) -> None:
        from deile.commands.registry import CommandRegistry

        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        (user_skills_dir / "v1.md").write_text(
            "---\nname: v1\n---\nbody-v1", encoding="utf-8"
        )

        cmd_registry = CommandRegistry()
        reload_registry(command_registry=cmd_registry, **paths)
        assert cmd_registry.get_command("v1") is not None

        # Replace v1 with v2 on disk and reload — v1 should be gone.
        (user_skills_dir / "v1.md").unlink()
        (user_skills_dir / "v2.md").write_text(
            "---\nname: v2\n---\nbody-v2", encoding="utf-8"
        )
        reload_registry(command_registry=cmd_registry, **paths)
        assert cmd_registry.get_command("v1") is None
        assert cmd_registry.get_command("v2") is not None


@pytest.mark.integration
class TestSkillsWatcher:
    def _wait_for(self, predicate, timeout: float = 5.0, poll: float = 0.05) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(poll)
        return False

    def test_creating_md_file_triggers_reload(self, tmp_path: Path) -> None:
        pytest.importorskip("watchdog")
        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        # Trigger an initial scan so the registry baseline is non-empty.
        reload_registry(**paths)

        watcher = SkillsWatcher(debounce_seconds=0.1, **paths)
        try:
            assert watcher.start() is True

            (user_skills_dir / "fresh.md").write_text(
                "---\nname: fresh\ndescription: Fresh\n---\nbody", encoding="utf-8"
            )

            assert self._wait_for(
                lambda: "fresh" in get_skill_registry().list_names()
            ), "watcher never picked up the new file"
        finally:
            watcher.stop()

    def test_modifying_md_file_triggers_reload(self, tmp_path: Path) -> None:
        pytest.importorskip("watchdog")
        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        target = user_skills_dir / "mutable.md"
        target.write_text(
            "---\nname: mutable\n---\nold body", encoding="utf-8"
        )
        reload_registry(**paths)
        assert get_skill_registry().get("mutable").body == "old body"

        watcher = SkillsWatcher(debounce_seconds=0.1, **paths)
        try:
            assert watcher.start() is True
            target.write_text(
                "---\nname: mutable\n---\nnew body", encoding="utf-8"
            )
            assert self._wait_for(
                lambda: get_skill_registry().get("mutable").body == "new body"
            )
        finally:
            watcher.stop()

    def test_non_md_files_are_ignored(self, tmp_path: Path) -> None:
        pytest.importorskip("watchdog")
        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        reload_registry(**paths)
        baseline = set(get_skill_registry().list_names())

        reloads: list = []
        watcher = SkillsWatcher(
            debounce_seconds=0.1,
            on_reload=lambda count: reloads.append(count),
            **paths,
        )
        try:
            assert watcher.start() is True
            # Drop a non-.md file — should NOT trigger a reload.
            (user_skills_dir / "notes.txt").write_text("nope", encoding="utf-8")
            time.sleep(0.5)  # well past debounce
            assert reloads == [], f"reload fired for non-.md file: {reloads}"
            # Registry contents should be unchanged.
            assert set(get_skill_registry().list_names()) == baseline
        finally:
            watcher.stop()

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        pytest.importorskip("watchdog")
        paths = _isolated(tmp_path)
        (paths["user_home"] / ".deile" / "skills").mkdir(parents=True)
        watcher = SkillsWatcher(debounce_seconds=0.1, **paths)
        watcher.start()
        watcher.stop()
        watcher.stop()  # second stop should not raise
        assert watcher.is_active is False
