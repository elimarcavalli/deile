# 00 — Master Execution Plan + Glossário Canônico

> **Fonte única de verdade** para nomes, ordem global de execução, dependências, milestones e checklists pré-implementação. Em conflito entre este doc e qualquer outro `*.md` em `docs/future/`, **este vence**.

## 1. Ordem global de execução

```
                     ┌─────────────────────────────────┐
                     │   M0 — Pré-flight (humano)      │
                     │   Rotação de tokens Discord     │
                     │   Setup CI markers              │
                     └──────────┬──────────────────────┘
                                │
                     ┌──────────▼──────────────────────┐
                     │   M1 — Foundation               │
                     │   foundation/01 → 02 → 03 → E2E │
                     │   → revisão                     │
                     └──────────┬──────────────────────┘
                                │
                     ┌──────────▼──────────────────────┐
                     │   M2 — DEILE hooks               │
                     │   deile/01 → 02 → 03 → E2E       │
                     │   → revisão                     │
                     └──────────┬──────────────────────┘
                                │
                     ┌──────────▼──────────────────────┐
                     │   M3 — Discord (FOCO)            │
                     │   discord/01 → 02 → 03 → 04      │
                     │   → E2E → revisão                │
                     └──────────┬──────────────────────┘
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
   ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
   │ M4a — Telegram   │ │ M4b — WhatsApp   │ │ M4c — Meta       │
   │ telegram/01-02   │ │ whatsapp/01-02   │ │ meta/01-02       │
   │ → E2E → revisão  │ │ → E2E → revisão  │ │ → E2E → revisão  │
   └──────────────────┘ └──────────────────┘ └──────────────────┘
```

### Observações sobre ordem

- M2 pode rodar parcialmente em paralelo com M1 fase 3 (a fase 1 do DEILE é independente da bridge). Mas a fase 3 do DEILE bloqueia a fase 3 do Discord.
- M4a, M4b, M4c são paralelizáveis após M3, mas WhatsApp **força extensões** na foundation (`ConversationWindow`, `TemplateMessage`, `OutboundIntent`) — se rodar em paralelo, alguma alma centralizadora precisa de mediar a foundation.
- **Decisão prática para o agente implementador**: rodar M4 sequencial (Telegram → WhatsApp → Meta), não paralelo, na primeira passada.

## 2. Glossário canônico (todos os docs DEVEM usar estes termos)

### 2.1. Tipos da foundation (provider-agnósticos)

| Nome canônico | Onde vive | O que é |
|---|---|---|
| `BotUser` | `deilebot/foundation/envelope.py` | Identidade de um humano ou bot externo (`bot_user_id` ULID estável) |
| `Channel` | `deilebot/foundation/envelope.py` | Canal/grupo/thread/DM normalizado |
| `ChannelScope` | `deilebot/foundation/envelope.py` | Enum: `DM`, `GROUP`, `THREAD`, `BROADCAST` |
| `Attachment` | `deilebot/foundation/envelope.py` | Anexo polimórfico |
| `AttachmentKind` | `deilebot/foundation/envelope.py` | Enum: `IMAGE`, `VIDEO`, `AUDIO`, `FILE`, `STICKER`, `OTHER` |
| `ReplyContext` | `deilebot/foundation/envelope.py` | Referência a mensagem respondida |
| `MessageEnvelope` | `deilebot/foundation/envelope.py` | DTO **inbound** normalizado |
| `OutboundEnvelope` | `deilebot/foundation/envelope.py` | DTO **outbound** com `intent`, `text`, `template`, `interactive`, `attachments`, `reply_to` |
| `OutboundIntent` | `deilebot/foundation/envelope.py` | Enum: `FREE_TEXT`, `TEMPLATE` |
| `TemplateMessage` | `deilebot/foundation/envelope.py` | DTO de mensagem-template (WhatsApp/Meta) |
| `ConversationWindow` | `deilebot/foundation/envelope.py` | DTO opcional: `last_inbound_at`, `window_hours`, `is_open` |
| `MarkupAST` | **`deile/common/markup_ast.py`** (singleton — DEILE e foundation importam dali) | Lista plana de spans |
| `MarkupSpan` | mesmo arquivo | Span com `kind`, `text`, `meta` |
| `SpanKind` | mesmo arquivo | Enum: `PLAIN`, `BOLD`, `ITALIC`, `STRIKE`, `CODE_INLINE`, `CODE_BLOCK`, `QUOTE`, `LINK`, `HEADING`, `BULLET`, `NUMBERED`, `LINE_BREAK` |
| `InteractiveControls` (plural — abstract) | `deilebot/foundation/interactive.py` | Marcador para botões/listas/quick replies |
| `InteractiveButton` | mesmo | Botão (label, callback_data, opt url) |
| `InteractiveButtonRow` | mesmo | Grupo de botões |
| `InteractiveList` | mesmo | Lista com seções (WhatsApp/Telegram) |
| `InteractiveListSection` | mesmo | Seção de uma lista |
| `QuickReply` | mesmo | Quick reply (Messenger/Telegram) |

