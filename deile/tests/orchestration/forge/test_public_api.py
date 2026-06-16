"""Surface-area smoke tests for :mod:`deile.orchestration.forge`.

Asserts that every symbol named in the issue #297 acceptance criteria is
importable and that ``__all__`` declares the public API explicitly. A
missing name here means a downstream import would break — caught at
import time so the test fails fast.
"""

from __future__ import annotations

import deile.orchestration.forge as forge


def test_forge_public_api():
    expected = {
        "ForgeClient",
        "ForgeConfig",
        "ForgeKind",
        "ForgeUrl",
        "GitHubForge",
        "GitLabForge",
        "ForgeError",
        "ForgeConfigError",
        "ForgeDetectionError",
        "ForgeCliNotFound",
        "ForgeCommandError",
        "GhCommandError",
        "MergeBlocked",
        "MergeBlockedByPipeline",
        "IssueRef",
        "PrRef",
        "MrRef",
        "CommentRef",
        "MentionTrigger",
        "compute_batch_id_for_number",
        "build_forge",
        "build_forge_config",
        "detect_forge_kind",
        "declared_hosts",
        "discover_cli",
        "parse_forge_url",
        "find_first_pr_url",
        "find_last_pr_url",
        "ForgeRouter",
        "get_forge_router",
    }
    actual = set(forge.__all__)
    missing = expected - actual
    assert not missing, f"forge.__all__ missing: {missing}"


def test_mr_ref_is_pr_ref_alias():
    # Documented in refs.py — MrRef is a pure alias so GitLab-shaped code
    # can read naturally without a behavioural divergence.
    assert forge.MrRef is forge.PrRef


def test_gh_command_error_subclasses_forge_command_error():
    assert issubclass(forge.GhCommandError, forge.ForgeCommandError)


def test_forge_config_error_subclasses_value_error():
    # Backwards-compat guarantee: legacy callers ``except ValueError`` for
    # bad repo strings must keep working after the migration.
    assert issubclass(forge.ForgeConfigError, ValueError)
