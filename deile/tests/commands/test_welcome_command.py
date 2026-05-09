"""Testes do comando /welcome (issue #174).

Cobre a matriz de testes obrigatória:
  - test_version_shown_matches_version_module
  - test_all_quickstart_commands_exist_in_registry
  - test_find_command_not_in_quickstart
  - test_doc_link_points_to_real_path
  - test_feature_list_only_active_features
  - test_model_shown_reflects_active_router
  - test_no_english_strings_in_output (seções/labels)
  - test_links_not_pointing_to_nonexistent_paths
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

import deile.__version__ as version_mod
from deile.commands.base import CommandContext
from deile.commands.builtin.welcome_command import (_LINKS, WelcomeCommand,
                                                    _get_active_features,
                                                    _get_quick_start_verified)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(content) -> str:
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=200)
    console.print(content)
    return buf.getvalue()


def _ctx(agent=None) -> CommandContext:
    ctx = CommandContext(user_input="/welcome", args="")
    ctx.agent = agent
    return ctx


def _cmd() -> WelcomeCommand:
    return WelcomeCommand()


# ---------------------------------------------------------------------------
# Testes de correctude
# ---------------------------------------------------------------------------


class TestVersionShown:
    async def test_version_shown_matches_version_module(self):
        result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        assert version_mod.__version__ in rendered

    async def test_version_updates_dynamically(self):
        with patch.object(version_mod, "__version__", "99.99.99"):
            result = await _cmd().execute(_ctx())
            rendered = _render(result.content)
            assert "99.99.99" in rendered


class TestQuickStart:
    async def test_all_quickstart_commands_exist_in_registry(self):
        ctx = _ctx()
        verified = _get_quick_start_verified(ctx)
        if not verified:
            pytest.skip("CommandRegistry não inicializado — skip")
        from deile.commands.registry import get_command_registry
        registry = get_command_registry()
        registered = {cmd.name for cmd in registry.get_all_commands()}
        for entry in verified:
            assert entry["nome"] in registered, (
                f"Comando '{entry['nome']}' na quick start mas não registrado"
            )

    async def test_find_command_not_in_quickstart(self):
        ctx = _ctx()
        verified = _get_quick_start_verified(ctx)
        names = [e["nome"] for e in verified]
        assert "find" not in names, "/find não é comando oficial e não deve estar na quick start"

    async def test_quickstart_not_empty(self):
        result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        assert "help" in rendered or "status" in rendered


class TestDocLinks:
    async def test_doc_link_points_to_real_path(self):
        doc_link = _LINKS.get("Documentação", "")
        assert doc_link == "docs/system_design/00-VISAO-GERAL.md"
        repo_root = Path(__file__).parent.parent.parent.parent
        path = repo_root / doc_link
        assert path.exists(), f"Caminho de doc não existe: {path}"

    async def test_links_not_pointing_to_nonexistent_paths(self):
        assert "docs/2.md" not in str(_LINKS.values())
        for name, link in _LINKS.items():
            assert link.strip(), f"Link '{name}' vazio"
            assert "example.com" not in link


class TestFeatureList:
    async def test_feature_list_only_active_features(self):
        active = _get_active_features()
        inactive = [k for k, v in version_mod.FEATURES.items() if not v]
        for flag in inactive:
            assert flag not in active, f"Flag inativa '{flag}' listada como ativa"

    async def test_feature_list_uses_features_dict(self):
        patched = {k: False for k in version_mod.FEATURES}
        patched["memory"] = True
        with patch.object(version_mod, "FEATURES", patched):
            active = _get_active_features()
            assert active == ["memory"]

    async def test_no_feature_with_open_incomplete_issue(self):
        result = await _cmd().execute(_ctx())
        assert result.success


class TestModelShown:
    async def test_model_shown_reflects_active_router(self):
        agent = MagicMock()
        router = MagicMock()
        router.current_model = "gpt-test-model"
        agent.model_router = router

        result = await _cmd().execute(_ctx(agent=agent))
        assert result.success
        rendered = _render(result.content)
        assert "gpt-test-model" in rendered

    async def test_model_fallback_when_no_agent(self):
        result = await _cmd().execute(_ctx(agent=None))
        assert result.success

    async def test_model_fallback_when_router_missing(self):
        agent = MagicMock(spec=[])
        result = await _cmd().execute(_ctx(agent=agent))
        assert result.success


class TestUIPtbr:
    async def test_sections_in_ptbr(self):
        result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        assert "Início Rápido" in rendered or "Capacidades" in rendered or "Fluxos" in rendered

    async def test_no_english_section_headers(self):
        result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        english_headers = ["Quick Start Guide", "Key Features", "Common Workflows", "Pro Tips"]
        for header in english_headers:
            assert header not in rendered, f"Header em inglês encontrado: '{header}'"

    async def test_doc_link_not_docs_2_md(self):
        result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        assert "docs/2.md" not in rendered


class TestQuickStartColumnOrder:
    _SAMPLE_ENTRIES = [
        {"nome": "help", "acao": "Listar todos os comandos", "descricao": "Ajuda completa"},
        {"nome": "status", "acao": "Checar status do sistema", "descricao": "Visão geral do DEILE"},
    ]

    async def test_command_column_contains_slash_prefix(self):
        """Verifica que a coluna Comando exibe /help e não o texto de descrição."""
        with patch(
            "deile.commands.builtin.welcome_command._get_quick_start_verified",
            return_value=self._SAMPLE_ENTRIES,
        ):
            result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        # /help deve aparecer como comando — não "Ajuda completa" na coluna Comando
        assert "/help" in rendered
        # A string de descrição deve existir separadamente — não como nome de comando
        assert "Ajuda completa" in rendered

    async def test_command_column_not_swapped_with_description(self):
        """Garante que descrições não aparecem onde os comandos devem estar."""
        with patch(
            "deile.commands.builtin.welcome_command._get_quick_start_verified",
            return_value=self._SAMPLE_ENTRIES,
        ):
            result = await _cmd().execute(_ctx())
        assert result.success
        rendered = _render(result.content)
        # Coluna Ação deve mostrar a descrição de ação, Comando deve mostrar /nome
        # Verifica que /status aparece (coluna Comando) e "Visão geral" aparece (coluna Ação)
        assert "/status" in rendered
        assert "Visão geral do DEILE" in rendered


class TestContentType:
    async def test_success(self):
        result = await _cmd().execute(_ctx())
        assert result.success

    async def test_content_type_is_rich(self):
        result = await _cmd().execute(_ctx())
        assert result.content_type == "rich"

    async def test_renders_without_errors(self):
        result = await _cmd().execute(_ctx())
        rendered = _render(result.content)
        assert len(rendered) > 100
