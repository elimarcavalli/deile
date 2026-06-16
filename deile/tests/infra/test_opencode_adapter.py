"""Testes do adapter OpenCode (Fase 4 — worker piloto da frota multi-CLI).

Cobre os quatro pontos especializados do contrato :class:`CliAdapter` para o
``opencode``:

  1. ``build_argv`` — flags exatas (autonomia, modelo, brief, format json) e o
     fato de ``resume`` ser ignorado (fresh-only).
  2. ``env_overlay`` — HOME/XDG graváveis + config de autonomia inline
     (``OPENCODE_CONFIG_CONTENT`` com ``permission: {"*": "allow"}``), SEM
     ``auth_env_keys``.
  3. ``parse_output`` — NDJSON de ``--format json`` (texto/erro/eventos sem
     veredito/saída vazia), tolerante a linhas malformadas.
  4. ``list_models`` — dinâmico via ``opencode models`` quando disponível;
     fallback no catálogo curado quando o binário/comando falha.

Além dos metadados (kind/porta/auth/resume/egress/...) que dirigem registro,
painel, NetworkPolicy e manifests.

O pacote ``cli_adapters`` vive em ``infra/k8s/`` (fora do pacote ``deile``); o
path é inserido manualmente — mesma convenção dos demais testes de infra.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Insere infra/k8s no sys.path para importar cli_adapters.
_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cli_adapters import base, get_adapter  # noqa: E402
from cli_adapters import opencode as oc_mod  # noqa: E402


@pytest.fixture
def adapter():
    return get_adapter("opencode")


# --------------------------------------------------------------------------- #
# Metadados (single source of truth p/ registro/painel/netpol/manifests)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_metadata_matches_plan(adapter):
    assert adapter.kind == "opencode"
    assert adapter.default_port == 8771  # §1.13
    assert adapter.auth_mode == "env"  # §1.11 — API key, não expira
    assert adapter.supports_resume is True  # issue #445 — resume via --session
    assert adapter.supports_reasoning is False
    assert adapter.git_strategy == "brief_driven"  # §1.5
    assert adapter.oauth is None
    assert "OPENROUTER_API_KEY" in adapter.auth_env_keys
    assert "openrouter.ai" in adapter.egress_hosts
    assert "models.dev" in adapter.egress_hosts


@pytest.mark.unit
def test_satisfies_protocol(adapter):
    assert isinstance(adapter, base.CliAdapter)


# --------------------------------------------------------------------------- #
# build_argv
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_build_argv_full(adapter):
    argv = adapter.build_argv(
        brief_path="/work/abc/.brief.md",
        model="openrouter/deepseek/deepseek-chat",
        reasoning=None,
        workdir="/work/abc",
        resume=None,
    )
    # Forma e ordem das flags-chave.
    assert argv[:4] == ["opencode", "run", "--dir", "/work/abc"]
    assert (
        "-m" in argv
        and argv[argv.index("-m") + 1] == "openrouter/deepseek/deepseek-chat"
    )
    assert "--dangerously-skip-permissions" in argv  # §1.4 autonomia
    assert argv[argv.index("--format") + 1] == "json"  # §1.6 saída estruturada
    # ``-f``/``--file`` é [array] no opencode (>=1.16) e o yargs é GULOSO: o
    # brief DEVE ser o ÚLTIMO token e a instrução posicional vem ANTES de ``-f``,
    # senão o array engole a instrução como arquivo (File not found) — regressão
    # da homologação E2E. Trava ambas as condições:
    assert argv[-2] == "-f" and argv[-1] == "/work/abc/.brief.md"  # brief é o último
    msg_idx = argv.index("--format") + 2  # token logo após "json"
    assert not argv[msg_idx].startswith("-")  # instrução posicional, não flag
    assert "brief" in argv[msg_idx].lower()
    assert msg_idx < argv.index("-f")  # mensagem ANTES do -f (anti-greedy)


@pytest.mark.unit
def test_build_argv_no_model_omits_flag(adapter):
    argv = adapter.build_argv(
        brief_path="/w/.brief.md",
        model=None,
        reasoning=None,
        workdir="/w",
        resume=None,
    )
    assert "-m" not in argv  # None → deixa o opencode decidir
    assert "--format" in argv and "-f" in argv


@pytest.mark.unit
def test_build_argv_fresh_has_no_session_flag(adapter):
    # Sem resume → argv fresh não ganha --session.
    argv = adapter.build_argv(
        brief_path="/w/.brief.md",
        model=None,
        reasoning=None,
        workdir="/w",
        resume=None,
    )
    assert "--session" not in argv


@pytest.mark.unit
def test_build_argv_resume_passes_session(adapter):
    # issue #445: resume → --session <session_id> para retomar a conversa nativa.
    resume = base.ResumeCtx(session_id="sess-123", prev_task_id="0123456789abcdef")
    argv = adapter.build_argv(
        brief_path="/w/.brief.md",
        model=None,
        reasoning=None,
        workdir="/w",
        resume=resume,
    )
    assert "--session" in argv
    assert argv[argv.index("--session") + 1] == "sess-123"


@pytest.mark.unit
def test_build_argv_reasoning_guard(adapter):
    # supports_reasoning=False na prática; mas se um reasoning vier, a guarda
    # defensiva o repassa via --variant (não quebra).
    argv = adapter.build_argv(
        brief_path="/w/.brief.md",
        model=None,
        reasoning="high",
        workdir="/w",
        resume=None,
    )
    assert argv[argv.index("--variant") + 1] == "high"


# --------------------------------------------------------------------------- #
# env_overlay
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_env_overlay_dirs_and_config(adapter):
    ov = adapter.env_overlay(home="/home/opencode")
    assert ov["HOME"] == "/home/opencode"
    assert ov["XDG_DATA_HOME"].startswith("/home/opencode")
    assert ov["XDG_CONFIG_HOME"].startswith("/home/opencode")
    assert ov["XDG_CACHE_HOME"].startswith("/home/opencode")
    # Config de autonomia inline (sem tocar disco — readOnlyRootFilesystem).
    cfg = json.loads(ov["OPENCODE_CONFIG_CONTENT"])
    assert cfg["permission"] == {"*": "allow"}  # libera toda tool sem prompt
    assert cfg["$schema"] == "https://opencode.ai/config.json"


@pytest.mark.unit
def test_env_overlay_excludes_auth_keys(adapter):
    # As auth_env_keys vêm do Secret no Deployment — o overlay NÃO as inclui.
    ov = adapter.env_overlay(home="/home/opencode")
    assert "OPENROUTER_API_KEY" not in ov


# --------------------------------------------------------------------------- #
# parse_output
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_parse_output_last_text_event(adapter):
    stdout = "\n".join(
        [
            json.dumps({"type": "step_start", "sessionID": "s1"}),
            json.dumps({"type": "tool_use", "tool": "bash"}),
            json.dumps({"type": "text", "text": "primeiro"}),
            json.dumps({"type": "text", "text": "veredito final do agente"}),
            json.dumps({"type": "step_finish"}),
        ]
    )
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.result_text == "veredito final do agente"
    assert wr.error_code is None


@pytest.mark.unit
def test_parse_output_error_event_fails(adapter):
    stdout = "\n".join(
        [
            json.dumps({"type": "step_start"}),
            json.dumps({"type": "error", "message": "tool execution failed"}),
        ]
    )
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is False
    assert "tool execution failed" in wr.result_text
    assert wr.error_code == "CLI_REPORTED_ERROR"


@pytest.mark.unit
def test_parse_output_provider_402_is_not_clean_completion(adapter):
    # issue #445: corte por 402 NUNCA vira conclusão limpa → resumível.
    wr = adapter.parse_output(
        stdout="Error: 402 Payment Required — insufficient credit",
        stderr="",
        rc=0,
    )
    assert wr.ok is False
    assert wr.error_code == "INSUFFICIENT_CREDIT"


@pytest.mark.unit
def test_parse_output_provider_429_classified(adapter):
    wr = adapter.parse_output(stdout="", stderr="429 Too Many Requests", rc=1)
    assert wr.ok is False
    assert wr.error_code == "RATE_LIMIT"


@pytest.mark.unit
def test_extract_session_id_from_ndjson(adapter):
    stdout = "\n".join(
        [
            json.dumps({"type": "step_start", "sessionID": "ses_abc123"}),
            json.dumps({"type": "text", "text": "done"}),
        ]
    )
    sid = adapter.extract_session_id(stdout=stdout, stderr="", task_id="t")
    assert sid == "ses_abc123"


@pytest.mark.unit
def test_extract_session_id_empty_when_no_event(adapter):
    sid = adapter.extract_session_id(stdout="no json here", stderr="", task_id="t")
    assert sid == ""


@pytest.mark.unit
def test_parse_output_tolerates_malformed_lines(adapter):
    stdout = "\n".join(
        [
            "isto não é json",
            "{ quebrado",
            json.dumps({"type": "text", "text": "ok apesar do ruído"}),
            "",
        ]
    )
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.result_text == "ok apesar do ruído"


@pytest.mark.unit
def test_parse_output_events_without_text(adapter):
    # Houve eventos (step/tool) mas nenhum texto → ok plausível; gate de git
    # do server confirma commit/push.
    stdout = "\n".join(
        [
            json.dumps({"type": "step_start"}),
            json.dumps({"type": "tool_use", "tool": "edit"}),
            json.dumps({"type": "step_finish"}),
        ]
    )
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.error_code is None


@pytest.mark.unit
def test_parse_output_no_output_fails(adapter):
    wr = adapter.parse_output(stdout="", stderr="boom: cli crashed", rc=1)
    assert wr.ok is False
    assert wr.error_code == "NO_OUTPUT"
    assert "boom" in wr.result_text


@pytest.mark.unit
def test_parse_output_nested_text_shape(adapter):
    # Tolera o conteúdo aninhado em sub-dict (variação de versão).
    stdout = json.dumps({"type": "text", "data": {"text": "aninhado"}})
    wr = adapter.parse_output(stdout=stdout, stderr="", rc=0)
    assert wr.ok is True
    assert wr.result_text == "aninhado"


# --------------------------------------------------------------------------- #
# list_models
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_models_dynamic_parses_output(adapter, monkeypatch):
    monkeypatch.setattr(oc_mod.shutil, "which", lambda _name: "/usr/bin/opencode")

    def _fake_run(argv, **_kw):
        assert argv == ["opencode", "models"]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                "openrouter/deepseek/deepseek-chat\n"
                "openrouter/anthropic/claude-3.7-sonnet\n"
                "\n"
                "ruído sem barra\n"  # descartado
                "openrouter/deepseek/deepseek-chat\n"  # duplicado → dedup
                "linha com espaço/model\n"  # tem espaço → descartado
            ),
            stderr="",
        )

    monkeypatch.setattr(oc_mod.subprocess, "run", _fake_run)
    models = adapter.list_models()
    ids = [m.id for m in models]
    assert ids == [
        "openrouter/deepseek/deepseek-chat",
        "openrouter/anthropic/claude-3.7-sonnet",
    ]
    assert models[0].provider == "openrouter"


@pytest.mark.unit
def test_list_models_falls_back_when_binary_missing(adapter, monkeypatch):
    monkeypatch.setattr(oc_mod.shutil, "which", lambda _name: None)
    models = adapter.list_models()
    # Catálogo curado garante picker não-vazio.
    assert models, "fallback catalog não pode ser vazio"
    assert all("/" in m.id for m in models)
    assert any("deepseek" in m.id for m in models)


@pytest.mark.unit
def test_list_models_falls_back_on_command_error(adapter, monkeypatch):
    monkeypatch.setattr(oc_mod.shutil, "which", lambda _name: "/usr/bin/opencode")

    def _boom(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="opencode models", timeout=20)

    monkeypatch.setattr(oc_mod.subprocess, "run", _boom)
    models = adapter.list_models()
    assert models == list(oc_mod._FALLBACK_MODELS)


@pytest.mark.unit
def test_list_models_falls_back_when_no_valid_lines(adapter, monkeypatch):
    monkeypatch.setattr(oc_mod.shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(
        oc_mod.subprocess,
        "run",
        lambda *_a, **_kw: subprocess.CompletedProcess(
            ["opencode", "models"],
            0,
            stdout="nada-valido\noutra linha\n",
            stderr="",
        ),
    )
    models = adapter.list_models()
    assert models == list(oc_mod._FALLBACK_MODELS)
