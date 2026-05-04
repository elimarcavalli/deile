# Fase 1 — Adapter WhatsApp Cloud API + webhook + business setup

## Pré-requisitos

- Conta Meta Business verificada (operação humana — pode levar dias).
- Número WhatsApp Business configurado.
- App Meta criado, Phone Number ID e WABA ID anotados.
- Webhook URL pública (ex.: via ngrok em dev, Cloud Run em prod).
- Token de System User com escopo `whatsapp_business_messaging` + `whatsapp_business_management`.
- Branch: `feat/whatsapp-adapter`.

## Entregáveis

```
deilebot/providers/whatsapp/
├── __init__.py
├── adapter.py
├── normalizer.py
├── formatter.py                 # WhatsApp text style (*bold* _italic_)
├── settings.py
├── webhook_routes.py            # FastAPI router montado em WebhookServer
├── api_client.py                # httpx wrapper sobre Cloud API
├── media.py                     # upload + reference flow
└── tests/
```

### 1.1. Settings

```python
class WhatsAppBotSettings(BaseSettings):
    phone_number_id: str
    waba_id: str
    access_token: SecretStr
    webhook_verify_token: SecretStr
    webhook_path: str = "/whatsapp"
    api_version: str = "v20.0"
    tier: Literal["tier_1", "tier_2", "tier_3", "unlimited"] = "tier_1"
    re_engagement_template: Optional[str] = None
    class Config:
        env_prefix = "DEILE_BOT_WA_"
```

### 1.2. `WhatsAppAdapter`

```python
class WhatsAppAdapter(ProviderAdapter):
    name = "whatsapp"
    capabilities = WHATSAPP_CAPABILITIES

    def __init__(self, settings, on_inbound, webhook_server: WebhookServer):
        self.settings = settings; self.on_inbound = on_inbound
        self.api = WhatsAppApiClient(settings)
        webhook_server.mount_route(settings.webhook_path, self._webhook_handler)

    async def start(self): ...
    async def stop(self): await self.api.close()

    async def send_message(self, channel, text, reply_to=None, attachments=()):
        # 1. Verificar janela: store.get_last_inbound_at(channel) → janela ok?
        # 2. Se ok: api.messages.send_text(...)
        # 3. Se não ok: tentar template re_engagement; se sem template, raise
        ...

    async def send_dm(self, user, text, attachments=()):
        # WhatsApp = todo chat é DM com phone number
        ...
```

### 1.3. `api_client.py`

```python
class WhatsAppApiClient:
    base_url = "https://graph.facebook.com/{version}/{phone_number_id}"
    async def send_text(self, to: str, text: str, reply_to: str | None) -> str: ...
    async def send_template(self, to: str, name: str, language: str, components: list) -> str: ...
    async def send_media(self, to: str, kind: AttachmentKind, media_id: str, caption: str | None) -> str: ...
    async def upload_media(self, kind: AttachmentKind, content: bytes, mime: str) -> str: ...   # retorna media_id
    async def fetch_profile(self, wa_id: str) -> dict: ...
```

### 1.4. Webhook handler

`_webhook_handler(request)`:

- Se `GET` e `?hub.verify_token` correto → responder `hub.challenge` (handshake Meta).
- Se `POST` → parse payload (eventos `messages`, `statuses`); para cada mensagem, normalize + dispatch para `on_inbound`.

### 1.5. Normalizer

Mapeamento Cloud API → `MessageEnvelope`. `from` (E.164) vira `provider_user_id`. `display_name` via campo `contacts[].profile.name`. `text.body` ou tipo (`image`, `audio`, `video`, `document`, `sticker`, `interactive` → reply_button/list_reply).

### 1.6. Formatter

```python
class WhatsAppOutputFormatter(OutputFormatter):
    name = "whatsapp"
    max_message_chars = 4096
    def render(self, ast):
        # PLAIN, BOLD (*x*), ITALIC (_x_), STRIKE (~x~), CODE_INLINE (```x```), CODE_BLOCK (```...```), QUOTE (> x), LINK (texto + url separado), HEADING (negrito), BULLET (• …), NUMBERED (1. …)
        ...
```

### 1.7. Business setup docs

`docs/future/deilebot/whatsapp/SETUP.md`:

- Roteiro passo-a-passo: criar Meta Business, verificar negócio, criar App, ativar produto WhatsApp, adicionar número de teste, gerar System User token, configurar webhook.
- Checklist legal: termos de uso WhatsApp Business, opt-in dos usuários, política de privacidade publicada.
- Custos: tabela de pricing por país.

### 1.8. Testes

- Webhook handshake mock.
- Normalizer para text/image/interactive.
- Formatter golden.
- `send_message` dentro da janela vs fora (mockado).

## Critérios de aceitação

| # | Verificar |
|---|---|
| AC-1 | Webhook handshake passa |
| AC-2 | Inbound text dispara pipeline |
| AC-3 | Outbound dentro da janela funciona |
| AC-4 | Outbound fora da janela cai para template (mock) |
| AC-5 | Setup docs revisado por outra pessoa |

## Estimativa

4 dias.
