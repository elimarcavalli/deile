"""xfail test for bug #768: CronRunner poll_interval_seconds frozen at import time.

Bug: CronRunner.__init__ uses `cron_poll_interval_seconds()` as a default
argument value, which Python evaluates exactly once when the `def` statement
executes (at module import time). This violates the explicit contract documented
in constants.py:19 which warns against storing the value locally.

Fix: Use None as default, resolve via cron_poll_interval_seconds() in __init__.
Tracker: #768
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 cron-runner-frozen-poll-interval — fix pending tracker #768",
)
def test_poll_interval_reflects_settings_at_instantiation_time(tmp_path) -> None:
    """CronRunner created after a settings change must use the new interval.

    When the bug is present:
      - runner.poll_interval_seconds equals the value frozen at import time (30)
      - The patched value (999) is ignored

    When fixed:
      - runner.poll_interval_seconds equals the value from settings at __init__ time
    """
    # Import the module (which may already be imported — either way, the default
    # arg is frozen to whatever cron_poll_interval_seconds() returned at import).
    import deile.cron.runner as runner_module  # noqa: PLC0415

    frozen_value = runner_module.CronRunner.__init__.__defaults__
    # frozen_value is a tuple; the frozen integer is the poll_interval default.
    # We extract the original to compare against.

    settings_mock = MagicMock()
    settings_mock.cron_poll_interval = 999
    settings_mock.loop_guard_disabled = False
    settings_mock.loop_guard_max_calls = 50
    settings_mock.loop_guard_repeat_threshold = 3
    settings_mock.loop_guard_window_size = 5
    settings_mock.loop_guard_window_threshold = 3
    settings_mock.loop_guard_no_progress = 6

    store = MagicMock()

    with patch("deile.cron.constants.get_settings", return_value=settings_mock), \
         patch("deile.config.settings.get_settings", return_value=settings_mock):
        runner = runner_module.CronRunner(store=store)

    assert runner.poll_interval_seconds == 999, (
        f"Expected poll_interval_seconds=999 (from patched settings), "
        f"got {runner.poll_interval_seconds}. "
        "Default arg was frozen at import time — bug confirmed."
    )
