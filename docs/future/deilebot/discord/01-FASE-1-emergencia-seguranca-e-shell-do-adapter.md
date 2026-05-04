# Fase 1 — Emergência de segurança + shell do adapter Discord

> **Para esta fase, ROTACIONAR PRIMEIRO os tokens de bot do Discord** (ação humana, fora do código). Só depois mexer no repositório. As credenciais hardcoded em `discord_bot/send_dm.py` e `discord_bot/salve_tiago.py` estão expostas no histórico do git e devem ser consideradas comprometidas.

## Pré-requisitos

- Foundation fase 1 mergeada (DTOs, ABC, settings).
- Operador humano:
  1. Acessou o Discord Developer Portal.
  2. Para cada bot afetado: **Reset Token** (revoga as anteriores).
  3. Adicionou o token novo em `.env` (campo `DISCORD_TOKEN`).
  4. Confirmou que nenhum push novo referencia tokens em texto plano.
- Branch: `feat/discord-emergency-and-shell`.

## Entregáveis

### 1.1. Arquivar `discord_bot/` legado

Em PR único:

```
discord_bot/  →  archive/discord_bot_legacy/
```

Comandos sugeridos (operador roda):

```bash
git mv discord_bot archive/discord_bot_legacy
echo "/archive/" >> .gitignore   # opcional, se quiser parar de versionar daqui pra frente
```

Adicionar `archive/discord_bot_legacy/README.md` explicando: pacote legado, não usar em produção, mantido apenas como referência histórica até remoção definitiva na fase 4.

### 1.2. Esqueleto do novo adapter

```
deilebot/
└── providers/
    └── discord/
        ├── __init__.py
        ├── adapter.py                 # DiscordAdapter(ProviderAdapter)
        ├── normalizer.py              # discord.Message → MessageEnvelope (ou stub)
        ├── formatter.py               # MarkupAST → Discord markdown (stub na fase 1)
        ├── settings.py                # DiscordBotSettings
        ├── intents.py                 # build_intents(settings)
        ├── cogs/
        │   ├── __init__.py
        │   ├── help_cog.py            # /help, d!help auto-gerado
        │   └── ping_cog.py            # /ping, d!ping (sanity check)
        └── tests/
            ├── __init__.py
            ├── test_adapter.py
            ├── test_normalizer.py
            ├── test_formatter.py
            └── test_help_cog.py
```

### 1.3. `DiscordAdapter`

```python
class DiscordAdapter(ProviderAdapter):
    name = "discord"
    capabilities = DISCORD_CAPABILITIES   # do 00-PLAN.md §4

    def __init__(self, settings: DiscordBotSettings, on_inbound: InboundCallback):
        self.settings = settings
        self.on_inbound = on_inbound
        self._client: Optional[discord.Client] = None
        self._self_user_id: Optional[str] = None

    @property
    def self_user_id(self) -> str: ...

    async def start(self) -> None:
        intents = build_intents(self.settings)
        self._client = DeileDiscordClient(intents=intents, adapter=self)
        await self._client.login(self.settings.token)
        await self._client.connect(reconnect=True)

    async def stop(self) -> None:
        if self._client:
            await self._client.close()

    async def send_message(self, channel: Channel, text: str, reply_to: Optional[str] = None, attachments: Sequence[Attachment] = ()) -> str:
        ch = self._client.get_channel(int(channel.provider_channel_id))
        ref = discord.MessageReference(message_id=int(reply_to), channel_id=ch.id) if reply_to else None
        files = [...]  # mapeia Attachment → discord.File
        msg = await ch.send(content=text, reference=ref, files=files, mention_author=False)
        return str(msg.id)

    async def edit_message(self, channel: Channel, message_id: str, new_text: str) -> None: ...
    async def react(self, channel: Channel, message_id: str, emoji: str) -> None: ...
    async def send_dm(self, user: BotUser, text: str, attachments: Sequence[Attachment] = ()) -> str: ...
    async def fetch_user_profile(self, user: BotUser) -> Mapping[str, Any]: ...
    async def send_typing(self, channel: Channel) -> None: ...
```

`DeileDiscordClient(discord.Client)`:

- `setup_hook`: carrega cogs (`help`, `ping`); `await self.tree.sync(...)` (com guild ids da settings se houver).
- `on_ready`: log estruturado; `change_presence(activity=Listening("DEILE"))`.
- `on_message`: chama `self.adapter._handle_inbound(message)` que constrói envelope e chama `self.adapter.on_inbound(env)`.

### 1.4. `DiscordBotSettings`

