# 00 — Plano completo: `deile_bot/foundation/`

## 1. Motivação

O `discord_bot/` atual é um silo: parser de `.env` próprio, persistência em JSON monolítico, system prompt em código, identidade por display_name, decisão LLM hard-coded no fluxo do `on_message`, zero reuso para Telegram/WhatsApp/Meta. Cada novo provider hoje custaria reimplementar tudo.

Foundation existe para que um adapter novo (Telegram, WhatsApp, Meta…) consuma serviços prontos e implemente apenas:

1. Tradução `EventoNativo → MessageEnvelope` (normalizer).
2. Tradução `MarkupAST → texto-formatado-do-provider` (formatter).
3. Implementação dos métodos do `ProviderAdapter` (start, stop, send_message, edit_message, react, send_dm, fetch_user_profile).
4. Eventos opcionais (member_join, thread_create, etc.) que o provider expõe.

Tudo o resto — identidade, permissões, rate limit, conversa, decisão de responder, chamada do agente, métricas, audit — é da foundation.

## 2. Princípios inegociáveis

Os princípios herdados de [`docs/system_design/03-PRINCIPIOS-ARQUITETURAIS.md`](../../../system_design/03-PRINCIPIOS-ARQUITETURAIS.md) se aplicam integralmente. Reforços específicos para a foundation:

| # | Princípio | Por quê |
|---|---|---|
| F1 | **Async/await em toda I/O.** Nenhum método de serviço da foundation é sync. | Coexistir com `discord.py` (gateway) e webhooks FastAPI sem bloquear loop. |
| F2 | **Identidade por `provider_user_id`, jamais por display_name/username.** | Display names são editáveis pelo usuário a qualquer momento → impersonação trivial. Vide vulnerabilidade S3 da auditoria do bot atual. |
| F3 | **Markup nunca atravessa a fronteira em texto literal.** Sempre passa por `MarkupAST`. | Discord markdown ≠ Telegram MarkdownV2 ≠ WhatsApp ≠ Messenger plain. |
| F4 | **Capability-flagged calls.** Métodos não-universais (edit, react, threads) precisam consultar `provider.capabilities.*` antes de chamar; falhas degradam para fallback. | Tools que o agente DEILE chamar não podem assumir features de um provider específico. |
| F5 | **Persistência em SQLite, nunca em JSON solto.** Reaproveita o `deile/storage/` existente onde fizer sentido. | `memory.json` do bot atual já tem race conditions, não escala, não consulta. |
| F6 | **Foundation não importa nada de `providers/*`.** A direção é só providers → foundation. | Garantia de reusabilidade. |
| F7 | **Foundation depende de `deile/` (memory, events, security, models, personas) mas não depende da CLI.** | Bot é outro modo de uso, não outra CLI. |
| F8 | **Toda saída para o usuário passa por permission gate, rate limit e audit log.** | Único lugar para enforcement. |

## 3. Decisões arquiteturais (com motivo)

