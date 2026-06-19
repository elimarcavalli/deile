"""xfail test for bug #768: _shell_security.py misses rm -fr flag order.

Bug: DANGEROUS_PATTERNS only matches `rm -rf` (r and f in that exact order).
The equivalent `rm -fr` (f before r) is not matched by any pattern — not
DANGEROUS, not even MODERATE. assess_risk('rm -fr /') returns ('safe', []),
allowing it through the risk gate at bash_tool.py:369.

Fix: r'rm\\s+.*-(rf|fr)\\s*/' in DANGEROUS_PATTERNS;
     r'rm\\s+.*-[a-z]*r' in MODERATE_PATTERNS.
Tracker: #768
"""

from __future__ import annotations

import pytest

from deile.tools._shell_security import assess_risk
from deile.tools.base import SecurityLevel


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 shell-security-rm-fr — fix pending tracker #768",
)
def test_rm_fr_slash_is_dangerous() -> None:
    """rm -fr / must be classified as DANGEROUS, not safe.

    When the bug is present:
      - assess_risk('rm -fr /') returns ('safe', [])
      - assertion below FAILS (level != 'dangerous') -> xfail -> green in suite

    When fixed:
      - assess_risk('rm -fr /') returns ('dangerous', [...])
      - assertion PASSES -> xpass -> strict=True marks as FAILED (guard regression)
    """
    level, _warnings = assess_risk("rm -fr /")
    assert level == SecurityLevel.DANGEROUS.value, (
        f"rm -fr / was classified as {level!r} — must be 'dangerous'."
    )


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 shell-security-rm-fr-home — fix pending tracker #768",
)
def test_rm_fr_home_is_dangerous() -> None:
    """rm -fr /home must be classified as DANGEROUS."""
    level, _warnings = assess_risk("rm -fr /home")
    assert level == SecurityLevel.DANGEROUS.value, (
        f"rm -fr /home was classified as {level!r} — must be 'dangerous'."
    )


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 shell-security-rm-fr-moderate — fix pending tracker #768",
)
def test_rm_fr_path_is_at_least_moderate() -> None:
    """rm -fr <any path> must be classified as at least MODERATE."""
    level, _warnings = assess_risk("rm -fr mydir")
    assert level != SecurityLevel.SAFE.value, (
        f"rm -fr mydir was classified as {level!r} — must be at least 'moderate'."
    )
