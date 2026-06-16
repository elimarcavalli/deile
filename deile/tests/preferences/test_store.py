"""Unit tests for ``deile.preferences.store.PreferenceStore``."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from deile.preferences.store import PreferenceStore, _validate_key, _validate_value

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> PreferenceStore:
    """Return a PreferenceStore whose backing file lives inside *tmp_path*."""
    prefs_file = tmp_path / "preferences.json"
    with (
        patch("deile.preferences.store._PREFS_FILE", prefs_file),
        patch("deile.preferences.store._PREFS_DIR", tmp_path),
    ):
        yield PreferenceStore()


# ── Key / value validation ────────────────────────────────────────────────


class TestKeyValidation:
    def test_valid_keys(self):
        _validate_key("a")
        _validate_key("subagents.mode")
        _validate_key("ui_theme")
        _validate_key("a" * 128)

    def test_key_must_start_with_letter(self):
        with pytest.raises(ValueError, match="Invalid preference key"):
            _validate_key("1key")

    def test_key_no_uppercase(self):
        with pytest.raises(ValueError, match="Invalid preference key"):
            _validate_key("SubAgent")

    def test_key_no_special_chars(self):
        with pytest.raises(ValueError, match="Invalid preference key"):
            _validate_key("key-name")

    def test_key_max_129_chars_rejected(self):
        with pytest.raises(ValueError, match="Invalid preference key"):
            _validate_key("a" * 129)

    def test_key_not_string(self):
        with pytest.raises(ValueError, match="must be a string"):
            _validate_key(123)


class TestValueValidation:
    def test_string_ok(self):
        _validate_value("hello")

    def test_int_ok(self):
        _validate_value(42)

    def test_float_ok(self):
        _validate_value(3.14)

    def test_bool_ok(self):
        _validate_value(True)

    def test_null_ok(self):
        _validate_value(None)

    def test_list_rejected(self):
        with pytest.raises(ValueError, match="not allowed"):
            _validate_value([1, 2, 3])

    def test_dict_rejected(self):
        with pytest.raises(ValueError, match="not allowed"):
            _validate_value({"nested": True})

    def test_string_too_long(self):
        with pytest.raises(ValueError, match="maximum length"):
            _validate_value("x" * 4097)


# ── CRUD tests ────────────────────────────────────────────────────────────


class TestStoreAndRetrieve:
    def test_store_and_get(self, store):
        store.store("u1", "theme", "dark")
        assert store.get("u1", "theme") == "dark"

    def test_get_nonexistent_key(self, store):
        assert store.get("u1", "missing") is None

    def test_get_nonexistent_user(self, store):
        assert store.get("ghost", "any") is None

    def test_overwrite(self, store):
        store.store("u1", "lang", "en")
        store.store("u1", "lang", "pt")
        assert store.get("u1", "lang") == "pt"

    def test_multiple_users(self, store):
        store.store("alice", "theme", "light")
        store.store("bob", "theme", "dark")
        assert store.get("alice", "theme") == "light"
        assert store.get("bob", "theme") == "dark"

    def test_boolean_value(self, store):
        store.store("u1", "enabled", True)
        assert store.get("u1", "enabled") is True

    def test_integer_value(self, store):
        store.store("u1", "count", 42)
        assert store.get("u1", "count") == 42

    def test_null_value(self, store):
        store.store("u1", "reset", None)
        assert store.get("u1", "reset") is None

    def test_dot_namespaced_key(self, store):
        store.store("u1", "subagents.mode", "manual")
        assert store.get("u1", "subagents.mode") == "manual"


class TestDelete:
    def test_delete_existing(self, store):
        store.store("u1", "theme", "dark")
        assert store.delete("u1", "theme") is True
        assert store.get("u1", "theme") is None

    def test_delete_nonexistent_idempotent(self, store):
        assert store.delete("u1", "ghost") is False

    def test_delete_nonexistent_user_idempotent(self, store):
        assert store.delete("ghost", "any") is False

    def test_delete_last_key_removes_user(self, store):
        store.store("u1", "theme", "dark")
        store.delete("u1", "theme")
        assert store.get_all("u1") == {}


class TestListKeys:
    def test_list_empty(self, store):
        assert store.list_keys("u1") == []

    def test_list_sorted(self, store):
        store.store("u1", "z_key", 1)
        store.store("u1", "a_key", 2)
        assert store.list_keys("u1") == ["a_key", "z_key"]

    def test_list_nonexistent_user(self, store):
        assert store.list_keys("ghost") == []


class TestGetAll:
    def test_get_all_empty(self, store):
        assert store.get_all("u1") == {}

    def test_get_all_returns_dict(self, store):
        store.store("u1", "a", 1)
        store.store("u1", "b", "two")
        result = store.get_all("u1")
        assert result == {"a": 1, "b": "two"}

    def test_get_all_nonexistent_user(self, store):
        assert store.get_all("ghost") == {}


# ── Persistence (read-back after new store instance) ──────────────────────


def test_persistence_across_instances(tmp_path: Path):
    prefs_file = tmp_path / "preferences.json"
    with (
        patch("deile.preferences.store._PREFS_FILE", prefs_file),
        patch("deile.preferences.store._PREFS_DIR", tmp_path),
    ):
        s1 = PreferenceStore()
        s1.store("u1", "theme", "dark")

        s2 = PreferenceStore()
        assert s2.get("u1", "theme") == "dark"


# ── Corrupted JSON recovery ───────────────────────────────────────────────


def test_corrupted_json_recovery(tmp_path: Path):
    prefs_file = tmp_path / "preferences.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    prefs_file.write_text("this is not json {{{")

    with (
        patch("deile.preferences.store._PREFS_FILE", prefs_file),
        patch("deile.preferences.store._PREFS_DIR", tmp_path),
    ):
        store = PreferenceStore()
        # get should not crash — returns None
        assert store.get("u1", "theme") is None
        # store should overwrite the corrupted file
        store.store("u1", "theme", "dark")
        assert store.get("u1", "theme") == "dark"


def test_not_a_dict_recovers(tmp_path: Path):
    prefs_file = tmp_path / "preferences.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    prefs_file.write_text("[]")

    with (
        patch("deile.preferences.store._PREFS_FILE", prefs_file),
        patch("deile.preferences.store._PREFS_DIR", tmp_path),
    ):
        store = PreferenceStore()
        assert store.get("u1", "x") is None
        store.store("u1", "x", 1)
        assert store.get("u1", "x") == 1


# ── Concurrent writes ─────────────────────────────────────────────────────


def test_concurrent_writes_no_corruption(tmp_path: Path):
    """Two writers hammering the same file must not corrupt it and
    every write must survive (no lost updates)."""
    prefs_file = tmp_path / "preferences.json"
    errors = []

    # Patch is applied ONCE on the main thread — ``unittest.mock.patch``
    # is not thread-safe (each context-manager exit restores whatever it
    # saved as "original", so two concurrent patches can leave the
    # module attr pointing back to ``~/.deile``).
    with (
        patch("deile.preferences.store._PREFS_FILE", prefs_file),
        patch("deile.preferences.store._PREFS_DIR", tmp_path),
    ):

        def writer(uid: str, start: int):
            s = PreferenceStore()
            for i in range(start, start + 20):
                try:
                    s.store(uid, f"key_{i}", i)
                except Exception as exc:
                    errors.append(exc)

        t1 = threading.Thread(target=writer, args=("u1", 0))
        t2 = threading.Thread(target=writer, args=("u1", 500))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Concurrent write errors: {errors}"

        # Verify file is valid JSON AND no writes were lost.
        s = PreferenceStore()
        all_prefs = s.get_all("u1")
        # 20 keys from t1 (0..19) + 20 keys from t2 (500..519) = 40.
        assert (
            len(all_prefs) == 40
        ), f"Lost updates: expected 40 keys, got {len(all_prefs)}"
        for k, v in all_prefs.items():
            assert isinstance(k, str)
            assert isinstance(v, (str, int, float, bool, type(None)))


# ── Invalid keys rejected at store level ──────────────────────────────────


def test_store_invalid_key_rejected(store):
    with pytest.raises(ValueError, match="Invalid preference key"):
        store.store("u1", "BadKey", "value")


def test_store_invalid_value_rejected(store):
    with pytest.raises(ValueError, match="not allowed"):
        store.store("u1", "key", {"nested": True})


def test_store_value_too_long_rejected(store):
    with pytest.raises(ValueError, match="maximum length"):
        store.store("u1", "key", "x" * 4097)