### 2.2. ABCs e capabilities

| Nome | Onde | Contrato |
|---|---|---|
| `ProviderAdapter` | `deilebot/providers/base.py` | ABC do adapter |
| `ProviderCapabilities` | `deilebot/foundation/capabilities.py` | Dataclass com flags |
| `OutputFormatter` | `deilebot/foundation/output_formatter.py` | ABC; subclasses por provider em `providers/<x>/formatter.py` |
| `WebhookRouter` | `deilebot/runtime/webhook_router.py` | Roteador HTTP central; adapters montam handlers via `register_object_handler(name, fn)` |
| `WebhookServer` | `deilebot/runtime/webhook_server.py` | FastAPI server que hospeda o `WebhookRouter` |
| `AgentMetaProvider` | `deilebot/foundation/agent_meta.py` | Acesso introspectivo às tools/modelos/personas do DEILE |
| `IntentClassifier` | `deilebot/foundation/intent.py` | Protocol; 4 implementações (`heuristic`, `llm`, `always_respond_to_addressed`, `always_respond`) |
| `BotTool` | `deilebot/foundation/tools/base.py` | Tool base que extrai `adapter` de `ctx.extra["bot_context"]` |

### 2.3. Serviços e pipelines

| Nome | Onde | Função |
|---|---|---|
| `IdentityResolver` | `deilebot/foundation/identity.py` | provider+id → `BotUser` |
| `PermissionGate` | `deilebot/foundation/permissions.py` | Allowlist/owner/blocklist |
| `RateLimiter` | `deilebot/foundation/rate_limit.py` | Token bucket + semáforo |
| `ConversationStore` | `deilebot/foundation/conversation_store.py` | SQLite persistência de mensagens |
| `BotAuditLogger` | `deilebot/foundation/audit.py` | Wrapper sobre `deile.security.audit_logger` |
| `AgentBridge` | `deilebot/foundation/agent_bridge.py` | ABC: `InProcessAgentBridge`, `OneshotSubprocessAgentBridge` |
| `CapabilityCatalog` | `deilebot/foundation/capabilities.py` | Snapshot de capacidades para system prompt e `/capabilities` |
| `PersonaSelector` | `deilebot/foundation/persona_selector.py` | `(env, user, is_owner) → persona_name` |
| `BotEventBus` | `deilebot/foundation/event_bus.py` | Wrap de `deile.events.event_bus` |
| `MetricsCollector` | `deilebot/foundation/metrics.py` | Counters/histograms/gauges em memória + emit no event_bus |
| `DeadLetterQueue` | `deilebot/foundation/dlq.py` | Fila SQLite de envios falhados |
| `IngressPipeline` | `deilebot/foundation/pipeline.py` | 16 passos: inbound → bridge |
| `EgressPipeline` | `deilebot/foundation/pipeline.py` | response → render → split → send |
| `SingleProviderRuntime` | `deilebot/runtime/single_runtime.py` | Roda 1 adapter |
| `MultiProviderRuntime` | `deilebot/runtime/multi_runtime.py` | Roda N adapters compartilhando foundation |
| `Scheduler` | `deilebot/runtime/scheduler.py` | Cron jobs YAML-driven |

