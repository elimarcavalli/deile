# Fase 2 — Pipeline completa + tools de bot

> Plugar adapter Discord na `IngressPipeline`/`EgressPipeline` da foundation. Implementar `formatter` e `normalizer` completos. Migrar `memory.json` legado para SQLite. Criar primeiras tools de bot que o agente DEILE pode invocar (`send_dm`, `get_user_profile`, `react_to_message`, `pin_message`, `start_thread`, `mention_role`).

## Pré-requisitos

- Fase 1 mergeada e bot conecta.
- Foundation fases 1, 2, 3 mergeadas.
- Branch: `feat/discord-pipeline-and-bot-tools`.

## Entregáveis

### 2.1. `normalizer.py` completo

```python
class DiscordNormalizer:
    def to_envelope(self, message: discord.Message) -> MessageEnvelope:
        ...
```

Mapeamentos:

- `provider="discord"`.
- `message_id = str(message.id)`.
- `channel.scope`:
  - `discord.DMChannel` → DM
  - `discord.Thread` → THREAD (com `parent_channel_id`)
  - `discord.TextChannel` em guild → GROUP
  - outros → BROADCAST
- `author = BotUser(provider="discord", provider_user_id=str(message.author.id), display_name=..., is_bot=...)`.
- `attachments`: cada `discord.Attachment` → `Attachment` (kind por content_type, url).
- `reply`: se `message.reference and message.reference.message_id`, popular `ReplyContext`. Tentar `fetch_message`; se falhar, `replied_excerpt = ""` e `replied_author = author dummy`.
- `mentions`: lista de `BotUser` para cada `discord.Member` em `message.mentions`.
- `markup`: `parse_discord_markdown(message.content)` (foundation `markup_ast.py`).
- `raw`: dict reduzido (não inclui o objeto `discord.Message` inteiro, evita serialização de coisas live).

### 2.2. `formatter.py` completo

```python
class DiscordOutputFormatter(OutputFormatter):
    name = "discord"
    max_message_chars = 2000

    def render(self, ast: MarkupAST) -> str:
        ...
```

Renderização (por `SpanKind`):

- `PLAIN` → escapa nada; concatena.
- `BOLD` → `**...**`
- `ITALIC` → `*...*`
- `STRIKE` → `~~...~~`
- `CODE_INLINE` → `` `...` ``
- `CODE_BLOCK` → ` ```{lang}\n...\n``` ` (com language opcional)
- `QUOTE` → `> ...` por linha
- `LINK` → `[texto](url)`
- `HEADING(1)` → `# ...`, etc.
- `BULLET` → `- ...` por item
- `NUMBERED` → `1. ...` por item
- `LINE_BREAK` → `\n`

`split(text)`:

- Se `len(text) <= max`, retorna `[text]`.
- Detecta blocos de código abertos no chunk; nunca corta no meio de um codeblock — abre/fecha pelos limites de chunk.
- Quebra preferencialmente em linhas em branco; depois em `\n`; depois em `. `.
- Cada chunk ≤ `max_message_chars`.

### 2.3. Plugar pipeline

`deile_bot/runtime/single_runtime.py` (criado nesta fase):

```python
class SingleProviderRuntime:
    def __init__(
        self,
        adapter: ProviderAdapter,
        ingress: IngressPipeline,
        ...
    ):
        adapter.on_inbound = self._dispatch

    async def start(self): ...
    async def stop(self): ...
    async def _dispatch(self, env: MessageEnvelope):
        await self.ingress.handle(env, self.adapter)
```

Bootstrap em `cli.py:run`:

```python
async def _run_discord():
    bot_settings = get_bot_settings()
    deile_settings = get_settings()
    config_manager = ConfigManager(); config_manager.load_config()

    # Foundation services
    store = ConversationStore(bot_settings.foundation.sqlite_path); await store.init()
    identity = IdentityResolver(store)
    permissions = PermissionGate(bot_settings, identity)
    rate_limit = RateLimiter(bot_settings)
    intent = build_intent_classifier(bot_settings.foundation)
    audit = BotAuditLogger(store, get_audit_logger())
    metrics = MetricsCollector()
    event_bus = BotEventBus(get_event_bus())
    dlq = DeadLetterQueue(store, bot_settings)

    # Agent (in-process or oneshot)
    bridge = build_agent_bridge(bot_settings.foundation)
    agent_meta = DeileAgentMetaProvider(bridge)
    capability_catalog = CapabilityCatalog(bot_settings)
    persona_selector = PersonaSelector(bot_settings, identity)

    # Formatters
    formatters = {"discord": DiscordOutputFormatter()}
    egress = EgressPipeline(formatters, rate_limit, store, audit, event_bus, metrics, dlq, RetryPolicy.default())

    ingress = IngressPipeline(identity, permissions, rate_limit, store, intent, bridge, capability_catalog, persona_selector, audit, event_bus, metrics, egress, agent_meta)

    # Adapter
    adapter = DiscordAdapter(bot_settings.discord, on_inbound=lambda env: ingress.handle(env, adapter))

    runtime = SingleProviderRuntime(adapter, ingress)
    await runtime.start()
```

### 2.4. Tools de bot

`deile_bot/foundation/tools/` — tools registráveis no `tool_registry` do DEILE quando o bridge é in-process:

```
deile_bot/foundation/tools/
├── __init__.py
├── base.py                      # BotTool base que recebe ctx.extra["bot_context"]
├── send_dm.py
├── get_user_profile.py
├── react_to_message.py
├── pin_message.py
├── start_thread.py
└── mention_role.py
```

