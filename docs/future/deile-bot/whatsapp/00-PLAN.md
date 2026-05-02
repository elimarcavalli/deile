# 00 — Plano completo: `deile_bot/providers/whatsapp/`

## 1. Motivação

WhatsApp é o canal #1 no Brasil. Ter DEILE acessível por WhatsApp expande o alcance massivamente. **Mas** o WhatsApp Cloud API tem restrições que **forçam mudanças na foundation** — esta é a primeira validação de quão bem a foundation lida com providers "diferentes".

## 2. Restrições críticas do WhatsApp Cloud API

| Restrição | Impacto | Tratamento |
|---|---|---|
| **Webhook only** (sem polling) | Precisa servidor HTTP público (HTTPS, certificado) | `WebhookServer` da fase 2 do Telegram, reusado |
| **Janela de 24h**: depois de 24h sem o usuário falar, só pode enviar **template aprovado** | Foundation precisa de `ConversationWindow` capability + `TemplateMessage` abstração | Fase 2 deste plano + extensão da foundation |
| **Templates aprovados pelo Meta** (cada um demora dias para aprovar) | Não pode improvisar mensagens proativas | Catálogo de templates em `config/whatsapp_templates.yaml`, cada com nome, idioma, parâmetros |
| **Business verification** (precisa CNPJ, conta verificada Meta Business) | Bloqueio operacional | Documentar; sem ela, só sandbox |
| **Rate limit por tier**: tier 1 = 1k conversas únicas/dia, tier 2 = 10k, tier 3 = 100k | RateLimiter dedicado | Configurável por tier |
| **Não tem reactions a partir do bot** (até a versão atual) | `can_react=False` | Foundation degrada gracefully |
| **Não pode editar mensagens** | `can_edit_message=False` — streaming via edit é proibido | Strategy alternativa: enviar mensagem completa após streaming acumulado |
| **Custo por conversa** (não por mensagem) — primeiras 1000/mês free, depois pay | Métrica financeira obrigatória | `bot_whatsapp_conversations_total` para projeção |
| **Mídia**: precisa upload prévio (`POST /media`) e referência por ID | Adapter precisa fluxo de upload | Implementar |
| **Encryption end-to-end** (cliente) — não muda API mas mensagens passam por servidor Meta | Compliance/privacidade | Documentar |

## 3. Decisões

| # | Decisão | Motivo |
|---|---|---|
| W1 | Lib: `httpx` direto sobre WhatsApp Cloud API REST (sem SDK Python oficial estável) | Flexibilidade |
| W2 | Webhook em `/whatsapp/<verify_token>` validado por desafio inicial do Meta | Padrão Meta |
| W3 | `ConversationWindow` na foundation: sub-objeto em `Channel` ou em `BotUser`. Calculado por `last_inbound_at` por usuário | Permitir checagem antes de mandar mensagem livre |
| W4 | Tentar mensagem livre; se rejeitada (erro `131047 — Re-engagement message`), fallback automático para template `re_engagement_default_<idioma>` se configurado | UX que não trava |
| W5 | Catálogo de templates em YAML com loader; cada template tem `name`, `language`, `category`, `body_params`, `header_params`, `button_params` | Hot-reload futuro |
| W6 | `can_edit_message=False` → adapter não suporta streaming visível; `EgressPipeline` detecta e cai para "send completo no done" | Sem hack |
| W7 | Rate limit dedicado por tier configurado em settings | Cumprir limites Meta |

## 4. Capabilities

```python
WHATSAPP_CAPABILITIES = ProviderCapabilities(
    can_edit_message=False,
    can_react=False,                    # bot react ainda não disponível
    can_send_dm=True,                   # WhatsApp = sempre privado/grupo
    can_threads=False,
    can_polls=False,
    can_inline_keyboards=True,          # Interactive Messages: List, Reply Buttons
    can_slash_commands=False,
    can_voice_messages=True,            # send media audio
    can_send_typing=False,              # API não expõe
    can_fetch_user_profile=True,        # parcial: nome + foto
    has_conversation_window=True,       # 24h
    max_message_chars=4096,
    max_attachments_per_message=1,      # 1 mídia por msg
    supported_attachment_kinds=frozenset({IMAGE, VIDEO, AUDIO, FILE, STICKER}),
)
```

## 5. Mudanças requeridas na foundation

| Mudança | Onde | Por quê |
|---|---|---|
| `ConversationWindow` em `MessageEnvelope` ou `BotUser` | foundation | Para egress saber se janela está aberta |
| `TemplateMessage` DTO + `OutboundIntent` (FREE_TEXT \| TEMPLATE) | foundation | Egress decide qual usar |
| `ProviderCapabilities.has_conversation_window` (já planejado) | foundation | Capability flag |
| `OutputFormatter.render` com fallback "no markdown" para WhatsApp | foundation/provider | WhatsApp aceita formatação simples (`*bold*`, `_italic_`, `~strike~`, ` ```mono``` `) — formatter próprio |
| `RateLimiter.acquire_outbound` com peso por tier | foundation | Limits diferentes |

Estas mudanças **não invalidam** o trabalho da foundation — são extensões previstas pelos princípios DI4 e F4. Documentar em `DECISOES.md` se chegarmos a este plano.

## 6. Mapa de fases

| Fase | Conteúdo | Esforço |
|---|---|---|
| 01 | Adapter + webhook + normalizer + formatter + business setup docs | 4 dias |
| 02 | Templates + ConversationWindow + Interactive Messages | 3 dias |
| E2E | Bateria contra número Business de teste | 2 dias |
| Revisão | Cética | 1 dia |

Total: ~10 dias.

## 7. Critérios

1. Bot WhatsApp recebe mensagem inbound, responde dentro da janela.
2. Após 24h sem resposta do usuário, mandar mensagem livre falha → template é tentado automaticamente.
3. Catálogo de templates carregado de YAML; lista visível em `/wa templates list` (CLI ou comando interno).
4. Interactive Messages (List, Reply Buttons) funcionam.
5. Métrica de conversações financeiras (`bot_whatsapp_conversations_total`).

## 8. Dependências

- `httpx`
- Foundation com `ConversationWindow`/`TemplateMessage`/`OutboundIntent`
- Meta Business verificado (operacional, fora de código)
- Domínio com HTTPS público para webhook
