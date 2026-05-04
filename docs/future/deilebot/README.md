# `deilebot/` — planos dos bots

> Conjunto de planos para o pacote `deilebot/`: uma camada provider-agnóstica (`foundation/`) e adapters por provider (`discord/`, `telegram/`, `whatsapp/`, `meta/`).

## Estado dos planos

| Plano | Estado | Ordem |
|---|---|---|
| [`foundation/`](foundation/) | **planejamento — bloqueia todos os outros** | 1º |
| [`discord/`](discord/) | planejamento — primeiro adapter, foco principal | 2º |
| [`telegram/`](telegram/) | esboço — paralelizável após discord | 3º (paralelo) |
| [`whatsapp/`](whatsapp/) | esboço — paralelizável após discord | 3º (paralelo) |
| [`meta/`](meta/) | esboço — paralelizável após discord | 3º (paralelo) |

## Arquitetura-alvo do pacote `deilebot/`

> Glossário canônico de tipos e paths em [`../00-MASTER-EXECUTION-PLAN.md`](../00-MASTER-EXECUTION-PLAN.md) §2. O esqueleto abaixo é não-normativo.

```
deilebot/
├── foundation/                  ← provider-agnóstico (PLANEJADO)
│   ├── envelope.py              DTOs inbound + outbound + ConversationWindow
│   ├── interactive.py           InteractiveControls/Button/Row/List/Section/QuickReply(ies)
│   ├── identity.py              IdentityResolver: provider_user_id → BotUser
│   ├── permissions.py           PermissionGate (allowlist por bot_user_id)
│   ├── rate_limit.py            TokenBucket + Semaphore por provider
│   ├── conversation_store.py    Histórico em SQLite (substitui memory.json atual)
│   ├── agent_bridge.py          Bridge para deile.core.agent.DeileAgent
│   ├── agent_meta.py            AgentMetaProvider ABC + DeileAgentMetaProvider
│   ├── capabilities.py          ProviderCapabilities + CapabilityCatalog
│   ├── persona_selector.py      Mapeia (provider, scope, user) → persona DEILE
│   ├── audit.py                 BotAuditLogger (wrapper sobre deile.security.audit_logger)
│   ├── intent.py                IntentClassifier (4 implementações)
│   ├── output_formatter.py      Renderer ABC; subclasses em providers/<x>/formatter.py
│   ├── pipeline.py              IngressPipeline + EgressPipeline
│   ├── settings.py              BotSettings (singleton via get_bot_settings)
│   ├── event_bus.py             BotEventBus (wrap de deile.events.event_bus)
│   ├── dlq.py                   DeadLetterQueue (SQLite)
│   ├── metrics.py               MetricsCollector
│   ├── logging.py               JSON-structured logging
│   ├── exceptions.py            BotFoundationError + subclasses tipadas
│   ├── _testing.py              FakeProviderAdapter, FakeAgentMetaProvider, factories
│   └── tools/
│       ├── base.py              BotTool base (extrai adapter de ctx.extra)
│       ├── send_dm.py           transversal
│       ├── get_user_profile.py  transversal
│       ├── react_to_message.py  transversal
│       └── send_template_message.py  transversal (WhatsApp/Meta)
├── providers/                   ← provider-específico
│   ├── base.py                  ProviderAdapter ABC
│   ├── discord/
│   │   ├── adapter.py
│   │   ├── normalizer.py
│   │   ├── formatter.py
│   │   ├── settings.py
│   │   ├── intents.py
│   │   ├── cogs/
│   │   └── tools/               pin_message, start_thread, mention_role
│   ├── telegram/
│   │   ├── adapter.py
│   │   ├── normalizer.py
│   │   ├── formatter.py
│   │   ├── settings.py
│   │   └── handlers.py
│   ├── whatsapp/
│   │   ├── adapter.py
│   │   ├── normalizer.py
│   │   ├── formatter.py
│   │   ├── settings.py
│   │   ├── api_client.py
│   │   ├── webhook_routes.py
│   │   └── media.py
│   └── meta/
│       ├── _common/
│       │   ├── webhook_router.py
│       │   ├── api_client.py
│       │   ├── auth.py
│       │   └── settings.py
│       ├── messenger/
│       └── instagram/
├── runtime/
│   ├── multi_runtime.py         Roda N adapters em paralelo
│   ├── single_runtime.py        Roda 1 adapter
│   ├── webhook_server.py        FastAPI server compartilhado (WA/Meta/Telegram opt)
│   ├── webhook_router.py        Dispatcher comum
│   └── scheduler.py             Cron jobs YAML-driven
├── tests/
│   ├── foundation/
│   ├── providers/
│   │   ├── discord/
│   │   ├── telegram/
│   │   ├── whatsapp/
│   │   └── meta/
│   ├── integration/
│   └── e2e/
│       ├── (foundation E2E com FakeProviderAdapter)
│       ├── discord/             E2E live
│       ├── telegram/
│       ├── whatsapp/
│       └── meta/
└── cli.py                       python3 -m deilebot.cli run --provider X
```

`deile/common/markup_ast.py` é entregue pelo plano DEILE (não vive em `deilebot/`).

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
