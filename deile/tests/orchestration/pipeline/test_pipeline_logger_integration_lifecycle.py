"""Integração lifecycle: sequência fixa, dedup cross-evento, no-secrets.

Exercita o pipeline_logger com 6 chamadas prescritas, validando:
- ordem canônica das 5 linhas emitidas
- supressão do passo 4 por dedup cross-evento (TTL 30 s)
- ausência de campos proibidos no conjunto agregado
"""

from __future__ import annotations

import logging

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl


@pytest.fixture(autouse=True)
def _reset_dedup(monkeypatch):
    monkeypatch.setattr(pl, "_DEDUP", pl._DedupCache())


def _pipeline_lines(caplog):
    return [r.message for r in caplog.records if r.name == "deile.pipeline.events"]


def test_lifecycle_canonical_order_and_dedup(caplog):
    """AC#1–#4: sequência fixa, ordem canônica, dedup cross-evento, no-secrets."""
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        # Passo 1 — emite refinement.critique
        pl.log_refinement_critique(issue=42, round=1, persona="Crítica", verdict="VAGO")
        # Passo 2 — emite label.change (primeira ocorrência)
        pl.log_label_change(
            target_kind="issue",
            target=42,
            removed=["refinar"],
            added=["~workflow:em_arquitetura"],
        )
        # Passo 3 — emite refinement.refine
        pl.log_refinement_refine(
            issue=42, round=1, persona="Refinador", body_chars=800, verdict="OK"
        )
        # Passo 4 — repetição exata do passo 2; suprimida por dedup (TTL 30 s)
        pl.log_label_change(
            target_kind="issue",
            target=42,
            removed=["refinar"],
            added=["~workflow:em_arquitetura"],
        )
        # Passo 5 — emite routing.mention
        pl.log_routing_mention(
            target_kind="issue", target=42, action="inject_workflow_nova"
        )
        # Passo 6 — emite routing.pr_unified
        pl.log_routing_pr_unified(target=99, role="author", mode="pr_unified")

    lines = _pipeline_lines(caplog)

    # AC#3: exatamente 5 linhas (passo 4 suprimido por dedup)
    assert len(lines) == 5, f"Esperado 5 linhas, obtido {len(lines)}: {lines}"

    # AC#2: ordem canônica e campos obrigatórios por posição
    assert lines[0].startswith("refinement.critique  "), f"lines[0]: {lines[0]}"
    assert "issue=42" in lines[0], f"lines[0]: {lines[0]}"
    assert "verdict=VAGO" in lines[0], f"lines[0]: {lines[0]}"

    assert lines[1].startswith("label.change  "), f"lines[1]: {lines[1]}"
    assert "target=42" in lines[1], f"lines[1]: {lines[1]}"
    assert "added=[~workflow:em_arquitetura]" in lines[1], f"lines[1]: {lines[1]}"

    assert lines[2].startswith("refinement.refine  "), f"lines[2]: {lines[2]}"
    assert "issue=42" in lines[2], f"lines[2]: {lines[2]}"
    assert "verdict=OK" in lines[2], f"lines[2]: {lines[2]}"

    assert lines[3].startswith("routing.mention  "), f"lines[3]: {lines[3]}"
    assert "target=42" in lines[3], f"lines[3]: {lines[3]}"
    assert "action=inject_workflow_nova" in lines[3], f"lines[3]: {lines[3]}"

    assert lines[4].startswith("routing.pr_unified  "), f"lines[4]: {lines[4]}"
    assert "target=99" in lines[4], f"lines[4]: {lines[4]}"
    assert "role=author" in lines[4], f"lines[4]: {lines[4]}"

    # AC#4: no-secrets — padrões proibidos (ref: test_pipeline_logger_no_secrets.py:44–48)
    for line in lines:
        assert "body=" not in line, f"'body=' found in: {line}"
        assert "Authorization" not in line, f"Authorization found in: {line}"
        assert "credential" not in line.lower(), f"credential found in: {line}"
