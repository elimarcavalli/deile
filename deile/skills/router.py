"""SkillRouter — picks the skills whose triggers fire for the current turn."""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional

from .base import Skill
from .language_detector import LanguageDetector
from .registry import SkillRegistry

logger = logging.getLogger(__name__)


# Cap how much of each referenced file we sample for ``file_content_patterns``.
# 4 KiB covers shebangs, module docstrings and typical import blocks without
# turning trigger evaluation into expensive disk I/O.
_FILE_CONTENT_SAMPLE_BYTES = 4096


@dataclass(frozen=True)
class SkillSelectionContext:
    """Per-turn inputs the router evaluates triggers against."""

    user_input: str = ""
    file_references: tuple = ()


def _matches_file_globs(globs: Iterable[str], file_refs: Iterable[str]) -> bool:
    if not globs:
        return False
    glob_list = list(globs)
    for ref in file_refs:
        name = Path(ref).name
        for pattern in glob_list:
            # Try matching against both basename and full path for "**/foo" style.
            if fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(ref, pattern):
                return True
    return False


def _matches_code_block_langs(langs: Iterable[str], detected: Iterable[str]) -> bool:
    if not langs:
        return False
    detected_set = set(detected)
    return any(lang.lower() in detected_set for lang in langs)


def _matches_keywords(keywords: Iterable[str], user_input: str) -> bool:
    """Word-boundary, case-insensitive match — ``\\b`` avoids 'rust' inside 'trust'."""
    if not keywords or not user_input:
        return False
    for raw in keywords:
        if not raw:
            continue
        pattern = r"\b" + re.escape(raw.strip()) + r"\b"
        if re.search(pattern, user_input, re.IGNORECASE):
            return True
    return False


def _resolve_within(ref: str, project_root: Path) -> Optional[Path]:
    """Resolve *ref* into an absolute path that stays inside *project_root*.

    Returns ``None`` for paths that escape the root — a defense against a
    malicious skill frontmatter using ``file_content_patterns`` to probe
    ``/etc/passwd`` via a crafted ``file_reference`` value. Tolerates a
    non-existent project root (legitimate during tests).
    """
    path = Path(ref)
    if not path.is_absolute():
        path = project_root / path
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        root_resolved = project_root.resolve()
    except (OSError, RuntimeError):
        return resolved
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return None
    return resolved


def _matches_file_content(
    patterns: Iterable[str],
    file_refs: Iterable[str],
    cache: dict,
    project_root: Path,
) -> bool:
    pattern_list = list(patterns)
    if not pattern_list:
        return False

    compiled: List[re.Pattern] = []
    for raw in pattern_list:
        if not raw:
            continue
        try:
            compiled.append(re.compile(raw, re.MULTILINE))
        except re.error as exc:
            logger.warning(
                "skills: invalid file_content_pattern %r: %s — pattern skipped",
                raw,
                exc,
            )
    if not compiled:
        return False

    for ref in file_refs:
        if not ref:
            continue
        resolved = _resolve_within(ref, project_root)
        if resolved is None:
            logger.debug(
                "skills: file_content trigger ignoring out-of-root reference %r", ref
            )
            continue
        key = str(resolved)
        if key in cache:
            sample = cache[key]
        else:
            try:
                with open(resolved, "rb") as fh:
                    raw_bytes = fh.read(_FILE_CONTENT_SAMPLE_BYTES)
            except (OSError, IsADirectoryError) as exc:
                logger.debug(
                    "skills: cannot read %s for file_content trigger (%s); skipped",
                    resolved,
                    exc,
                )
                cache[key] = ""
                continue
            sample = raw_bytes.decode("utf-8", errors="replace")
            cache[key] = sample
        if not sample:
            continue
        for compiled_pattern in compiled:
            if compiled_pattern.search(sample):
                return True
    return False


