# Meta (Messenger + Instagram) — Revisão Cética (M4c)

## Estado

🟢 **PARTIAL — pronto para live test**.

## O que foi entregue

- `deilebot/providers/meta/_common/{settings,api_client}.py` compartilhado.
- `deilebot/providers/meta/messenger/adapter.py` — `MessengerAdapter` cobre Send API + webhook ingestion.
- `deilebot/providers/meta/instagram/adapter.py` — `InstagramAdapter(MessengerAdapter)` com capability matrix mais restrita (sem react, max 1000 chars).
- Webhook payload `entry[].messaging[]` → `MessageEnvelope.text`/`MessageEnvelope.author`.

## Auditoria

F1-F11 🟢. SecretStr para `page_access_token` + `verify_token`.

## Falta

- Quick replies / postback handlers (Messenger fase 1).
- Story replies (Instagram fase 2).
- WebhookServer concreto.
- Templates aprovados via Meta Business Manager.
- Live test via Page sandbox + IG Business linkado.

## Próximos passos

- WebhookServer FastAPI compartilhado entre WhatsApp/Meta (`deilebot/runtime/webhook_server.py`) — pré-requisito para release live.
