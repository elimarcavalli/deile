# discord_bot_legacy — archived

This directory holds a placeholder for the legacy `discord_bot/` package that
was prototyped before the foundation existed. **The legacy package was never
checked into this branch's history** (the repository's `main` does not contain
it), so the only thing archived here is this README documenting the migration
contract.

## What used to be here (per docs/future/deilebot/discord/00-PLAN.md)

- `bot.py`, `cogs/`, `memory.py`, `llm_generate.py`, `discord_utils.py`,
  `scheduler_333.py`, `disparar_agora.py`, `demo.py`, `nuke.py`,
  `salve_tiago.py`, `send_dm.py`
- `memory.json` — message history dumped as JSON
- `.settings.json` — bot config

## Migration

- Tokens hardcoded in `send_dm.py` and `salve_tiago.py` were rotated by the
  human operator before this PR. The new bot uses `pydantic.SecretStr` for
  `DISCORD_TOKEN` (see `DiscordBotSettings`).
- `memory.json` migration: see `scripts/migrate_memory_json_to_sqlite.py`.
- Replacement code lives in `deilebot/providers/discord/`.

This README is kept so the directory exists in the tree (per the master plan
mandate to archive rather than delete).
