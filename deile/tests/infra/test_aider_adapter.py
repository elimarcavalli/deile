"""Testes do adapter Aider (Fase 5 — frota multi-CLI, Tier 1).

Cobre os pontos especializados do contrato :class:`CliAdapter` para o ``aider``:

  1. ``build_argv`` — ``--message-file <brief>`` (path, não conteúdo — o Aider lê
     o arquivo), autonomia (``--yes-always``), ``--auto-commits`` (git_strategy
     cli_autocommit), e os ``--no-attribute-*`` (regra do projeto: sem marca
     "aider"/Co-Authored-By); modelo via ``--model``; resume/reasoning ignorados.
  2. ``env_overlay`` — HOME gravável, SEM ``auth_env_keys``.
  3. ``parse_output`` — heurística (Aider não tem JSON headless): marcadores de
     erro → ok=False; saída plausível → ok=True com tail.
  4. ``list_models`` — dinâmico via ``aider --list-models`` com fallback curado.

Além dos metadados (kind/porta/auth/git_strategy=cli_autocommit/egress).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cli_adapters import base, get_adapter  # noqa: E402
from cli_adapters import aider as ai_mod  # noqa: E402


@pytest.fixture
def adapter():
    return get_adapter("aider")


@pytest.mark.unit
def test_metadata_matches_plan(adapter):
    assert adapter.kind == "aider"
    assert adapter.default_port == 8774              # §1.13
    assert adapter.auth_mode == "env"
    assert adapter.supports_resume is False
    assert adapter.supports_reasoning is False
    assert adapter.git_strategy == "cli_autocommit"  # §2.4 — ÚNICO da frota
    assert adapter.oauth is None
    assert "OPENROUTER_API_KEY" in adapter.auth_env_keys
    assert "openrouter.ai" in adapter.egress_hosts


@pytest.mark.unit
def test_satisfies_protocol(adapter):
    assert isinstance(adapter, base.CliAdapter)


@pytest.mark.unit
def test_build_argv_message_file_is_path_not_content(adapter):
    # Aider lê o arquivo → passa-se o PATH, não o conteúdo (difere de codex/qwen).
    argv = adapter.build_argv(
        brief_path="/work/abc/.brief.md", model="deepseek/deepseek-chat",
        reasoning=None, workdir="/work/abc", resume=None,
    )
    assert argv[0] == "aider"
    assert argv[argv.index("--message-file") + 1] == "/work/abc/.brief.md"
    assert argv[argv.index("--model") + 1] == "deepseek/deepseek-chat"
    assert "--yes-always" in argv          # §1.4 autonomia
    assert "--auto-commits" in argv        # §1.5 cli_autocommit


@pytest.mark.unit
def test_build_argv_no_attribute_flags(adapter):
    # Regra do projeto: sem "(aider)"/Co-Authored-By nos commits.
    argv = adapter.build_argv(
        brief_path="/w/.brief.md", model=None, reasoning=None,
        workdir="/w", resume=None,
    )
    assert "--no-attribute-author" in argv
    assert "--no-attribute-committer" in argv
    assert "--no-attribute-commit-message-author" in argv


@pytest.mark.unit
def test_build_argv_no_model_omits_flag(adapter):
    argv = adapter.build_argv(
        brief_path="/w/.brief.md", model=None, reasoning=None,
        workdir="/w", resume=None,
    )
    assert "--model" not in argv


@pytest.mark.unit
def test_build_argv_ignores_resume(adapter):
    resume = base.ResumeCtx(session_id="sX", prev_task_id="0123456789abcdef")
    argv = adapter.build_argv(
        brief_path="/w/.brief.md", model=None, reasoning="high",
        workdir="/w", resume=resume,
    )
    assert "sX" not in argv
    assert "--restore-chat-history" not in argv


@pytest.mark.unit
def test_env_overlay(adapter):
    ov = adapter.env_overlay(home="/home/aider")
    assert ov["HOME"] == "/home/aider"
    assert "OPENROUTER_API_KEY" not in ov
    assert "DEEPSEEK_API_KEY" not in ov


@pytest.mark.unit
def test_parse_output_error_marker_fails(adapter):
    wr = adapter.parse_output(
        stdout="", stderr="litellm.exceptions.AuthenticationError: bad key", rc=1,
    )
    assert wr.ok is False
    assert wr.error_code == "CLI_REPORTED_ERROR"
    assert "AuthenticationError" in wr.result_text


@pytest.mark.unit
def test_parse_output_plausible_run_ok(adapter):
    wr = adapter.parse_output(
        stdout="Applied edit to app.py\nCommit a1b2c3d feat: add feature\n",
        stderr="", rc=0,
    )
    assert wr.ok is True
    assert "Commit" in wr.result_text


@pytest.mark.unit
def test_parse_output_empty_defers_to_git_gate(adapter):
    # Sem saída: ok plausível — o gate de commit/push do server confirma.
    wr = adapter.parse_output(stdout="", stderr="", rc=0)
    assert wr.ok is True
    assert "git" in wr.result_text.lower()


# --------------------------------------------------------------------------- #
# list_models — dinâmico via `aider --list-models`, com fallback
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_models_dynamic_parses_output(adapter, monkeypatch):
    monkeypatch.setattr(ai_mod.shutil, "which", lambda _n: "/usr/bin/aider")

    def _fake_run(argv, **_kw):
        assert argv == ["aider", "--list-models", ""]
        return subprocess.CompletedProcess(
            argv, 0,
            stdout=(
                "Models which match:\n"          # cabeçalho → descartado
                "- openrouter/anthropic/claude-3.7-sonnet\n"
                "- deepseek/deepseek-chat\n"
                "ruído sem barra\n"               # descartado
                "- deepseek/deepseek-chat\n"      # duplicado → dedup
                "- com espaço/model\n"            # tem espaço → descartado
            ),
            stderr="",
        )

    monkeypatch.setattr(ai_mod.subprocess, "run", _fake_run)
    models = adapter.list_models()
    ids = [m.id for m in models]
    assert ids == [
        "openrouter/anthropic/claude-3.7-sonnet",
        "deepseek/deepseek-chat",
    ]
    assert models[0].provider == "openrouter"


@pytest.mark.unit
def test_list_models_falls_back_when_binary_missing(adapter, monkeypatch):
    monkeypatch.setattr(ai_mod.shutil, "which", lambda _n: None)
    models = adapter.list_models()
    assert models, "fallback catalog não pode ser vazio"
    assert any("deepseek" in m.id for m in models)


@pytest.mark.unit
def test_list_models_falls_back_on_command_error(adapter, monkeypatch):
    monkeypatch.setattr(ai_mod.shutil, "which", lambda _n: "/usr/bin/aider")

    def _boom(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="aider --list-models", timeout=20)

    monkeypatch.setattr(ai_mod.subprocess, "run", _boom)
    models = adapter.list_models()
    assert models == list(ai_mod._FALLBACK_MODELS)


@pytest.mark.unit
def test_list_models_falls_back_when_no_valid_lines(adapter, monkeypatch):
    monkeypatch.setattr(ai_mod.shutil, "which", lambda _n: "/usr/bin/aider")
    monkeypatch.setattr(
        ai_mod.subprocess, "run",
        lambda *_a, **_kw: subprocess.CompletedProcess(
            ["aider", "--list-models", ""], 0,
            stdout="nada-valido\noutra linha\n", stderr="",
        ),
    )
    models = adapter.list_models()
    assert models == list(ai_mod._FALLBACK_MODELS)
