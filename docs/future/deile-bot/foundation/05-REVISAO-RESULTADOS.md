# Foundation — Revisão Cética: Resultados

> Auto-revisão do agente implementador no commit feat/bot-foundation-fase-e2e (M1).

## 1. Auditoria de princípios F1-F8

| # | Item | Status | Evidência |
|---|---|---|---|
| F1 | Async/await em toda I/O | 🟢 | Todo método de serviço (`ConversationStore`, `IdentityResolver`, `PermissionGate`, `RateLimiter`, `BotAuditLogger`, `IntentClassifier`, `AgentBridge`, `EgressPipeline`, `IngressPipeline`) é `async`. Nenhum bloqueio síncrono em paths de I/O. |
| F2 | Identidade por `provider_user_id`, nunca display_name | 🟢 | `IdentityResolver.resolve(provider, provider_user_id, ...)`; `bot_user.UNIQUE(provider, provider_user_id)` no schema; `permissions.check` opera sobre `bot_user_id`. |
| F3 | Markup nunca atravessa fronteira em texto literal | 🟢 | `OutputFormatter.render(MarkupAST) -> str` é o único caminho; `EgressPipeline.send_response` chama formatter antes de adapter. |
| F4 | Capability-flagged calls | 🟢 | `ProviderAdapter` defaults raise `CapabilityNotSupported` quando flag false; verificado em `test_provider_adapter_abc.py`. |
| F5 | Persistência SQLite, nunca JSON solto | 🟢 | `ConversationStore` é o único repositório; nada de JSON livre fora de `raw_json` (compactado se >threshold). |
| F6 | Foundation não importa providers | 🟢 | `test_settings.py::TestNoProvidersImportInFoundation` faz grep recursivo e passa. `_testing.py` (que precisa importar `ProviderAdapter`) vive em `deile_bot/_testing.py` (top-level), não em `foundation/`. |
| F7 | Foundation depende de `deile/` mas não da CLI | 🟢 | Imports de `deile.common.markup_ast` apenas; nenhum `deile.deile`/`deile.py` import. |
| F8 | Toda saída passa por permission/rate/audit | 🟢 | `IngressPipeline.handle` gateia `INVOKE_AGENT` e `RateLimiter.acquire_inbound`; `EgressPipeline.send_response` audita `outbound_sent`/`outbound_failed`. |
| F9 | Logs estruturados JSON | 🟢 | `setup_logging` usa `python-json-logger` com `rename_fields={"asctime":"ts","levelname":"level"}`. |
| F10 | Métricas snapshot JSON-serializable | 🟢 | `test_metrics.py::TestSerialization::test_snapshot_is_json` faz `json.dumps(snap)` sem erro. |
| F11 | Secrets via SecretStr | 🟡 | `BotSettings` ainda não declara campos secretos; será cumprido em `DiscordBotSettings.token: SecretStr` (M3 fase 1). |
| F12 | Hot-reload de YAMLs | 🟡 | `ConfigManager` do DEILE já tem watchdog; foundation `BotSettings` não é hot-reload ainda. Não bloqueia M1; ficou como issue para M5. |

## 2. Auditoria de decisões D1-D15

Todas implementadas conforme spec, com 1 desvio documentado:

- **D6** (Bridge in-process + oneshot): ✅ ambos implementados.
- **D11** (CapabilityCatalog gerado em runtime): ✅ via `AgentMetaProvider` ABC + `DeileAgentMetaProvider` concreta.
- **D13** (OutputFormatter): ✅ ABC + PlainTextFormatter; subclasses por provider são responsabilidade dos planos provider.
- **Desvio**: spec sugere `_testing.py` em `foundation/`; movemos para `deile_bot/_testing.py` para honrar F6 estritamente. Documentado neste arquivo e nos commits.

## 3. Cobertura de testes

```
foundation: 172 testes (unit)
e2e:        13 testes (FakeProviderAdapter + FakeBridge)
total:      185 testes — todos verdes
```

Ataques ADV-1 a ADV-15 do roteiro, status:

