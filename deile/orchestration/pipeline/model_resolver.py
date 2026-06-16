"""Per-stage model resolver for the autonomous pipeline (issue #305).

Each pipeline stage (`classify`, `refine`, `implement`, `pr_review`,
`follow_ups`) can be pinned to a specific LLM model via
``pipeline.models.<stage>`` in ``~/.deile/settings.json``. When no override is
set, the stage falls back to the global preferred model
(``settings.preferred_model`` / ``DEILE_PREFERRED_MODEL``), preserving the
legacy single-model-per-deployment behavior.

The resolver lives in ``pipeline/`` (not ``config/``) because the knowledge
of *which stages exist* is a pipeline concern; settings only stores 5 dumb
optional strings and this module gives them semantics. Callers (the
``WorkerImplementer`` methods in :mod:`implementer`) pass the result through
to ``DispatchPayload.preferred_model``; the worker injects it into
``session.context_data["preferred_model"]`` and the agent's
``_choose_provider_for_turn`` picks it up as a soft override (see the
``preferred_model`` row in the ``soft_candidates`` list in
``deile/core/agent.py``).

Design choices:

- **Returns ``None`` on no-override** — the caller decides whether to send the
  global default explicitly or let the worker's own default kick in. Sending
  ``None`` keeps the wire payload minimal (Pydantic ``exclude_none=True``).
- **Strict stage name validation** — an unknown stage is a programming bug
  (typo in implementer.py), not an operator config error. Raising
  ``ValueError`` surfaces it immediately in tests.
- **No caching** — settings hot-reload via ``watchdog`` (pilar 09); the
  resolver MUST see fresh values on every call. ``get_settings()`` returns the
  live singleton, so the cost is one attribute lookup.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from deile.config.settings import get_settings

#: Canonical list of pipeline stages that accept per-stage model overrides.
#: Order matches the operational lifecycle: classify → refine → implement →
#: pr_review → follow_ups. The names are also used as the JSON key suffix
#: (``pipeline.models.<stage>``) and the settings attribute suffix
#: (``pipeline_model_<stage>``); keep all three in sync.
PIPELINE_STAGES: Tuple[str, ...] = (
    "classify",
    "refine",
    "implement",
    "pr_review",
    "follow_ups",
)


def resolve_stage_model(stage: str) -> Optional[str]:
    """Return the override model for *stage*, or ``None`` to fall back.

    ``None`` is a load-bearing return — it tells the caller "no override; let
    the worker resolve the model from its own ``DEILE_PREFERRED_MODEL`` /
    ``settings.preferred_model``". This keeps the dispatch payload minimal
    when no override exists, and avoids accidentally pinning the worker to a
    snapshot of the pipeline's view of ``preferred_model`` (which can drift
    if the worker pod is restarted with a different env).

    Raises:
        ValueError: if *stage* is not in :data:`PIPELINE_STAGES`. This is a
            programming bug, not user input — the implementer methods pass
            literal strings, so a wrong name surfaces immediately in tests.
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown pipeline stage: {stage!r} " f"(expected one of {PIPELINE_STAGES})"
        )
    settings = get_settings()
    raw = getattr(settings, f"pipeline_model_{stage}", None)
    # Treat empty-string as unset — defensive against a partial write that
    # left "" in the JSON. The strict converter (_to_optional_model_slug in
    # settings.py) already collapses "" to None, but the loose loader path
    # (_apply_nested_dict + _set_typed) could still store "".
    if isinstance(raw, str) and not raw.strip():
        return None
    return raw or None


def resolve_stage_cli_model(stage: str) -> Optional[str]:
    """Return the per-stage **CLI** model override for *stage*, or ``None``.

    Espelha :func:`resolve_stage_model`, mas para os **workers da frota CLI**
    (``opencode-worker``, ``codex-worker``, ...): o valor é o **model-id NATIVO
    do CLI** — uma *string livre* (ex.: ``openrouter/deepseek/deepseek-chat``,
    ``qwen3-coder-plus``, ``gpt-5.5-codex``), NÃO o formato ``provider:model``
    do deile-worker.

    **Por que lê env direto (e não ``settings.pipeline_model_<stage>``):** a
    camada de settings valida ``DEILE_PIPELINE_MODEL_<STAGE>`` /
    ``pipeline.models.<stage>`` pelo regex ``provider:model``
    (``_to_optional_model_slug``) e DESCARTA qualquer string que não case — o que
    aniquilaria um id de CLI livre como ``openrouter/deepseek/deepseek-chat``.
    Portanto o id nativo do CLI viaja **sempre pela env var**
    ``DEILE_PIPELINE_MODEL_<STAGE>`` (per-stage) → fallback ``DEILE_PREFERRED_MODEL``
    (global), lidas como string livre — exatamente o que o plano §1.3 especifica.
    Isto espelha o caminho de cluster: o painel grava
    ``DEILE_PIPELINE_MODEL_<STAGE>`` no Deployment, e o worker CLI consome o id
    cru. O eixo de configuração ("modelo") continua único — o que muda é apenas
    como o valor é *interpretado* no destino (provider:model × id nativo). O
    ``implementer`` escolhe qual resolver chamar pelo dispatcher do stage
    (deile/claude-worker → :func:`resolve_stage_model`; ``*-worker`` CLI → este).

    ``None`` é load-bearing — significa "sem override; deixe o CLI worker usar
    o modelo default do adapter / da imagem".

    Raises:
        ValueError: if *stage* is not in :data:`PIPELINE_STAGES` (programming bug).
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown pipeline stage: {stage!r} " f"(expected one of {PIPELINE_STAGES})"
        )
    # Per-stage env var (string livre — id nativo do CLI).
    raw = os.environ.get(f"DEILE_PIPELINE_MODEL_{stage.upper()}")
    if raw and raw.strip():
        return raw.strip()
    # Global fallback (string livre também — não passa pelo slug regex).
    global_raw = os.environ.get("DEILE_PREFERRED_MODEL")
    if global_raw and global_raw.strip():
        return global_raw.strip()
    return None
