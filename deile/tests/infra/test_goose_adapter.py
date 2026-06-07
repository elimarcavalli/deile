"""Testes do adapter Goose (Fase 5 — frota multi-CLI, Tier 1).

Cobre os pontos especializados do contrato :class:`CliAdapter` para o ``goose``:

  1. ``build_argv`` — ``goose run --no-session --quiet --output-format json
     --max-turns N -t <brief>`` (conteúdo lido do arquivo); mapeamento de
     ``provider/model`` → ``--provider``/``--model``; resume/reasoning ignorados.
  2. ``env_overlay`` — HOME/XDG graváveis + ``GOOSE_MODE=auto`` +
     ``GOOSE_DISABLE_KEYRING=1`` (obrigatório), SEM ``auth_env_keys`` nem
     GOOSE_PROVIDER/GOOSE_MODEL.
  3. ``parse_output`` — objeto JSON único e JSONL (resposta/erro/vazio),
     tolerante a saída malformada.
  4. ``list_models`` — catálogo estático curado (Goose não tem list-models).

Além dos metadados (kind/porta/auth/resume/egress).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cli_adapters import base, get_adapter  # noqa: E402
from cli_adapters import goose as go_mod  # noqa: E402


@pytest.fixture
def adapter():
    return get_adapter("goose")


@pytest.fixture
def brief(tmp_path):
    p = tmp_path / ".brief.md"
    p.write_text("REFATORE O MÓDULO Z", encoding="utf-8")
    return str(p)


@pytest.mark.unit
def test_metadata_matches_plan(adapter):
    assert adapter.kind == "goose"
    assert adapter.default_port == 8775          # §1.13
    assert adapter.auth_mode == "env"
    assert adapter.supports_resume is False      # --no-session
    assert adapter.supports_reasoning is False
    assert adapter.git_strategy == "brief_driven"
    assert adapter.oauth is None
    assert "OPENROUTER_API_KEY" in adapter.auth_env_keys
    assert "openrouter.ai" in adapter.egress_hosts


@pytest.mark.unit
def test_satisfies_protocol(adapter):
    assert isinstance(adapter, base.CliAdapter)


@pytest.mark.unit
def test_build_argv_form(adapter, brief):
    argv = adapter.build_argv(
        brief_path=brief, model=None, reasoning=None, workdir="/w", resume=None,
    )
    assert argv[:2] == ["goose", "run"]
    assert "--no-session" in argv
    assert "--quiet" in argv
    assert argv[argv.index("--output-format") + 1] == "json"  # §1.6
    assert "--max-turns" in argv                              # teto de custo
    assert argv[argv.index("-t") + 1] == "REFATORE O MÓDULO Z"  # conteúdo


@pytest.mark.unit
def test_build_argv_maps_provider_model(adapter, brief):
    argv = adapter.build_argv(
        brief_path=brief, model="openrouter/anthropic/claude-sonnet-4",
        reasoning=None, workdir="/w", resume=None,
    )
    assert argv[argv.index("--provider") + 1] == "openrouter"
    assert argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-4"


@pytest.mark.unit
def test_build_argv_model_without_slash(adapter, brief):
    argv = adapter.build_argv(
        brief_path=brief, model="gpt-4o", reasoning=None, workdir="/w", resume=None,
    )
    assert "--provider" not in argv          # provider fica a cargo do env
    assert argv[argv.index("--model") + 1] == "gpt-4o"


@pytest.mark.unit
def test_build_argv_no_model_omits_flags(adapter, brief):
    argv = adapter.build_argv(
        brief_path=brief, model=None, reasoning=None, workdir="/w", resume=None,
    )
    assert "--model" not in argv and "--provider" not in argv


@pytest.mark.unit
def test_build_argv_ignores_resume(adapter, brief):
    resume = base.ResumeCtx(session_id="sQ", prev_task_id="0123456789abcdef")
    argv = adapter.build_argv(
        brief_path=brief, model=None, reasoning="high", workdir="/w", resume=resume,
    )
    assert "sQ" not in argv
    assert "--resume" not in argv


@pytest.mark.unit
def test_env_overlay_keyring_and_mode(adapter):
    ov = adapter.env_overlay(home="/home/goose")
    assert ov["HOME"] == "/home/goose"
    assert ov["XDG_CONFIG_HOME"].startswith("/home/goose")
    assert ov["GOOSE_MODE"] == "auto"               # §1.4 autonomia
    assert ov["GOOSE_DISABLE_KEYRING"] == "1"       # §2.5 OBRIGATÓRIO (DBus)
    # Provider/model + chave vêm do Deployment, não do overlay.
    assert "GOOSE_PROVIDER" not in ov
    assert "GOOSE_MODEL" not in ov
    assert "OPENROUTER_API_KEY" not in ov


@pytest.mark.unit
def test_parse_output_single_object(adapter):
    wr = adapter.parse_output(
        stdout=json.dumps({"result": "tarefa concluída"}), stderr="", rc=0,
    )
    assert wr.ok is True
    assert wr.result_text == "tarefa concluída"


@pytest.mark.unit
def test_parse_output_object_error(adapter):
    wr = adapter.parse_output(
        stdout=json.dumps({"error": "provider indisponível"}), stderr="", rc=0,
    )
    assert wr.ok is False
    assert "provider indisponível" in wr.result_text
    assert wr.error_code == "CLI_REPORTED_ERROR"


@pytest.mark.unit
def test_parse_output_jsonl_fallback(adapter):
    stdout = "\n".join([
        json.dumps({"type": "tool", "name": "shell"}),
        json.dumps({"type": "message", "content": "veredito jsonl"}),
    ])
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.result_text == "veredito jsonl"


@pytest.mark.unit
def test_parse_output_tolerates_malformed(adapter):
    stdout = "\n".join(["ruído", "{ broken", json.dumps({"text": "ok"})])
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.result_text == "ok"


@pytest.mark.unit
def test_parse_output_no_output_fails(adapter):
    wr = adapter.parse_output(stdout="", stderr="crash", rc=1)
    assert wr.ok is False
    assert wr.error_code == "NO_OUTPUT"
    assert "crash" in wr.result_text


@pytest.mark.unit
def test_list_models_static_catalog(adapter):
    models = adapter.list_models()
    assert models
    assert any("deepseek" in m.id for m in models)
    assert any(m.provider == "openrouter" for m in models)


@pytest.mark.unit
def test_list_models_returns_copy(adapter):
    a = adapter.list_models()
    a.clear()
    assert len(adapter.list_models()) == len(go_mod._MODELS)


@pytest.mark.unit
def test_parse_output_messages_shape_extracts_verdict_at_end(adapter):
    """Regressão (homologação E2E refine): goose run --output-format json emite
    ``{"messages":[...],"metadata":{...}}``; o veredito conclui no FIM da última
    msg assistant (content[].text). O parser deve extraí-lo e NÃO truncar o fim."""
    import json as _json
    long_analysis = "Análise detalhada. " * 800  # >>2000 chars
    out = _json.dumps({
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "critique"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "pensando..."},
                    {"type": "text", "text": long_analysis + "\n\nVEREDITO: CLARO"},
                ],
            },
        ],
        "metadata": {"total_tokens": 81823, "status": "completed"},
    })
    res = adapter.parse_output(stdout=out, stderr="", rc=0)
    assert res.ok is True
    assert "VEREDITO: CLARO" in res.result_text  # fim preservado (não [:2000])
