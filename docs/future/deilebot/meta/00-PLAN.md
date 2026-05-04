# 00 — Plano completo: `deilebot/providers/meta/`

## 1. Motivação

Messenger e Instagram Direct expandem alcance social. Compartilham backbone Meta (Pages, Graph API, App, Webhooks) — vão num pacote conjunto com 2 sub-adapters.

## 2. Estrutura

```
deilebot/providers/meta/
├── __init__.py
├── _common/
│   ├── webhook_router.py             # /meta endpoint compartilhado
│   ├── api_client.py                 # Graph API base
│   ├── auth.py                       # Page Access Token rotation
│   └── settings.py                   # MetaCommonSettings
├── messenger/
│   ├── adapter.py
│   ├── normalizer.py
│   ├── formatter.py                  # plain text strip
│   └── settings.py                   # MessengerBotSettings
└── instagram/
    ├── adapter.py
    ├── normalizer.py
    ├── formatter.py                  # plain text
    └── settings.py                   # InstagramBotSettings
```

## 3. Restrições

| Restrição | Messenger | Instagram |
|---|---|---|
| Webhook only | ✅ | ✅ |
| Janela de 24h após inbound (sem template) | ✅ (com message tags para extensões) | ✅ (com human_agent tag estendendo p/ 7 dias) |
| Mensagens marketing fora da janela | precisa message tag aprovada | restrito |
| Markup | nenhum (texto puro) | nenhum (texto puro) |
| Reactions | ✅ | parcial (heart) |
| Edit msg | ❌ | ❌ |
| DM | ✅ | ✅ |
| Group | parcial (Workplace) | ❌ |
| Mídia | ✅ (image/audio/video/file) | ✅ (mas restrita) |
| Quick replies / postbacks | ✅ | parcial |
| Stories replies | n/a | ✅ (entrega como mensagem com quote) |

## 4. Capabilities

```python
MESSENGER_CAPABILITIES = ProviderCapabilities(
    can_edit_message=False, can_react=True, can_send_dm=True,
    can_threads=False, can_polls=False, can_inline_keyboards=True,
    can_slash_commands=False, can_voice_messages=True, can_send_typing=True,
    can_fetch_user_profile=True, has_conversation_window=True,
    max_message_chars=2000, max_attachments_per_message=1,
    supported_attachment_kinds=frozenset({IMAGE, VIDEO, AUDIO, FILE}),
)
INSTAGRAM_CAPABILITIES = ProviderCapabilities(
    can_edit_message=False, can_react=True, can_send_dm=True,
    can_threads=False, can_polls=False, can_inline_keyboards=True,
    can_slash_commands=False, can_voice_messages=False, can_send_typing=True,
    can_fetch_user_profile=True, has_conversation_window=True,
    max_message_chars=1000, max_attachments_per_message=1,
    supported_attachment_kinds=frozenset({IMAGE, VIDEO}),
)
```

## 5. Reuso esperado

- `MetaCommonSettings`: app_id, app_secret, verify_token, webhook_path.
- `MetaApiClient`: HTTP wrapper sobre Graph API; subclasses para versão de endpoint específica.
- `webhook_router`: roteia POSTs para handler do adapter certo (campo `object: "page"` → Messenger; `"instagram"` → Instagram).
- Reuso de `ConversationWindow`, `OutboundIntent`, `TemplateMessage` da foundation (já estendida pelo plano WhatsApp).
- Tools `send_dm`, `react_to_message`, `get_user_profile` foundation funcionam.
- Quick replies / postbacks → `InteractiveControls` da foundation (renderer Messenger/Instagram convertem).

## 6. Mapa de fases

| Fase | Conteúdo | Esforço |
|---|---|---|
| 01 | `_common` + Messenger adapter completo | 4 dias |
| 02 | Instagram Direct adapter (reusa `_common`) | 2 dias |
| E2E | Ambos | 2 dias |
| Revisão | Cética | 1 dia |

Total: ~9 dias.

## 7. Critérios

1. Bot Messenger conecta via webhook, recebe mensagens, responde via DEILE.
2. Bot Instagram Direct idem para Business Account.
3. Quick replies / postbacks funcionam.
4. Janela de 24h respeitada; tags configuráveis para extensões.
5. Mesma foundation, mesma pipeline, mesmas tools.

## 8. Pré-requisitos operacionais (humanos)

- App Meta criado com produtos `Messenger` e `Instagram Graph API`.
- Page do Facebook (para Messenger) com Page Access Token de longa duração.
- Conta Instagram Business vinculada à Page.
- Webhook subscriptions: `messages`, `messaging_postbacks`, `messaging_optins`, `message_reactions`, `instagram` (para IG).
- Aprovação App Review do Meta para `pages_messaging`, `instagram_manage_messages` (pode levar 1-3 semanas).
- HTTPS público e webhook URL com `verify_token` configurado.

## 9. Riscos consolidados

| Risco | Prob | Impacto | Mitigação |
|---|---|---|---|
| Page Access Token expirar (60 dias) | alta | alto | Token de longa duração + rotação automática 7 dias antes; tarefa cron na fase 2 |
| App Review rejeitado | média | crítico | Implementar features mínimas funcionais antes de submeter; documentação detalhada de cada permissão pedida |
| Webhook URL muda em deploy → re-verificação Meta | média | médio | Domínio fixo (custom domain Cloud Run / Fly); reverificação como passo de deploy |
| Janela 24h fechada sem human_agent tag → erro | alta | médio | `EgressPipeline` consciente; opt para human_agent tag se aplicável |
| Story replies vêm com `referral.story_id` mas story já expirou | média | baixo | `replied_excerpt` com fallback "story expirada" |
| Rate limit Graph API (varia por uso) | média | médio | RateLimiter + backoff + DLQ |
| Mídia upload precisa de URL pública prévia | alta | baixo | `media.py` faz upload para storage interno + URL temporária |
| Quick replies postback chega como inbound de payload sintético | sempre | baixo | Normalizer trata; envelope tem `force_respond=True` |

## 10. Capability matrix (resumo)

| Capacidade | Messenger | Instagram |
|---|---|---|
| Edit message | ❌ | ❌ |
| React | ✅ (limitado) | parcial (apenas heart) |
| DM | ✅ | ✅ |
| Threads | ❌ | ❌ |
| Polls | ❌ | ❌ |
| Quick replies | ✅ (até 13) | parcial |
| Inline buttons | ✅ (Generic Template) | ❌ |
| Carousel | ✅ | ❌ |
| Stories reply (entrega como mensagem) | n/a | ✅ |
| Janela 24h | ✅ (com tags) | ✅ (com human_agent) |
| Mídia | ✅ (image/audio/video/file) | ✅ (image/video) |
| Voice messages | ✅ | ❌ |
| Typing indicator | ✅ | ✅ |
| Markup nativo | ❌ (texto puro) | ❌ (texto puro) |
