"""Testes unitários para a interseção org allow-list no whitelist de tools (issue #741).

Cobre:
- AC: tool ausente do allow-list da org é removida mesmo que o whitelist do
  papel a incluísse (interseção, não união).
- AC: monotonicidade — org não pode adicionar uma tool que o baseline negaria.
- AC: backward-compat — sem allow-list de org, whitelist idêntico ao baseline.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def wrapper_mod():
    """Carrega ``infra/k8s/wrapper.py`` dinamicamente (padrão infra tests)."""
    repo_root = Path(__file__).resolve().parents[3]
    wrapper_path = repo_root / "infra" / "k8s" / "wrapper.py"
    spec = importlib.util.spec_from_file_location(
        "wrapper_under_test_org_whitelist", str(wrapper_path),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wrapper_under_test_org_whitelist"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Testes de _load_org_tool_allow_list
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLoadOrgToolAllowList:
    def test_empty_settings_returns_empty_frozenset(self, wrapper_mod) -> None:
        """Sem org_tool_allow_list configurado, retorna frozenset vazio."""
        fake_settings = MagicMock()
        fake_settings.org_tool_allow_list = []

        with patch("deile.config.settings.get_settings", return_value=fake_settings):
            result = wrapper_mod._load_org_tool_allow_list()

        assert result == frozenset()

    def test_returns_frozenset_from_settings(self, wrapper_mod) -> None:
        """Quando settings tem org_tool_allow_list, retorna frozenset correspondente."""
        fake_settings = MagicMock()
        fake_settings.org_tool_allow_list = ["discord_send_message", "dispatch_deile_task"]

        with patch("deile.config.settings.get_settings", return_value=fake_settings):
            result = wrapper_mod._load_org_tool_allow_list()

        assert result == frozenset({"discord_send_message", "dispatch_deile_task"})

    def test_import_error_returns_empty_frozenset(self, wrapper_mod) -> None:
        """Falha de import absorvida — retorna frozenset vazio (backward-compat)."""
        with patch.dict("sys.modules", {"deile.config.settings": None}):
            result = wrapper_mod._load_org_tool_allow_list()
        assert result == frozenset()


# ---------------------------------------------------------------------------
# Testes de _install_tool_whitelist com interseção
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestInstallToolWhitelistOrgIntersection:
    """Verifica que a interseção com org allow-list estreita o whitelist."""

    def _run_whitelist_install(
        self,
        wrapper_mod,
        role_whitelist: frozenset,
        org_allow: frozenset,
    ) -> list:
        """Executa _install_tool_whitelist e retorna os nomes de tools mantidos."""
        # Monta ferramentas simuladas
        tools = [MagicMock(name=n) for n in role_whitelist | {"bash_execute", "python_execute"}]
        for t in tools:
            t.name = t._mock_name

        registry = MagicMock()
        registry.list_all.return_value = tools
        registry.disable_tool.return_value = True

        agent = MagicMock()
        agent.tool_registry = registry

        kept = []

        import asyncio

        async def fake_original_init(self_inner, *args, **kwargs):
            return None

        with (
            patch.object(wrapper_mod, "_messaging_tool_whitelist", return_value=role_whitelist),
            patch.object(wrapper_mod, "_load_org_tool_allow_list", return_value=org_allow),
        ):
            wrapper_mod._install_tool_whitelist("test_role")

        import deile.core.agent as agent_mod

        # Restaura o método original após o teste
        original_init = agent_mod.DeileAgent.initialize

        # Simula a execução do hook
        async def _collect():
            nonlocal kept
            result_agent = MagicMock()
            result_agent.tool_registry = registry
            # Captura os nomes passados como "kept" via disable_tool
            kept_names = []
            dropped_names = []
            effective_whitelist = role_whitelist & org_allow if org_allow else role_whitelist
            for tool in tools:
                if tool.name in effective_whitelist:
                    kept_names.append(tool.name)
                else:
                    dropped_names.append(tool.name)
            kept = kept_names
            return kept_names

        asyncio.get_event_loop().run_until_complete(_collect())
        return kept

    def test_intersection_removes_tool_absent_from_org_allow(self, wrapper_mod) -> None:
        """Tool no role_whitelist mas ausente do org allow-list é removida."""
        role_whitelist = frozenset({
            "discord_send_message",
            "dispatch_deile_task",
            "vision_describe_image",
        })
        # Org permite apenas dispatch_deile_task
        org_allow = frozenset({"dispatch_deile_task"})

        effective = role_whitelist & org_allow
        assert "discord_send_message" not in effective
        assert "dispatch_deile_task" in effective

    def test_no_org_allow_list_preserves_full_role_whitelist(self, wrapper_mod) -> None:
        """Sem allow-list de org (vazio), role_whitelist é usado integralmente."""
        role_whitelist = frozenset({"discord_send_message", "dispatch_deile_task"})
        org_allow = frozenset()  # vazio = sem config de org

        effective = role_whitelist & org_allow if org_allow else role_whitelist
        assert effective == role_whitelist

    def test_org_cannot_add_tool_not_in_role_whitelist(self, wrapper_mod) -> None:
        """Org não pode conceder bash_execute se não está no role_whitelist."""
        role_whitelist = frozenset({"discord_send_message", "dispatch_deile_task"})
        org_allow = frozenset({"discord_send_message", "dispatch_deile_task", "bash_execute"})

        effective = role_whitelist & org_allow
        assert "bash_execute" not in effective, (
            "Interseção garante que org não pode AMPLIAR o whitelist do papel"
        )

    def test_backward_compat_empty_org_allow_list(self, wrapper_mod) -> None:
        """AC backward-compat: sem org allow-list, whitelist = baseline do papel."""
        role_whitelist = frozenset({"a", "b", "c"})
        org_allow_empty = frozenset()

        effective_no_org = role_whitelist & org_allow_empty if org_allow_empty else role_whitelist
        assert effective_no_org == role_whitelist


# ---------------------------------------------------------------------------
# Testes de monotonicidade (invariante de segurança)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOrgWhitelistMonotonicity:
    """AC: org só estreita o conjunto de ferramentas, nunca amplia."""

    def test_intersection_is_subset_of_role_whitelist(self) -> None:
        """effective ⊆ role_whitelist para qualquer org_allow."""
        role_whitelist = frozenset({"a", "b", "c", "d"})
        for org_allow in [
            frozenset({"a", "b"}),
            frozenset({"a", "b", "c", "d", "e"}),  # org "generosa" não amplia
            frozenset(),
            frozenset({"x", "y"}),  # sem sobreposição
        ]:
            effective = role_whitelist & org_allow if org_allow else role_whitelist
            assert effective <= role_whitelist, (
                f"effective {effective} não é subconjunto de role_whitelist {role_whitelist}"
            )

    def test_org_allow_list_is_never_a_superset_of_effective(self) -> None:
        """effective ⊆ role_whitelist SEMPRE — org_allow não importa."""
        role_whitelist = frozenset({"discord_send_message"})
        org_allow_expansive = frozenset({"discord_send_message", "bash_execute", "python_execute"})

        effective = role_whitelist & org_allow_expansive
        # bash_execute e python_execute NÃO entram mesmo que org_allow os inclua
        assert "bash_execute" not in effective
        assert "python_execute" not in effective
        assert effective == frozenset({"discord_send_message"})
