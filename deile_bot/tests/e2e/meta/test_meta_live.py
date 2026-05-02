"""Meta (Messenger + Instagram) live E2E (ready-skipped)."""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.e2e_meta_live,
    pytest.mark.skipif(
        not os.environ.get("META_PAGE_ACCESS_TOKEN") and not os.environ.get("DEILE_BOT_META_PAGE_ACCESS_TOKEN"),
        reason="requires live META_PAGE_ACCESS_TOKEN",
    ),
]


async def test_me2e_messenger_quick_reply():
    pytest.skip("manual — requires Page + Messenger conversation")


async def test_me2e_messenger_postback():
    pytest.skip("manual")


async def test_ie2e_instagram_story_reply():
    pytest.skip("manual — requires IG Business account linked to Page")
