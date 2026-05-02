# Fase 1 — Adapter Telegram (polling + comandos básicos)

## Pré-requisitos

- Foundation completa.
- Discord adapter de referência (padrão a seguir).
- Bot Telegram criado via `@BotFather`; token em `.env`.
- Branch: `feat/telegram-adapter`.

## Entregáveis

```
deile_bot/providers/telegram/
├── __init__.py
├── adapter.py                     # TelegramAdapter
├── normalizer.py                  # Update → MessageEnvelope
├── formatter.py                   # MarkupAST → MarkdownV2
├── settings.py
├── commands.py                    # BotCommands sync
├── handlers.py                    # message_handler, command_handler
└── tests/
```

### 1.1. `TelegramAdapter`

```python
class TelegramAdapter(ProviderAdapter):
    name = "telegram"
    capabilities = TELEGRAM_CAPABILITIES

    def __init__(self, settings: TelegramBotSettings, on_inbound):
        self.settings = settings; self.on_inbound = on_inbound
        self._app: Application = ...

    async def start(self):
        self._app = Application.builder().token(self.settings.token).build()
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        for cmd in BUILTIN_COMMANDS:
            self._app.add_handler(CommandHandler(cmd.name, cmd.handler))
        await self._app.bot.set_my_commands([(c.name, c.description) for c in BUILTIN_COMMANDS])
        await self._app.initialize(); await self._app.start(); await self._app.updater.start_polling()

    async def stop(self):
        await self._app.updater.stop(); await self._app.stop(); await self._app.shutdown()

    async def send_message(self, channel, text, reply_to=None, attachments=()):
        ...

    async def edit_message(self, channel, message_id, new_text): ...
    async def react(self, channel, message_id, emoji): ...
    async def send_dm(self, user, text, attachments=()): ...
    async def fetch_user_profile(self, user) -> Mapping: ...
    async def send_typing(self, channel): ...
```

### 1.2. Normalizer

Mapeamento direto. `chat.type` → `ChannelScope`. `message.reply_to_message` → `ReplyContext`. `message.entities` para mentions.

### 1.3. Formatter

`MarkdownV2` com `escape_markdown_v2` aplicado em todo `PLAIN`. Testes golden cobrindo bold/italic/strike/code/code_block/quote/link/list.

### 1.4. BotCommands

```python
BUILTIN_COMMANDS = [
    Cmd("start", "Iniciar conversa com DEILE"),
    Cmd("help", "Listar comandos"),
    Cmd("deile", "Pergunta direta ao agente"),
    Cmd("capabilities", "Capacidades do bot"),
    Cmd("forget", "Esquecer histórico (owner)"),
]
```

### 1.5. Testes

Mock de `Update`; `normalizer` produz envelope correto; formatter golden; `start()` registra handlers (verificável); smoke manual com bot real.

## Critérios de aceitação

| # | Verificar |
|---|---|
| AC-1 | Bot Telegram conecta via polling |
| AC-2 | `/deile <prompt>` invoca o pipeline e responde |
| AC-3 | BotCommands aparecem no auto-complete |
| AC-4 | Streaming via edit funciona |

## Estimativa

3 dias.