```python
class DiscordBotSettings(BaseSettings):
    token: SecretStr                                 # DISCORD_TOKEN (env)
    intents_message_content: bool = True
    intents_members: bool = True
    intents_presences: bool = False
    command_prefix: str = "d!"
    slash_sync_guild_ids: list[int] = []
    reaction_trigger_emoji: str = "🤖"
    message_edit_debounce_ms: int = 800
    on_member_join_enabled: bool = True

    class Config:
        env_prefix = "DEILE_BOT_DISCORD_"
        env_file = ".env"
```

### 1.5. Help cog auto-gerado (`help_cog.py`)

```python
class HelpCog(commands.Cog):
    @commands.hybrid_command(name="help", description="Lista comandos do bot")
    async def help(self, ctx: commands.Context):
        slash = self.bot.tree.get_commands()
        prefix = [c for c in self.bot.commands if c.name != "help"]
        embed = discord.Embed(title="📚 DEILE — Comandos", color=...)
        if slash:
            embed.add_field(name="Slash", value="\n".join(f"/{c.name} — {c.description}" for c in slash), inline=False)
        if prefix:
            embed.add_field(name="Prefixo (d!)", value="\n".join(f"d!{c.name} — {c.help or c.description or ''}" for c in prefix), inline=False)
        embed.set_footer(text="Mais detalhes via /capabilities")
        await ctx.send(embed=embed)
```

`/capabilities` (versão completa) entra na fase 3.

### 1.6. Ping cog (sanity check)

```python
class PingCog(commands.Cog):
    @commands.hybrid_command(name="ping", description="Latência do bot")
    async def ping(self, ctx):
        ws = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 `{ws}ms`")
```

### 1.7. CLI mínimo

`deilebot/cli.py`:

```python
def main():
    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(_run(args.provider))

async def _run(provider: str):
    settings = get_bot_settings()
    if provider == "discord":
        from deilebot.providers.discord.adapter import DiscordAdapter
        adapter = DiscordAdapter(settings.discord, on_inbound=_simple_echo_inbound)  # provisório
        await adapter.start()
```

Provisório: `_simple_echo_inbound` apenas loga o envelope. Pipeline completa entra na fase 2.

### 1.8. Endereçar S1-S4 da auditoria

| Vulnerabilidade | Ação nesta fase |
|---|---|
| **S1 — token hardcoded em send_dm.py** | Arquivo arquivado em `archive/`. Token revogado pelo operador. |
| **S2 — token hardcoded em salve_tiago.py** | Idem. |
| **S3 — privilégio por display_name** | Novo adapter usa `user.id` exclusivamente; `BotSettings.permissions.owners` é lista de `bot_user_id` (resolvidos via foundation). |
| **S4 — jailbreak set_modulo_regulador no system prompt** | Persona `developer.md` não contém esse bloco. Persona é Markdown, sem código. Auditar antes de merge. |

### 1.9. Testes desta fase

- `test_adapter`: `DiscordAdapter()` instancia, `start()` falha graciosamente sem token (com erro claro), `capabilities` corretas.
- `test_normalizer`: `discord.Message` mockado → `MessageEnvelope` com campos certos (DM detectada, mention detectada, reply detectado).
- `test_formatter`: stub funcional para texto plano (renderização rica vai na fase 2).
- `test_help_cog`: `help` produz embed com pelo menos `/ping` e `/help` listados.
- Smoke local manual: `python3 -m deilebot.cli run --provider discord` com `.env` contendo `DISCORD_TOKEN` real → bot conecta, `/help` responde, `/ping` responde.

## Critérios de aceitação

| # | Como verificar |
|---|---|
| AC-1 | `archive/discord_bot_legacy/` existe; `discord_bot/` não |
| AC-2 | Tokens revogados (confirmação humana no PR) |
| AC-3 | `pytest deilebot/providers/discord/tests/ -v` passa |
| AC-4 | Bot conecta ao Discord (smoke manual) |
| AC-5 | `/help` e `/ping` (slash) funcionam |
| AC-6 | `d!help` e `d!ping` (prefix) funcionam |
| AC-7 | `git grep -E "MTk|MTQ|MTM|MTI|MTA" archive/` não acha tokens em outros arquivos não esperados |

## Pontos de atenção

- **Não habilitar pipeline ainda.** Fase 1 é shell vivo + sanity. Pipeline completa é fase 2.
- **Slash sync**: usar `self.tree.sync(guild=guild)` em dev para feedback rápido (≤ 5s). Sync global pode demorar ~1h para propagar (limite do Discord).
- **`message_content` intent é privilegiado** em bots > 100 servidores. Documentar no README.
- **Reaction-trigger** ainda não implementado nesta fase — só na 3.

## Estimativa

2 dias.