class SkillRouter:
    """Selects active skills for a turn based on registered trigger conditions.

    Deterministic: ties on ``priority`` are broken by ``skill.name`` so the
    same context always yields the same ordering.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        language_detector: Optional[LanguageDetector] = None,
        max_skills_per_turn: int = 4,
        project_root: Optional[Path] = None,
    ) -> None:
        self._registry = registry
        self._detector = language_detector or LanguageDetector()
        self._max = max_skills_per_turn
        self._project_root = project_root or Path.cwd()
        # Optional handle to a running ``SkillsWatcher`` — set by
        # ``bootstrap_skills`` when hot-reload is enabled so callers can
        # ``stop()`` it on shutdown without cluttering the constructor.
        self.watcher: Optional[Any] = None

    def select_skills(self, context: SkillSelectionContext) -> List[Skill]:
        """Return the skills whose triggers fire, capped at ``max_skills_per_turn``."""
        if not self._registry.list_all():
            return []

        code_block_langs = self._detector.langs_in_code_blocks(context.user_input)
        file_languages = self._detector.languages_for_paths(context.file_references)

        # A skill whose ``code_block_langs`` includes "python" should also fire
        # for a ``.py`` file reference even without a code-fence in the input.
        all_detected_langs = list({*code_block_langs, *file_languages})

        # Per-turn cache so multiple skills referencing the same file read it once.
        content_cache: dict = {}

        matched: List[Skill] = []
        for skill in self._registry.list_all():
            trig = skill.triggers
            if _matches_file_globs(trig.file_globs, context.file_references):
                matched.append(skill)
                continue
            if _matches_code_block_langs(trig.code_block_langs, all_detected_langs):
                matched.append(skill)
                continue
            if _matches_keywords(trig.keywords, context.user_input):
                matched.append(skill)
                continue
            if _matches_file_content(
                trig.file_content_patterns,
                context.file_references,
                content_cache,
                self._project_root,
            ):
                matched.append(skill)
                continue

        matched.sort(key=lambda s: (-s.priority, s.name))
        return matched[: self._max]

    def render_block(self, skills: List[Skill]) -> str:
        """Format selected skills as a block ready to append to the system prompt."""
        if not skills:
            return ""
        sections: List[str] = ["## Active Skills"]
        for skill in skills:
            sections.append(f"### Skill: {skill.name}\n{skill.content}")
        return "\n\n".join(sections)

    def render_catalog(self, exclude_names: Iterable[str] = ()) -> str:
        """Render a compact catalog of every registered skill.

        The directive at the top is load-bearing: empirical testing (commit
        25b2cd7) showed that without it the LLM does not spontaneously call
        ``invoke_skill`` even when a skill clearly matches the question.
        """
        excluded = set(exclude_names)
        skills = [s for s in self._registry.list_all() if s.name not in excluded]
        if not skills:
            return ""
        skills.sort(key=lambda s: s.name)

        lines: List[str] = [
            "## Available Skills",
            "",
            "**Before answering any question, check whether one of the skills below "
            "covers the topic.** Each skill contains project-specific rules that "
            "OVERRIDE generic knowledge from your training. To consult one, call "
            "the `invoke_skill` tool with its name — do this BEFORE writing your "
            "answer, not after. Failing to consult an applicable skill is a "
            "regression: the user added these skills precisely so you would use "
            "them.",
            "",
            'Concrete example: if the user asks _"how should I handle X in '
            '<topic>?"_ and a skill named `<topic>` is listed below, your first '
            'action MUST be `invoke_skill(name="<topic>")` — even if you think '
            "you already know the answer. Read the returned body, THEN answer.",
            "",
            "Catalog (`name` — description — _trigger hint_):",
            "",
        ]
        for skill in skills:
            hint = _trigger_hint(skill)
            suffix = f" — _{hint}_" if hint else ""
            lines.append(f"- `{skill.name}` — {skill.description}{suffix}")
        return "\n".join(lines)


def _trigger_hint(skill: Skill) -> str:
    """One-line "auto-active when ..." hint describing the skill's triggers."""
    parts: List[str] = []
    trig = skill.triggers
    if trig.file_globs:
        parts.append("files: " + ", ".join(trig.file_globs))
    if trig.code_block_langs:
        parts.append("langs: " + ", ".join(trig.code_block_langs))
    if trig.keywords:
        parts.append("keywords: " + ", ".join(trig.keywords))
    if trig.file_content_patterns:
        parts.append(f"{len(trig.file_content_patterns)} content pattern(s)")
    if not parts:
        return ""
    return "auto-active when " + "; ".join(parts)
