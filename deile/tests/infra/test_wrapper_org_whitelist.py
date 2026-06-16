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
#
# NOTA: estes testes EXERCEM `_install_tool_whitelist` de verdade — instalam o
# hook (que embrulha `DeileAgent.initialize`), aguardam o initialize embrulhado
# sobre um agente fake e observam quais tools o registry REALMENTE desabilitou.
# Não recomputam `role & org` inline (isso só testaria o operador `&` da
# linguagem, dando falsa segurança sobre a interseção de produção).
# ---------------------------------------------------------------------------

async def _exercise_install(wrapper_mod, role_whitelist, org_allow, present_tools):
    """Roda `_install_tool_whitelist` e devolve `(kept, disabled)` REAIS.

    Patcha `DeileAgent.initialize` com um stub async, instala o whitelist (que
    embrulha o stub), aguarda o initialize embrulhado sobre um agente fake cujo
    registry contém `present_tools`, e captura os `disable_tool` reais. Restaura
    `DeileAgent.initialize` ao final (isolamento de teste).
    """
    import deile.core.agent as agent_mod

    disabled: list = []
    tools = []
    for name in present_tools:
        tool = MagicMock()
        tool.name = name
        tools.append(tool)

    registry = MagicMock()
    registry.list_all.return_value = tools
    registry.disable_tool.side_effect = lambda name: (disabled.append(name) or True)

    fake_agent = MagicMock()
    fake_agent.tool_registry = registry

    orig_init = agent_mod.DeileAgent.initialize

    async def _stub_init(self, *args, **kwargs):
        return "initialized"

    agent_mod.DeileAgent.initialize = _stub_init
    try:
        with (
            patch.object(wrapper_mod, "_messaging_tool_whitelist", return_value=role_whitelist),
            patch.object(wrapper_mod, "_load_org_tool_allow_list", return_value=org_allow),
        ):
            wrapper_mod._install_tool_whitelist("test_role")
        # `DeileAgent.initialize` agora é o hook endurecido sobre `_stub_init`.
        await agent_mod.DeileAgent.initialize(fake_agent)
    finally:
        agent_mod.DeileAgent.initialize = orig_init

    kept = [t.name for t in tools if t.name not in disabled]
    return kept, disabled


@pytest.mark.unit
class TestInstallToolWhitelistOrgIntersection:
    """Verifica que a interseção com org allow-list estreita o whitelist REAL."""

    async def test_intersection_removes_tool_absent_from_org_allow(self, wrapper_mod) -> None:
        """Tool no role_whitelist mas ausente do org allow-list é DESABILITADA."""
        role_whitelist = frozenset({
            "discord_send_message",
            "dispatch_deile_task",
            "vision_describe_image",
        })
        org_allow = frozenset({"dispatch_deile_task"})  # org permite só uma
        present = list(role_whitelist) + ["bash_execute"]

        kept, disabled = await _exercise_install(wrapper_mod, role_whitelist, org_allow, present)

        assert kept == ["dispatch_deile_task"]
        assert "discord_send_message" in disabled
        assert "vision_describe_image" in disabled

    async def test_no_org_allow_list_preserves_full_role_whitelist(self, wrapper_mod) -> None:
        """Sem allow-list de org (vazio), role_whitelist é usado integralmente."""
        role_whitelist = frozenset({"discord_send_message", "dispatch_deile_task"})
        present = list(role_whitelist) + ["bash_execute"]

        kept, disabled = await _exercise_install(wrapper_mod, role_whitelist, frozenset(), present)

        assert set(kept) == role_whitelist
        # Só o que está fora do role_whitelist é desabilitado (baseline).
        assert disabled == ["bash_execute"]

    async def test_org_cannot_add_tool_not_in_role_whitelist(self, wrapper_mod) -> None:
        """Org não pode conceder bash_execute se não está no role_whitelist."""
        role_whitelist = frozenset({"discord_send_message", "dispatch_deile_task"})
        org_allow = frozenset({"discord_send_message", "dispatch_deile_task", "bash_execute"})
        present = ["discord_send_message", "dispatch_deile_task", "bash_execute"]

        kept, disabled = await _exercise_install(wrapper_mod, role_whitelist, org_allow, present)

        assert "bash_execute" not in kept, (
            "Interseção garante que org não pode AMPLIAR o whitelist do papel"
        )
        assert "bash_execute" in disabled


# ---------------------------------------------------------------------------
# Testes de monotonicidade (invariante de segurança) — exercem a função real.
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOrgWhitelistMonotonicity:
    """AC: org só estreita o conjunto de ferramentas, nunca amplia."""

    async def test_effective_kept_is_subset_of_role_whitelist(self, wrapper_mod) -> None:
        """`kept` ⊆ role_whitelist para qualquer org_allow — provado pela função."""
        role_whitelist = frozenset({"a", "b", "c", "d"})
        present = list(role_whitelist) + ["e", "x"]
        for org_allow in [
            frozenset({"a", "b"}),
            frozenset({"a", "b", "c", "d", "e"}),  # org "generosa" inclui "e"
            frozenset(),
            frozenset({"x", "y"}),  # sem sobreposição com o papel
        ]:
            kept, _ = await _exercise_install(wrapper_mod, role_whitelist, org_allow, present)
            assert set(kept) <= role_whitelist, (
                f"kept {kept} não é subconjunto de role_whitelist {role_whitelist} "
                f"(org_allow={org_allow})"
            )

    async def test_generous_org_does_not_widen_whitelist(self, wrapper_mod) -> None:
        """Org "generosa" (inclui bash/python) NÃO os adiciona ao whitelist efetivo."""
        role_whitelist = frozenset({"discord_send_message"})
        org_allow = frozenset({"discord_send_message", "bash_execute", "python_execute"})
        present = ["discord_send_message", "bash_execute", "python_execute"]

        kept, disabled = await _exercise_install(wrapper_mod, role_whitelist, org_allow, present)

        assert kept == ["discord_send_message"]
        assert "bash_execute" in disabled
        assert "python_execute" in disabled