### 2.4. Persistência — caminhos canônicos

| Arquivo | Conteúdo | Owner |
|---|---|---|
| `data/deilebot.sqlite` | `bot_user`, `channel`, `message`, `attachment`, `dlq`, `audit`, `schema_version` | `ConversationStore` (foundation) |
| `data/deile_sessions.sqlite` | `persisted_session` | `SessionStore` (DEILE) |
| `data/logs/deilebot.log` | logs JSON-structured rotativos | `setup_logging` |

> Decisão: **dois arquivos SQLite separados** por simetria de ownership (foundation vs DEILE core). Conexões independentes via `aiosqlite`. WAL mode em ambos.

### 2.5. CLI — invocação canônica

```bash
# Forma canônica
python3 -m deilebot.cli run --provider discord
python3 -m deilebot.cli run --provider telegram --provider whatsapp   # multi
python3 -m deilebot.cli dlq list [--provider X]
python3 -m deilebot.cli sessions purge --older-than-days N
python3 -m deilebot.cli metrics
python3 -m deilebot.cli migrate-memory-json --source PATH
python3 -m deilebot.cli persona list
```

`pip install -e .` adiciona console_script `deilebot` que é alias.

### 2.6. session_strategy

Foundation expõe três estratégias:

| Valor | bot_session_id derivado de |
|---|---|
| `per_user` (default) | `bot_user_id` |
| `per_user_channel` | `bot_user_id + channel.provider_channel_id` |
| `per_channel` | `channel.provider_channel_id` |

Configurável em `BotSettings.foundation.session_strategy`. Plano DEILE fase 1 documenta a chave de sessão como `bot_session_<derived>`.

### 2.7. force_respond — contrato oficial

Mensagens sintéticas geradas por slash commands ou reaction triggers carregam `MessageEnvelope.raw["force_respond"] = True`. `HeuristicIntentClassifier` checa esta chave **primeiro** e retorna `True` direto. **Não é hack** — é o contrato oficial para "intent classifier deve ser bypassado nesta entrada".

## 3. Princípios cross-cutting (todos os planos honram)

| # | Princípio | Documento dono |
|---|---|---|
| F1 | Async/await em toda I/O | foundation/00-PLAN.md §2 |
| F2 | Identidade por `provider_user_id` | foundation/00-PLAN.md §2 |
| F3 | Markup nunca atravessa fronteira em texto literal | foundation/00-PLAN.md §2 |
| F4 | Capability-flagged calls | foundation/00-PLAN.md §2 |
| F5 | Persistência SQLite, nunca JSON solto | foundation/00-PLAN.md §2 |
| F6 | Foundation nunca importa providers | foundation/00-PLAN.md §2 |
| F7 | Foundation depende de `deile/`, não da CLI | foundation/00-PLAN.md §2 |
| F8 | Toda saída passa por permission/rate/audit | foundation/00-PLAN.md §2 |
| **F9** | **Logs estruturados JSON sempre** | este doc, §5 |
| **F10** | **Métricas snapshot é JSON-serializable** | este doc, §5 |
| **F11** | **Secrets via `pydantic.SecretStr`, nunca `str`** | este doc, §5 |
| **F12** | **Hot-reload de YAMLs via watchdog onde já configurado** | este doc, §5 |

## 4. Marcos com critérios de fim

### M0 — Pré-flight

