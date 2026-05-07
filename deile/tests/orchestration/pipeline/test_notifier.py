"""Unit tests for DiscordNotifier."""

from __future__ import annotations

from deile.orchestration.pipeline.notifier import DiscordNotifier


class TestNotifierEnabled:
    def test_disabled_when_no_user_id(self, monkeypatch):
        monkeypatch.delenv("DEILE_PIPELINE_NOTIFY_USER_ID", raising=False)
        n = DiscordNotifier()
        assert not n.enabled

    def test_enabled_with_user_id(self):
        n = DiscordNotifier(user_id="123")
        assert n.enabled

    def test_user_id_from_env(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_NOTIFY_USER_ID", "42")
        n = DiscordNotifier()
        assert n.user_id == "42"


class TestNotifierEvents:
    async def test_disabled_notifier_noops(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append((uid, text))
            return {"ok": True}

        n = DiscordNotifier(user_id="", dm_fn=fake_dm)
        await n.issue_picked_up(1, "t", "u")
        assert sent == []

    async def test_issue_picked_up_sends_dm(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append((uid, text))

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        await n.issue_picked_up(7, "title", "https://x")
        assert len(sent) == 1
        assert sent[0][0] == "42"
        assert "#7" in sent[0][1]
        assert "title" in sent[0][1]

    async def test_pr_reviewed_distinguishes_merged(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append(text)

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        await n.pr_reviewed(1, "t", "u", merged=True)
        await n.pr_reviewed(2, "t", "u", merged=False)
        assert "mergeada" in sent[0]
        assert "🟣" in sent[0]
        assert "revisada" in sent[1]
        assert "✅" in sent[1]

    async def test_implementation_finished_handles_missing_pr(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append(text)

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        await n.implementation_finished(1, None)
        await n.implementation_finished(1, "https://github.com/x/y/pull/1")
        assert "sem PR" in sent[0]
        assert "https://github.com/x/y/pull/1" in sent[1]

    async def test_dm_failure_swallowed(self):
        async def failing_dm(uid, text):
            raise RuntimeError("network")

        n = DiscordNotifier(user_id="42", dm_fn=failing_dm)
        # Must not raise.
        await n.issue_picked_up(1, "t", "u")
