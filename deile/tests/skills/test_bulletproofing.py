"""Thread-safety regressions for ``SkillRegistry`` and its singleton accessor.

Every other bulletproofing assertion lives next to the code it tests
(test_loader for CRLF/priority edge cases, test_router for path-traversal
containment and the catalog directive, test_skill_tools for invoke_skill
error capping, test_watcher for serialized reloads,
test_config_and_bootstrap for config defaults + bootstrap handle).
"""

from __future__ import annotations

import threading

import pytest

from deile.skills.base import Skill
from deile.skills.registry import (
    SkillRegistry,
    get_skill_registry,
    reset_skill_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_skill_registry()
    yield
    reset_skill_registry()


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
                # window would record a zero count. With replace_all holding
                # the lock, this never happens.
                if len(reg.list_all()) == 0:
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
