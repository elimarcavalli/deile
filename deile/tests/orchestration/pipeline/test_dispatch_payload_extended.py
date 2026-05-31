"""Unit tests para os campos novos opcionais do DispatchPayload (#309 fase 2).

Backward compat é crítico — deile-worker existente não sabe dos campos novos,
mas DEVE aceitar payloads que os carregam (ignorando se não usar) e gerar
payloads válidos sem eles.

Notas de tradução vs. plano de execução:
- ``DispatchPayload`` é um Pydantic ``BaseModel`` (não um ``@dataclass``), então
  validação roda em ``@field_validator`` e exceções vêm como
  ``pydantic.ValidationError`` (que wrappea o ``ValueError`` do validator).
- O wire-format helper canônico é ``model_dump(exclude_none=True)`` — já existe
  no módulo e já omite ``None`` para preservar backward compat com worker
  antigo. Não introduzimos um ``to_dict()`` paralelo; os testes asseguram o
  contrato sobre ``model_dump(exclude_none=True)`` que é o método que o
  ``DeileWorkerClient.dispatch`` de fato usa pra serializar pra rede.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from deile.infrastructure.deile_worker_client import DispatchPayload
from deile.orchestration.pipeline.dispatch_resolver import PIPELINE_STAGES


def test_minimal_payload_still_works():
    """Campos antigos sozinhos (backward compat)."""
    p = DispatchPayload(brief="implement #1", channel_id="auto/issue-1")
    assert p.brief == "implement #1"
    assert p.channel_id == "auto/issue-1"
    assert p.stage is None
    assert p.action_kind is None
    assert p.issue_number is None
    assert p.branch is None


def test_full_payload_with_new_fields():
    p = DispatchPayload(
        brief="implement #309",
        channel_id="auto/issue-309",
        preferred_model="anthropic:claude-opus-4-8",
        stage="implement",
        action_kind="implement",
        issue_number=309,
        branch="auto/issue-309",
    )
    assert p.stage == "implement"
    assert p.action_kind == "implement"
    assert p.issue_number == 309
    assert p.branch == "auto/issue-309"


def test_payload_model_dump_omits_none_fields():
    """model_dump(exclude_none=True) omite campos opcionais ausentes — é o
    helper que o DeileWorkerClient.dispatch usa pra serializar pra rede, e é
    o que garante que deile-worker antigo não receba chaves desconhecidas.
    """
    p = DispatchPayload(brief="x", channel_id="c")
    d = p.model_dump(exclude_none=True)
    # Campos novos opcionais não devem aparecer quando ausentes.
    assert "stage" not in d
    assert "action_kind" not in d
    assert "issue_number" not in d
    assert "branch" not in d
    assert "preferred_model" not in d
    # Campos obrigatórios permanecem.
    assert d["brief"] == "x"
    assert d["channel_id"] == "c"


def test_payload_model_dump_with_full_fields():
    p = DispatchPayload(
        brief="x",
        channel_id="c",
        stage="pr_review",
        issue_number=42,
        branch="auto/issue-42",
        action_kind="review",
        preferred_model="anthropic:claude-sonnet-4-6",
    )
    d = p.model_dump(exclude_none=True)
    assert d["stage"] == "pr_review"
    assert d["issue_number"] == 42
    assert d["branch"] == "auto/issue-42"
    assert d["action_kind"] == "review"
    assert d["preferred_model"] == "anthropic:claude-sonnet-4-6"
    # Roundtrip JSON — a serialização pra HTTP é json.dumps(d).
    parsed = json.loads(json.dumps(d))
    assert parsed["stage"] == "pr_review"
    assert parsed["issue_number"] == 42


def test_invalid_stage_raises():
    """Stage value validation — se setado, deve estar em PIPELINE_STAGES.

    Pydantic wrappea o ``ValueError`` do validator em ``ValidationError`` antes
    de propagar; o teste matcha a mensagem que o validator emite.
    """
    with pytest.raises(ValidationError, match="invalid stage|unknown stage"):
        DispatchPayload(brief="x", channel_id="c", stage="garbage_stage")


@pytest.mark.parametrize("stage", PIPELINE_STAGES)
def test_valid_stages_accepted(stage):
    """Cada stage de PIPELINE_STAGES é aceito."""
    p = DispatchPayload(brief="x", channel_id="c", stage=stage)
    assert p.stage == stage


def test_build_dispatch_payload_forwards_new_fields():
    """build_dispatch_payload helper deve aceitar e forwardar os 4 campos novos.

    A helper retorna ``Dict[str, Any]`` (wire-format direto), então os campos
    são consultados via chaves de dict, não atributos — alinhado com o caller
    que serializa o dict via ``json.dumps`` ao POST /v1/dispatch.
    """
    from deile.infrastructure.deile_worker_client import build_dispatch_payload

    p = build_dispatch_payload(
        brief="test brief",
        channel_id="auto/issue-309",
        preferred_model="anthropic:claude-opus-4-8",
        stage="implement",
        action_kind="implement",
        issue_number=309,
        branch="auto/issue-309",
    )
    assert p["stage"] == "implement"
    assert p["action_kind"] == "implement"
    assert p["issue_number"] == 309
    assert p["branch"] == "auto/issue-309"
    assert p["preferred_model"] == "anthropic:claude-opus-4-8"
    # Campos antigos preservados (regression guard).
    assert p["brief"] == "test brief"
    assert p["channel_id"] == "auto/issue-309"


def test_build_dispatch_payload_omits_unset_new_fields():
    """Sem os 4 fields novos, helper produz payload backward-compat.

    Mesmo critério do ``preferred_model``: chaves opcionais ausentes não
    aparecem no payload, garantindo que worker antigo não receba campos
    desconhecidos.
    """
    from deile.infrastructure.deile_worker_client import build_dispatch_payload

    p = build_dispatch_payload(
        brief="test brief",
        channel_id="auto/issue-309",
    )
    assert "stage" not in p
    assert "action_kind" not in p
    assert "issue_number" not in p
    assert "branch" not in p


def test_build_dispatch_payload_omits_when_explicit_none():
    """Passar ``None`` explicitamente equivale ao default — chave dropada."""
    from deile.infrastructure.deile_worker_client import build_dispatch_payload

    p = build_dispatch_payload(
        brief="x",
        channel_id="c",
        stage=None,
        action_kind=None,
        issue_number=None,
        branch=None,
    )
    assert "stage" not in p
    assert "action_kind" not in p
    assert "issue_number" not in p
    assert "branch" not in p
