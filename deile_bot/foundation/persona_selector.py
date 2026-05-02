"""PersonaSelector — resolve (env, user, is_owner) -> persona_name."""

from __future__ import annotations

from typing import Any, Mapping

from deile_bot.foundation.envelope import BotUser, MessageEnvelope
from deile_bot.foundation.identity import IdentityResolver
from deile_bot.foundation.settings import BotSettings, PersonaRule


class PersonaSelector:
    """Walk YAML rules in order; first match wins."""

    def __init__(self, settings: BotSettings, identity: IdentityResolver):
        self._settings = settings
        self._identity = identity

    @staticmethod
    def _matches(rule: PersonaRule, env: MessageEnvelope, is_owner: bool) -> bool:
        when: Mapping[str, Any] = rule.when or {}
        prov = when.get("provider")
        if prov and prov != env.channel.provider:
            return False
        scope = when.get("scope")
        if scope and scope != env.channel.scope.value:
            return False
        owner_required = when.get("owner")
        if owner_required is not None and bool(owner_required) != is_owner:
            return False
        names_in = when.get("channel_name_in")
        if names_in:
            if env.channel.name not in set(names_in):
                return False
        return True

    async def resolve(
        self,
        env: MessageEnvelope,
        bot_user: BotUser,
        is_owner: bool,
    ) -> str:
        personas = self._settings.personas
        for rule in personas.rules:
            if self._matches(rule, env, is_owner):
                return rule.use
        return personas.default
