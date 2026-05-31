"""Root conftest for the deile test suite.

Resets the Settings singleton before and after each test so that
monkeypatch.setenv / monkeypatch.delenv changes are always picked up
by modules that call get_settings().

Also redirects the AuditLogger singleton to a per-session tmp directory so
tests that exercise audit-emitting code (e.g. ``set_setting``,
``add_skills_path``) do not pollute ``~/.deile/logs/security_audit.log``
on the developer's HOME (issue #125 reviewer finding).

Issue #432: three additional autouse fixtures prevent ordering-dependent
failures caused by leaked global state (os.environ, logging handlers,
sys.stdio) across tests that do not use monkeypatch for cleanup.
"""
from __future__ import annotations

import logging
import os
import sys

import pytest


@pytest.fixture(autouse=True)
def _snapshot_os_environ():
    """Restore os.environ to its pre-test state after each test.

    Prevents tests that mutate os.environ via direct assignment (not
    monkeypatch) from leaking token variables such as GITHUB_TOKEN /
    GITLAB_TOKEN / GL_TOKEN into subsequent tests, which would suppress the
    expected WARNING in test_warns_when_no_tokens (issue #432).
    """
    saved = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _clean_logging_handlers():
    """Snapshot and restore logger handler/level/propagate state around each test.

    caplog captures records by attaching a handler to the root logger and
    relying on propagation. If a test adds handlers to a named logger, sets
    propagate=False, or changes its effective level without cleanup, subsequent
    tests using caplog.at_level(..., logger=name) receive 0 records even when
    the code does emit — this is the root cause of the 7 TestTickSummary and
    1 test_warns_when_no_tokens ordering failures (issue #432).

    Also restores logging.Manager.disable: deile/cli.py calls logging.disable()
    to suppress output during CLI runs; without this restore, all subsequent
    logging.info() / logging.warning() calls return False from isEnabledFor()
    and are silently dropped — causing deile/tests/log_mgmt/ failures.
    """
    root = logging.root
    root_handlers_before = root.handlers[:]
    root_level_before = root.level
    manager_disable_before = root.manager.disable

    mgr = logging.Logger.manager
    snapshot: dict = {}
    for name, obj in list(mgr.loggerDict.items()):
        if isinstance(obj, logging.Logger):
            snapshot[name] = {
                "handlers": obj.handlers[:],
                "level": obj.level,
                "propagate": obj.propagate,
            }

    yield

    if root.manager.disable != manager_disable_before:
        logging.disable(manager_disable_before)

    root.handlers[:] = root_handlers_before
    root.level = root_level_before

    for name, state in snapshot.items():
        obj = mgr.loggerDict.get(name)
        if isinstance(obj, logging.Logger):
            obj.handlers[:] = state["handlers"]
            obj.level = state["level"]
            obj.propagate = state["propagate"]

    for name, obj in list(mgr.loggerDict.items()):
        if name not in snapshot and isinstance(obj, logging.Logger):
            obj.handlers.clear()
            obj.level = logging.NOTSET
            obj.propagate = True


@pytest.fixture(autouse=True)
def _guard_sys_stdio():
    """Restore sys.stdout/stderr/stdin to their pre-test values after each test.

    SubAgentOrchestrator(capture_output=True) replaces sys.stdout during its
    run and restores it in a finally block. If a test (or earlier fixture)
    replaces sys.stdout without cleanup, test_renderer_task_awaited_before_
    stdout_restore captures the wrong reference as saved_stdout and the
    identity assertion fails (issue #432).
    """
    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    saved_stdin = sys.stdin
    yield
    sys.stdout = saved_stdout
    sys.stderr = saved_stderr
    sys.stdin = saved_stdin


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