- [ ] Tokens do `archive/discord_bot_legacy/` rotacionados no Developer Portal
- [ ] `pytest.ini` registra markers: `e2e`, `e2e_discord_live`, `slow`, `manual`
- [ ] `pip install -e .` funciona
- [ ] Branch `deilebot` ativa e atualizada com main

### M1 — Foundation pronto

- [ ] `pytest deilebot/tests/foundation/ -v` 100%
- [ ] Coverage `deilebot/foundation/` ≥ 88% (unit + e2e)
- [ ] `pytest -m e2e deilebot/tests/e2e/ -v` 100%
- [ ] `grep -r "from deilebot.providers" deilebot/foundation/` vazio
- [ ] Revisão cética concluída sem 🔴

### M2 — DEILE hooks pronto

- [ ] CLI `python3 deile.py "olá"` regressão zero
- [ ] Sessão persistente sobrevive restart
- [ ] `extra_system_prompt` chega no provider LLM
- [ ] `process_input_stream` integridade de chunks
- [ ] Revisão cética concluída

### M3 — Discord pronto

- [ ] Bot conecta, slash sync, `/help` `/ping` `/deile` `/capabilities` operacionais
- [ ] Streaming visível em mensagens longas
- [ ] Reaction-trigger 🤖 funciona
- [ ] member_join, threads, daily digest funcionam
- [ ] `/dlq /forget /sessions /metrics /audit /persona` operacionais
- [ ] 31 itens da auditoria do legado 🟢
- [ ] E2E live 10/10 cenários
- [ ] Revisão cética concluída

### M4 — outros providers

Critérios análogos por provider (ver respectivo `05-FASE-E2E.md`).

## 5. Cross-cutting concerns (resoluções definitivas)

### 5.1. Logging estruturado

`deilebot/foundation/logging.py`:

```python
def setup_logging(settings: BotSettings) -> None:
    """JSON-friendly stdout (handler 0) + RotatingFileHandler em data/logs/deilebot.log."""
```

Format por entrada: `{"ts": "ISO8601Z", "level": "INFO", "logger": "...", "event": "outbound_sent", "...": "..."}`. Sem strings interpoladas — sempre `extra={...}` ou `LogRecord` adicional.

Lib: `python-json-logger` (adicionar a `requirements.txt` na fase 1 da foundation). Alternativa aceita: `structlog` se já estiver no projeto.

### 5.2. Métricas — formato

`MetricsCollector.snapshot()` retorna:

```python
{
  "counters": { "bot_inbound_total": { "labels": {...}, "value": 42 }, ... },
  "histograms": { "bot_agent_invocation_seconds": { "labels": {...}, "buckets": {...}, "sum": 12.3, "count": 5 } },
  "gauges": { "bot_dlq_size": { "labels": {...}, "value": 0 } },
  "exported_at": "2026-05-02T08:00:00Z"
}
```

Serializável a JSON. Comando `/metrics` (Discord) usa este snapshot. CLI `metrics` faz `print(json.dumps(snap, indent=2))`.

Exporter Prometheus opcional (futuro): `MetricsCollector.to_prometheus_text()`.

### 5.3. Secrets handling

- Todo campo de settings que é credencial: `pydantic.SecretStr`.
- Logs nunca podem imprimir `SecretStr` raw — sempre `.get_secret_value()` apenas onde indispensável.
- `ConversationStore` antes de gravar `raw_json`/`payload_json` aplica `deile.security.secrets_scanner.SecretsScanner.redact(...)`.
- Audit log faz o mesmo.

### 5.4. Hot-reload de YAMLs

- `config/deilebot.yaml`, `config/whatsapp_templates.yaml`, etc. carregados via `ConfigManager` com `watchdog` (já no projeto).
- Mudanças disparam `BotEventBus.publish("bot.config.reloaded", {...})`. Serviços que dependem (PersonaSelector, IntentClassifier) re-bindam.

### 5.5. Tracing (opcional, futuro)