| # | Decisão | Motivo | Alternativas descartadas |
|---|---|---|---|
| D1 | Pacote separado `deile_bot/` na raiz, não submódulo de `deile/` | Ciclo de release independente; deile pode rodar sem o bot, bot depende de deile como lib | Submódulo `deile/bot/` (acopla demais) |
| D2 | `ProviderAdapter` como ABC, não Protocol | Métodos com default behavior (ex.: `react()` que loga "not supported" se `can_react=False`) precisam de classe base concreta | Protocol (sem default impl) |
| D3 | `MessageEnvelope` é dataclass frozen | Imutável → seguro para passar entre tasks asyncio sem risco de mutação | Dict (sem tipos) |
| D4 | `MarkupAST` é lista plana de spans com tipo + texto, não árvore aninhada | LLM cospe markdown plano; árvore aninhada exigiria parser complexo. Spans cobrem 95% dos casos | Árvore (over-engineering) |
| D5 | `ConversationStore` em SQLite com schema próprio (não reusa `episodic_memory.py`) | Histórico de bot precisa de índice por `(provider, channel, message_id)` — schema diferente. Mas usa o mesmo arquivo SQLite quando possível | Reusar `episodic_memory` (schema errado) / NoSQL (overkill) |
| D6 | `AgentBridge` tem dois modos: `in_process` (default) e `oneshot_subprocess` (fallback). Configurável por settings. | In-process é 10x mais rápido e suporta streaming + sessão persistente. Subprocess é safety net (isolamento se uma tool do agente travar) | Só um dos dois |
| D7 | `IdentityResolver` mantém tabela `bot_user` separada. Mapeia `(provider, provider_user_id) → bot_user_id`. | Mesmo humano pode aparecer em N providers; queremos uma identidade DEILE-side estável | Usar provider_user_id direto como chave (acopla) |
| D8 | `PermissionGate` lê allowlist de `bot_settings.yaml` por `bot_user_id`. Owner é flag separada. | Owner precisa de privilégio máximo; allowlist comum é só "pode falar com o agente" | RBAC complexo (over) |
| D9 | `RateLimiter` é token bucket por `bot_user_id` + semáforo global. Limites em settings. | Cobre flood individual e custo total | Apenas per-user (não controla custo agregado) |
| D10 | `IntentClassifier` (should-respond) é pluggable: heurística simples + LLM fallback. Default: heurística. | Toda mensagem chamando LLM custa $$$ desnecessário. Bot atual gasta uma chamada LLM mesmo para "ok", "rsrs". | Sempre LLM (caro) / nunca LLM (perde nuance) |
| D11 | `CapabilityCatalog` é gerado em runtime introspecionando registries do agente + cogs do adapter | Self-aware system prompt sem manutenção manual | Catálogo estático (defasa) |
| D12 | `PersonaSelector` resolve persona a partir de `(provider, scope, user)` consultando `bot_settings.yaml` | Mesma instância de bot pode ter persona "developer" em DM com owner e "host" em canal público | Persona única hardcoded |
| D13 | `OutputFormatter` é renderer ABC; cada provider implementa `render(MarkupAST) -> str`. Truncamento, splits e quote-blocks ficam aqui | Lógica de formatação fora do adapter (testável) | Formatação inline no adapter |
| D14 | `EventBus` da foundation é wrapper fino sobre `deile.events.event_bus.EventBus`. Tipos de evento novos prefixados `bot.*` | Reusa pub/sub existente do DEILE; observabilidade unificada | EventBus próprio (silo) |
| D15 | `DeadLetterQueue` para envios falhados após N tentativas; persiste em SQLite com motivo e payload | Sem DLQ, mensagens perdidas viram bug invisível | Sem retry / só log |

