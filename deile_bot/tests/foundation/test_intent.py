"""Tests for IntentClassifier implementations."""

from __future__ import annotations

from types import MappingProxyType

from deile_bot._testing import make_channel, make_envelope, make_user
from deile_bot.foundation.envelope import ChannelScope, ReplyContext
from deile_bot.foundation.intent import (AlwaysRespond,
                                         AlwaysRespondToAddressed,
                                         HeuristicIntentClassifier,
                                         LLMIntentClassifier,
                                         build_intent_classifier)
from deile_bot.foundation.settings import FoundationSettings

SELF_ID = "bot-self"


def _bot_user():
    return make_user(provider_user_id=SELF_ID, display_name="DEILE", is_bot=True)


class TestHeuristic:
    async def test_dm_responds(self):
        c = HeuristicIntentClassifier()
        env = make_envelope(channel=make_channel(scope=ChannelScope.DM))
        d = await c.decide(env, [], SELF_ID)
        assert d.should_respond and d.reason == "dm"

    async def test_mention_responds(self):
        c = HeuristicIntentClassifier()
        env = make_envelope(
            channel=make_channel(scope=ChannelScope.GROUP),
            mentions=(_bot_user(),),
            text="hello bot",
        )
        d = await c.decide(env, [], SELF_ID)
        assert d.should_respond and d.reason == "mention"

    async def test_reply_to_bot_responds(self):
        c = HeuristicIntentClassifier()
        bot = _bot_user()
        env = make_envelope(
            channel=make_channel(scope=ChannelScope.GROUP),
            reply=ReplyContext(
                replied_message_id="b1", replied_author=bot, replied_excerpt="hi"
            ),
            text="thanks bro",
        )
        d = await c.decide(env, [], SELF_ID)
        assert d.should_respond and d.reason == "reply_to_bot"

    async def test_short_message_skipped(self):
        c = HeuristicIntentClassifier(min_chars=4)
        env = make_envelope(channel=make_channel(scope=ChannelScope.GROUP), text="ok")
        d = await c.decide(env, [], SELF_ID)
        assert not d.should_respond and d.reason == "too_short"

    async def test_command_prefix_skipped(self):
        c = HeuristicIntentClassifier(command_prefix="d!")
        env = make_envelope(channel=make_channel(scope=ChannelScope.GROUP), text="d!ping")
        d = await c.decide(env, [], SELF_ID)
        assert not d.should_respond and d.reason == "command_prefix"

    async def test_force_respond_first(self):
        c = HeuristicIntentClassifier()
        env = make_envelope(
            channel=make_channel(scope=ChannelScope.GROUP),
            text="ok",
            raw=MappingProxyType({"force_respond": True}),
        )
        d = await c.decide(env, [], SELF_ID)
        assert d.should_respond and d.reason == "force_respond"

    async def test_noise_skipped(self):
        c = HeuristicIntentClassifier()
        env = make_envelope(
            channel=make_channel(scope=ChannelScope.GROUP),
            text="random text in a public channel",
        )
        d = await c.decide(env, [], SELF_ID)
        assert not d.should_respond and d.reason == "noise"


class TestAlwaysRespondToAddressed:
    async def test_dm(self):
        c = AlwaysRespondToAddressed()
        env = make_envelope(channel=make_channel(scope=ChannelScope.DM))
        d = await c.decide(env, [], SELF_ID)
        assert d.should_respond

    async def test_group_silence(self):
        c = AlwaysRespondToAddressed()
        env = make_envelope(channel=make_channel(scope=ChannelScope.GROUP))
        d = await c.decide(env, [], SELF_ID)
        assert not d.should_respond


class TestAlwaysRespond:
    async def test_always(self):
        c = AlwaysRespond()
        env = make_envelope(channel=make_channel(scope=ChannelScope.GROUP), text="x")
        d = await c.decide(env, [], SELF_ID)
        assert d.should_respond


class TestLLMClassifier:
    async def test_dm_short_circuits(self):
        c = LLMIntentClassifier()
        env = make_envelope(channel=make_channel(scope=ChannelScope.DM))
        d = await c.decide(env, [], SELF_ID)
        assert d.should_respond and d.reason == "addressed"

    async def test_no_invoker_returns_false(self):
        c = LLMIntentClassifier()
        env = make_envelope(channel=make_channel(scope=ChannelScope.GROUP), text="hmm")
        d = await c.decide(env, [], SELF_ID)
        assert not d.should_respond

    async def test_with_invoker(self):
        class Invoker:
            async def ask(self, text):
                return True

        c = LLMIntentClassifier(invoker=Invoker())
        env = make_envelope(channel=make_channel(scope=ChannelScope.GROUP), text="hmm")
        d = await c.decide(env, [], SELF_ID)
        assert d.should_respond and d.reason == "llm"


class TestBuilder:
    def test_builds_each(self):
        for name, cls in [
            ("heuristic", HeuristicIntentClassifier),
            ("llm", LLMIntentClassifier),
            ("always_respond_to_addressed", AlwaysRespondToAddressed),
            ("always_respond", AlwaysRespond),
        ]:
            c = build_intent_classifier(FoundationSettings(intent_classifier=name))
            assert isinstance(c, cls)