**`BotTool` base**:

```python
class BotTool(Tool):
    """Tool que precisa de adapter ativo. Recupera adapter via ctx.extra['bot_context']['adapter_ref']."""

    def _adapter(self, ctx: ToolContext) -> ProviderAdapter:
        bc = ctx.extra.get("bot_context", {})
        adapter = bc.get("adapter_ref")
        if not adapter:
            raise ToolError("This tool requires running within a bot context")
        return adapter
```

**`send_dm`** (exemplo):

```python
@register_tool
class SendDMTool(BotTool):
    name = "send_dm"
    description = "Send a direct message to a user. Requires owner permission."
    schema = ToolSchema(parameters={
        "bot_user_id": SchemaField(type="string", required=True),
        "text": SchemaField(type="string", required=True),
    })

    async def execute(self, args, ctx):
        adapter = self._adapter(ctx)
        # Permission re-check (defense in depth — pipeline já checou, mas tool independente é melhor)
        bc = ctx.extra["bot_context"]
        permissions: PermissionGate = bc["permissions"]
        invoker_user: BotUser = bc["invoker_user"]
        decision = await permissions.check(invoker_user, Action.SEND_DM, scope=ChannelScope.DM)
        if not decision.allowed:
            return ToolResult.failure(reason=decision.reason)

        target = await bc["identity"].by_bot_user_id(args["bot_user_id"])
        if not target: return ToolResult.failure(reason="user_not_found")

        msg_id = await adapter.send_dm(target, args["text"])
        return ToolResult.success(data={"message_id": msg_id})
```

Análogo para `get_user_profile` (chama `adapter.fetch_user_profile`), `react_to_message` (`adapter.react`), `pin_message` (Discord-specific via `discord.Message.pin()`), `start_thread` (Discord-specific), `mention_role` (Discord-specific).

> **Observação:** `pin_message`, `start_thread`, `mention_role` são Discord-specific. A `BotTool` base é foundation; as tools especializadas vivem em `deile_bot/providers/discord/tools/` para não poluir foundation. Já `send_dm` e `get_user_profile` e `react_to_message` são genéricos (todo provider tem) e vivem em `deile_bot/foundation/tools/`.

Registro: tools são registradas no `tool_registry` do DEILE no bootstrap do bridge in-process. Tools só são úteis quando o bot está rodando — fora do bot, `_adapter()` levanta.

### 2.5. Migração `memory.json` → SQLite

`scripts/migrate_memory_json_to_sqlite.py`:

```python
def main():
    parser.add_argument("--source", default="archive/discord_bot_legacy/memory.json")
    parser.add_argument("--target-db", default="data/deile_bot.sqlite")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--guild-name-mapping", help="JSON: guild_name → provider")
    ...

    async def _migrate():
        store = ConversationStore(args.target_db); await store.init()
        with open(args.source) as f: data = json.load(f)
        total = 0; skipped = 0
        for cid, ch in data["channels"].items():
            channel = Channel(provider="discord", provider_channel_id=cid, name=ch["channel_name"], scope=ChannelScope.GROUP)
            if not args.dry_run: await store.upsert_channel(channel)
            for m in ch["messages"]:
                user = BotUser(...); env = MessageEnvelope(...)
                if not args.dry_run:
                    await store.upsert_user(user)
                    await store.record_inbound(env)
                    if m.get("bot_response"):
                        await store.record_outbound(...)
                total += 1
        print(f"Migrated: {total}, skipped: {skipped}")
```

Idempotente (unique constraint de `message` previne dupla inserção em re-run).

### 2.6. Persona Discord

Criar `deile/personas/instructions/discord_developer.md` (ou reusar `developer.md` se cobrir):

- Curta, direta.
- Faz uso de Discord markdown (a foundation cuida da renderização — persona escreve em markdown padrão).
- Sem bloco de "obediência" / "módulo regulador".
- Sem identificação por display_name.
- Pode mencionar: "você roda dentro de um bot Discord, com acesso a tools que afetam mensagens reais".

`PersonaSelector` aponta para `discord_developer` quando provider=discord, scope=DM.

### 2.7. Testes desta fase

| Caso | Cobertura |
|---|---|
| Normalizer DM, GROUP, THREAD | Mapeamento correto |
| Normalizer com reply válido + reply quebrado | Reply popular ou degradar |
| Formatter render para todos SpanKind | Discord markdown correto |
| Formatter split em mensagem 5000 chars com codeblock | Codeblock preservado, splits ≤ 2000 |
| Pipeline E2E com FakeProviderAdapter (não Discord ainda — sem rede) | Inbound → outbound chega no inbox |
| Tools `send_dm`/`get_user_profile`/`react_to_message`: ctx faltando = falha; ctx ok = chamada do adapter mockada |
| Migration: `memory.json` exemplo → SQLite tem N inbound + M outbound |

## Critérios de aceitação

| # | Verificar |
|---|---|
| AC-1 | `pytest deile_bot/providers/discord/tests/` passa |
| AC-2 | `pytest deile_bot/foundation/tools/tests/` passa |
| AC-3 | Smoke real: bot recebe mensagem em DM, responde com fallback "agente desconectado" (bridge ainda não implementado para Discord, ok) |
| AC-4 | Migration script executado em `memory.json` real, contagens batem |
| AC-5 | `ConversationStore` populado, audit log visível, métricas incrementando |
| AC-6 | Persona `discord_developer` carregada, sem bloco de jailbreak |

## Estimativa

4 dias.
