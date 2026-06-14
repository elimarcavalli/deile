"""EnvStore — store environment variables in ~/.deile/settings.json.

Replaces the .env file approach: variables are kept in
~/.deile/settings.json (0o600, owner-only) under the ``env.exports``
key and exported into os.environ at process startup.

Security properties:
  - No .env file in the project directory (no accidental git commits)
  - File lives in ~/.deile/ -- outside any repo working tree
  - 0o600 permissions: owner read/write only
  - Values are only in memory (os.environ) after load

Usage in cli.py main():
    from deile.config.env_store import load_exported_vars
    load_exported_vars()   # call before bootstrap_providers()
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_file_lock = threading.Lock()

# Patterns that mark a key as sensitive -- values are masked in list output.
_SENSITIVE_PATTERNS = ("key", "token", "secret", "password", "api")


def _settings_path(home=None):
    return (home or Path.home()) / ".deile" / "settings.json"


def _load_raw(path):
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("env_store: cannot read %s: %s", path, exc)
        return {}


def _save_raw(path, data):
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".{}.".format(path.name),
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name  # capture before any write so except can clean up
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        tmp_name = None  # atomic replace succeeded; nothing left to clean up
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.error("env_store: cannot write %s: %s", path, exc)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return False


def _get_exports(data):
    env_section = data.get("env")
    if not isinstance(env_section, dict):
        return {}
    exports = env_section.get("exports")
    return exports if isinstance(exports, dict) else {}


def _set_exports(data, exports):
    if "env" not in data or not isinstance(data["env"], dict):
        data["env"] = {}
    data["env"]["exports"] = exports


def is_sensitive(key):
    """Return True when *key* looks like it holds a secret value."""
    lower = key.lower()
    return any(pat in lower for pat in _SENSITIVE_PATTERNS)


def load_exported_vars(home=None):
    """Export vars from ~/.deile/settings.json env.exports into os.environ.

    Existing os.environ values take precedence -- an already-set var is
    never overwritten. Returns the dict of keys that were actually exported.
    """
    path = _settings_path(home)
    data = _load_raw(path)
    exports = _get_exports(data)

    loaded = {}
    for key, value in exports.items():
        if not isinstance(key, str) or not key:
            continue
        if not isinstance(value, str):
            value = str(value)
        if key not in os.environ:
            os.environ[key] = value
            loaded[key] = value

    if loaded:
        logger.debug("env_store: exported %d var(s) into os.environ", len(loaded))
    return loaded


def store_var(key, value, home=None):
    """Persist *key*=*value* in ~/.deile/settings.json under env.exports.

    Also sets os.environ[key] immediately so the value is available for
    the rest of the current session without a restart.
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be a non-empty string")
    key = key.strip()
    if not all(c.isalnum() or c == "_" for c in key):
        raise ValueError(
            "Invalid key {!r}: only letters, digits and underscores allowed".format(key)
        )
    if not isinstance(value, str):
        raise TypeError("value must be a string")

    path = _settings_path(home)
    with _file_lock:
        data = _load_raw(path)
        exports = dict(_get_exports(data))
        exports[key] = value
        _set_exports(data, exports)
        ok = _save_raw(path, data)

    if ok:
        os.environ[key] = value
    return ok


def unset_var(key, home=None):
    """Remove *key* from env.exports and from os.environ.

    Returns True if the key existed in the store.
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be a non-empty string")
    key = key.strip()

    path = _settings_path(home)
    with _file_lock:
        data = _load_raw(path)
        exports = dict(_get_exports(data))
        existed = key in exports
        ok = False
        if existed:
            del exports[key]
            _set_exports(data, exports)
            ok = _save_raw(path, data)

    if existed and ok:
        os.environ.pop(key, None)
    return existed


def list_vars(home=None):
    """Return stored env vars with sensitive values replaced by ``<masked>``.

    Keys whose names match common secret patterns (api, key, token, secret,
    password) have their value hidden.
    """
    path = _settings_path(home)
    data = _load_raw(path)
    exports = _get_exports(data)

    result = {}
    for key, value in exports.items():
        if not isinstance(key, str):
            continue
        if is_sensitive(key):
            result[key] = "<masked>"
        else:
            result[key] = str(value) if not isinstance(value, str) else value
    return result
