# Fase 2 — Instagram Direct adapter

## Pré-requisitos

- Conta Instagram Business vinculada à Page Facebook.
- Webhook subscription `instagram` ativo.
- App Review aprovado para `instagram_manage_messages`.
- Branch: `feat/instagram-adapter`.

## Entregáveis

### 2.1. `instagram/adapter.py`

Estrutura idêntica ao Messenger. Diferenças:

- Endpoint de envio: `POST /{ig_business_account_id}/messages`.
- IDs: usa IGSID (Instagram-Scoped ID).
- `max_message_chars=1000`.
- Sem typing indicator (verificar versão atual).
- Story replies vêm como `message` com `referral.story_id`.

### 2.2. Normalizer

Detecta `entry[].changes[].field == "messages"` e mapeia. Story reply é `MessageEnvelope.reply` com `replied_excerpt` indicando "story".

### 2.3. Formatter

`PlainTextFormatter` com `max_message_chars=1000`.

### 2.4. Tool `reply_to_story`

Tool especializada que o agente DEILE pode invocar para responder a uma story do próprio Page. Argumentos: `story_id`, `text`. Vive em `deile_bot/providers/meta/instagram/tools/`.

### 2.5. Ice-breakers (opcional)

Configurar mensagens de ice-breaker no setup do app — não é runtime, é config no Meta Console. Documentar.

### 2.6. Testes

- Inbound text.
- Inbound story reply.
- Outbound dentro janela.
- Outbound fora janela com human_agent tag (estende para 7 dias, política Meta).

## Critérios

| # | Verificar |
|---|---|
| AC-1 | Bot Instagram responde em conversa de teste |
| AC-2 | Story reply normalizado |
| AC-3 | `reply_to_story` tool funciona |

## Estimativa

2 dias.
