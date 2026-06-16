"""Tests for new DiscordNotifier methods added for issue #164 (gaps 2+4)."""

from __future__ import annotations

from deile.orchestration.pipeline.notifier import DiscordNotifier


class TestPrAutoClassified:
    async def test_sends_dm_with_pr_number(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append((uid, text))

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        await n.pr_auto_classified(7, "My PR title", "https://github.com/o/r/pull/7")
        assert len(sent) == 1
        assert "#7" in sent[0][1] or "7" in sent[0][1]

    async def test_sends_dm_with_title(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append((uid, text))

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        await n.pr_auto_classified(3, "My Feature PR", "https://github.com/o/r/pull/3")
        assert len(sent) == 1
        assert "My Feature PR" in sent[0][1]

    async def test_sends_dm_with_url(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append((uid, text))

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        url = "https://github.com/o/r/pull/5"
        await n.pr_auto_classified(5, "title", url)
        assert url in sent[0][1]

    async def test_noop_when_disabled(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append((uid, text))

        n = DiscordNotifier(user_id="", dm_fn=fake_dm)
        await n.pr_auto_classified(1, "t", "u")
        assert sent == []

    async def test_truncates_long_title(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append(text)

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        long_title = "A" * 200
        await n.pr_auto_classified(1, long_title, "https://github.com/o/r/pull/1")
        assert len(sent) == 1
        # Title is truncated to 100 chars in the message
        assert long_title[:100] in sent[0]
        assert long_title[:101] not in sent[0]


class TestMentionProcessed:
    async def test_sends_dm_with_author(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append((uid, text))

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        await n.mention_processed("https://github.com/o/r/issues/3#c1", "alice")
        assert len(sent) == 1
        assert "alice" in sent[0][1]

    async def test_sends_dm_with_context_url(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append((uid, text))

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        url = "https://github.com/o/r/issues/3#c1"
        await n.mention_processed(url, "alice")
        assert url in sent[0][1]

    async def test_noop_when_disabled(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append((uid, text))

        n = DiscordNotifier(user_id="", dm_fn=fake_dm)
        await n.mention_processed("url", "user")
        assert sent == []

    async def test_dm_failure_swallowed(self):
        async def failing_dm(uid, text):
            raise RuntimeError("network error")

        n = DiscordNotifier(user_id="42", dm_fn=failing_dm)
        # Must not raise
        await n.mention_processed("https://github.com/o/r/issues/1", "bob")
