# Fase 2 — Templates aprovados + ConversationWindow + Interactive Messages

## Entregáveis

### 2.1. `ConversationWindow` na foundation

Extensão da foundation:

```python
@dataclass(frozen=True)
class ConversationWindow:
    last_inbound_at: Optional[datetime]
    window_hours: int                     # 24 para WhatsApp
    @property
    def is_open(self) -> bool: ...

class ConversationStore:
    async def get_window(self, provider: str, user: BotUser, window_hours: int) -> ConversationWindow: ...
```

### 2.2. `OutboundIntent`

```python
class OutboundIntent(str, Enum):
    FREE_TEXT = "free_text"
    TEMPLATE = "template"

@dataclass(frozen=True)
class TemplateMessage:
    name: str
    language: str
    body_params: list[str] = []
    header_params: list[str] = []
    button_params: list[dict] = []

@dataclass(frozen=True)
class OutboundEnvelope:
    intent: OutboundIntent
    text: Optional[str] = None
    template: Optional[TemplateMessage] = None
    interactive: Optional[InteractiveControls] = None
    attachments: tuple[Attachment, ...] = ()
```

`EgressPipeline` ganha consciência: para WhatsApp, antes de send_message, consulta `get_window(...)`. Se aberto → FREE_TEXT. Se fechado → TEMPLATE (com fallback configurado, senão raise + DLQ).

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
