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
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

GLOBAL = "global"
PROJECT = "project"
_VALID_SCOPES = {GLOBAL, PROJECT}

_SENTINEL = object()


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
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Cannot read settings file %s: %s — using defaults", path, exc)
            return {}
        if not isinstance(data, dict):
            logger.warning("Settings file %s has unexpected format — using defaults", path)
            return {}
        return data

    def _save(self, path: Path, data: dict) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True
        except OSError as exc:
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
    def _deep_merge(user: dict, project: dict) -> dict:
        """Deep-merge *project* onto *user*. Project wins at every leaf.

        Lists are not concatenated — project list replaces user list.
        Only nested dicts are merged recursively.
        """
        merged: dict = copy.deepcopy(user)
        for key, project_value in project.items():
            user_value = merged.get(key, _SENTINEL)
            if (
                user_value is not _SENTINEL
                and isinstance(user_value, dict)
                and isinstance(project_value, dict)
            ):
                merged[key] = SettingsManager._deep_merge(user_value, project_value)
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

        Creates parent directories and any intermediate dict nodes that
        don't exist. If an intermediate node exists but is not a dict,
        raises :class:`ValueError` rather than overwriting silently.

        Args:
            key_path: Dotted key, e.g. ``"logging.level"``.
            value:    Any JSON-serializable value.
            scope:    ``"global"`` (default) or ``"project"``.

        Returns:
            ``True`` if the file was written; ``False`` on I/O error.
        """
        self._validate_scope(scope)
        parts = self._split_key_path(key_path)
        if scope == GLOBAL:
            self._ensure_global_dir()
        path = self._settings_path(scope)
        data = self._load(path)
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
        return self._save(path, data)

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
        are removed; the first occurrence wins.
        """
        seen: set[str] = set()
        result: List[Path] = []
        for scope in (GLOBAL, PROJECT):
            for raw in self.list_skills_paths(scope):
                try:
                    resolved = str(Path(raw).expanduser().resolve())
                except OSError:
                    resolved = raw
                if resolved not in seen:
                    seen.add(resolved)
                    try:
                        result.append(Path(raw).expanduser())
                    except (TypeError, ValueError):
                        result.append(Path(raw))
        return result

    def add_skills_path(self, path: "str | Path", scope: str = GLOBAL) -> bool:
        """Add *path* to ``skills_paths`` in *scope*.

        Creates the settings file (and parent directories) if absent.

        Args:
            path:  Directory path to add.
            scope: ``"global"`` (default) or ``"project"``.

        Returns:
            ``True`` if added, ``False`` if already present (no-op).
        """
        self._validate_scope(scope)
        self._ensure_global_dir()
        settings_path = self._settings_path(scope)
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
        return self._save(settings_path, data)

    def remove_skills_path(self, path: "str | Path", scope: str = GLOBAL) -> bool:
        """Remove *path* from ``skills_paths`` in *scope*.

        Args:
            path:  Directory path to remove.
            scope: ``"global"`` (default) or ``"project"``.

        Returns:
            ``True`` if removed, ``False`` if path was not found.
        """
        self._validate_scope(scope)
        settings_path = self._settings_path(scope)
        data = self._load(settings_path)
        existing = data.get("skills_paths")
        if not isinstance(existing, list):
            return False
        norm = str(path)
        if norm not in existing:
            return False
        existing.remove(norm)
        data["skills_paths"] = existing
        return self._save(settings_path, data)
