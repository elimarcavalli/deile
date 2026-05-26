"""Unit tests for DiscordNotifier."""

from __future__ import annotations

from types import SimpleNamespace

from deile.orchestration.pipeline.notifier import DiscordNotifier


def _stub_settings(user_id: str = ""):
    """Return a fake ``get_settings`` callable that exposes a chosen user id.

    Isolates the test from the operator's actual ``~/.deile/settings.json``
    (which may legitimately set ``pipeline.notify_user_id`` since the env-var
    deprecation in issue #111 — env-only stubs are no longer sufficient).
    """
    def _factory():
        return SimpleNamespace(pipeline_notify_user_id=user_id)
    return _factory


class TestNotifierEnabled:
    def test_disabled_when_no_user_id(self, monkeypatch):
        monkeypatch.delenv("DEILE_PIPELINE_NOTIFY_USER_ID", raising=False)
        # Also stub the layered settings (issue #111: notify_user_id now lives
        # in ~/.deile/settings.json, not the env var). Without this stub the
        # test reads the operator's real settings and false-passes/fails
        # depending on their config.
        monkeypatch.setattr(
            "deile.config.settings.get_settings", _stub_settings(""),
        )
        n = DiscordNotifier()
        assert not n.enabled

    def test_enabled_with_user_id(self):
        n = DiscordNotifier(user_id="123")
        assert n.enabled

    def test_user_id_from_env(self, monkeypatch):
        # Notifier reads user_id via get_settings().pipeline_notify_user_id —
        # which IS populated from DEILE_PIPELINE_NOTIFY_USER_ID at boot but
        # only on the singleton's first build. Stub the layered settings
        # directly so the test doesn't depend on import order.
        monkeypatch.setenv("DEILE_PIPELINE_NOTIFY_USER_ID", "42")
        monkeypatch.setattr(
            "deile.config.settings.get_settings", _stub_settings("42"),
        )
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

    async def test_implementation_parked_is_actionable(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append(text)

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        await n.implementation_parked(7, "o agente finalizou sem abrir PR")
        assert len(sent) == 1
        # Mentions the issue, why it parked, where it parked, and how to retry.
        assert "#7" in sent[0]
        assert "sem abrir PR" in sent[0]
        assert "~workflow:em_implementacao" in sent[0]
        assert "~workflow:revisada" in sent[0]

    async def test_implementation_resumed_mentions_attempt(self):
        # Resume feature (issue #254): the DM names the issue + attempt and
        # reassures the operator the partial work is preserved.
        sent = []

        async def fake_dm(uid, text):
            sent.append(text)

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        await n.implementation_resumed(7, 3)
        assert len(sent) == 1
        assert "#7" in sent[0]
        assert "3" in sent[0]
        assert "sem reset" in sent[0]

    async def test_implementation_blocked_is_actionable(self):
        # The block DM names the issue, the reason, the label, and how to unblock.
        sent = []

        async def fake_dm(uid, text):
            sent.append(text)

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        await n.implementation_blocked(9, "falta a credencial X")
        assert len(sent) == 1
        assert "#9" in sent[0]
        assert "falta a credencial X" in sent[0]
        assert "~workflow:bloqueada" in sent[0]

    async def test_dm_failure_swallowed(self):
        async def failing_dm(uid, text):
            raise RuntimeError("network")

        n = DiscordNotifier(user_id="42", dm_fn=failing_dm)
        # Must not raise.
        await n.issue_picked_up(1, "t", "u")
