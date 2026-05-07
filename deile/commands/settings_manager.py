"""SettingsManager — two-layer settings for DEILE (global + project).

Manages ~/.deile/settings.json (global) and <project>/.deile/settings.json (project).

Full preference schema documented in docs/system_design/09-CONFIGURACAO.md.
The get_all_skills_paths() helper and the add/remove skills API remain for
backward compatibility with the /settings slash command.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_SETTINGS: dict = {"skills_paths": []}

GLOBAL = "global"
PROJECT = "project"
_VALID_SCOPES = {GLOBAL, PROJECT}


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
            return {"skills_paths": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Cannot read settings file %s: %s — using defaults", path, exc)
            return {"skills_paths": []}
        if not isinstance(data, dict):
            logger.warning("Settings file %s has unexpected format — using defaults", path)
            return {"skills_paths": []}
        if not isinstance(data.get("skills_paths"), list):
            data["skills_paths"] = []
        return data

    def _load_raw(self, path: Path) -> dict:
        """Load the raw JSON dict; empty dict if missing or invalid."""
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Cannot read settings file %s: %s", path, exc)
            return {}

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_skills_paths(self, scope: str = GLOBAL) -> List[str]:
        """Return skills_paths for the given scope (not merged).

        Args:
            scope: ``"global"`` or ``"project"``.

        Returns:
            List of raw path strings as stored in settings.json.
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(f"Invalid scope {scope!r}. Use 'global' or 'project'.")
        return list(self._load(self._settings_path(scope)).get("skills_paths", []))

    def get_all_skills_paths(self) -> List[Path]:
        """Return the merged union of global + project skills_paths as Path objects.

        Global paths come first, then project paths. Duplicates (resolved) are
        removed; the first occurrence wins.
        """
        seen: set[str] = set()
        result: List[Path] = []
        for scope in (GLOBAL, PROJECT):
            for raw in self.list_skills_paths(scope):
                try:
                    resolved = str(Path(raw).expanduser().resolve())
                except Exception:
                    resolved = raw
                if resolved not in seen:
                    seen.add(resolved)
                    try:
                        result.append(Path(raw).expanduser())
                    except Exception:
                        result.append(Path(raw))
        return result

    def add_skills_path(self, path: "str | Path", scope: str = GLOBAL) -> bool:
        """Add *path* to skills_paths in *scope*.

        Creates the settings file (and parent directories) if absent.

        Args:
            path:  Directory path to add.
            scope: ``"global"`` (default) or ``"project"``.

        Returns:
            ``True`` if added, ``False`` if already present (no-op).
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(f"Invalid scope {scope!r}. Use 'global' or 'project'.")
        self._ensure_global_dir()
        settings_path = self._settings_path(scope)
        data = self._load(settings_path)
        # Always resolve to absolute so the path works regardless of CWD at load time.
        try:
            norm = str(Path(str(path)).expanduser().resolve())
        except Exception:
            norm = str(path)
        if norm in data["skills_paths"]:
            return False
        data["skills_paths"].append(norm)
        return self._save(settings_path, data)

    def remove_skills_path(self, path: "str | Path", scope: str = GLOBAL) -> bool:
        """Remove *path* from skills_paths in *scope*.

        Args:
            path:  Directory path to remove.
            scope: ``"global"`` (default) or ``"project"``.

        Returns:
            ``True`` if removed, ``False`` if path was not found.
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(f"Invalid scope {scope!r}. Use 'global' or 'project'.")
        settings_path = self._settings_path(scope)
        data = self._load(settings_path)
        norm = str(path)
        if norm not in data["skills_paths"]:
            return False
        data["skills_paths"].remove(norm)
        return self._save(settings_path, data)

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