OpenTelemetry tracing como hook opcional via `BotEventBus`. Não bloqueia M1-M4. Pode entrar em M5 (post-launch).

## 6. Dependências externas — tabela mestra

| Dependência | Versão | Quem usa | Adicionada em |
|---|---|---|---|
| `aiosqlite` | última | foundation, DEILE session store | foundation/01 |
| `pydantic` | ≥2 | settings em todo lugar | já no projeto |
| `tenacity` | última | egress retries | foundation/01 |
| `python-json-logger` | última | logs estruturados | foundation/01 |
| `apscheduler` | ≥3.10 | runtime/scheduler | discord/04 |
| `discord.py` | `>=2.3,<3.0` | discord adapter | discord/01 |
| `python-telegram-bot` | ≥20 | telegram adapter | telegram/01 |
| `httpx` | ≥0.25 | whatsapp + meta clients | whatsapp/01 |
| `fastapi` + `uvicorn` | latest | webhook server | telegram/02 (introduz) |

`requirements.txt` da raiz **NÃO** declara deps de provider — cada provider opcional via extras. Setup proposto:

```toml
# pyproject.toml fragment
[project.optional-dependencies]
discord = ["discord.py>=2.3,<3.0"]
telegram = ["python-telegram-bot>=20"]
whatsapp = ["httpx>=0.25"]
meta = ["httpx>=0.25"]
all-bots = ["discord.py>=2.3,<3.0", "python-telegram-bot>=20", "httpx>=0.25"]
```

## 7. Checklist global pré-implementação (responsabilidade do operador humano)

### 7.1. Credenciais e contas

- [ ] Tokens Discord do `archive/` revogados
- [ ] Bot Discord novo criado (produção e dev separados)
- [ ] Bot Telegram criado via @BotFather (produção e dev separados)
- [ ] Conta Meta Business verificada (necessário para WhatsApp/Meta)
- [ ] Page Facebook + Instagram Business linkado (para Meta)
- [ ] Domínio com HTTPS público para webhooks (WhatsApp/Meta)

### 7.2. Servidores de teste

- [ ] Servidor Discord de testes com canais `#geral`, `#admin`, `#welcome`, `#teste-thread`
- [ ] Bot Telegram de teste em chat privado + 1 grupo de teste
- [ ] Número WhatsApp Business de testes
- [ ] Page de testes Messenger + IG Business linkado

### 7.3. Configuração runtime

- [ ] `.env` com `ANTHROPIC_API_KEY` ou outro provider para o agente DEILE
- [ ] `.env` com `DISCORD_TOKEN`
- [ ] `config/deilebot.yaml` preenchido (template em discord/00-PLAN.md §10)
- [ ] `config/whatsapp_templates.yaml` (quando entrar M4b)

## 8. Convenções de commits e PRs

- Prefixos: `feat(bot-foundation):`, `feat(bot-discord):`, `feat(bot-telegram):`, `feat(bot-whatsapp):`, `feat(bot-meta):`, `feat(deile-hooks):`, `docs(future):`, `test(bot-...):`.
- Cada fase = 1 PR (branch `feat/<area>-<fase>`).
- PR description referencia o doc de plano correspondente.
- Fase E2E: 1 PR à parte por provider.
- Fase revisão cética: gera o `*-REVISAO-RESULTADOS.md` e abre PR para mergear.

## 9. Definition of Done — pacote inteiro

```
✓ M0 ✓ M1 ✓ M2 ✓ M3 ✓ M4a ✓ M4b ✓ M4c
                 ↓
        ┌────────▼────────┐
        │   PR final       │
        │   "deilebot v1" │
        └─────────────────┘
```

Quando todos os marcos checados, abre-se um PR rotulando a release `deilebot v1`. Esse PR é a gate final de revisão pelo operador humano.

## 10. Coverage matrix — decisões críticas vs cenário E2E

