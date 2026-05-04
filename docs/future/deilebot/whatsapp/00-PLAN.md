# 00 — Plano completo: `deilebot/providers/whatsapp/`

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

## 5. O que já está pronto na foundation (zero mudanças necessárias)

A revisão de integridade (commit 2 dos planos) **antecipou** as extensões que originalmente nasceriam aqui — agora vivem na foundation desde a fase 1:

| Item | Onde já vive | Status |
|---|---|---|
| `ConversationWindow` DTO + property `is_open` | `deilebot/foundation/envelope.py` | ✅ na foundation |
| `OutboundIntent` enum (FREE_TEXT, TEMPLATE) | `deilebot/foundation/envelope.py` | ✅ |
| `TemplateMessage` DTO | `deilebot/foundation/envelope.py` | ✅ |
| `OutboundEnvelope` DTO | `deilebot/foundation/envelope.py` | ✅ |
| `InteractiveControls`/`InteractiveList`/`InteractiveButtonRow`/`QuickReplies` | `deilebot/foundation/interactive.py` | ✅ |
| `ProviderCapabilities.has_conversation_window` flag | `deilebot/foundation/capabilities.py` | ✅ |
| `WebhookServer` | `deilebot/runtime/webhook_server.py` | introduzido no plano Telegram fase 2 |

Pequenas adições ainda exigidas por este plano:

| Item | Onde | Quem entrega |
|---|---|---|
| `ConversationStore.get_window(provider, user, window_hours)` | `deilebot/foundation/conversation_store.py` | esta fase, PR pequeno na foundation |
| `EgressPipeline` consciente de janela e template fallback | `deilebot/foundation/pipeline.py` | esta fase, PR pequeno na foundation |
| `RateLimiter.acquire_outbound` com peso por tier | `deilebot/foundation/rate_limit.py` | esta fase, PR pequeno na foundation |

Esses PRs pequenos na foundation são incrementais, retro-compatíveis e ganham testes próprios na fase E2E desta área.

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

- `httpx>=0.25`
- Foundation completa (ConversationWindow/TemplateMessage/OutboundIntent já presentes desde fase 1)
- Meta Business verificado (operacional, fora de código)
- Domínio com HTTPS público para webhook
- App Meta com produto WhatsApp ativado, Phone Number ID, WABA ID, System User token

## 9. Riscos consolidados

| Risco | Prob | Impacto | Mitigação |
|---|---|---|---|
| Templates demoram dias para Meta aprovar | sempre | alto | Estoque de templates "operacionais" prontos antes do go-live; processo documentado |
| Janela 24h fechada + sem template → mensagem perdida | alta | alto | Fallback automático para `re_engagement_template` configurável; sem template → DLQ + alerta |
| Tier 1 (1k conv/dia) limita rapidamente | média | médio | Métricas projetam consumo; alertas a 70%/85%/95%; processo de upgrade tier documentado |
| Cobrança por conversa (não mensagem) — billing surpresa | média | alto | Métrica `bot_whatsapp_conversations_total{type}` + dashboard custo; budget por dia |
| Token System User expira/é revogado | baixa | crítico | Monitor 401; alerta operacional; rotação preventiva trimestral |
| WhatsApp não suporta editar mensagem → streaming inviável | sempre | baixo | Estratégia: enviar resposta completa só no `done` do stream |
| Mídia precisa upload prévio → latência | sempre | baixo | Cache de `media_id` por hash do conteúdo; reuse |
| Compliance (opt-in obrigatório, política privacidade) | alta | crítico | Fluxo de opt-in registrado em `bot_user.opted_in_at`; auditoria |
| Rate limit nativo varia por tier e qualidade rating | média | médio | `RateLimiter.acquire_outbound` com peso configurável |
| Reactions (bot react) não disponível ainda | sempre | baixo | `can_react=False`; degradar gracefully quando agente pedir |