Decisões de implementação detalhadas vivem nas fases. Decisões cross-cutting que mudarem depois de implementadas vão para `DECISOES.md` da raiz do system_design (tabela #17 em diante).

## 4. Modelo de domínio (resumo de tipos públicos)

```python
# deile_bot/foundation/envelope.py

@dataclass(frozen=True)
class BotUser:
    bot_user_id: str                  # interno, estável
    provider: str                     # 'discord' | 'telegram' | ...
    provider_user_id: str             # ID nativo no provider
    display_name: str                 # informativo, NUNCA usado para autorizar
    is_bot: bool

@dataclass(frozen=True)
class Channel:
    provider: str
    provider_channel_id: str
    name: Optional[str]
    scope: ChannelScope               # DM | GROUP | THREAD | BROADCAST

@dataclass(frozen=True)
class Attachment:
    kind: AttachmentKind              # IMAGE | VIDEO | AUDIO | FILE | STICKER
    url: Optional[str]                # se hospedado pelo provider
    bytes_inline: Optional[bytes]     # se vem inline
    mime: Optional[str]
    filename: Optional[str]
    size_bytes: Optional[int]

@dataclass(frozen=True)
class ReplyContext:
    replied_message_id: str
    replied_author: BotUser
    replied_excerpt: str              # texto curto para contexto

@dataclass(frozen=True)
class MessageEnvelope:
    message_id: str                   # ID estável do provider
    channel: Channel
    author: BotUser
    sent_at: datetime                 # UTC
    text: str
    markup: Optional[MarkupAST]       # se o provider já parseou
    attachments: tuple[Attachment, ...]
    reply: Optional[ReplyContext]
    mentions: tuple[BotUser, ...]
    raw: Mapping[str, Any]            # payload original do provider, leitura-only
```

`MarkupAST` é `list[MarkupSpan]`, com `MarkupSpan = (kind, text)` onde `kind ∈ {plain, bold, italic, strike, code_inline, code_block, quote, link(url), heading(level), bullet, numbered}`.

## 5. Pilha de serviços (visão única)

```
┌────────────────────────────────────────────────────────────────┐
│  ProviderAdapter (ABC, fora da foundation)                     │
│  recebe eventos nativos → MessageEnvelope → entrega à foundation│
└────────────────────────┬───────────────────────────────────────┘
                         │
┌────────────────────────▼───────────────────────────────────────┐
│  IngressPipeline (foundation/pipeline.py)                       │
│   1. IdentityResolver.resolve(envelope)  → BotUser              │
│   2. PermissionGate.allow(user, action)?                        │
│   3. RateLimiter.acquire(user)?                                 │
│   4. ConversationStore.record_inbound(envelope)                 │
│   5. IntentClassifier.should_respond(envelope, history)?        │
│   6. AuditLogger.log(...)                                       │
└────────────────────────┬───────────────────────────────────────┘
                         │ if should_respond
┌────────────────────────▼───────────────────────────────────────┐
│  AgentBridge.invoke(envelope, history, persona, capabilities)   │
│   ↓ (in-process: deile.core.agent.DeileAgent)                   │
│   ↓ (oneshot: subprocess deile.py "<msg>")                      │
│  → AgentResponse (stream chunks ou final)                       │
└────────────────────────┬───────────────────────────────────────┘
                         │
┌────────────────────────▼───────────────────────────────────────┐
│  EgressPipeline                                                 │
│   1. OutputFormatter.render(MarkupAST) → str do provider        │
│   2. RateLimiter.acquire_send(user)?                            │
│   3. ProviderAdapter.send_message(channel, text, ...)           │
│   4. ConversationStore.record_outbound(...)                     │
│   5. AuditLogger.log(...)                                       │
│   6. (em falha) DeadLetterQueue.enqueue(...)                    │
└────────────────────────────────────────────────────────────────┘
```

## 6. Escopo (in / out)

**Dentro do escopo da foundation:**

- DTOs (`MessageEnvelope`, `BotUser`, `Channel`, `Attachment`, `ReplyContext`, `MarkupAST`, `MarkupSpan`).
- `ProviderAdapter` ABC + `ProviderCapabilities` dataclass.
- `IngressPipeline` e `EgressPipeline` orquestrando os serviços.
- Serviços: `IdentityResolver`, `PermissionGate`, `RateLimiter`, `ConversationStore`, `IntentClassifier`, `AgentBridge` (in-process + oneshot), `CapabilityCatalog`, `PersonaSelector`, `OutputFormatter` (ABC), `AuditLogger` wrap, `DeadLetterQueue`, `EventBus` wrap, `MetricsCollector`.
- Settings (`BotSettings`) singleton via `get_bot_settings()`.
- Schema SQLite com migrations versionadas.
- Exceptions tipadas.
- Testes unit cobrindo cada serviço com fakes.

**Fora do escopo (nas pastas de provider):**

- Adapters concretos (`discord/`, `telegram/`, `whatsapp/`, `meta/`).
- Cogs/handlers específicos.
- Webhook server FastAPI (vai em `deile_bot/runtime/webhook_server.py` na fase do primeiro provider que precisar dele — WhatsApp).
- CLI `deile-bot run --provider X` (vai em `deile_bot/cli.py` no plano do Discord).

## 7. Dependências externas e do projeto

| Dependência | Por que | Onde declarar |
|---|---|---|
| `deile.memory.*` (consultivo, não obrigatório) | Persona Selector pode perguntar à memória semântica do DEILE quem é "elimar.ciss" | import |
| `deile.events.event_bus` | Pub/sub | import |
| `deile.security.audit_logger` | Audit estruturado | import |
| `deile.security.permissions` | (opcional) consulta a regras existentes | import |
| `deile.core.agent.DeileAgent` | bridge in-process | import |
| `deile.config.settings.get_settings` | settings raiz | import |
| `aiosqlite` | acesso assíncrono ao SQLite | `requirements.txt` (novo) |
| `pydantic` ≥ 2 | settings + DTOs | já no projeto |
| `tenacity` | retry de envio com backoff exponencial | `requirements.txt` (novo) |

Nenhuma dependência de provider-específico aqui (`discord.py`, `python-telegram-bot`, `httpx-webhooks`, etc.) — esses entram nos respectivos planos.

## 8. Riscos e mitigações

| Risco | Probabilidade | Impacto | Mitigação |
|---|---|---|---|
| Schema da foundation precisar mudar a cada novo provider | média | alto | Capability matrix da seção 4 do `deile-bot/README.md`. Revisão da ABC quando o **terceiro** provider for planejado. |
| Bridge in-process derrubar o adapter quando uma tool DEILE travar | média | alto | Try/except agressivo na borda + timeout obrigatório por invocation + DLQ |
| `ConversationStore` virar gargalo (write amplification) | baixa | médio | WAL mode, batch writes, índice composto, TTL por canal |
| `IntentClassifier` heurístico decidir mal | alta | baixo | Settings permitem alternar para "sempre LLM" ou "sempre responder a menção+reply"; métricas mostram a precisão |
| Persistência cresce sem bound | média | médio | Job de retenção configurável (TTL por canal, hard cap em N msgs) |
| Race em `ConversationStore` entre adapters quando rodam no mesmo processo | média | alto | Lock por (provider, channel_id); transações WAL; testes de concorrência na fase E2E |

## 9. Critérios de "feito" para o plano inteiro (todas as fases)

A foundation está pronta quando:

1. Testes unitários cobrem ≥85% das linhas dos módulos da foundation.
2. Existe um `FakeProviderAdapter` no pacote de testes que implementa a ABC inteira em memória.
3. A bateria E2E da fase 4 prova: ingress → bridge → egress de ponta a ponta com `FakeProviderAdapter` + `DeileAgent` real (modelo barato) + `aiosqlite` em memória.
4. `BotSettings` pode ser configurado por YAML, env e código.
5. Nenhum import de `deile_bot/providers/*` aparece em `deile_bot/foundation/*` (verificado por teste).
6. Audit log produz entradas tipadas para: `inbound_received`, `should_respond_decided`, `agent_invoked`, `outbound_sent`, `outbound_failed`, `permission_denied`, `rate_limited`, `dlq_enqueued`.
7. Documentação inline (docstrings) em todas as interfaces públicas.
8. Revisão cética (fase 5) executada e fechada.

## 10. Mapa de fases

| Fase | Entregáveis | Bloqueia |
|---|---|---|
| 01 | Pacote, DTOs, MarkupAST, settings, exceptions, ProviderAdapter ABC, ProviderCapabilities | tudo abaixo |
| 02 | IdentityResolver, PermissionGate, RateLimiter, ConversationStore (SQLite), AuditLogger wrap, IntentClassifier | fase 03 |
| 03 | AgentBridge (in-process + oneshot), CapabilityCatalog, PersonaSelector, EventBus wrap, MetricsCollector, DLQ, IngressPipeline, EgressPipeline | fase E2E |
| E2E | `FakeProviderAdapter` + bateria E2E com `DeileAgent` real e modelo barato (deepseek) | fase revisão |
| Revisão | Roteiro de revisão cética por outra pessoa, com lista de ataques | release |

## 11. Notas para o implementador

- Seguir os templates de [`docs/system_design/12-PADROES-CODIGO.md`](../../../system_design/12-PADROES-CODIGO.md) onde aplicável (registries, async, exceptions tipadas, testes).
- Toda função pública tem docstring com `Args/Returns/Raises`.
- Toda exceção pública é subclasse de `BotFoundationError` em `exceptions.py`.
- Settings usa `pydantic.BaseSettings` com prefix `DEILE_BOT_`.
- Migrations SQLite via SQL puro versionado em `deile_bot/foundation/sql/V001__init.sql`, etc. Loader em `conversation_store.py:_run_migrations`.
- Logs estruturados via `structlog` se já estiver no projeto, senão `logging` com formato JSON-friendly.

## 12. Glossário

| Termo | Significado |
|---|---|
| **Adapter** | Implementação concreta de `ProviderAdapter` para um provider (ex.: Discord). |
| **Bridge** | Componente que entrega uma `MessageEnvelope` ao agente DEILE e recebe `AgentResponse`. |
| **Bot user** | Identidade DEILE-side de um humano que pode aparecer em vários providers. |
| **Capability** | Feature suportada por um provider (`can_edit`, `can_react`, `can_threads`, …). |
| **Channel** | Abstração unificada de canal/grupo/thread/DM. |
| **DLQ** | Dead-Letter Queue: fila para envios que falharam após retries. |
| **Envelope** | DTO `MessageEnvelope` que carrega uma mensagem de entrada normalizada. |
| **Intent (should-respond)** | Decisão "o bot deve falar agora?". |
| **MarkupAST** | Representação intermediária de texto formatado, agnóstica a provider. |
| **Persona** | Instrução do DEILE selecionada por contexto (provider, canal, usuário). |
| **Pipeline** | `IngressPipeline` (entrada) e `EgressPipeline` (saída) — orquestram serviços em ordem. |
