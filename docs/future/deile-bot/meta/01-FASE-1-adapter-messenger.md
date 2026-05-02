# Fase 1 — Messenger adapter + `_common`

## Pré-requisitos

- App Meta + Page + Page Access Token (operação humana).
- Webhook subscriptions configuradas em `messages`, `messaging_postbacks`, `messaging_optins`, `message_reactions`.
- Branch: `feat/messenger-adapter`.

## Entregáveis

### 1.1. `_common/`

- `MetaCommonSettings`: `app_id`, `app_secret`, `verify_token`, `webhook_path` (default `/meta`).
- `webhook_router.py`: dispatcher que olha `entry[].messaging[]` (Messenger) ou `entry[].changes[]` (Instagram) e chama o adapter correspondente.
- `api_client.py`: GET/POST sobre `https://graph.facebook.com/{version}/...`, retry, rate limit.
- `auth.py`: rotaciona Page Access Token; suporta token de longa duração.

### 1.2. `messenger/adapter.py`

```python
class MessengerAdapter(ProviderAdapter):
    name = "messenger"
    capabilities = MESSENGER_CAPABILITIES

    def __init__(self, settings: MessengerBotSettings, on_inbound, webhook_router):
        webhook_router.register_object_handler("page", self._handle_event)

    async def send_message(self, channel, text, reply_to=None, attachments=()):
        # Channel.provider_channel_id = PSID (Page-Scoped User ID) do destinatário
        # Janela 24h respeitada; sem janela → message_tag (config: ACCOUNT_UPDATE | CONFIRMED_EVENT_UPDATE | etc.)
        ...

    async def react(self, channel, message_id, emoji): ...
    async def send_dm(self, user, text, attachments=()): ...   # mesmo que send_message; messenger é tudo DM
    async def fetch_user_profile(self, user) -> Mapping: ...   # GET /{psid}?fields=name,profile_pic
    async def send_typing(self, channel): ...                   # sender_action: typing_on
```

### 1.3. Normalizer

`event["sender"]["id"]` → PSID → `provider_user_id`. `message.text` ou `attachments`. `postback.payload` → mensagem sintética (similar a slash). Quick reply selecionado → mensagem com `payload`.

### 1.4. Formatter

`PlainTextFormatter` (já existe na foundation). Strip de tudo que é markup; preserva quebras de linha; truncamento.

### 1.5. Quick replies + postbacks

`InteractiveButtons` (foundation) → JSON `quick_replies` no payload Messenger. Postbacks chegam como inbound com flag.

### 1.6. Testes

- Webhook handshake.
- Normalizer text/postback/quick_reply.
- send_message dentro/fora janela.
- Quick replies render.

## Critérios

| # | Verificar |
|---|---|
| AC-1 | Webhook recebe e despacha |
| AC-2 | Bot responde em conversa Messenger de teste |
| AC-3 | Quick replies funcionam |
| AC-4 | Janela 24h respeitada |

## Estimativa

4 dias.
