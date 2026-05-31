"""Resolver de *reasoning effort* por etapa do pipeline (espelha o model_resolver).

Cada etapa (``classify``/``refine``/``implement``/``pr_review``/``follow_ups``)
pode fixar um esforço de raciocínio via ``pipeline.reasoning.<stage>`` no
``~/.deile/settings.json`` ou ``DEILE_PIPELINE_REASONING_<STAGE>`` no cluster.
Sem override por etapa, cai no esforço global (``reasoning_effort`` /
``DEILE_REASONING_EFFORT``); sem isso, ``None`` (o worker/provider usa o
default dele).

Difere de :func:`deile.orchestration.pipeline.model_resolver.resolve_stage_model`
num ponto: o esforço **global** é dobrado aqui (igual a
``resolve_stage_timeout_s``), porque ``reasoning_effort`` é um conceito de
primeira classe — a linha "Global default" do painel deve propagar para as
etapas sem override próprio. O valor resolvido é repassado em
``DispatchPayload.preferred_reasoning``; o deile-worker injeta em
``session.context_data["reasoning_effort"]`` (provider traduz) e o
claude-worker o passa a ``claude --effort``.

O conjunto de etapas é reusado de :data:`model_resolver.PIPELINE_STAGES` — não
duplicar. A validação do *valor* é deliberadamente frouxa aqui (qualquer string
não-vazia), pois o conjunto válido depende do worker/provider da etapa; o
consumidor (mapeamento em :mod:`deile.core.models.reasoning` ou
``claude --effort``) lida com nível desconhecido com fail-open.
"""

from __future__ import annotations

from typing import Optional

from deile.config.settings import get_settings
from deile.orchestration.pipeline.model_resolver import PIPELINE_STAGES


def resolve_stage_reasoning(stage: str) -> Optional[str]:
    """Retorna o esforço de raciocínio efetivo para *stage*, ou ``None``.

    Cadeia (alta → baixa precedência):

    1. ``settings.pipeline_reasoning_<stage>`` (override por etapa).
    2. ``settings.reasoning_effort`` (global).
    3. ``None`` (sem override; default do worker/provider).

    Raises:
        ValueError: se *stage* não está em :data:`PIPELINE_STAGES` (bug de
            programação — o implementer passa strings de uma whitelist).
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown pipeline stage: {stage!r} "
            f"(expected one of {PIPELINE_STAGES})"
        )
    settings = get_settings()
    raw = getattr(settings, f"pipeline_reasoning_{stage}", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    glob = getattr(settings, "reasoning_effort", None)
    if isinstance(glob, str) and glob.strip():
        return glob.strip()
    return None
