"""Testes do adapter Qwen Code (Fase 5 — frota multi-CLI, Tier 2).

Cobre os pontos especializados do contrato :class:`CliAdapter` para o ``qwen``:

  1. ``build_argv`` — ``qwen -p <brief>`` (conteúdo lido do arquivo), autonomia
     (``--yolo``), ``--output-format json``; modelo NÃO no argv (viaja por
     ``OPENAI_MODEL`` no env); resume/reasoning ignorados.
  2. ``env_overlay`` — HOME gravável + ``QWEN_CODE_UNATTENDED_RETRY``, SEM a
     tríade OpenAI (``OPENAI_API_KEY``/``_BASE_URL``/``_MODEL``).
  3. ``parse_output`` — objeto JSON único e JSONL (resposta/erro/vazio),
     tolerante a saída malformada.
  4. ``list_models`` — catálogo estático curado (Qwen não tem list-models).

Além dos metadados (kind/porta/auth/resume/egress) que dirigem registro, painel,
NetworkPolicy e manifests.
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
from cli_adapters import qwen as qw_mod  # noqa: E402


@pytest.fixture
def adapter():
    return get_adapter("qwen")


@pytest.fixture
def brief(tmp_path):
    p = tmp_path / ".brief.md"
    p.write_text("CORRIJA O BUG Y", encoding="utf-8")
    return str(p)


@pytest.mark.unit
def test_metadata_matches_plan(adapter):
    assert adapter.kind == "qwen"
    assert adapter.default_port == 8773  # §1.13
    assert adapter.auth_mode == "env"  # §2.3 — OPENAI_API_KEY (tríade)
    assert adapter.supports_resume is True  # issue #445 — qwen --resume
    assert adapter.supports_reasoning is False
    assert adapter.git_strategy == "brief_driven"
    assert adapter.oauth is None
    assert "OPENAI_API_KEY" in adapter.auth_env_keys
    assert "dashscope.aliyuncs.com" in adapter.egress_hosts
    assert "openrouter.ai" in adapter.egress_hosts


@pytest.mark.unit
def test_satisfies_protocol(adapter):
    assert isinstance(adapter, base.CliAdapter)


@pytest.mark.unit
def test_build_argv_form(adapter, brief):
    argv = adapter.build_argv(
        brief_path=brief,
        model="ignored-in-argv",
        reasoning=None,
        workdir="/w",
        resume=None,
    )
    assert argv[0] == "qwen"
    assert argv[argv.index("-p") + 1] == "CORRIJA O BUG Y"  # conteúdo, não path
    assert "--yolo" in argv  # §1.4 autonomia
    # --auth-type openai: obrigatório em modo não-interativo (regressão homolog
    # pr_review — sem ele o qwen-code aborta "No auth type is selected").
    assert argv[argv.index("--auth-type") + 1] == "openai"
    assert argv[argv.index("--output-format") + 1] == "json"  # §1.6
    # Modelo via -m (o qwen-code TEM a flag; o OPENAI_MODEL-via-env nunca foi
    # wirado e o qwen caía no default qwen3.5-plus rejeitado pelo OpenRouter).
    assert argv[argv.index("-m") + 1] == "ignored-in-argv"


@pytest.mark.unit
def test_build_argv_fresh_has_no_resume(adapter, brief):
    argv = adapter.build_argv(
        brief_path=brief,
        model=None,
        reasoning=None,
        workdir="/w",
        resume=None,
    )
    assert "--resume" not in argv


@pytest.mark.unit
def test_build_argv_resume_passes_session(adapter, brief):
    # issue #445: resume → --resume <session_id>; reasoning segue ignorado.
    resume = base.ResumeCtx(session_id="s9", prev_task_id="0123456789abcdef")
    argv = adapter.build_argv(
        brief_path=brief,
        model=None,
        reasoning="high",
        workdir="/w",
        resume=resume,
    )
    assert argv[argv.index("--resume") + 1] == "s9"
    assert "--variant" not in argv  # reasoning não suportado → ignorado


@pytest.mark.unit
def test_extract_session_id_from_events(adapter):
    import json

    stdout = json.dumps(
        [
            {"type": "system", "subtype": "session_start", "session_id": "qwen-1"},
            {
                "type": "result",
                "session_id": "qwen-1",
                "result": "ok",
                "is_error": False,
            },
        ]
    )
    assert adapter.extract_session_id(stdout=stdout, stderr="", task_id="t") == "qwen-1"


@pytest.mark.unit
def test_parse_output_provider_429_classified(adapter):
    import json

    stdout = json.dumps(
        [
            {"type": "result", "is_error": True, "result": "429 rate limit exceeded"},
        ]
    )
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is False
    assert wr.error_code == "RATE_LIMIT"


@pytest.mark.unit
def test_build_argv_brief_read_failure_degrades(adapter):
    argv = adapter.build_argv(
        brief_path="/nao/existe/.brief.md",
        model=None,
        reasoning=None,
        workdir="/w",
        resume=None,
    )
    assert "/nao/existe/.brief.md" in argv[argv.index("-p") + 1]


@pytest.mark.unit
def test_env_overlay(adapter):
    ov = adapter.env_overlay(home="/home/qwen")
    assert ov["HOME"] == "/home/qwen"
    assert ov["QWEN_CODE_UNATTENDED_RETRY"] == "1"
    # Suprime o aviso de yolo headless que senão polui o stdout e derruba o
    # dispatch com NO_OUTPUT (regressão da homologação E2E do stage pr_review).
    assert ov["QWEN_CODE_SUPPRESS_YOLO_WARNING"] == "1"
    # A tríade de provider vem do Secret/ConfigMap, não do overlay.
    assert "OPENAI_API_KEY" not in ov
    assert "OPENAI_BASE_URL" not in ov
    assert "OPENAI_MODEL" not in ov


@pytest.mark.unit
def test_parse_output_single_json_object(adapter):
    wr = adapter.parse_output(
        stdout=json.dumps({"response": "feito com sucesso"}),
        stderr="",
        rc=0,
    )
    assert wr.ok is True
    assert wr.result_text == "feito com sucesso"


@pytest.mark.unit
def test_parse_output_object_error(adapter):
    wr = adapter.parse_output(
        stdout=json.dumps({"error": "invalid api key"}),
        stderr="",
        rc=0,
    )
    assert wr.ok is False
    assert "invalid api key" in wr.result_text
    assert wr.error_code == "CLI_REPORTED_ERROR"


@pytest.mark.unit
def test_parse_output_jsonl_fallback(adapter):
    stdout = "\n".join(
        [
            json.dumps({"type": "tool_call", "name": "edit"}),
            json.dumps({"type": "text", "text": "veredito jsonl"}),
        ]
    )
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.result_text == "veredito jsonl"


@pytest.mark.unit
def test_parse_output_tolerates_malformed(adapter):
    stdout = "\n".join(["lixo", "{ truncado", json.dumps({"result": "ok"})])
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.result_text == "ok"


@pytest.mark.unit
def test_parse_output_no_output_fails(adapter):
    wr = adapter.parse_output(stdout="", stderr="boom", rc=1)
    assert wr.ok is False
    assert wr.error_code == "NO_OUTPUT"
    assert "boom" in wr.result_text


@pytest.mark.unit
def test_list_models_static_catalog(adapter):
    models = adapter.list_models()
    assert models
    ids = [m.id for m in models]
    assert "qwen3-coder-plus" in ids
    assert any(m.provider == "openrouter" for m in models)  # rota OpenRouter
    assert any(m.provider == "dashscope" for m in models)  # rota Dashscope


@pytest.mark.unit
def test_list_models_returns_copy(adapter):
    a = adapter.list_models()
    a.clear()
    assert len(adapter.list_models()) == len(qw_mod._MODELS)
