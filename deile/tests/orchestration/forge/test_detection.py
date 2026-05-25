"""Tests for :func:`deile.orchestration.forge.detect_forge_kind` and friends."""

from __future__ import annotations

import pytest

from deile.orchestration.forge import (ForgeDetectionError, ForgeKind,
                                       declared_hosts, detect_forge_kind)


def test_detect_explicit_override_wins_github():
    # Even with a GitLab URL, the explicit override decides.
    kind = detect_forge_kind(
        url="https://gitlab.com/g/p/-/issues/1",
        env={"DEILE_FORGE_KIND": "github"},
    )
    assert kind is ForgeKind.GITHUB


def test_detect_explicit_override_wins_gitlab():
    kind = detect_forge_kind(
        url="https://github.com/o/r/pull/1",
        env={"DEILE_FORGE_KIND": "gitlab"},
    )
    assert kind is ForgeKind.GITLAB


def test_detect_from_url_github_cloud():
    assert detect_forge_kind(url="https://github.com/o/r", env={}) is ForgeKind.GITHUB


def test_detect_from_url_gitlab_cloud():
    assert detect_forge_kind(url="https://gitlab.com/g/p", env={}) is ForgeKind.GITLAB


def test_detect_from_env_custom_github_host():
    env = {"DEILE_GITHUB_HOST": "ghe.empresa.com"}
    assert detect_forge_kind(
        url="https://ghe.empresa.com/team/svc/pull/1",
        env=env,
    ) is ForgeKind.GITHUB


def test_detect_from_env_custom_gitlab_host():
    env = {"DEILE_GITLAB_HOST": "gitlab.empresa.com"}
    assert detect_forge_kind(
        url="https://gitlab.empresa.com/team/svc/-/issues/1",
        env=env,
    ) is ForgeKind.GITLAB


def test_detect_nested_path_resolves_to_gitlab():
    """A 3-segment project path can ONLY be GitLab — unambiguous."""
    assert detect_forge_kind(project_path="g/sub/proj", env={}) is ForgeKind.GITLAB


def test_detect_two_segment_path_defaults_to_github_for_compat():
    """A 2-segment path is ambiguous in principle; defaults to GH to preserve
    the historical behaviour (every pre-#297 deployment was GitHub)."""
    assert detect_forge_kind(project_path="owner/repo", env={}) is ForgeKind.GITHUB


def test_detect_no_signals_fails_fast():
    with pytest.raises(ForgeDetectionError) as exc_info:
        detect_forge_kind(env={})
    msg = str(exc_info.value)
    # Error message MUST name the env vars the operator can set — it is the
    # only way the operator escapes the failure mode.
    assert "DEILE_FORGE_KIND" in msg
    assert "DEILE_GITHUB_HOST" in msg or "DEILE_GITLAB_HOST" in msg


def test_detect_unknown_explicit_kind_raises():
    """An explicit but invalid kind raises a forge-layer error. The exact
    subclass (``ForgeConfigError`` or ``ForgeDetectionError``) is an
    implementation detail; both subclass :class:`ForgeError`."""
    from deile.orchestration.forge import ForgeError
    with pytest.raises(ForgeError):
        detect_forge_kind(env={"DEILE_FORGE_KIND": "bitbucket"})


def test_detect_auto_value_treated_as_unset():
    # The CLI default is ``auto`` (declared in settings); detection must
    # honour it as "use the heuristic" rather than parse it as a kind.
    assert detect_forge_kind(
        url="https://github.com/o/r", env={"DEILE_FORGE_KIND": "auto"},
    ) is ForgeKind.GITHUB


def test_declared_hosts_default():
    assert declared_hosts({"DEILE_GITHUB_HOST": "", "DEILE_GITLAB_HOST": ""}) == {
        "github_hosts": (),
        "gitlab_hosts": (),
    }


def test_declared_hosts_csv():
    result = declared_hosts({
        "DEILE_GITHUB_HOST": "ghe-a.empresa.com, ghe-b.empresa.com",
        "DEILE_GITLAB_HOST": "gitlab.empresa.com",
    })
    assert "ghe-a.empresa.com" in result["github_hosts"]
    assert "ghe-b.empresa.com" in result["github_hosts"]
    assert result["gitlab_hosts"] == ("gitlab.empresa.com",)