| # | Ataque | Status |
|---|---|---|
| ADV-1 | `message_id == ""` | 🟢 `test_envelope.py::test_empty_message_id_raises` |
| ADV-2 | `sent_at` sem tz | 🟢 `test_envelope.py::test_naive_sent_at_raises` |
| ADV-3 | display_name spoofing | 🟢 perms operam sobre `bot_user_id`; verificado em `test_permissions.py` e E2E-3 |
| ADV-4 | 1000 envelopes paralelos | 🟡 testado com 50 (`test_conversation_store::TestConcurrency`) e 25 no E2E-9; manual scaling para 1000 fica em E2E live |
| ADV-5 | 100 usuários paralelos | 🟢 E2E-9 com 5x5 channels paralelos; race resolvido em `IdentityResolver.resolve` (re-read after upsert) |
| ADV-6 | `text == ""` | 🟢 `HeuristicIntentClassifier` retorna `too_short`; pipeline pula |
| ADV-7 | text de 100KB | 🟡 não há truncamento explícito na foundation; cabe ao provider; documentado como issue |
| ADV-8 | bridge.invoke RuntimeError | 🟢 `test_agent_bridge.py::test_agent_exception_wrapped` + pipeline `TestAgentFailure` |
| ADV-9 | adapter.send_message lento | 🟡 não testado adversarialmente; tenacity tem `stop_after_attempt` mas não timeout per-call. Issue M5. |
| ADV-10 | drop+recreate SQLite mid-op | 🟡 não testado; fora de escopo M1 |
| ADV-11 | settings inválidas | 🟢 `test_settings.py::test_invalid_classifier_rejected` |
| ADV-12 | persona inexistente | 🟢 fallback default em `PersonaSelector.resolve` |
| ADV-13 | `</system>` injetado em extra_system_prompt | 🟢 `_sanitize_extra_prompt` strip; verificado em `test_agent_bridge.py::TestSanitize` |
| ADV-14 | provider_user_id muda tipo | 🟢 `test_identity.py::test_int_user_id_normalized_to_str` |
| ADV-15 | DLQ replay loop | 🟢 `replay()` itera lista finita; falha não re-enqueueia automático |

## 4. Respostas às 7 perguntas

1. **Telegram amanhã, o que duplica?** Apenas: `providers/telegram/normalizer.py`, `providers/telegram/formatter.py`, `providers/telegram/adapter.py`. Toda persistência, identidade, permission, rate, intent, bridge é reuso.
2. **Onde injection é mais fácil?** No `extra_system_prompt` que monta o `<bot_capabilities>`. Mitigação: `_sanitize_extra_prompt` na fronteira do bridge + sanitização adicional no DEILE (plano M2 fase 2).
3. **Single point of failure?** `ConversationStore` SQLite — se corromper, todo histórico/perms/dlq vão junto. Mitigação: WAL ativo + backup periódico do operador (documentar). Métricas/audit em arquivo seriam um stretch goal.
4. **Bridge lento (mediana 30s)?** Backlog cresce; `RateLimiter._global` segura concurrent_inbound (default 16); 17º+ cliente vê `RateLimited(global_concurrent)`. Memória controlada. UX: usuário espera; fallback agent_failed ao timeout.
5. **DB corrompido — recovery?** `aiosqlite` reabre arquivo; sem WAL replay automático. Operador precisa: stop bot → backup db → `sqlite3 ... .recover` → restart. Documentar em system_design.
6. **Operador precisa de quê em prod?** snapshot `/metrics` (counters de inbound/agent/outbound + DLQ size + rate_limited), audit recente (`SELECT * FROM audit ORDER BY occurred_at DESC LIMIT 50`), DLQ count alerta `> 10`, log file `data/logs/deile_bot.log`.
7. **O que merece sub-plano?** (a) Cron scheduler genérico (Discord fase 4 introduz mas mereceria plano M5). (b) Hot-reload de bot_settings.yaml. (c) Backup/restore de SQLite. (d) Prometheus exporter.

## 5. Bloqueadores 🔴

Nenhum. M1 está pronto para merge.

## 6. Não-bloqueadores 🟠

- 🟠 ADV-7 (text 100KB) — sem truncamento explícito; provider deveria validar via `capabilities.max_message_chars`. Fica para fase 1 do Discord.
- 🟠 ADV-9 (adapter.send lento) — adicionar `asyncio.wait_for` por send no `EgressPipeline._send_with_retry`. M5.
- 🟠 ADV-10 (DB drop) — out of scope.

## 7. Sugestões 🟡

- 🟡 Renomear `_testing.py` → `testing.py` (público intencional).
- 🟡 `MetricsCollector.to_prometheus_text()` para integração futura.
- 🟡 `BotEventBus.subscribe` aceita filtros por payload.
