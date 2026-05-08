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
import hashlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

GLOBAL = "global"
PROJECT = "project"
_VALID_SCOPES = {GLOBAL, PROJECT}

_SENTINEL = object()

# Max size for settings files — prevents memory exhaustion from crafted files.
_MAX_SETTINGS_BYTES = 1_048_576  # 1 MB

# Secret-looking key patterns — refuse to store these in settings files.
_SECRET_KEY_PATTERNS = ("token", "key", "secret", "password", "api_")

# Process-wide lock for read-modify-write operations on settings files.
_file_lock = threading.Lock()


def _is_secret_key(key_path: str) -> bool:
    """Return True if *key_path* matches the secret-key blocklist."""
    key_lower = key_path.lower()
    return any(pat in key_lower for pat in _SECRET_KEY_PATTERNS)


def _hash_value(value: Any) -> str:
    """Return a SHA-256 truncated hex digest of *value*'s JSON form.

    The raw value is never logged. For values that are not JSON-serializable
    we fall back to ``repr()`` — still no leakage of secrets because callers
    redact secret keys before this function is reached.
    """
    try:
        encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _value_fingerprint(key_path: str, value: Any) -> str:
    """Return ``"<redacted>"`` for secret keys, else a SHA-256 truncated hash.

    ``None`` (no previous value) maps to ``"<absent>"`` so audit consumers
    can distinguish "first write" from "value mutated".
    """
    if value is None:
        return "<absent>"
    if _is_secret_key(key_path):
        return "<redacted>"
    return _hash_value(value)


def _check_settings_write_permission(scope: str, resource_detail: str) -> bool:
    """Resolve the permission manager (best-effort) and check write access.

    Returns ``True`` when no permission manager is configured (preserves
    backward-compat for unit-tests that bypass the security stack), and
    ``True``/``False`` according to the manager's verdict otherwise.

    The resource string follows the convention ``"settings:<scope>:<detail>"``
    so rule authors can scope policies tighter than "any settings write".
    """
    try:
        from deile.security.permissions import get_permission_manager
    except ImportError:
        return True
    try:
        pm = get_permission_manager()
    except Exception:  # pragma: no cover — defensive
        logger.exception("settings: cannot resolve PermissionManager; allowing write")
        return True
    if pm is None:
        return True
    resource = f"settings:{scope}:{resource_detail}"
    try:
        return bool(
            pm.check_permission(
                tool_name="settings_manager",
                resource=resource,
                action="write",
                context={"scope": scope, "detail": resource_detail},
            )
        )
    except Exception:  # pragma: no cover — defensive
        logger.exception("settings: PermissionManager.check_permission raised; denying")
        return False


def _emit_settings_audit(
    *,
    scope: str,
    resource_detail: str,
    action: str,
    result: str,
    details: dict,
) -> None:
    """Emit a typed ``AuditEvent`` for a settings.json mutation (best-effort).

    Uses :class:`AuditEventType.SECURITY_POLICY_CHANGED` because changes to
    flags like ``file_safety.enabled`` shift the security posture of the
    process. Failures to emit (logger missing, I/O error) are swallowed —
    they must never block the caller.
    """
    try:
        from deile.security.audit_logger import (AuditEventType, SeverityLevel,
                                                 get_audit_logger)
    except ImportError:
        return
    try:
        logger_obj = get_audit_logger()
    except Exception:  # pragma: no cover
        logger.exception("settings: cannot resolve AuditLogger; skipping audit emit")
        return
    try:
        severity = SeverityLevel.INFO if result == "allowed" else SeverityLevel.WARNING
        logger_obj.log_event(
            event_type=AuditEventType.SECURITY_POLICY_CHANGED,
            severity=severity,
            actor="settings_manager",
            resource=f"settings:{scope}:{resource_detail}",
            action=action,
            result=result,
            details=details,
        )
    except Exception:  # pragma: no cover
        logger.exception("settings: audit emission failed")


def _validate_against_override_handlers(key_path: str, value: Any) -> Optional[str]:
    """Dry-run validate *value* against ``_OVERRIDE_HANDLERS`` for *key_path*.

    Returns ``None`` when validation passes (or the key has no handler — the
    handler set is intentionally a subset of valid keys). Returns a human
    readable error string when the converter rejects the value, so the caller
    can refuse the write before touching disk.
    """
    try:
        from deile.config.settings import _OVERRIDE_HANDLERS
    except ImportError:
        return None
    handler = _OVERRIDE_HANDLERS.get(key_path)
    if handler is None:
        return None
    _field_name, converter = handler
    try:
        converter(value)
    except (TypeError, ValueError) as exc:
        return f"{exc}"
    return None


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

        if _is_secret_key(key_path):
            logger.error(
                "set_setting: refusing to store potential secret in key %r"
                " — use env vars for secrets",
                key_path,
            )
            return False

        self._validate_scope(scope)
        parts = self._split_key_path(key_path)

        # Dry-run validation against the canonical override handlers (issue #125).
        # Reject silently-divergent writes like `set_setting('logging.level', 42)`
        # before touching disk — the on-disk JSON would never round-trip into a
        # valid Settings field if the converter cannot accept the value.
        validation_error = _validate_against_override_handlers(key_path, value)
        if validation_error is not None:
            logger.error(
                "set_setting: rejecting %r=%r in %s — type mismatch with handler: %s",
                key_path,
                value,
                scope,
                validation_error,
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

        # Permission gate (issue #125). Denial is a hard stop — no write,
        # no success-audit, and the audit reflects the denial.
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

    def add_skills_path(self, path: "str | Path", scope: str = GLOBAL) -> bool:
        """Add *path* to ``skills_paths`` in *scope*.

        Creates the settings file (and parent directories) if absent.
        Invalidates the in-memory singleton after a successful write.

        Args:
            path:  Directory path to add.
            scope: ``"global"`` (default) or ``"project"``.

        Returns:
            ``True`` if added, ``False`` if already present (no-op).
        """
        from deile.config.settings import reset_settings

        self._validate_scope(scope)
        # Permission gate (issue #125) — denial returns False, no write,
        # no success-audit. The "duplicate path" no-op below also returns
        # False, matching the pre-#125 contract.
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
            return False

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
                return False
            existing.append(norm)
            data["skills_paths"] = existing
            result = self._save(settings_path, data)

        if result:
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
        return result

    def remove_skills_path(self, path: "str | Path", scope: str = GLOBAL) -> bool:
        """Remove *path* from ``skills_paths`` in *scope*.

        Invalidates the in-memory singleton after a successful write.

        Args:
            path:  Directory path to remove.
            scope: ``"global"`` (default) or ``"project"``.

        Returns:
            ``True`` if removed, ``False`` if path was not found.
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
            return False

        settings_path = self._settings_path(scope)

        with _file_lock:
            data = self._load(settings_path)
            existing = data.get("skills_paths")
            if not isinstance(existing, list):
                return False
            norm = str(path)
            if norm not in existing:
                return False
            existing.remove(norm)
            data["skills_paths"] = existing
            result = self._save(settings_path, data)

        if result:
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
        return result

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

        Args:
            key:   Top-level key in the JSON (e.g. ``"model"``).
            value: JSON-serialisable value.
            scope: ``"global"`` (default) or ``"project"``.

        Returns:
            ``True`` on success, ``False`` on write error.
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(f"Invalid scope {scope!r}. Use 'global' or 'project'.")
        self._ensure_global_dir()
        settings_path = self._settings_path(scope)
        data = self._load_raw(settings_path)
        data[key] = value
        if not isinstance(data.get("skills_paths"), list):
            data["skills_paths"] = []
        return self._save(settings_path, data)
