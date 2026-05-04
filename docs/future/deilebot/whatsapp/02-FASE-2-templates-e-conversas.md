# Fase 2 — Templates aprovados + janela de 24h + Interactive Messages

> **Importante:** os DTOs `ConversationWindow`, `OutboundEnvelope`, `OutboundIntent`, `TemplateMessage`, `InteractiveControls`/`InteractiveList`/`InteractiveButtonRow`/`QuickReplies` já moram na foundation desde a fase 1 (ver `00-MASTER-EXECUTION-PLAN.md` §2.1 e `deilebot/foundation/01-FASE-1-...`). Esta fase **consome** e adiciona suporte específico WhatsApp.

## Entregáveis

### 2.1. `ConversationStore.get_window` (extensão na foundation se ainda não existe)

```python
class ConversationStore:
    async def get_window(self, provider: str, user: BotUser, window_hours: int) -> ConversationWindow:
        last = await self._fetch_last_inbound_at(provider, user)
        return ConversationWindow(last_inbound_at=last, window_hours=window_hours)
```

Se a foundation fase 2 já entregou `_fetch_last_inbound_at` ou similar, reuso. Se não, **adicionar agora à foundation** (PR pequeno separado, lifeline).

### 2.2. `EgressPipeline` consciente da janela

Para providers com `capabilities.has_conversation_window=True`, antes de `adapter.send_message`:

```python
window = await store.get_window("whatsapp", user, window_hours=24)
if window.is_open:
    intent = OutboundIntent.FREE_TEXT
    out = OutboundEnvelope(intent=intent, text=rendered_text, ...)
else:
    template = templates.get(settings.re_engagement_template)  # opt
    if template is None:
        await dlq.enqueue(...); audit.log(OUTBOUND_FAILED, reason="window_closed_no_template"); return
    out = OutboundEnvelope(intent=OutboundIntent.TEMPLATE, template=template.with_params(...))
await adapter.send_outbound(channel, out)
```

> **Nota:** `adapter.send_message(channel, text, ...)` continua existindo como conveniência sobre `send_outbound(channel, OutboundEnvelope(intent=FREE_TEXT, text=text, ...))`. Adapters dever implementar `send_outbound` quando `has_conversation_window=True` ou quando suportam `interactive`/`template`.

### 2.3. Catálogo de templates

`config/whatsapp_templates.yaml`:

```yaml
templates:
  - name: re_engagement_default_pt_br
    language: pt_BR
    category: marketing
    body: "Olá! Está tudo bem? Você falou conosco há {{1}} dias. Posso ajudar em algo?"
    body_params: ["dias_desde_ultima_msg"]
  - name: welcome_pt_br
    language: pt_BR
    category: utility
    body: "Bem-vindo ao DEILE!"
```

Loader: `WhatsAppTemplateCatalog.load(path)`. Disponível para tools (ex.: `send_template_message`).

### 2.4. Interactive Messages

```python
class InteractiveList(InteractiveControls):
    button: str
    sections: list[InteractiveListSection]

class InteractiveButtons(InteractiveControls):
    buttons: list[InteractiveReplyButton]   # max 3
```

Formatter WhatsApp converte para o JSON correto da Cloud API.

### 2.5. Tool nova: `send_template_message`

```python
@register_tool
class SendTemplateMessageTool(BotTool):
    name = "send_template_message"
    description = "Send a pre-approved WhatsApp template message to a user"
    schema = ToolSchema(parameters={
        "bot_user_id": SchemaField(type="string", required=True),
        "template_name": SchemaField(type="string", required=True),
        "params": SchemaField(type="array", items={"type": "string"}, required=False),
    })
```

### 2.6. Métrica de conversação

`bot_whatsapp_conversations_total{type=marketing|utility|authentication|service}` — incrementada quando uma janela nova é aberta (primeira mensagem em > 24h).

### 2.7. Testes

- Window aberta → FREE_TEXT enviado, conversação **service** (initiated by user).
- Window fechada + template configurado → TEMPLATE enviado, conversação **utility** ou **marketing** dependendo do template.
- Window fechada + sem template → erro graceful + DLQ.
- InteractiveList renderizado corretamente.

## Critérios

| # | Verificar |
|---|---|
| AC-1 | ConversationWindow funciona |
| AC-2 | Template fallback automático |
| AC-3 | Catálogo de templates carregado de YAML |
| AC-4 | Interactive messages testados |
| AC-5 | Métrica de conversações por tipo |

## Estimativa

3 dias.
