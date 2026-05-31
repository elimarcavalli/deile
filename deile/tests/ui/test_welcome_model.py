"""Regressão: a tela de boas-vindas (após /clear, /resume ou startup) deve
refletir o modelo *atualmente selecionado* na sessão, não o default da config.

Antes, ``_resolve_provider_model`` lia apenas ``config_manager.get_config().default_model``,
ignorando ``session.context_data["forced_model"]`` definido por ``/model use``
ou ``/model select``.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from deile.ui.console_ui import ConsoleUIManager


def _make_ui_with_default(default_model: str | None) -> ConsoleUIManager:
    """Cria UI com um config_manager fake que devolve ``default_model``."""
    cfg = SimpleNamespace(default_model=default_model)
    config_manager = SimpleNamespace(get_config=lambda: cfg)
    ui = ConsoleUIManager.__new__(ConsoleUIManager)
    # bypass __init__ — só precisamos do config_manager e do console
    ui.console = Console(file=io.StringIO(), width=120, force_terminal=False, no_color=True)
    ui.session = None
    ui.is_initialized = True
    ui.config_manager = config_manager
    ui.working_directory = None
    return ui


def _session(forced_model: str | None) -> SimpleNamespace:
    return SimpleNamespace(context_data={"forced_model": forced_model} if forced_model else {})


@pytest.mark.unit
def test_resolve_provider_model_prefers_session_forced_model_over_config():
    """Modelo forçado na sessão tem precedência sobre o default da config."""
    ui = _make_ui_with_default("anthropic:claude-opus-4-8")
    session = _session("openai:gpt-5.3")
    provider, model = ui._resolve_provider_model(session)
    assert provider == "OpenAI"
    assert model == "gpt-5.3"


@pytest.mark.unit
def test_resolve_provider_model_falls_back_to_config_when_session_has_no_override():
    """Sem forced_model na sessão, usa o default da config."""
    ui = _make_ui_with_default("anthropic:claude-opus-4-8")
    session = _session(None)
    provider, model = ui._resolve_provider_model(session)
    assert provider == "Anthropic"
    assert model == "claude-opus-4-8"


@pytest.mark.unit
def test_resolve_provider_model_handles_none_session():
    """Compat: sem session, comportamento antigo (default da config)."""
    ui = _make_ui_with_default("gemini:gemini-2.5-pro")
    provider, model = ui._resolve_provider_model(None)
    assert provider == "Gemini"
    assert model == "gemini-2.5-pro"


@pytest.mark.unit
def test_resolve_provider_model_forced_without_provider_separator():
    """forced_model sem ":" cai no fallback "—"."""
    ui = _make_ui_with_default("anthropic:claude-opus-4-8")
    session = _session("custom-bare-name")
    provider, model = ui._resolve_provider_model(session)
    assert provider == "—"
    assert model == "custom-bare-name"


@pytest.mark.unit
def test_show_welcome_renders_session_model_in_panel():
    """End-to-end: show_welcome(session) imprime o forced_model no banner."""
    ui = _make_ui_with_default("anthropic:claude-opus-4-8")
    session = _session("deepseek:deepseek-chat")
    ui.show_welcome(session)
    output = ui.console.file.getvalue()
    assert "DeepSeek" in output
    assert "deepseek-chat" in output
    # Garantia: o modelo da config (que NÃO foi o último selecionado) não vaza no banner.
    assert "claude-opus-4-8" not in output
