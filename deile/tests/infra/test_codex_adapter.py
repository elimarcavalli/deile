"""Testes do adapter Codex (Fase 5 — frota multi-CLI, Tier 2).

Cobre os pontos especializados do contrato :class:`CliAdapter` para o ``codex``:

  1. ``build_argv`` — ``codex exec`` (nunca ``codex`` puro), autonomia
     (``--dangerously-bypass-approvals-and-sandbox``), ``--json``,
     ``--skip-git-repo-check``, modelo via ``-m``, reasoning via
     ``-c model_reasoning_effort=`` (vocabulário validado), brief lido do arquivo
     como prompt posicional, resume ignorado (fresh-only).
  2. ``env_overlay`` — HOME + CODEX_HOME graváveis, SEM ``auth_env_keys``.
  3. ``parse_output`` — JSONL de ``--json`` (mensagem do agente / erro / eventos
     sem texto / vazio), tolerante a linhas malformadas e ao aninhamento em
     ``msg``.
  4. ``list_models`` — catálogo estático curado (Codex não tem list-models).

Além dos metadados (kind/porta/auth/resume/reasoning/oauth/egress) que dirigem
registro, painel, NetworkPolicy e manifests.

O pacote ``cli_adapters`` vive em ``infra/k8s/`` (fora do pacote ``deile``); o
path é inserido manualmente — mesma convenção dos demais testes de infra.
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
from cli_adapters import codex as cx_mod  # noqa: E402


@pytest.fixture
def adapter():
    return get_adapter("codex")


@pytest.fixture
def brief(tmp_path):
    p = tmp_path / ".brief.md"
    p.write_text("IMPLEMENTE A FEATURE X", encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------- #
# Metadados
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_metadata_matches_plan(adapter):
    assert adapter.kind == "codex"
    assert adapter.default_port == 8772          # §1.13
    assert adapter.auth_mode == "env"            # §2.2 — OPENAI_API_KEY default
    assert adapter.supports_resume is False      # fresh-only na frota
    assert adapter.supports_reasoning is True    # §1.10 — model_reasoning_effort
    assert adapter.git_strategy == "brief_driven"
    assert "OPENAI_API_KEY" in adapter.auth_env_keys
    assert "api.openai.com" in adapter.egress_hosts
    # OAuth opt-in (DEILE_CODEX_AUTH=oauth) modelado em metadado.
    assert adapter.oauth is not None
    assert adapter.oauth.login_cmd == ["codex", "login", "--device-auth"]
    assert adapter.oauth.cred_path.endswith("auth.json")


@pytest.mark.unit
def test_satisfies_protocol(adapter):
    assert isinstance(adapter, base.CliAdapter)


# --------------------------------------------------------------------------- #
# build_argv
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_build_argv_uses_exec_subcommand(adapter, brief):
    # SEMPRE `codex exec` — nunca `codex` puro (panica sem TTY).
    argv = adapter.build_argv(
        brief_path=brief, model=None, reasoning=None, workdir="/w", resume=None,
    )
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert argv[argv.index("--cd") + 1] == "/w"
    assert "--dangerously-bypass-approvals-and-sandbox" in argv  # §1.4
    assert "--json" in argv                                       # §1.6
    assert "--skip-git-repo-check" in argv
    # Brief vira o prompt posicional (último token, conteúdo lido do arquivo).
    assert argv[-1] == "IMPLEMENTE A FEATURE X"


@pytest.mark.unit
def test_build_argv_model_and_reasoning(adapter, brief):
    argv = adapter.build_argv(
        brief_path=brief, model="gpt-5.5-codex", reasoning="high",
        workdir="/w", resume=None,
    )
    assert argv[argv.index("-m") + 1] == "gpt-5.5-codex"
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=high"


@pytest.mark.unit
def test_build_argv_invalid_reasoning_is_ignored(adapter, brief):
    # Vocabulário fora do conjunto oficial → fail-open (não emite -c).
    argv = adapter.build_argv(
        brief_path=brief, model=None, reasoning="ultracode",
        workdir="/w", resume=None,
    )
    assert "-c" not in argv


@pytest.mark.unit
def test_build_argv_no_model_omits_flag(adapter, brief):
    argv = adapter.build_argv(
        brief_path=brief, model=None, reasoning=None, workdir="/w", resume=None,
    )
    assert "-m" not in argv


@pytest.mark.unit
def test_build_argv_ignores_resume(adapter, brief):
    resume = base.ResumeCtx(session_id="s1", prev_task_id="0123456789abcdef")
    argv = adapter.build_argv(
        brief_path=brief, model=None, reasoning=None, workdir="/w", resume=resume,
    )
    assert "s1" not in argv
    assert "--resume" not in argv and "-c" not in argv


@pytest.mark.unit
def test_build_argv_brief_read_failure_degrades(adapter):
    # Arquivo inexistente → prompt mínimo apontando ao caminho (não estoura).
    argv = adapter.build_argv(
        brief_path="/nao/existe/.brief.md", model=None, reasoning=None,
        workdir="/w", resume=None,
    )
    assert "/nao/existe/.brief.md" in argv[-1]


# --------------------------------------------------------------------------- #
# env_overlay
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_env_overlay_dirs(adapter):
    ov = adapter.env_overlay(home="/home/codex")
    assert ov["HOME"] == "/home/codex"
    assert ov["CODEX_HOME"].startswith("/home/codex")


@pytest.mark.unit
def test_env_overlay_excludes_auth_keys(adapter):
    ov = adapter.env_overlay(home="/home/codex")
    assert "OPENAI_API_KEY" not in ov


# --------------------------------------------------------------------------- #
# parse_output
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_parse_output_agent_message(adapter):
    stdout = "\n".join([
        json.dumps({"type": "task_started"}),
        json.dumps({"type": "agent_message", "message": "veredito final"}),
        json.dumps({"type": "task_complete"}),
    ])
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.result_text == "veredito final"
    assert wr.error_code is None


@pytest.mark.unit
def test_parse_output_nested_msg_type(adapter):
    # Tolera o tipo aninhado em `msg`.
    stdout = json.dumps({"msg": {"type": "agent_message", "message": "ok aninhado"}})
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.result_text == "ok aninhado"


@pytest.mark.unit
def test_parse_output_error_event_fails(adapter):
    stdout = json.dumps({"type": "error", "message": "rate limit excedido"})
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is False
    assert "rate limit" in wr.result_text
    assert wr.error_code == "CLI_REPORTED_ERROR"


@pytest.mark.unit
def test_parse_output_tolerates_malformed(adapter):
    stdout = "\n".join([
        "isto não é json",
        "{ quebrado",
        json.dumps({"type": "agent_message", "message": "apesar do ruído"}),
    ])
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.result_text == "apesar do ruído"


@pytest.mark.unit
def test_parse_output_events_without_text(adapter):
    stdout = "\n".join([
        json.dumps({"type": "task_started"}),
        json.dumps({"type": "exec_command_begin", "command": "ls"}),
    ])
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.error_code is None


@pytest.mark.unit
def test_parse_output_no_output_fails(adapter):
    wr = adapter.parse_output(stdout="", stderr="panic: no tty", rc=1)
    assert wr.ok is False
    assert wr.error_code == "NO_OUTPUT"
    assert "panic" in wr.result_text


# --------------------------------------------------------------------------- #
# list_models
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_models_static_catalog(adapter):
    models = adapter.list_models()
    assert models, "catálogo estático não pode ser vazio"
    ids = [m.id for m in models]
    assert "gpt-5.3-codex" in ids
    assert "gpt-5.1-codex-mini" in ids
    # Codex assume OpenAI direto (sem prefixo de provider no id nativo).
    assert all(m.provider == "openai" for m in models)
    assert all("/" not in m.id for m in models)


@pytest.mark.unit
def test_list_models_auth_by_model(adapter):
    """Cada modelo declara o modo de auth exigido — codex dual-mode (Frente 4).

    Modelos ``gpt-5*-codex`` premium exigem ChatGPT (OAuth); ``-mini``/
    ``codex-mini-latest`` aceitam API key. Toda entrada do catálogo deve
    declarar ``auth`` (não pode ficar ``None``).
    """
    by_id = {m.id: m for m in adapter.list_models()}
    assert by_id["gpt-5.3-codex"].auth == "chatgpt"
    assert by_id["gpt-5-codex"].auth == "chatgpt"
    assert by_id["gpt-5.1-codex-mini"].auth == "apikey"
    assert by_id["codex-mini-latest"].auth == "apikey"
    assert all(m.auth in ("chatgpt", "apikey") for m in adapter.list_models())


@pytest.mark.unit
def test_list_models_carry_prices(adapter):
    """Preço input/output presente em todos os modelos do catálogo codex."""
    for m in adapter.list_models():
        assert m.price_in is not None and m.price_in > 0
        assert m.price_out is not None and m.price_out > 0


@pytest.mark.unit
def test_list_models_returns_copy(adapter):
    a = adapter.list_models()
    a.append(base.ModelInfo(id="x"))
    b = adapter.list_models()
    assert len(b) == len(cx_mod._MODELS)  # mutação no retorno não vaza


@pytest.mark.unit
def test_parse_output_item_completed_agent_message(adapter):
    """Regressão #25 (homolog follow_ups): codex >=0.13x emite
    ``{type:item.completed, item:{type:agent_message, text:"..."}}``; o veredito
    está em item.text. O parser deve extraí-lo (não cair no fallback)."""
    out = "\n".join([
        '{"type":"thread.started"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"HOMOLOG CODEX OK."}}',
        '{"type":"turn.completed","usage":{"output_tokens":40}}',
    ])
    res = adapter.parse_output(stdout=out, stderr="", rc=0)
    assert res.ok is True
    assert "HOMOLOG CODEX OK." in res.result_text
