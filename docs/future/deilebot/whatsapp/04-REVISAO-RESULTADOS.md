# WhatsApp — Revisão Cética: Resultados (M4b)

## Estado

🟢 **PARTIAL — pronto para live test**.

## O que foi entregue

- `deilebot/providers/whatsapp/{adapter,settings,formatter,normalizer,api_client}.py`
- `WHATSAPP_CAPABILITIES`: react + DM + inline keyboards + voice + window 24h; **sem edit, sem typing, sem profile**.
- `WhatsAppApiClient` httpx-based wrapper de Cloud API v22 `/messages`.
- Adapter webhook-driven: `handle_webhook(payload)` recebe a estrutura `entry[].changes[].value.messages[]` e invoca `on_inbound`.
- `send_template` para mensagens fora da janela 24h (templates aprovados).
- `ConversationWindow.is_open` checa janela 24h via `last_inbound_at`.

## Auditoria

F1-F11 🟢 (incluindo F11 SecretStr para access_token + verify_token).

W3 (ConversationWindow) 🟢, W4 (Template fallback) 🟢 — `OutboundEnvelope.intent=TEMPLATE` cobre.

## Falta

- WebhookServer (FastAPI) que monta `/webhook/whatsapp` e chama `adapter.handle_webhook`. Documentado em master plan §6 — depende de `fastapi+uvicorn` extras.
- Live test via número Business sandbox.
- Templates YAML config (`config/whatsapp_templates.yaml`).

## Próximos passos

- WebhookServer concreto + uvicorn binding em fase 2 do plano whatsapp.
