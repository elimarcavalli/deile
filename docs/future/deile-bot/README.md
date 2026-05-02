# `deile-bot/` — planos dos bots

> Conjunto de planos para o pacote `deile_bot/`: uma camada provider-agnóstica (`foundation/`) e adapters por provider (`discord/`, `telegram/`, `whatsapp/`, `meta/`).

## Estado dos planos

| Plano | Estado | Ordem |
|---|---|---|
| [`foundation/`](foundation/) | **planejamento — bloqueia todos os outros** | 1º |
| [`discord/`](discord/) | planejamento — primeiro adapter, foco principal | 2º |
| [`telegram/`](telegram/) | esboço — paralelizável após discord | 3º (paralelo) |
| [`whatsapp/`](whatsapp/) | esboço — paralelizável após discord | 3º (paralelo) |
| [`meta/`](meta/) | esboço — paralelizável após discord | 3º (paralelo) |

## Arquitetura-alvo do pacote `deile_bot/`

```
deile_bot/
├── foundation/                  ← provider-agnóstico (PLANEJADO)
│   ├── envelope.py              MessageEnvelope, Attachment, ReplyContext (DTOs)
│   ├── identity.py              IdentityResolver: provider_user_id → BotUser
│   ├── permissions.py           PermissionGate (allowlist por user.id)
│   ├── rate_limit.py            TokenBucket + Semaphore por provider
│   ├── conversation_store.py    Histórico em SQLite (substitui memory.json atual)
│   ├── agent_bridge.py          Bridge para deile.core.agent.DeileAgent
│   ├── capabilities.py          CapabilityCatalog para auto-introspecção
│   ├── persona_selector.py      Mapeia (provider, scope, user) → persona DEILE
│   ├── audit.py                 Wrapper sobre deile.security.audit_logger
│   ├── intent.py                Should-respond classifier
│   ├── markup_ast.py            AST de marcação rica (B/I/code/quote/link/list)
│   ├── output_formatter.py      Renderer ABC; subclasses por provider
│   ├── settings.py              BotSettings (singleton via get_bot_settings)
│   ├── event_bus.py             Wrap de deile.events.event_bus
│   ├── dlq.py                   Dead-letter queue para envios falhados
│   ├── metrics.py               Contadores e histogramas
│   └── exceptions.py
├── providers/                   ← provider-específico
│   ├── base.py                  ProviderAdapter (ABC) + ProviderCapabilities
│   ├── discord/
│   ├── telegram/
│   ├── whatsapp/
│   └── meta/
│       ├── messenger/
│       └── instagram/
├── runtime/
│   ├── multi_runtime.py         Roda N adapters em paralelo
│   ├── single_runtime.py        Roda 1 adapter
│   └── webhook_server.py        FastAPI server compartilhado (WA/Meta)
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
└── cli.py                       deile-bot run --provider discord [--provider telegram]
```

## Capability matrix (por que a foundation pode existir)

| Capacidade | Discord | Telegram | WhatsApp | Messenger | Instagram | Foundation modela como |
|---|---|---|---|---|---|---|
| Texto | ✅ | ✅ | ✅ | ✅ | ✅ | sempre disponível |
| Mídia (img/vídeo/áudio/arquivo) | ✅ | ✅ | ✅ | ✅ | ✅ | `Attachment` polimórfico |
| Reply a msg | ✅ | ✅ | ✅ (Quote) | ✅ | ✅ | `ReplyContext` opcional |
| React com emoji | ✅ | ✅ | ✅ | ✅ | parcial | `react()` com fallback |
| Editar msg | ✅ | ✅ | ❌ | ❌ | ❌ | `edit()` na ABC; `NotSupported` no resto |
| DM / privado | ✅ | ✅ (default) | ✅ (default) | ✅ (default) | ✅ | `send_dm()` |
| Grupo | ✅ (channel/guild) | ✅ | ✅ | limitado | ❌ | `Channel` abstrato |
| Slash commands | ✅ | ✅ (BotCommands) | ❌ | ❌ | ❌ | feature opcional |
| Inline keyboards / quick replies | components | inline kb | interactive msgs | quick replies | ❌ | `InteractiveControls` opcional |
| Threads | ✅ | topics em supergroups | ❌ | ❌ | ❌ | `ThreadContext` opcional |
| Polls | ✅ | ✅ | ❌ | ❌ | ❌ | feature opcional |
| Perfil de usuário | parcial (sem bio oficial) | bio, foto | só nome+foto | nome, foto | nome, foto | `UserProfile` parcial |
| Transporte | Gateway WS | longpoll/webhook | webhook | webhook | webhook | `start()` polimórfico |
| Janela de 24h obrigatória | ❌ | ❌ | ✅ (templates fora dela) | parcial | parcial | `ConversationWindow` opcional |
| Rate limit nativo | rota+global | 30 msg/s | 80/s (varia tier) | variável | variável | `RateLimiter` por provider |

**Conclusões da matrix** (decisões que cascateiam para a foundation):

1. Texto + mídia + reply é o **núcleo comum**. A `MessageEnvelope` fala disso primeiro.
2. **Editar msg só existe em 2 dos 5** — então `OutboundFormatter.edit()` é capability-flagged. Quem chamar precisa olhar `provider.capabilities.can_edit`.
3. **Reactions têm semânticas diferentes** — degradar gracefully (Instagram: emoji limitado; WhatsApp: 1 emoji por msg).
4. **Markup é incompatível 4-a-4** — solução é `MarkupAST` interno + renderer por provider. Nunca exponha markdown literal nas tools/personas.
5. **Conversation window de 24h** é exclusiva de WhatsApp/Meta — a foundation precisa de `ConversationWindow` opcional para que o bridge saiba quando precisa enviar via template aprovado.
6. **Transporte heterogêneo** (gateway vs webhook) força runtime polimórfico — `single_runtime` para gateway, `webhook_server` compartilhado para os HTTP-only.

## Referência cruzada

- Mudanças que o **agente DEILE** precisa receber para suportar o bridge: ver [`../deile/00-PLAN.md`](../deile/00-PLAN.md).
- Decisões arquiteturais do projeto-mãe: [`docs/system_design/00-VISAO-GERAL.md`](../../system_design/00-VISAO-GERAL.md).
- Análise crítica do `discord_bot/` legado (pacote-protótipo que será absorvido/depreciado): conversa de auditoria em `2026-05-01`. Os achados S1-S8, B1-B11, P1-P6, A1-A12 dessa auditoria são insumo direto da fase 1 do plano `discord/`.