| Decisão | Plano | Cenário E2E que prova |
|---|---|---|
| Identidade por user.id (F2) | foundation/discord | EE2E-6 ADV-D1 (impersonação por nick) |
| Async/await em I/O (F1) | foundation | E2E-9 (concorrência multi-canal sem deadlock) |
| Capability-flagged (F4) | foundation | unit `test_provider_adapter_abc` + integração capability_catalog |
| SQLite WAL (F5) | foundation | E2E-10 persistência sobrevive restart |
| Sessão por bot_user_id (D1 DEILE) | deile | E2E-D1 sessão sobrevive restart + EE2E-8 Discord |
| extra_system_prompt (D3 DEILE) | deile | E2E-D2 chega ao provider |
| bot_context (D4 DEILE) | deile | E2E-D3 tool recebe |
| Streaming chunk-a-chunk (D6 DEILE) | deile + discord | E2E-D5 + EE2E-2 Discord |
| Slash sync (D3 Discord) | discord | EE2E-1 (Slash funciona) |
| Reaction trigger 🤖 (D7 Discord) | discord | EE2E-1.4 + ADV-D2 (cool-down) |
| /capabilities introspectivo (D11 Discord) | discord | EE2E-9 |
| Permissões por user.id (D12 Discord) | discord | EE2E-3 + ADV-D1 |
| /forget (D16 Discord) | discord | EE2E-3.3 |
| /dlq (D17 Discord) | discord | EE2E-7 |
| Streaming via edit (D9 Discord) | discord | EE2E-2 |
| Split codeblock-aware (D10 Discord) | discord | ADV-D14 |
| Member join greeting (Discord 04) | discord | EE2E-4.1 |
| Thread context inheritance (D6 Discord) | discord | EE2E-4.2 |
| Daily digest scheduler (D15 Discord) | discord | EE2E-4.3 |
| ConversationWindow (W3) | whatsapp | WE2E-3 |
| Template fallback (W4) | whatsapp | WE2E-3 + WE2E-7 |
| Polling vs Webhook (T2 Telegram) | telegram | TE2E-10 |
| BotCommands sync (T6 Telegram) | telegram | TE2E-4 |
| Inline keyboards (T5 Telegram) | telegram | TE2E-6 |
| Quick replies / postbacks (Messenger) | meta | ME2E-2/ME2E-3 |
| Story replies (Instagram) | meta | IE2E-2 |

Itens **sem** cenário E2E nominado (precisam ser criados antes da fase E2E rodar):

- Hot-reload de config (F12)
- Métricas snapshot JSON-serializable (F10)
- Secrets via SecretStr (F11)
- Logs estruturados JSON (F9)
- DLQ purge (D17 parcial)

> Bloqueador de release: cada item nesta lista deve ter um teste antes do PR final do M3. O agente implementador deve **ler esta seção** antes de fechar o E2E.

## 11. Mudanças vs primeira versão dos planos

Esta versão integrou:

- **Tipos canônicos** unificados (resolve conflito `MarkupAST` em 2 locais; `InteractiveControl(s)` plural; etc.).
- `OutboundEnvelope`, `OutboundIntent`, `TemplateMessage`, `ConversationWindow`, `InteractiveControls` **declarados na foundation desde a fase 1** (não mais "extensão WhatsApp").
- `WebhookRouter`/`WebhookServer` formalizados em `runtime/`.
- `AgentMetaProvider` formalizado em `foundation/agent_meta.py`.
- `BotTool` movida para `foundation/tools/base.py` (Discord cita; outros providers reusam).
- `session_strategy` configurável documentada.
- `force_respond` virou contrato oficial.
- Logging, métricas, secrets, hot-reload — princípios F9-F12.
- Dois arquivos SQLite separados (`deilebot.sqlite` e `deile_sessions.sqlite`) — decisão final.
- CLI canônica `python3 -m deilebot.cli`.
- pyproject extras para deps opcionais por provider.
