# Fase 2 — Features Telegram-específicas

## Entregáveis

### 2.1. Webhook server (alternativa a polling)

`deile_bot/runtime/webhook_server.py` (criado nesta fase, reusado por WhatsApp/Meta):

```python
class WebhookServer:
    def __init__(self, host="0.0.0.0", port=8443, ssl_cert=..., ssl_key=...):
        self.app = FastAPI()
    def mount_route(self, path: str, handler: Callable[[Request], Awaitable[Response]]): ...
    async def start(self): uvicorn.Server(...)
```

`TelegramAdapter.start_webhook(public_url)` registra `/telegram/<secret>` e chama `bot.set_webhook`.

### 2.2. Inline keyboards (renderer Telegram para `InteractiveControls` da foundation)

> Os tipos `InteractiveControls`, `InteractiveButton`, `InteractiveButtonRow`, `InteractiveList`, `QuickReplies` já vivem na foundation desde a fase 1 (ver `00-MASTER-EXECUTION-PLAN.md` §2.1). Telegram só fornece o **renderer**.

`providers/telegram/formatter.py`:

```python
def render_interactive(controls: InteractiveControls) -> InlineKeyboardMarkup | ReplyKeyboardMarkup | None:
    if isinstance(controls, InteractiveButtonRow):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(b.label, callback_data=b.callback_data, url=b.url)
            for b in controls.buttons
        ]])
    if isinstance(controls, InteractiveList):
        # Telegram não tem List nativa equivalente — degradar para múltiplos rows
        rows = [[InlineKeyboardButton(it.label, callback_data=it.callback_data) for it in sec.items] for sec in controls.sections]
        return InlineKeyboardMarkup(rows)
    if isinstance(controls, QuickReplies):
        # Telegram quick reply = ReplyKeyboardMarkup com one_time_keyboard
        return ReplyKeyboardMarkup([[KeyboardButton(o.label) for o in controls.options]], one_time_keyboard=True)
    return None
```

`adapter.send_message(channel, OutboundEnvelope(intent=FREE_TEXT, text=..., interactive=InteractiveButtonRow(buttons=(...))))`. Outros providers ignoram se `can_inline_keyboards=False`.

Callbacks: handler dedicado para `Update.callback_query`; callback_data normalizado em envelope sintético com `force_respond=True` no `raw`.

### 2.3. Polls

`adapter.send_poll(channel, question, options, anonymous=True)` — feature opcional via `can_polls`.

### 2.4. Deep linking

Suporte a `t.me/<bot>?start=<payload>` chegando como mensagem `/start <payload>`. Útil para onboarding cross-app.

### 2.5. Edit progressivo (streaming)

Debounce mais alto (1.5s) por causa do limite de 30 edits/s — em raridade ultrapassa. Buffer de chunks.

### 2.6. Topics em supergroups

`ChannelScope.THREAD` quando `message.message_thread_id` está populado. `parent_channel_id = chat.id`. Histórico herdado quando o `IngressPipeline` perceber.

### 2.7. Testes

- Webhook server inicia, recebe POST mockado, dispatch correto.
- InlineKeyboard renderiza via Telegram (mock); ignorado em FakeAdapter.
- Poll enviado em chat de teste (E2E).

## Critérios de aceitação

| # | Verificar |
|---|---|
| AC-1 | Webhook funciona em produção (smoke real) |
| AC-2 | Inline keyboards renderizam |
| AC-3 | Polls enviáveis |
| AC-4 | Deep linking captura payload |
| AC-5 | Topics herdam contexto |

## Estimativa

2 dias.
