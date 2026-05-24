"""Loads ``deile/config/skills.yaml`` into a ``SkillsConfig`` dataclass.

Mirrors the pattern used by ``_apply_profile_layer`` in
``deile/config/settings.py`` (lazy import of PyYAML, silent no-op on missing
file). The skills subsystem owns its YAML rather than bloating the global
``Settings`` dataclass — only fields that are read in two or more subsystems
belong there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "skills.yaml"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class SkillsConfig:
    """Resolved skills-subsystem configuration."""

    enabled: bool = True
    max_per_turn: int = 4
    library_paths: List[Path] = field(default_factory=list)
    extension_map: Dict[str, str] = field(default_factory=dict)
    basename_map: Dict[str, str] = field(default_factory=dict)


def load_skills_config(path: Optional[Path] = None) -> SkillsConfig:
    """Read *path* (or the bundled default) and return a ``SkillsConfig``.

    Fallback semantics:

    - **Missing file** → defaults (``enabled=True``). The skills system is
      built to ship with sensible defaults, so the absence of a YAML
      override is not a reason to turn the whole subsystem off.
    - **Unreadable file** (permissions, I/O error) → defaults + WARNING log.
      Same reasoning: the user intends skills to work and was not asked to
      provide this file.
    - **Malformed YAML** → ``enabled=False`` + WARNING log. A broken YAML is
      a deliberate configuration that the user wrote and got wrong; turning
      the subsystem off avoids silently ignoring intent.
    - **Non-mapping root** (e.g. YAML list) → ``enabled=False`` + WARNING log.
      Same reason as malformed YAML.
    """
    config_path = path or _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        logger.debug("skills: config file not found at %s; using defaults", config_path)
        return SkillsConfig()

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        logger.warning(
            "skills: cannot read %s (%s); using defaults instead",
            config_path,
            exc,
        )
        return SkillsConfig()
    except yaml.YAMLError as exc:
        logger.warning(
            "skills: %s is malformed YAML (%s); subsystem disabled — fix the file to re-enable",
            config_path,
            exc,
        )
        return SkillsConfig(enabled=False)

    if not isinstance(raw, dict):
        logger.warning(
            "skills: %s root is %s, expected a YAML mapping; subsystem disabled",
            config_path,
            type(raw).__name__,
        )
        return SkillsConfig(enabled=False)

    enabled = bool(raw.get("enabled", True))

    try:
        max_per_turn = max(1, int(raw.get("max_per_turn", 4)))
    except (TypeError, ValueError):
        logger.warning("skills: invalid max_per_turn in %s; defaulting to 4", config_path)
        max_per_turn = 4

    library_paths = _resolve_library_paths(raw.get("library_paths") or [])
    extension_map = _coerce_str_str_map(raw.get("extension_map"), "extension_map")
    basename_map = _coerce_str_str_map(raw.get("basename_map"), "basename_map")

    return SkillsConfig(
        enabled=enabled,
        max_per_turn=max_per_turn,
        library_paths=library_paths,
        extension_map=extension_map,
        basename_map=basename_map,
    )


def _resolve_library_paths(raw: object) -> List[Path]:
    if not isinstance(raw, list):
        logger.warning("skills: library_paths must be a list; ignoring")
        return []
    out: List[Path] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            continue
        p = Path(entry)
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        out.append(p)
    return out


def _coerce_str_str_map(raw: object, field_name: str) -> Dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("skills: %s must be a mapping; ignoring", field_name)
        return {}
    return {str(k): str(v) for k, v in raw.items() if k is not None and v is not None}
