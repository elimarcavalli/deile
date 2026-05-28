"""SettingsManager — two-layer settings for DEILE (global + project).

Manages ``~/.deile/settings.json`` (global / user) and
``<project>/.deile/settings.json`` (project).

Layering semantics:
  - **Project layer wins** over user (global) layer for any conflicting key.
  - ``skills_paths`` is special-cased: union of both layers (global before
    project), with duplicates removed. See :meth:`get_all_skills_paths`.
  - All other keys: project value REPLACES user value at the leaf. Nested
    dicts are deep-merged (project keys override matching user keys, but
    sibling keys from both layers coexist).

Schema is loose: any JSON object is accepted. Validation is the caller's
responsibility. Dotted access (e.g. ``"logging.level"``) is supported by
:meth:`get_setting` and :meth:`set_setting`.

Origin: introduced in #104 for ``skills_paths`` only; extended in #111 to
serve as the single source of personal/project preferences (replaces the
legacy ``config/settings.json`` flow and most ``DEILE_*`` env vars).

Full preference schema documented in docs/system_design/09-CONFIGURACAO.md.
The get_all_skills_paths() helper and the add/remove skills API remain for
backward compatibility with the /settings slash command.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, List, Optional, Tuple

# Security helpers extracted to a sibling module (issue #125, S-4):
# permission gate, audit emission, secret-key check, fingerprinting,
# dry-run validation. They are re-exported below for the existing test
# imports (``from deile.commands.settings_manager import _is_secret_key``).
from ._settings_security_hooks import \
    check_settings_write_permission as _check_settings_write_permission
from ._settings_security_hooks import \
    emit_settings_audit as _emit_settings_audit
from ._settings_security_hooks import hash_value as _hash_value
from ._settings_security_hooks import is_secret_key as _is_secret_key
from ._settings_security_hooks import \
    validate_against_override_handlers as _validate_against_override_handlers
from ._settings_security_hooks import value_fingerprint as _value_fingerprint

logger = logging.getLogger(__name__)

GLOBAL = "global"
PROJECT = "project"
_VALID_SCOPES = {GLOBAL, PROJECT}

_SENTINEL = object()

# Max size for settings files — prevents memory exhaustion from crafted files.
_MAX_SETTINGS_BYTES = 1_048_576  # 1 MB

# Process-wide lock for read-modify-write operations on settings files.
_file_lock = threading.Lock()


class SettingsManager:
    """Reads and writes DEILE settings across global and project scopes.

    Args:
        project_dir: Root of the current project (defaults to ``Path.cwd()``).
        user_home:   User's home directory (defaults to ``Path.home()``).
    """

    GLOBAL = GLOBAL
    PROJECT = PROJECT

    def __init__(
        self,
        project_dir: Optional[Path] = None,
        user_home: Optional[Path] = None,
    ) -> None:
        self._project_dir = project_dir or Path.cwd()
        self._user_home = user_home or Path.home()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @property
    def global_settings_path(self) -> Path:
        return self._user_home / ".deile" / "settings.json"

    @property
    def project_settings_path(self) -> Path:
        return self._project_dir / ".deile" / "settings.json"

    def _settings_path(self, scope: str) -> Path:
        if scope == PROJECT:
            return self.project_settings_path
        return self.global_settings_path

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    def _load(self, path: Path) -> dict:
        """Load a settings file with size cap, symlink guard, and JSON validation."""
        if not path.exists():
            return {}
        if path.is_symlink():
            logger.error("Refusing to load settings from symlink %s", path)
            return {}
        try:
            size = path.stat().st_size
        except OSError as exc:
            logger.warning("Cannot stat settings file %s: %s", path, exc)
            return {}
        if size > _MAX_SETTINGS_BYTES:
            logger.warning(
                "Settings file %s exceeds %d bytes (%d) — using defaults",
                path,
                _MAX_SETTINGS_BYTES,
                size,
            )
            return {}
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                logger.warning(
                    "Settings file %s is not a JSON object — using defaults", path
                )
                return {}
            return data
        except json.JSONDecodeError as exc:
            logger.warning(
                "Cannot parse settings file %s as JSON (line %d, col %d) — using defaults",
                path,
                exc.lineno,
                exc.colno,
            )
            return {}
        except OSError as exc:
            logger.warning(
                "Cannot read settings file %s: %s — using defaults", path, exc
            )
            return {}

    def _load_raw(self, path: Path) -> dict:
        """Load the raw JSON dict; empty dict if missing or invalid."""
        return self._load(path)

    def _save(self, path: Path, data: dict) -> bool:
        """Atomically write *data* as JSON to *path*.

        Uses a temp file + ``os.replace`` for crash-safety. Sets 0o600
        permissions on global settings.
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                json.dump(data, tmp, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_name = tmp.name
            os.replace(tmp_name, path)
            # Restrict global settings to owner-only
            if path == self.global_settings_path:
                os.chmod(path, 0o600)
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.error("Cannot write settings file %s: %s", path, exc)
            return False

    def _ensure_global_dir(self) -> None:
        try:
            self.global_settings_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Cannot create global settings dir: %s", exc)

    @staticmethod
    def _validate_scope(scope: str) -> None:
        if scope not in _VALID_SCOPES:
            raise ValueError(f"Invalid scope {scope!r}. Use 'global' or 'project'.")

    @staticmethod
    def _split_key_path(key_path: str) -> List[str]:
        if not isinstance(key_path, str) or not key_path.strip():
            raise ValueError("key_path must be a non-empty string")
        parts = key_path.split(".")
        if any(not part for part in parts):
            raise ValueError(f"Invalid key_path {key_path!r}: empty segment")
        return parts

    # ------------------------------------------------------------------
    # Layer access (raw, not merged)
    # ------------------------------------------------------------------

    def get_layer(self, scope: str) -> dict:
        """Return the raw settings dict for *scope* (no merge, deep copy).

        Args:
            scope: ``"global"`` or ``"project"``.

        Returns:
            A deep copy of the on-disk JSON (or ``{}`` if the file is
            missing or malformed). Mutations do not leak into storage.
        """
        self._validate_scope(scope)
        return copy.deepcopy(self._load(self._settings_path(scope)))

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    @staticmethod
    def _deep_merge(user: dict, project: dict, _depth: int = 0) -> dict:
        """Deep-merge *project* onto *user*. Project wins at every leaf.

        Lists are not concatenated — project list replaces user list.
        Only nested dicts are merged recursively. Depth is capped at 32
        to prevent stack overflow from adversarially crafted input.
        """
        if _depth > 32:
            logger.warning("settings: deep-merge depth cap hit; returning project subtree")
            return copy.deepcopy(project)
        merged: dict = copy.deepcopy(user)
        for key, project_value in project.items():
            user_value = merged.get(key, _SENTINEL)
            if (
                user_value is not _SENTINEL
                and isinstance(user_value, dict)
                and isinstance(project_value, dict)
            ):
                merged[key] = SettingsManager._deep_merge(
                    user_value, project_value, _depth + 1
                )
            else:
                merged[key] = copy.deepcopy(project_value)
        return merged

    def get_merged(self) -> dict:
        """Return user + project deep-merged (project wins).

        ``skills_paths`` is intentionally NOT special-cased here — it
        follows the standard "project replaces user" rule like any other
        key. Use :meth:`get_all_skills_paths` for the union semantics.
        """
        user = self._load(self.global_settings_path)
        project = self._load(self.project_settings_path)
        return self._deep_merge(user, project)

    # ------------------------------------------------------------------
    # Dotted-key access on the merged view
    # ------------------------------------------------------------------

    def get_setting(self, key_path: str, default: Any = None) -> Any:
        """Read a dotted-path setting from the merged view (project > user).

        Args:
            key_path: Dotted key, e.g. ``"logging.level"``.
            default:  Returned when the path resolves to nothing.

        Returns:
            The value at *key_path*, or *default* if the path is missing
            or one of the intermediate nodes is not a dict.
        """
        parts = self._split_key_path(key_path)
        node: Any = self.get_merged()
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set_setting(self, key_path: str, value: Any, scope: str = GLOBAL) -> bool:
        """Write *value* at *key_path* in *scope*'s settings file.

        Refuses keys that look like secrets (token, key, secret, password,
        api_). Creates parent directories and any intermediate dict nodes
        that don't exist. If an intermediate node exists but is not a dict,
        raises :class:`ValueError` rather than overwriting silently.

        Invalidates the in-memory singleton after a successful write.

        Args:
            key_path: Dotted key, e.g. ``"logging.level"``.
            value:    Any JSON-serializable value.
            scope:    ``"global"`` (default) or ``"project"``.

        Returns:
            ``True`` if the file was written; ``False`` on I/O error or
            if the key looks like a secret.
        """
        from deile.config.settings import reset_settings

        self._validate_scope(scope)
        parts = self._split_key_path(key_path)

        # P1-2: secret refusal must emit audit (sec-relevant signal that
        # someone is trying to stash credentials in settings.json). The
        # audit payload NEVER includes the raw *value*.
        if _is_secret_key(key_path):
            logger.error(
                "set_setting: refusing to store potential secret in key %r"
                " — use env vars for secrets",
                key_path,
            )
            _emit_settings_audit(
                scope=scope,
                resource_detail=key_path,
                action="write",
                result="refused_secret",
                details={
                    "key_path": key_path,
                    "scope": scope,
                    "reason": "secret_pattern",
                },
            )
            return False

        # P2-1: permission check FIRST, before any validation work.
        # Otherwise a caller without write permission could probe the
        # validation surface (key existence, expected types) by observing
        # ``result="invalid"`` vs ``result="denied"``.
        if not _check_settings_write_permission(scope, key_path):
            _emit_settings_audit(
                scope=scope,
                resource_detail=key_path,
                action="write",
                result="denied",
                details={
                    "key_path": key_path,
                    "scope": scope,
                    "reason": "permission_denied",
                },
            )
            logger.warning(
                "set_setting: permission denied for %r in %s scope", key_path, scope
            )
            return False

        # Dry-run validation against the canonical override handlers (issue #125).
        # Reject silently-divergent writes like `set_setting('logging.level', 42)`
        # before touching disk — the on-disk JSON would never round-trip into a
        # valid Settings field if the converter cannot accept the value.
        # P0-1: NEITHER log nor audit may carry the raw `value` — callers can
        # accidentally pass a secret to a non-secret-shaped key. We log a
        # fingerprint and the converter message is sanitized.
        validation_error = _validate_against_override_handlers(key_path, value)
        if validation_error is not None:
            logger.error(
                "set_setting: rejecting %r=<fingerprint:%s> in %s — type mismatch with handler",
                key_path,
                _value_fingerprint(key_path, value),
                scope,
            )
            _emit_settings_audit(
                scope=scope,
                resource_detail=key_path,
                action="write",
                result="invalid",
                details={
                    "key_path": key_path,
                    "scope": scope,
                    "reason": "validation_failed",
                    "error": validation_error,
                    "new_value_fingerprint": _value_fingerprint(key_path, value),
                },
            )
            return False

        if scope == GLOBAL:
            self._ensure_global_dir()
        path = self._settings_path(scope)

        with _file_lock:
            data = self._load(path)
            # Capture the old value (if any) for the audit fingerprint.
            old_node: Any = data
            old_value: Any = None
            for part in parts:
                if isinstance(old_node, dict) and part in old_node:
                    old_node = old_node[part]
                    old_value = old_node
                else:
                    old_value = None
                    break
            node: dict = data
            for part in parts[:-1]:
                existing = node.get(part)
                if existing is None:
                    existing = {}
                    node[part] = existing
                elif not isinstance(existing, dict):
                    raise ValueError(
                        f"Cannot set {key_path!r} in {scope}: intermediate node "
                        f"{part!r} is {type(existing).__name__}, not dict"
                    )
                node = existing
            node[parts[-1]] = value
            result = self._save(path, data)

        if result:
            reset_settings()
            _emit_settings_audit(
                scope=scope,
                resource_detail=key_path,
                action="write",
                result="allowed",
                details={
                    "key_path": key_path,
                    "scope": scope,
                    "old_value_fingerprint": _value_fingerprint(key_path, old_value),
                    "new_value_fingerprint": _value_fingerprint(key_path, value),
                },
            )
        return result

    # ------------------------------------------------------------------
    # skills_paths — backward-compatible API (#104)
    # ------------------------------------------------------------------

    def list_skills_paths(self, scope: str = GLOBAL) -> List[str]:
        """Return ``skills_paths`` for the given scope (not merged).

        Args:
            scope: ``"global"`` or ``"project"``.

        Returns:
            List of raw path strings as stored in settings.json.
        """
        self._validate_scope(scope)
        data = self._load(self._settings_path(scope))
        raw = data.get("skills_paths", [])
        if not isinstance(raw, list):
            return []
        return list(raw)

    def get_all_skills_paths(self) -> List[Path]:
        """Return the merged union of global + project ``skills_paths``.

        Global paths come first, then project paths. Duplicates (resolved)
        are removed; the first occurrence wins. All returned paths are
        expanded and resolved.
        """
        seen: set[str] = set()
        result: List[Path] = []
        for raw in self.list_skills_paths(GLOBAL) + self.list_skills_paths(PROJECT):
            p = Path(raw).expanduser().resolve()
            key = str(p)
            if key not in seen:
                seen.add(key)
                result.append(p)
        return result

    def add_skills_path_detailed(
        self, path: "str | Path", scope: str = GLOBAL
    ) -> Tuple[bool, str]:
        """Add *path* to ``skills_paths`` in *scope* — returns (success, reason).

        P2-3: callers (notably ``/skills add``) need to tell apart
        ``"already_present"`` from ``"denied"`` so the UI can render a
        meaningful message. Reason ∈ {``"added"``, ``"already_present"``,
        ``"denied"``, ``"io_error"``}.
        """
        from deile.config.settings import reset_settings

        self._validate_scope(scope)
        # Permission gate (issue #125) — denial returns (False, "denied").
        if not _check_settings_write_permission(scope, "skills_paths"):
            _emit_settings_audit(
                scope=scope,
                resource_detail="skills_paths",
                action="add_skills_path",
                result="denied",
                details={
                    "scope": scope,
                    "reason": "permission_denied",
                    "path_fingerprint": _hash_value(str(path)),
                },
            )
            logger.warning(
                "add_skills_path: permission denied in %s scope", scope
            )
            return False, "denied"

        # P2-4: only ensure the global dir when actually writing global.
        if scope == GLOBAL:
            self._ensure_global_dir()
        settings_path = self._settings_path(scope)

        with _file_lock:
            data = self._load(settings_path)
            existing = data.get("skills_paths")
            if not isinstance(existing, list):
                existing = []
            try:
                norm = str(Path(str(path)).expanduser().resolve())
            except OSError:
                norm = str(path)
            if norm in existing:
                return False, "already_present"
            existing.append(norm)
            data["skills_paths"] = existing
            saved = self._save(settings_path, data)

        if not saved:
            return False, "io_error"

        reset_settings()
        _emit_settings_audit(
            scope=scope,
            resource_detail="skills_paths",
            action="add_skills_path",
            result="allowed",
            details={
                "scope": scope,
                "operation": "add",
                "path_fingerprint": _hash_value(norm),
                "new_count": len(existing),
            },
        )
        return True, "added"

    def add_skills_path(self, path: "str | Path", scope: str = GLOBAL) -> bool:
        """Add *path* to ``skills_paths`` in *scope`` — boolean shim.

        Backward-compatible wrapper around :meth:`add_skills_path_detailed`.
        Returns ``True`` only when the path was actually added — preserves
        the pre-#125 contract for callers that don't need a reason code.
        """
        success, _reason = self.add_skills_path_detailed(path, scope=scope)
        return success

    def remove_skills_path_detailed(
        self, path: "str | Path", scope: str = GLOBAL
    ) -> Tuple[bool, str]:
        """Remove *path* from ``skills_paths`` in *scope* — returns (success, reason).

        Reason ∈ {``"removed"``, ``"not_found"``, ``"denied"``, ``"io_error"``}.
        """
        from deile.config.settings import reset_settings

        self._validate_scope(scope)
        # Permission gate (issue #125) — same contract as add_skills_path.
        if not _check_settings_write_permission(scope, "skills_paths"):
            _emit_settings_audit(
                scope=scope,
                resource_detail="skills_paths",
                action="remove_skills_path",
                result="denied",
                details={
                    "scope": scope,
                    "reason": "permission_denied",
                    "path_fingerprint": _hash_value(str(path)),
                },
            )
            logger.warning(
                "remove_skills_path: permission denied in %s scope", scope
            )
            return False, "denied"

        settings_path = self._settings_path(scope)

        with _file_lock:
            data = self._load(settings_path)
            existing = data.get("skills_paths")
            if not isinstance(existing, list):
                return False, "not_found"
            norm = str(path)
            if norm not in existing:
                return False, "not_found"
            existing.remove(norm)
            data["skills_paths"] = existing
            saved = self._save(settings_path, data)

        if not saved:
            return False, "io_error"

        reset_settings()
        _emit_settings_audit(
            scope=scope,
            resource_detail="skills_paths",
            action="remove_skills_path",
            result="allowed",
            details={
                "scope": scope,
                "operation": "remove",
                "path_fingerprint": _hash_value(norm),
                "new_count": len(existing),
            },
        )
        return True, "removed"

    def remove_skills_path(self, path: "str | Path", scope: str = GLOBAL) -> bool:
        """Remove *path* from ``skills_paths`` in *scope`` — boolean shim."""
        success, _reason = self.remove_skills_path_detailed(path, scope=scope)
        return success

    def load_all_preferences(self, scope: str = GLOBAL) -> dict:
        """Return the full preference dict for *scope* (raw JSON content).

        Unlike ``list_skills_paths``, this returns the entire settings dict so
        callers can read any preference field, not just skills.

        Args:
            scope: ``"global"`` or ``"project"``.

        Returns:
            Dict with all fields stored in the settings file for that scope.
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(f"Invalid scope {scope!r}. Use 'global' or 'project'.")
        return self._load_raw(self._settings_path(scope))

    def get_all_preferences(self) -> dict:
        """Return merged preferences: global first, project overrides.

        Project-scope keys win over global-scope keys at the top level and
        within nested dicts (one-level deep merge).
        """
        merged: dict = {}
        for scope in (GLOBAL, PROJECT):
            for key, value in self.load_all_preferences(scope).items():
                if (
                    key in merged
                    and isinstance(merged[key], dict)
                    and isinstance(value, dict)
                ):
                    merged[key] = {**merged[key], **value}
                else:
                    merged[key] = value
        return merged

    def set_preference(self, key: str, value: object, scope: str = GLOBAL) -> bool:
        """Write a single top-level preference key to *scope*.

        Creates the settings file if absent.

        P0-2 (issue #125): this is a public write endpoint into
        ``~/.deile/settings.json`` and ``<project>/.deile/settings.json``,
        so it goes through the SAME security pipeline as
        :meth:`set_setting`:
          - secret-key blocklist (with audit emission on refusal),
          - ``PermissionManager`` write check (denial = audit + return False),
          - audit emission on success with old/new fingerprints.

        Args:
            key:   Top-level key in the JSON (e.g. ``"model"``).
            value: JSON-serialisable value.
            scope: ``"global"`` (default) or ``"project"``.

        Returns:
            ``True`` on success, ``False`` on refusal, denial, or write error.
        """
        from deile.config.settings import reset_settings

        if scope not in _VALID_SCOPES:
            raise ValueError(f"Invalid scope {scope!r}. Use 'global' or 'project'.")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("set_preference: key must be a non-empty string")

        # Mirror set_setting: secret refusal -> audit -> deny.
        if _is_secret_key(key):
            logger.error(
                "set_preference: refusing to store potential secret in key %r"
                " — use env vars for secrets",
                key,
            )
            _emit_settings_audit(
                scope=scope,
                resource_detail=key,
                action="write",
                result="refused_secret",
                details={
                    "key_path": key,
                    "scope": scope,
                    "reason": "secret_pattern",
                },
            )
            return False

        # Permission gate FIRST (P2-1 ordering).
        if not _check_settings_write_permission(scope, key):
            _emit_settings_audit(
                scope=scope,
                resource_detail=key,
                action="write",
                result="denied",
                details={
                    "key_path": key,
                    "scope": scope,
                    "reason": "permission_denied",
                },
            )
            logger.warning(
                "set_preference: permission denied for %r in %s scope", key, scope
            )
            return False

        if scope == GLOBAL:
            self._ensure_global_dir()
        settings_path = self._settings_path(scope)

        with _file_lock:
            data = self._load_raw(settings_path)
            old_value = data.get(key)
            data[key] = value
            if not isinstance(data.get("skills_paths"), list):
                data["skills_paths"] = []
            saved = self._save(settings_path, data)

        if saved:
            reset_settings()
            _emit_settings_audit(
                scope=scope,
                resource_detail=key,
                action="write",
                result="allowed",
                details={
                    "key_path": key,
                    "scope": scope,
                    "old_value_fingerprint": _value_fingerprint(key, old_value),
                    "new_value_fingerprint": _value_fingerprint(key, value),
                },
            )
        return saved

    def unset_setting(self, key_path: str, scope: str = GLOBAL) -> bool:
        """Remove *key_path* from *scope*'s settings file.

        The key is deleted from the innermost nested dict it lives in.
        After deletion, if the parent dict becomes empty it is left in place
        (pruning empty dicts is deliberately avoided to keep the file stable).

        Refuses keys that look like secrets (same blocklist as
        :meth:`set_setting`). Goes through the same permission / audit
        pipeline. Invalidates the singleton after a successful write.

        Args:
            key_path: Dotted key, e.g. ``"logging.level"``.
            scope:    ``"global"`` (default) or ``"project"``.

        Returns:
            ``True`` if the key was removed (or was already absent);
            ``False`` on I/O error, secret refusal, or permission denial.
        """
        from deile.config.settings import reset_settings

        self._validate_scope(scope)
        parts = self._split_key_path(key_path)

        if _is_secret_key(key_path):
            logger.error(
                "unset_setting: refusing to operate on potential secret key %r",
                key_path,
            )
            _emit_settings_audit(
                scope=scope,
                resource_detail=key_path,
                action="delete",
                result="refused_secret",
                details={"key_path": key_path, "scope": scope, "reason": "secret_pattern"},
            )
            return False

        if not _check_settings_write_permission(scope, key_path):
            _emit_settings_audit(
                scope=scope,
                resource_detail=key_path,
                action="delete",
                result="denied",
                details={"key_path": key_path, "scope": scope, "reason": "permission_denied"},
            )
            logger.warning(
                "unset_setting: permission denied for %r in %s scope", key_path, scope
            )
            return False

        if scope == GLOBAL:
            self._ensure_global_dir()
        path = self._settings_path(scope)

        with _file_lock:
            data = self._load(path)
            node: Any = data
            for part in parts[:-1]:
                if not isinstance(node, dict) or part not in node:
                    # Key already absent — treat as success.
                    _emit_settings_audit(
                        scope=scope,
                        resource_detail=key_path,
                        action="delete",
                        result="allowed",
                        details={"key_path": key_path, "scope": scope, "was_present": False},
                    )
                    return True
                node = node[part]
            if not isinstance(node, dict) or parts[-1] not in node:
                _emit_settings_audit(
                    scope=scope,
                    resource_detail=key_path,
                    action="delete",
                    result="allowed",
                    details={"key_path": key_path, "scope": scope, "was_present": False},
                )
                return True
            del node[parts[-1]]
            result = self._save(path, data)

        if result:
            reset_settings()
            _emit_settings_audit(
                scope=scope,
                resource_detail=key_path,
                action="delete",
                result="allowed",
                details={"key_path": key_path, "scope": scope, "was_present": True},
            )
        return result
