"""Root conftest for the deile test suite.

Resets the Settings singleton before and after each test so that
monkeypatch.setenv / monkeypatch.delenv changes are always picked up
by modules that call get_settings().

Also redirects the AuditLogger singleton to a per-session tmp directory so
tests that exercise audit-emitting code (e.g. ``set_setting``,
``add_skills_path``) do not pollute ``~/.deile/logs/security_audit.log``
on the developer's HOME (issue #125 reviewer finding).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_settings_singleton():
    from deile.config.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


@pytest.fixture(autouse=True, scope="session")
def _isolate_audit_logger(tmp_path_factory):
    """Point the global ``AuditLogger`` at a session-scoped temp dir.

    Without this, every test that hits ``set_setting`` /
    ``add_skills_path`` / any auditing path appends to
    ``~/.deile/logs/security_audit.log`` on the real HOME — a confirmed
    pollution vector during the issue #125 review (369 SECURITY_POLICY_CHANGED
    entries from a single ``pytest`` run).

    We replace the module-level singleton with one whose ``log_dir`` lives
    under ``tmp_path_factory`` so the events are still emitted (real audit
    paths still execute end-to-end) but the file is destroyed with the
    session.
    """
    from deile.security import audit_logger as audit_module

    log_dir = tmp_path_factory.mktemp("audit_logs")
    isolated = audit_module.AuditLogger(log_dir=str(log_dir))
    saved = audit_module._audit_logger
    audit_module._audit_logger = isolated
    try:
        yield isolated
    finally:
        audit_module._audit_logger = saved


@pytest.fixture
def allow_settings_writes():
    """Install a permissive ``settings_write_default`` rule for the test.

    Issue #125 made the default rule fail-closed (``PermissionLevel.READ``).
    Tests that exercise ``set_setting`` / ``add_skills_path`` / ``set_preference``
    happy paths need a permissive override; tests that exercise denial paths
    construct their own ``MagicMock`` PM instead.

    The fixture snapshots the existing rule, replaces it with a WRITE rule,
    and restores the snapshot on teardown — so the singleton is left clean
    even when tests run standalone. Centralized here (issue #125 follow-up)
    so individual test files do not need to copy-paste the rule definition,
    and so a forgotten cleanup (one polluted singleton) cannot mask a real
    regression in unrelated test files.
    """
    from deile.security import permissions as perm_module
    from deile.security.permissions import (PermissionLevel, PermissionRule,
                                            ResourceType)

    pm = perm_module.get_permission_manager()
    saved = pm.get_rule_by_id("settings_write_default")
    pm.add_rule(
        PermissionRule(
            id="settings_write_default",
            name="Settings Write (Test)",
            description="Test override — allow settings writes.",
            resource_type=ResourceType.FILE,
            resource_pattern=r"^settings:(global|project):.*$",
            tool_names=["settings_manager"],
            permission_level=PermissionLevel.WRITE,
            priority=50,
        )
    )
    try:
        yield pm
    finally:
        if saved is not None:
            pm.add_rule(saved)
        else:
            pm.remove_rule("settings_write_default")
