"""Tests for login validation in ``_has_bot_activity_impl`` (issue #478 finding #4).

Ensures that ``bot_login`` values that would inject into the jq filter are
rejected via ``_GH_LOGIN_RE`` before any subprocess is invoked, and that a
valid login follows the normal execution path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from deile.orchestration.forge.github_forge import GitHubForge


@pytest.fixture
def forge(github_config):
    return GitHubForge(github_config)


# ---------------------------------------------------------------------------
# Malicious / invalid logins are short-circuited before subprocess execution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_login", [
    'x") | true | select(true',      # jq injection
    "",                               # empty string
    "-startwithhyphen",              # invalid GitHub username (leading hyphen)
    "endswithhyphen-",               # invalid GitHub username (trailing hyphen)
    "a" * 40,                        # too long (>39 chars)
    "has space",                     # space not allowed
    "has@symbol",                    # @ not allowed
])
async def test_invalid_login_returns_false_without_subprocess(forge, bad_login):
    """Malicious or invalid logins must return False without calling gh."""
    with patch.object(forge, "_run", new_callable=AsyncMock) as mock_run:
        result = await forge._has_bot_activity_impl("issue", 42, bad_login, 0)
    assert result is False, f"Expected False for bad_login={bad_login!r}"
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Valid login follows the normal path (subprocess IS called)
# ---------------------------------------------------------------------------

async def test_valid_login_calls_subprocess(forge):
    """A well-formed login must reach the subprocess layer."""
    with patch.object(forge, "_run", new_callable=AsyncMock) as mock_run:
        # Return (rc=1, "", "") so the function bottoms out as False
        mock_run.return_value = (1, "", "")
        result = await forge._has_bot_activity_impl("issue", 42, "deile-one", 0)
    assert mock_run.called, "Expected subprocess to be invoked for a valid login"
    assert result is False  # rc=1 → no match


async def test_valid_login_with_activity_returns_true(forge):
    """A login that matches activity timestamps returns True."""
    import time
    since = int(time.time()) - 100  # 100 seconds ago

    async def _run_side_effect(*args, **kwargs):
        # Simulate a comment created NOW (after since_ts)
        import datetime
        now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        return (0, now_iso, "")

    with patch.object(forge, "_run", new_callable=AsyncMock, side_effect=_run_side_effect):
        result = await forge._has_bot_activity_impl("issue", 42, "deile-one", since)
    assert result is True
