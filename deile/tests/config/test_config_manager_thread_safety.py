"""Thread-safety test for get_config_manager() singleton — issue #666.

Verifica que get_config_manager() usa double-checked locking e não cria
múltiplas instâncias em init concorrente, espelhando get_settings().
"""

from __future__ import annotations

import threading

import pytest

from deile.config.manager import ConfigManager, get_config_manager


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the singleton before and after each test."""
    import deile.config.manager as mgr_mod

    mgr_mod._config_manager = None
    yield
    mgr_mod._config_manager = None


@pytest.mark.unit
def test_get_config_manager_returns_same_instance():
    """Successive calls return the identical singleton object."""
    a = get_config_manager()
    b = get_config_manager()
    assert a is b


@pytest.mark.unit
def test_get_config_manager_concurrent_init_single_instance():
    """Concurrent first-time calls must produce exactly one ConfigManager instance."""
    import deile.config.manager as mgr_mod

    mgr_mod._config_manager = None

    instances: list[ConfigManager] = []
    lock = threading.Lock()
    errors: list[Exception] = []

    def worker():
        try:
            inst = get_config_manager()
            with lock:
                instances.append(inst)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"threads raised: {errors}"
    assert len(instances) == 20
    # All threads must have received the same instance
    first = instances[0]
    assert all(
        inst is first for inst in instances
    ), "get_config_manager() returned different instances under concurrent init"


@pytest.mark.unit
def test_get_config_manager_lock_exists():
    """_config_manager_lock must be a threading.Lock (mirrors _settings_lock)."""
    import deile.config.manager as mgr_mod

    assert hasattr(
        mgr_mod, "_config_manager_lock"
    ), "_config_manager_lock not found in deile.config.manager"
    assert isinstance(
        mgr_mod._config_manager_lock, type(threading.Lock())
    ), "_config_manager_lock is not a threading.Lock instance"
