"""Central constants for the autonomous pipeline.

Deployment-tunable values are read from ``~/.deile/settings.json`` (or the
project-level ``.deile/settings.json``). DEILE_PIPELINE_* env vars remain
supported as a deprecated fallback — set the JSON key instead.
Internal sizing limits are pure Python constants and are not intended to be
changed without a code review.
"""
from __future__ import annotations

import logging

from deile.config.settings import get_settings
from deile.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


# ── ClaudeDispatcher ──────────────────────────────────────────────────────
#: Maximum seconds a ``claude -p`` subprocess may run before it is killed.
def claude_timeout_seconds() -> int:
    """Live: re-reads settings at every call. Use this, NOT a frozen const.

    **NÃO ARMAZENE LOCALMENTE** — chamar esta função em loop infinito e guardar
    o resultado numa variável local reintroduziria o freeze que esta função
    existe para evitar. Chame-a a cada uso.
    """
    return get_settings().pipeline_claude_timeout

# ── PipelineMonitor ───────────────────────────────────────────────────────
#: Default polling cadence for :class:`PipelineMonitor`.
def pipeline_poll_interval_seconds() -> int:
    """Live: re-reads settings at every call. Use this, NOT a frozen const.

    **NÃO ARMAZENE LOCALMENTE** — chamar esta função em loop infinito e guardar
    o resultado numa variável local reintroduziria o freeze que esta função
    existe para evitar. Chame-a a cada uso.
    """
    return get_settings().pipeline_poll_interval
#: Seconds ``stop()`` waits for the loop task before cancelling it.
PIPELINE_STOP_TIMEOUT_SECONDS: int = 5

# ── Forge / pipeline repo ─────────────────────────────────────────────────
#: Error message raised when the pipeline runs without a configured repo.
#: Issue #612 (project-agnostic): the harness no longer silently falls back to
#: a hardcoded ``elimarcavalli/deile``. Operators MUST supply the target repo
#: (via ``DEILE_FORGE_REPO`` / ``forge.repo`` or the legacy ``pipeline.repo``);
#: the k8s reference deploy provides it through the ``deile-runtime-config``
#: ConfigMap, not through code.
_NO_REPO_MESSAGE: str = (
    "No forge repository is configured. The pipeline is project-agnostic and "
    "refuses to fall back to a hardcoded default. Set DEILE_FORGE_REPO "
    "(owner/repo on GitHub, group/.../project on GitLab) or the forge.repo / "
    "pipeline.repo key in settings.json before running the pipeline."
)


def resolve_forge_repo(*, require: bool = True, fallback: str = "") -> str:
    """Return the active project path for the pipeline repository.

    Accepts both shapes: GitHub ``owner/repo`` and GitLab
    ``group/(subgroup/)*project``. Reads ``forge.repo`` from
    :class:`Settings` first (new canonical), falling back to the legacy
    ``pipeline.repo`` for transitional compatibility. Single source of truth
    for both the pipeline tool and the slash command — they used to inline the
    same expression independently.

    Issue #612 (project-agnostic): there is **no** hardcoded default repo. When
    nothing is configured the behaviour depends on the caller:

    - ``require=True`` (default, the production pipeline path): raise
      :class:`ConfigurationError` so config-absence fails loud at startup
      instead of silently operating against the wrong project.
    - ``require=False`` with a ``fallback``: log a ``WARNING`` flagging that the
      caller-supplied fallback is in use, and return it. This is for graceful
      surfaces (panel/CLI display) that must degrade with a clear message
      rather than abort.
    - ``require=False`` without a ``fallback``: return ``""``.
    """
    settings = get_settings()
    repo = (getattr(settings, "forge_repo", "") or settings.pipeline_repo or "").strip()
    if repo:
        return repo
    if require:
        raise ConfigurationError(_NO_REPO_MESSAGE, config_key="forge.repo")
    if fallback:
        logger.warning(
            "No forge repository configured; using caller-supplied fallback %r. "
            "Set DEILE_FORGE_REPO or forge.repo/pipeline.repo to silence this.",
            fallback,
        )
        return fallback
    return ""


def resolve_pipeline_repo(*, require: bool = True, fallback: str = "") -> str:
    """Deprecated alias for :func:`resolve_forge_repo`.

    Kept here so callers (especially tests) do not have to migrate in
    lock-step with the rename. New code should use
    :func:`resolve_forge_repo`.
    """
    return resolve_forge_repo(require=require, fallback=fallback)

# ── Prompt / message truncation ───────────────────────────────────────────
#: Max chars of issue body EMBEDDED in a worker brief. Kept well under the 8000
#: dispatch-payload cap so the brief (template + body) never overflows — the
#: worker reads the FULL live issue via ``gh issue view`` anyway, so the embedded
#: copy is just initial context. (A refined feature_request body can be large;
#: 6000 + the refine brief template overflowed 8000 — issue #257.)
ISSUE_BODY_MAX_CHARS: int = 5000
#: Max chars of stderr / error detail shown in Discord notifications.
PIPELINE_MSG_TRUNCATE_CHARS: int = 1500
