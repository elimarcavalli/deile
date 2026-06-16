"""Tests: /permissions command — all bugs fixed + full stubs implemented (issue #166)."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from deile.commands.base import CommandContext
from deile.commands.builtin.permissions_command import PermissionsCommand
from deile.security.permissions import (
    PermissionLevel,
    PermissionManager,
    PermissionRule,
    ResourceType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(content) -> str:
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=120)
    console.print(content)
    return buf.getvalue()


def _ctx(args: str = "") -> CommandContext:
    return CommandContext(user_input=f"/permissions {args}".strip(), args=args)


def _fresh_pm(config_path: Path | None = None) -> PermissionManager:
    return PermissionManager(config_path=config_path)


def _cmd_with_pm(pm: PermissionManager) -> PermissionsCommand:
    cmd = PermissionsCommand.__new__(PermissionsCommand)
    from deile.config.manager import CommandConfig

    config = CommandConfig(name="permissions", description="test")
    from deile.commands.base import DirectCommand

    DirectCommand.__init__(cmd, config)
    cmd.permission_manager = pm
    return cmd


def _add_test_rule(pm: PermissionManager, rule_id: str = "test_rule") -> PermissionRule:
    rule = PermissionRule(
        id=rule_id,
        name="Test Rule",
        description="Test",
        resource_type=ResourceType.FILE,
        resource_pattern=r".*\.txt$",
        tool_names=["write_file"],
        permission_level=PermissionLevel.WRITE,
        priority=200,
    )
    pm.add_rule(rule)
    return rule


# ---------------------------------------------------------------------------
# Bug fix #1 — show uses get_rule_by_id (not get_rule)
# ---------------------------------------------------------------------------


class TestShowUsesGetRuleById:
    async def test_show_uses_get_rule_by_id(self):
        """show must use get_rule_by_id; AttributeError must not occur."""
        pm = _fresh_pm()
        rule = _add_test_rule(pm)
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx(f"show {rule.id}"))
        assert result.success is True
        rendered = _render(result.content)
        assert rule.id in rendered

    async def test_show_unknown_id_raises(self):
        from deile.core.exceptions import CommandError

        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        with pytest.raises(CommandError):
            await cmd.execute(_ctx("show non_existent_id_xyz"))


# ---------------------------------------------------------------------------
# Bug fix #2 — enable uses get_rule_by_id
# ---------------------------------------------------------------------------


class TestEnableUsesGetRuleById:
    async def test_enable_uses_get_rule_by_id(self):
        pm = _fresh_pm()
        rule = _add_test_rule(pm)
        rule.enabled = False
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx(f"enable {rule.id}"))
        assert result.success is True
        assert pm.get_rule_by_id(rule.id).enabled is True

    async def test_enable_unknown_id_raises(self):
        from deile.core.exceptions import CommandError

        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        with pytest.raises(CommandError):
            await cmd.execute(_ctx("enable no_such_rule"))


# ---------------------------------------------------------------------------
# Bug fix #3 — disable uses get_rule_by_id
# ---------------------------------------------------------------------------


class TestDisableUsesGetRuleById:
    async def test_disable_uses_get_rule_by_id(self):
        pm = _fresh_pm()
        rule = _add_test_rule(pm)
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx(f"disable {rule.id}"))
        assert result.success is True
        assert pm.get_rule_by_id(rule.id).enabled is False

    async def test_disable_unknown_id_raises(self):
        from deile.core.exceptions import CommandError

        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        with pytest.raises(CommandError):
            await cmd.execute(_ctx("disable no_such_rule"))


# ---------------------------------------------------------------------------
# Add rule creates and persists
# ---------------------------------------------------------------------------


class TestAddRuleCreatesAndPersists:
    async def test_add_rule_creates_and_persists(self, tmp_path):
        config_path = tmp_path / "permissions.yaml"
        pm = _fresh_pm(config_path=config_path)
        cmd = _cmd_with_pm(pm)

        result = await cmd.execute(
            _ctx("add my_rule MyRule file r'.*\\.txt' write write_file")
        )
        assert result.success is True
        assert pm.get_rule_by_id("my_rule") is not None
        assert config_path.exists(), "add must persist rules to disk"

    async def test_add_rule_returns_id(self, tmp_path):
        config_path = tmp_path / "permissions.yaml"
        pm = _fresh_pm(config_path=config_path)
        cmd = _cmd_with_pm(pm)

        result = await cmd.execute(_ctx("add rule42 RuleFortyTwo file '.*' read *"))
        rendered = _render(result.content)
        assert "rule42" in rendered

    async def test_add_duplicate_id_raises(self):
        from deile.core.exceptions import CommandError

        pm = _fresh_pm()
        _add_test_rule(pm, "existing_rule")
        cmd = _cmd_with_pm(pm)
        with pytest.raises(CommandError, match="já existe"):
            await cmd.execute(_ctx("add existing_rule Name file '.*' read *"))

    async def test_add_invalid_type_raises(self):
        from deile.core.exceptions import CommandError

        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        with pytest.raises(CommandError, match="inválido"):
            await cmd.execute(_ctx("add r1 Name invalid_type '.*' read *"))

    async def test_add_invalid_level_raises(self):
        from deile.core.exceptions import CommandError

        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        with pytest.raises(CommandError, match="inválido"):
            await cmd.execute(_ctx("add r1 Name file '.*' superpower *"))


# ---------------------------------------------------------------------------
# Remove rule deletes and persists
# ---------------------------------------------------------------------------


class TestRemoveRuleDeletesAndPersists:
    async def test_remove_without_confirm_shows_warning(self):
        pm = _fresh_pm()
        rule = _add_test_rule(pm)
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx(f"remove {rule.id}"))
        assert result.success is True
        rendered = _render(result.content)
        assert "confirm" in rendered.lower() or "Confirm" in rendered
        # Rule must still exist (not deleted without confirmation)
        assert pm.get_rule_by_id(rule.id) is not None

    async def test_remove_rule_deletes_and_persists(self, tmp_path):
        config_path = tmp_path / "permissions.yaml"
        pm = _fresh_pm(config_path=config_path)
        rule = _add_test_rule(pm)
        pm.save_rules_to_config(config_path)
        cmd = _cmd_with_pm(pm)

        result = await cmd.execute(_ctx(f"remove {rule.id} --confirm"))
        assert result.success is True
        assert pm.get_rule_by_id(rule.id) is None, "rule must be removed"
        assert config_path.exists(), "remove must persist to disk"

    async def test_remove_unknown_rule_raises(self):
        from deile.core.exceptions import CommandError

        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        with pytest.raises(CommandError):
            await cmd.execute(_ctx("remove unknown_rule --confirm"))


# ---------------------------------------------------------------------------
# Audit log reads from AuditLogger
# ---------------------------------------------------------------------------


class TestAuditLogReadsFromAuditLogger:
    async def test_audit_log_reads_from_audit_logger(self):
        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("audit"))
        assert result.success is True
        rendered = _render(result.content)
        assert (
            "Auditoria" in rendered
            or "auditoria" in rendered
            or "evento" in rendered.lower()
        )

    async def test_audit_renders_without_crash(self):
        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("audit 10"))
        assert result.success is True
        assert _render(result.content).strip()


# ---------------------------------------------------------------------------
# Sandbox activates real flag
# ---------------------------------------------------------------------------


class TestSandboxActivatesRealFlag:
    async def test_sandbox_on_activates_real_flag(self):
        pm = _fresh_pm()
        assert pm.sandbox_enabled is False
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("sandbox on"))
        assert result.success is True
        assert pm.sandbox_enabled is True

    async def test_sandbox_off_deactivates_flag(self):
        pm = _fresh_pm()
        pm.sandbox_enabled = True
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("sandbox off"))
        assert result.success is True
        assert pm.sandbox_enabled is False

    async def test_sandbox_status_reads_real_state(self):
        pm = _fresh_pm()
        pm.sandbox_enabled = True
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("sandbox status"))
        rendered = _render(result.content)
        assert "ATIVO" in rendered

    async def test_sandbox_status_inactive(self):
        pm = _fresh_pm()
        pm.sandbox_enabled = False
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("sandbox status"))
        rendered = _render(result.content)
        assert "INATIVO" in rendered

    async def test_sandbox_invalid_mode_raises(self):
        from deile.core.exceptions import CommandError

        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        with pytest.raises(CommandError):
            await cmd.execute(_ctx("sandbox maybe"))


# ---------------------------------------------------------------------------
# Persistence verified by restart
# ---------------------------------------------------------------------------


class TestPersistenceAcrossRestart:
    async def test_enable_persists_across_restart(self, tmp_path):
        config_path = tmp_path / "permissions.yaml"
        pm1 = _fresh_pm(config_path=config_path)
        rule = _add_test_rule(pm1)
        rule.enabled = False
        pm1.save_rules_to_config(config_path)

        cmd1 = _cmd_with_pm(pm1)
        await cmd1.execute(_ctx(f"enable {rule.id}"))
        # pm1 now has the rule enabled and it was persisted via enable_rule

        pm2 = PermissionManager(config_path=config_path)
        restored = pm2.get_rule_by_id(rule.id)
        assert restored is not None
        assert restored.enabled is True, "enabled state must survive process restart"

    async def test_remove_does_not_reappear_after_restart(self, tmp_path):
        config_path = tmp_path / "permissions.yaml"
        pm1 = _fresh_pm(config_path=config_path)
        rule = _add_test_rule(pm1)
        pm1.save_rules_to_config(config_path)

        cmd1 = _cmd_with_pm(pm1)
        await cmd1.execute(_ctx(f"remove {rule.id} --confirm"))

        pm2 = PermissionManager(config_path=config_path)
        assert (
            pm2.get_rule_by_id(rule.id) is None
        ), "removed rule must not reappear after restart"


# ---------------------------------------------------------------------------
# Every mutation emits audit event
# ---------------------------------------------------------------------------


class TestEveryMutationEmitsAuditEvent:
    async def _count_security_events(self) -> int:
        from deile.security.audit_logger import AuditEventType, get_audit_logger

        return sum(
            1
            for e in get_audit_logger().recent_events
            if e.event_type == AuditEventType.SECURITY_POLICY_CHANGED
        )

    async def test_add_emits_audit_event(self, tmp_path):
        pm = _fresh_pm(config_path=tmp_path / "permissions.yaml")
        cmd = _cmd_with_pm(pm)
        before = await self._count_security_events()
        await cmd.execute(_ctx("add r1 N1 file '.*' read *"))
        after = await self._count_security_events()
        assert after > before

    async def test_enable_emits_audit_event(self):
        pm = _fresh_pm()
        rule = _add_test_rule(pm)
        rule.enabled = False
        cmd = _cmd_with_pm(pm)
        before = await self._count_security_events()
        await cmd.execute(_ctx(f"enable {rule.id}"))
        after = await self._count_security_events()
        assert after > before

    async def test_disable_emits_audit_event(self):
        pm = _fresh_pm()
        rule = _add_test_rule(pm)
        cmd = _cmd_with_pm(pm)
        before = await self._count_security_events()
        await cmd.execute(_ctx(f"disable {rule.id}"))
        after = await self._count_security_events()
        assert after > before

    async def test_remove_emits_audit_event(self, tmp_path):
        pm = _fresh_pm(config_path=tmp_path / "permissions.yaml")
        rule = _add_test_rule(pm)
        cmd = _cmd_with_pm(pm)
        before = await self._count_security_events()
        await cmd.execute(_ctx(f"remove {rule.id} --confirm"))
        after = await self._count_security_events()
        assert after > before

    async def test_sandbox_on_emits_audit_event(self):
        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        before = await self._count_security_events()
        await cmd.execute(_ctx("sandbox on"))
        after = await self._count_security_events()
        assert after > before


# ---------------------------------------------------------------------------
# Overview and list
# ---------------------------------------------------------------------------


class TestOverviewAndList:
    async def test_overview_renders(self):
        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx(""))
        assert result.success is True
        assert _render(result.content).strip()

    async def test_list_shows_rules(self):
        pm = _fresh_pm()
        _add_test_rule(pm)
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("list"))
        assert result.success is True
        rendered = _render(result.content)
        assert "test_rule" in rendered or "Test Rule" in rendered

    async def test_help_renders(self):
        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("help"))
        assert result.success is True

    async def test_check_permission(self):
        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("check write_file /etc/passwd write"))
        assert result.success is True

    async def test_sandbox_enabled_shown_in_overview(self):
        pm = _fresh_pm()
        pm.sandbox_enabled = True
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx(""))
        rendered = _render(result.content)
        assert "Ativo" in rendered or "ativo" in rendered.lower()

    async def test_list_with_resource_type_filter(self):
        pm = _fresh_pm()
        _add_test_rule(pm)
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("list file"))
        assert result.success is True

    async def test_list_with_permission_level_filter(self):
        pm = _fresh_pm()
        _add_test_rule(pm)
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("list write"))
        assert result.success is True

    async def test_list_with_text_filter(self):
        pm = _fresh_pm()
        _add_test_rule(pm)
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx("list nonexistent_text_filter"))
        assert result.success is True
        rendered = _render(result.content)
        assert "Nenhuma regra" in rendered

    async def test_check_permission_no_matching_rule_shows_default(self):
        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        # With no rules, no rule applies -> shows default permission
        result = await cmd.execute(_ctx("check some_tool /no/match read"))
        assert result.success is True
        rendered = _render(result.content)
        assert "Padrão" in rendered or "padrão" in rendered or "Regra" in rendered

    async def test_enable_already_enabled_shows_no_change(self):
        pm = _fresh_pm()
        rule = _add_test_rule(pm)
        rule.enabled = True
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx(f"enable {rule.id}"))
        assert result.success is True
        rendered = _render(result.content)
        assert "já está" in rendered or "Sem Alteração" in rendered

    async def test_disable_already_disabled_shows_no_change(self):
        pm = _fresh_pm()
        rule = _add_test_rule(pm)
        rule.enabled = False
        cmd = _cmd_with_pm(pm)
        result = await cmd.execute(_ctx(f"disable {rule.id}"))
        assert result.success is True
        rendered = _render(result.content)
        assert "já está" in rendered or "Sem Alteração" in rendered

    async def test_audit_exception_shows_error(self):
        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        with patch("deile.security.audit_logger.get_audit_logger") as mock_al:
            mock_al.side_effect = RuntimeError("audit DB gone")
            result = await cmd.execute(_ctx("audit"))
            assert result.success is True
            rendered = _render(result.content)
            assert "audit DB gone" in rendered or "Erro" in rendered

    async def test_missing_required_arg_err(self):
        from deile.core.exceptions import CommandError

        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        with pytest.raises(CommandError):
            await cmd.execute(_ctx("disable"))

    async def test_unknown_action_raises_command_error(self):
        from deile.core.exceptions import CommandError

        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        with pytest.raises(CommandError):
            await cmd.execute(_ctx("frobnicator"))

    async def test_get_help_string(self):
        pm = _fresh_pm()
        cmd = _cmd_with_pm(pm)
        help_text = cmd.get_help()
        assert isinstance(help_text, str)
