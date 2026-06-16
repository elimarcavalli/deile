"""Testes de integração — permissões da org (issue #741).

Cobre:
- AC: regra de org que nega uma tool bloqueia check_permission em runtime.
- AC: monotonicidade — org não pode conceder além do que o baseline nega.
- AC: backward-compat — sem permissions.yaml de org, comportamento idêntico.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from deile.security.permissions import (
    PermissionLevel,
    PermissionManager,
    PermissionRule,
    ResourceType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_org_permissions(org_root: Path, rules: list) -> None:
    """Escreve org_root/permissions.yaml com as regras fornecidas."""
    org_root.mkdir(parents=True, exist_ok=True)
    (org_root / "permissions.yaml").write_text(
        yaml.dump({"permission_rules": rules}),
        encoding="utf-8",
    )


_DENY_BASH_RULE = {
    "id": "deny_bash",
    "name": "Org denies bash",
    "description": "Org policy: bash_execute is forbidden",
    "resource_type": "command",
    "resource_pattern": r".*",
    "tool_names": ["bash_execute"],
    "permission_level": "none",
    "priority": 1,
    "enabled": True,
}


# ---------------------------------------------------------------------------
# Testes de carregamento das regras de org
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLoadOrgRules:
    def test_loads_rules_from_org_config_root(self, tmp_path: Path) -> None:
        _write_org_permissions(tmp_path, [_DENY_BASH_RULE])
        mgr = PermissionManager()
        mgr.load_org_rules(tmp_path)
        rule_ids = [r.id for r in mgr.rules]
        assert "org__deny_bash" in rule_ids

    def test_no_permissions_yaml_is_noop(self, tmp_path: Path) -> None:
        """Sem permissions.yaml, load_org_rules não altera as regras."""
        mgr = PermissionManager()
        rules_before = len(mgr.rules)
        mgr.load_org_rules(tmp_path)  # arquivo não existe
        assert len(mgr.rules) == rules_before

    def test_empty_permission_rules_list(self, tmp_path: Path) -> None:
        _write_org_permissions(tmp_path, [])
        mgr = PermissionManager()
        rules_before = len(mgr.rules)
        mgr.load_org_rules(tmp_path)
        assert len(mgr.rules) == rules_before

    def test_org_rules_have_org_prefix(self, tmp_path: Path) -> None:
        _write_org_permissions(tmp_path, [_DENY_BASH_RULE])
        mgr = PermissionManager()
        mgr.load_org_rules(tmp_path)
        org_rules = [r for r in mgr.rules if r.id.startswith("org__")]
        assert len(org_rules) == 1

    def test_invalid_yaml_does_not_raise(self, tmp_path: Path) -> None:
        """Org permissions.yaml inválido não deve lançar exceção."""
        org_root = tmp_path
        org_root.mkdir(parents=True, exist_ok=True)
        (org_root / "permissions.yaml").write_text("not: valid: yaml: :", encoding="utf-8")
        mgr = PermissionManager()
        # Não deve lançar
        mgr.load_org_rules(org_root)


# ---------------------------------------------------------------------------
# Testes de enforcement (integração — testa o check_permission em runtime)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestOrgPermissionEnforcement:
    """Testa que a regra de org efetivamente bloqueia o check_permission."""

    def test_org_deny_rule_blocks_check_permission(self, tmp_path: Path) -> None:
        """AC: regra de org que nega bash_execute bloqueia check_permission."""
        _write_org_permissions(tmp_path, [_DENY_BASH_RULE])

        mgr = PermissionManager()
        mgr.load_org_rules(tmp_path)

        # Deve bloquear — a regra da org tem prioridade=1 (> qualquer default).
        result = mgr.check_permission(
            tool_name="bash_execute",
            resource="/bin/bash",
            action="execute",
        )
        assert result is False, (
            "Regra de org com permission_level=none deve bloquear check_permission"
        )

    def test_org_deny_does_not_block_other_tools(self, tmp_path: Path) -> None:
        """Regra de org que nega bash_execute não afeta outras tools."""
        _write_org_permissions(tmp_path, [_DENY_BASH_RULE])
        mgr = PermissionManager()
        mgr.load_org_rules(tmp_path)

        result = mgr.check_permission(
            tool_name="read_file",
            resource="/tmp/test.txt",
            action="read",
        )
        # read_file não é bloqueada pela regra de org
        assert result is True

    def test_multiple_org_rules_all_applied(self, tmp_path: Path) -> None:
        rules = [
            _DENY_BASH_RULE,
            {
                "id": "deny_python",
                "name": "Org denies python_execute",
                "description": "",
                "resource_type": "command",
                "resource_pattern": r".*",
                "tool_names": ["python_execute"],
                "permission_level": "none",
                "priority": 1,
                "enabled": True,
            },
        ]
        _write_org_permissions(tmp_path, rules)
        mgr = PermissionManager()
        mgr.load_org_rules(tmp_path)

        assert mgr.check_permission("bash_execute", "/bin/bash", "execute") is False
        assert mgr.check_permission("python_execute", "print()", "execute") is False


# ---------------------------------------------------------------------------
# Testes de monotonicidade (invariante de segurança)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestOrgPermissionsMonotonicity:
    """AC: org não pode ampliar permissões — apenas aperta.

    Invariante de segurança (issue #741): nenhuma regra de org consegue conceder
    uma capacidade que o baseline negaria, **independente da prioridade** da regra
    de org. ``check_permission`` aplica ``baseline AND org`` para o id ``org__*``.
    """

    def test_org_cannot_grant_permission_denied_by_default_baseline(self, tmp_path: Path) -> None:
        """Adversarial: org tenta conceder ADMIN p/ bash em /etc/ (que o baseline
        ``protect_system_dirs`` nega); resultado DEVE permanecer negado."""
        grant_rule = {
            "id": "org_grant_bash_admin",
            "name": "Org tries to grant bash admin on /etc",
            "description": "Tentativa adversarial de escalada de privilégio",
            "resource_type": "system",
            "resource_pattern": r"^/etc/.*",
            "tool_names": ["bash_execute"],
            "permission_level": "admin",
            "priority": 1,  # mais prioritária que o default protect_system_dirs (10)
            "enabled": True,
        }
        _write_org_permissions(tmp_path, [grant_rule])

        # Manager com as regras default — protect_system_dirs (priority=10, NONE)
        # nega qualquer tool em /etc/. A org tenta sobrepor com admin/priority=1.
        mgr = PermissionManager()
        mgr.load_org_rules(tmp_path)

        result = mgr.check_permission("bash_execute", "/etc/passwd", "execute")
        assert result is False, (
            "Monotonicidade violada: regra de org concedeu acesso que o baseline "
            "nega. O veredito efetivo deve ser baseline AND org."
        )

    def test_org_cannot_override_explicit_baseline_deny(self, tmp_path: Path) -> None:
        """Mesmo invariante com uma negação de baseline explícita (não-default)."""
        grant_rule = {
            "id": "grant_bash",
            "name": "Org grants bash",
            "description": "",
            "resource_type": "command",
            "resource_pattern": r".*",
            "tool_names": ["bash_execute"],
            "permission_level": "admin",
            "priority": 1,
            "enabled": True,
        }
        _write_org_permissions(tmp_path, [grant_rule])

        mgr = PermissionManager()
        baseline_deny = PermissionRule(
            id="baseline_deny_bash",
            name="Baseline denies bash",
            description="",
            resource_type=ResourceType.COMMAND,
            resource_pattern=r".*",
            tool_names=["bash_execute"],
            permission_level=PermissionLevel.NONE,
            priority=10,
        )
        mgr.add_rule(baseline_deny)
        mgr.load_org_rules(tmp_path)  # org tenta conceder admin com priority menor

        result = mgr.check_permission("bash_execute", "/bin/bash", "execute")
        assert result is False, (
            "Org com priority=1 (admin) NÃO pode reverter uma negação de baseline "
            "(priority=10, none) — org é estritamente subtrativa."
        )

    def test_org_deny_still_tightens_an_allowing_baseline(self, tmp_path: Path) -> None:
        """O lado complementar: quando o baseline PERMITE, a negação de org aperta."""
        _write_org_permissions(tmp_path, [_DENY_BASH_RULE])
        mgr = PermissionManager()
        allowing_baseline = PermissionRule(
            id="baseline_allow_bash",
            name="Baseline allows bash",
            description="",
            resource_type=ResourceType.COMMAND,
            resource_pattern=r".*",
            tool_names=["bash_execute"],
            permission_level=PermissionLevel.EXECUTE,
            priority=10,
        )
        mgr.add_rule(allowing_baseline)
        mgr.load_org_rules(tmp_path)

        # Baseline permitiria (EXECUTE); a org nega (none) → efetivo = negado.
        assert mgr.check_permission("bash_execute", "/bin/bash", "execute") is False


# ---------------------------------------------------------------------------
# Backward-compat
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOrgPermissionsBackwardCompat:
    def test_without_org_config_rules_unchanged(self, tmp_path: Path) -> None:
        """Sem org_config_root com permissions.yaml, regras idênticas ao baseline."""
        mgr_baseline = PermissionManager()
        mgr_with_empty_org = PermissionManager()
        mgr_with_empty_org.load_org_rules(tmp_path)  # diretório existe, mas sem yaml

        ids_baseline = {r.id for r in mgr_baseline.rules}
        ids_with_org = {r.id for r in mgr_with_empty_org.rules}
        assert ids_baseline == ids_with_org
