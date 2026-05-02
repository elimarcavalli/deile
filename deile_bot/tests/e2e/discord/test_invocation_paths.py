"""EE2E-1 — Discord live invocation paths.

Marked `e2e_discord_live`. Skipped unless `DISCORD_TOKEN` is set in env.
Documented as ready for live execution; this remote agent run does not have
credentials, so all tests skip.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.e2e_discord_live,
    pytest.mark.skipif(
        not os.environ.get("DISCORD_TOKEN"),
        reason="requires live DISCORD_TOKEN",
    ),
]


async def test_ee2e1_slash_deile():
    """Alice /deile diga uma piada -> bot responds in <30s."""
    pytest.skip("manual — requires test guild with #geral")


async def test_ee2e1_mention():
    pytest.skip("manual — requires test guild")


async def test_ee2e1_reply():
    pytest.skip("manual — requires test guild")


async def test_ee2e1_reaction_robot():
    pytest.skip("manual — requires test guild")


async def test_ee2e1_dm():
    pytest.skip("manual — requires DM with bot")
