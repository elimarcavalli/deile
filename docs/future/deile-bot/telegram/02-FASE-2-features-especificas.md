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

### 2.2. Inline keyboards

`InlineKeyboardBuilder` na foundation (já que outros providers também têm conceitos similares — Telegram, WhatsApp interactive, Messenger quick_replies):

```python
class InteractiveControl: ...   # ABC
class InlineButton(InteractiveControl): label, callback_data, url=None
class QuickReply(InteractiveControl): label, payload
```

`adapter.send_message(..., interactive=InlineKeyboard(rows=[[btn1, btn2], ...]))`.

Telegram render: `InlineKeyboardMarkup`. Outros providers ignoram se `can_inline_keyboards=False`.

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
