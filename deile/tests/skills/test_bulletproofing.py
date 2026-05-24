"""Regression tests for the skeptical-review bulletproofing pass.

One assertion per concrete bug/quality finding so a regression on any
single one fails loudly. Findings are grouped by area to keep the file
navigable.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

from deile.skills.base import Skill, SkillTrigger
from deile.skills.bootstrap import (
    BootstrapResult,
    bootstrap_skills,
    bootstrap_skills_with_handle,
)
from deile.skills.config import SkillsConfig, load_skills_config
from deile.skills.loader import parse_skill_text
from deile.skills.registry import (
    SkillRegistry,
    get_skill_registry,
    reset_skill_registry,
)
from deile.skills.router import SkillRouter, SkillSelectionContext, _resolve_within
from deile.skills.watcher import reload_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_skill_registry()
    yield
    reset_skill_registry()


# ---------------------------------------------------------------------------
# config: missing file != broken file
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfigDefaults:
    def test_missing_file_uses_enabled_defaults(self, tmp_path: Path) -> None:
        cfg = load_skills_config(tmp_path / "missing.yaml")
        # Previously this returned enabled=False, silently turning off the
        # whole subsystem if the YAML file was absent. The dataclass default
        # is enabled=True, and missing-file should agree.
        assert cfg.enabled is True
        assert cfg.max_per_turn == 4
        assert cfg.library_paths == []

    def test_malformed_yaml_disables(self, tmp_path: Path) -> None:
        f = tmp_path / "skills.yaml"
        f.write_text("name: 'unclosed\n", encoding="utf-8")
        # Malformed = user wrote a config and got it wrong → disable to
        # surface the mistake. Different from missing.
        assert load_skills_config(f).enabled is False

    def test_unreadable_file_falls_back_to_enabled(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        f = tmp_path / "skills.yaml"
        f.write_text("enabled: true", encoding="utf-8")
        f.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger="deile.skills.config"):
                cfg = load_skills_config(f)
            # Behave like missing: defaults + WARNING. We can't assert the
            # warning text because it depends on the OS error, but the
            # config must be usable.
            assert cfg.enabled is True
        finally:
            f.chmod(0o644)


# ---------------------------------------------------------------------------
# loader: CRLF, bool priority, name uppercase
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoaderRobustness:
    def test_crlf_frontmatter_is_recognized(self) -> None:
        # A file saved by a Windows editor (or git core.autocrlf=true) used
        # to slip through frontmatter parsing entirely.
        text = "---\r\nname: win\r\ndescription: From Windows\r\n---\r\nbody"
        skill = parse_skill_text(text, Path("win.md"))
        assert skill is not None
        assert skill.name == "win"
        assert skill.description == "From Windows"
        assert skill.body == "body"

    def test_priority_yes_no_yaml_bool_is_rejected(self) -> None:
        # YAML 1.1 reads `yes` as True. ``int(True) == 1`` silently coerces
        # to priority=1 — almost certainly not what the author meant.
        text = "---\nname: x\ndescription: y\npriority: yes\n---\nbody"
        skill = parse_skill_text(text, Path("x.md"))
        assert skill is not None
        assert skill.priority == 0

    def test_priority_dict_value_is_rejected(self) -> None:
        text = "---\nname: x\ndescription: y\npriority: {nested: 5}\n---\nbody"
        skill = parse_skill_text(text, Path("x.md"))
        assert skill is not None
        assert skill.priority == 0

    def test_priority_string_numeric_still_works(self) -> None:
        # A string that parses as int is OK — YAML quoting habits vary.
        text = "---\nname: x\ndescription: y\npriority: \"42\"\n---\nbody"
        skill = parse_skill_text(text, Path("x.md"))
        assert skill is not None
        assert skill.priority == 42


# ---------------------------------------------------------------------------
# router: path traversal containment for file_content_patterns
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPathTraversal:
    def test_resolve_within_accepts_in_root(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        out = _resolve_within("a.py", tmp_path)
        assert out is not None
        assert out == (tmp_path / "a.py").resolve()

    def test_resolve_within_rejects_dotdot_escape(self, tmp_path: Path) -> None:
        # A skill author cannot make us sample /etc/passwd via a crafted
        # reference like "../../etc/passwd".
        out = _resolve_within("../../etc/passwd", tmp_path)
        assert out is None

    def test_resolve_within_rejects_absolute_outside(self, tmp_path: Path) -> None:
        out = _resolve_within("/etc/hostname", tmp_path)
        assert out is None

    def test_resolve_within_accepts_absolute_inside(self, tmp_path: Path) -> None:
        target = tmp_path / "b.py"
        target.write_text("x", encoding="utf-8")
        out = _resolve_within(str(target), tmp_path)
        assert out == target.resolve()

    def test_file_content_trigger_does_not_leak_outside_root(self, tmp_path: Path) -> None:
        reg = SkillRegistry()
        reg.register(
            Skill(
                name="probe",
                description="d",
                body="b",
                triggers=SkillTrigger(file_content_patterns=[r".+"]),
            )
        )
        router = SkillRouter(reg, project_root=tmp_path)
        # If the path-traversal guard were missing, this regex would match
        # /etc/passwd's first byte and trigger the skill. With the guard,
        # the reference is filtered before any read happens.
        out = router.select_skills(
            SkillSelectionContext(user_input="", file_references=("../../etc/passwd",))
        )
        assert out == []


# ---------------------------------------------------------------------------
# registry: thread-safety + singleton race
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegistryThreadSafety:
    def test_concurrent_register_does_not_corrupt(self) -> None:
        reg = SkillRegistry()

        def worker(start: int) -> None:
            for i in range(start, start + 200):
                reg.register(Skill(name=f"s{i}", description="d", body="b"))

        threads = [threading.Thread(target=worker, args=(i * 200,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 1600 distinct names registered without dropping any.
        assert len(reg) == 1600

    def test_replace_all_is_atomic_under_concurrent_reads(self) -> None:
        reg = SkillRegistry()
        for i in range(100):
            reg.register(Skill(name=f"s{i}", description="d", body="b"))

        observed_zero = []

        def reader() -> None:
            for _ in range(500):
                # A reader that catches the "swapped to empty for a moment"
                # window would record a zero count here. With replace_all
                # holding the lock, this never happens.
                count = len(reg.list_all())
                if count == 0:
                    observed_zero.append(True)
                    return

        def writer() -> None:
            for _ in range(50):
                reg.replace_all(
                    Skill(name=f"new{i}", description="d", body="b") for i in range(100)
                )

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=writer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert observed_zero == [], "reader saw an empty registry during replace_all"

    def test_singleton_accessor_is_thread_safe(self) -> None:
        reset_skill_registry()
        instances = []

        def grab() -> None:
            instances.append(get_skill_registry())

        threads = [threading.Thread(target=grab) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All 16 must point at the same singleton.
        assert len(set(id(x) for x in instances)) == 1


# ---------------------------------------------------------------------------
# watcher: serialized reload + idempotent stop
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReloadSerialization:
    def test_reload_registry_uses_atomic_swap(self, tmp_path: Path) -> None:
        # Two threads call reload_registry concurrently — the second should
        # block on the lock, never producing a torn state. We can't easily
        # detect the lock from outside, so the proxy assertion is: after
        # both reloads complete, the registry has the expected count.
        user_skills_dir = tmp_path / "home" / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        (user_skills_dir / "z.md").write_text(
            "---\nname: z\n---\nbody", encoding="utf-8"
        )

        def go() -> None:
            for _ in range(20):
                reload_registry(
                    project_dir=tmp_path / "project",
                    user_home=tmp_path / "home",
                )

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        names = set(get_skill_registry().list_names())
        # 'z' from disk plus the bundled skills (python/typescript/tdd).
        assert "z" in names
        assert {"python", "typescript", "tdd"}.issubset(names)


# ---------------------------------------------------------------------------
# bootstrap: BootstrapResult + watcher handle
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBootstrapHandle:
    async def test_bootstrap_with_handle_returns_typed_dataclass(self, tmp_path: Path) -> None:
        result = await bootstrap_skills_with_handle(
            project_dir=tmp_path / "p",
            user_home=tmp_path / "h",
        )
        assert isinstance(result, BootstrapResult)
        assert result.router is not None
        # Without hot_reload=True, watcher is None.
        assert result.watcher is None

    async def test_bootstrap_with_handle_returns_none_when_disabled(self) -> None:
        result = await bootstrap_skills_with_handle(SkillsConfig(enabled=False))
        assert result is None

    async def test_legacy_bootstrap_skills_returns_just_router(self, tmp_path: Path) -> None:
        # The legacy contract — bootstrap_skills returns just the router or
        # None — must still hold for backward compat.
        router = await bootstrap_skills(
            project_dir=tmp_path / "p",
            user_home=tmp_path / "h",
        )
        assert router is not None
        # The watcher attribute exists on the router for direct access.
        assert router.watcher is None


# ---------------------------------------------------------------------------
# invoke_skill: error message capping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInvokeSkillErrorCapping:
    async def test_error_truncates_long_name_list(self) -> None:
        from deile.tools.base import ToolContext
        from deile.tools.skill_tools import InvokeSkillTool

        reg = get_skill_registry()
        for i in range(50):
            reg.register(Skill(name=f"skill-{i:03d}", description="d", body="b"))

        tool = InvokeSkillTool()
        result = await tool.execute(
            ToolContext(user_input="", parsed_args={"name": "does-not-exist"})
        )
        assert result.is_error
        # Cap is 25; message must indicate the cut-off.
        assert "more" in result.message
        assert "list_skills" in result.message

    async def test_error_below_cap_shows_full_list(self) -> None:
        from deile.tools.base import ToolContext
        from deile.tools.skill_tools import InvokeSkillTool

        reg = get_skill_registry()
        reg.register(Skill(name="alpha", description="d", body="b"))
        reg.register(Skill(name="beta", description="d", body="b"))

        tool = InvokeSkillTool()
        result = await tool.execute(
            ToolContext(user_input="", parsed_args={"name": "gamma"})
        )
        assert result.is_error
        # Full list visible — no truncation hint.
        assert "alpha" in result.message
        assert "beta" in result.message
        assert "more" not in result.message


# ---------------------------------------------------------------------------
# router catalog: directive text persists
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCatalogDirective:
    def test_directive_includes_concrete_example(self) -> None:
        reg = SkillRegistry()
        reg.register(Skill(name="python", description="d", body="b"))
        router = SkillRouter(reg)
        out = router.render_catalog()
        # Empirical testing showed the abstract directive alone wasn't
        # enough to coax invoke_skill calls; a concrete example helps.
        assert "Concrete example" in out
        assert "invoke_skill" in out

    def test_empty_registry_yields_empty_string(self) -> None:
        router = SkillRouter(SkillRegistry())
        assert router.render_catalog() == ""
