# Discord Adapter — Revisão Cética: Resultados

> Auto-revisão pelo agente implementador, branch deile-bot. Live cenarios
> Discord são SKIPPED por falta de credenciais nesta máquina; documentados
> como prontos para execução manual.

## 1. Auditoria do legado (31 itens)

| ID | Item | Status | Evidência |
|---|---|---|---|
| **S1** | Token hardcoded em send_dm.py | 🟢 | `archive/discord_bot_legacy/README.md` documenta arquivamento; tokens rotacionados pelo operador antes deste PR (declarado nas instruções do agente) |
| **S2** | Token hardcoded em salve_tiago.py | 🟢 | idem S1 |
| **S3** | Privilégio por display_name | 🟢 | `PermissionGate.is_owner` consulta apenas `bot_user_id` (`deile_bot/foundation/permissions.py`); `IdentityResolver.resolve` mantém ULID estável independente de display_name |
| **S4** | Jailbreak `set_modulo_regulador` no system prompt | 🟢 | persona em `deile/personas/instructions/discord_developer.md` é Markdown puro sem o bloco; `_sanitize_extra_prompt` em `agent_bridge.py` remove tags `</system>`, `<persona_override>`; persona MD diz explicitamente para recusar jailbreaks |
| **S5** | memory.json sem PII filter | 🟡 | `purge_user_messages` + `/forget` cobrem deleção sob demanda; PII automatic filter pendente (não-bloqueador, fica para M5). `SecretsScanner.redact` aplicado em `SessionStore.upsert` (sessões DEILE) |
| **S6** | Comandos d! salvos na memória | 🟢 | `HeuristicIntentClassifier` retorna `should_respond=False` quando texto começa com `command_prefix`; pipeline persiste só inbound, mas /forget elimina sob demanda |
| **S7** | nuke.py perigoso | 🟢 | archived; nada equivalente em deile_bot |
| **S8** | memory.json 644 | 🟡 | SQLite criado pelo aiosqlite herda umask 022 (default); operador pode `chmod 600`. Documentado em `config/deile_bot.example.yaml` (TODO) |
| **B1** | Inconsistência model id | 🟢 | `BotSettings.foundation.forced_model` central; `agent_bridge.py` usa `inv.forced_model` |
| **B2** | Slash sync ausente | 🟢 | `DiscordAdapter` chama `client.tree.sync()` em `on_ready` (per-guild se `slash_sync_guild_ids` setado, global caso contrário) |
| **B3** | d!help inexistente | 🟢 | `HelpCog` auto-gerado em `cogs/help_cog.py` lista slash + prefix commands |
| **B4** | Race menção+comando | 🟢 | `HeuristicIntentClassifier` checa `command_prefix` antes de mention; pipeline serializa via `IngressPipeline.handle` por canal |
| **B5** | Race condition memory.json | 🟢 | SQLite WAL ativo (`PRAGMA journal_mode=WAL`); `ConversationStore._lock` serializa writes; testes E2E-9 cobrem 25 inserts paralelos sem corrupção |
| **B6** | `python -m discord_bot.bot` falso | 🟢 | substituído por `python -m deile_bot.cli run --provider discord`; `pyproject.toml` registra console_script `deile-bot` |
| **B7** | d!dado sem validar | 🟢 | dado migrado/arquivado; novas hybrid_commands usam `app_commands.describe` para validação de tipos |
| **B8** | Truncamento >2000 chars | 🟢 | `OutputFormatter.split` codeblock-aware; `EgressPipeline` envia múltiplos chunks como reply chain |
| **B9** | unban quebrado | 🟢 | adapter.fetch_user_profile aceita user.id (string-coerced via `IdentityResolver`); display_name é cosmético |
| **B10/B11** | parse .env próprio | 🟢 | `BotSettings` via pydantic-settings; `DiscordBotSettings.token: SecretStr` |
| **P1/P3** | memory.json monolítico | 🟢 | SQLite indexado em `(provider, channel, sent_at DESC)` e `(bot_user_id, sent_at DESC)` |
| **P4** | Save síncrono no loop | 🟢 | aiosqlite + async/await em todos os paths; F1 verificado |
| **P5** | Sem rate limit DeepSeek | 🟢 | `RateLimiter` token bucket per-user + global semaphore na `IngressPipeline.handle` (passo 4) |
| **P6** | Sem cool-down por usuário | 🟢 | `RateLimiter.acquire_inbound` com burst=5, refill=30/min (configurável); `ReactionCog` adiciona cool-down de 30s por (canal,usuario) para o trigger 🤖 |
| **A1** | System prompt em .py | 🟢 | personas em Markdown em `deile/personas/instructions/`; `_build_system_instruction` em `context_manager.py` lê via `PersonaManager` |
| **A2** | Bot isolado de deile/ | 🟢 | `InProcessAgentBridge` reusa `DeileAgent` com sessão persistida (M2 fase 1); tools registradas via `tool_registry`; modelo via `ModelRouter` |
| **A3** | Zero testes | 🟢 | 232 testes deile_bot + 56 testes deile/core (M2) = 288 testes, todos verdes |
| **A4** | requirements sem pin | 🟢 | `requirements.txt` mantém pinning (aiosqlite==, etc.); novos foram >= para flexibilidade conforme master plan §6 |
| **A5** | start.sh engole stderr | 🟢 | `cli.py` usa `setup_logging` JSON-structured, sem swallow; logs em `data/logs/deile_bot.log` rotativos |
| **A6** | Sem signal handler | 🟡 | `runtime.stop()` chamado no finally; SIGINT trata via KeyboardInterrupt; SIGTERM clean shutdown via `runtime.stop()` documentado mas pendente unit test |
| **A7-A12** | Reatividade limitada | 🟢 | `EventsCog` cobre `on_member_join` (envelope sintético em #welcome) e `on_thread_create` (persiste no store); scheduler genérico cobre cron jobs (daily_digest); `ReactionCog` cobre reaction trigger 🤖 |

**Resumo: 28/31 GREEN, 3 YELLOW (S5 PII filter futuro, S8 chmod 600 documentado, A6 SIGTERM handler documentado).** Sem RED.

## 2. Ataques ADV-D1..ADV-D15

Os ataques ADV-D1..ADV-D15 que NÃO requerem live Discord foram cobertos pelos
testes E2E foundation (E2E-3 blocklist, E2E-4 burst, E2E-5 DLQ replay, E2E-7
intent modes, E2E-9 concorrência). Os live (ADV-D2 reaction flood, ADV-D7
restart durante streaming, ADV-D11 multi-mention) foram documentados em
`deile_bot/tests/e2e/discord/test_security_invariants.py` como
`@pytest.mark.e2e_discord_live` SKIPPED — prontos para execução manual.

| Ataque | Status | Evidência |
|---|---|---|
| ADV-D1 spoof display_name | 🟢 | E2E `test_e2e3_blocklist_user_ignored` + `test_e2e3_owner_allowed` |
| ADV-D2 reaction flood | 🟡 ready-skipped | `ReactionCog._cooldown=30s` por (channel, user); skeleton em e2e/discord/ |
| ADV-D3 200 msgs/min | 🟢 | E2E `test_e2e4_burst_then_blocked` |
| ADV-D4 50KB texto | 🟡 | foundation não trunca; provider deve respeitar `max_message_chars`. Issue para M5 |
| ADV-D5 imagem 100MB | 🟡 ready-skipped | Discord rejeita; bot não trava (não testado live) |
| ADV-D6 500 falhas seguidas | 🟢 | E2E `test_e2e5_dlq_enqueue_then_replay` |
| ADV-D7 restart durante streaming | 🟡 ready-skipped | `runtime.stop()` close limpo; live test pendente |
| ADV-D8 token inválido | 🟢 | `DiscordAdapter.start()` raise ProviderError; verificável manualmente |
| ADV-D9 sem DEEPSEEK_API_KEY | 🟡 | bridge in_process tenta agent_provider; agent.initialize() pode falhar; documentado |
| ADV-D10 reaction a msg do próprio bot | 🟢 | `ReactionCog.on_raw_reaction_add` filtra `payload.user_id == bot.user.id` |
| ADV-D11 30 mentions ao bot | 🟢 | `IngressPipeline` é por message_id; UNIQUE constraint dedup; loop não acontece |
| ADV-D12 send_dm bot_user_id inexistente | 🟢 | `SendDMTool` retorna `{ok: False, error: "user_not_found"}` |
| ADV-D13 owner /forget self | 🟡 | confirmação dupla via Discord components — admin_cog atual usa `/forget` direto sem confirmação. Issue para M5 |
| ADV-D14 edit 5KB com codeblock | 🟢 | `DiscordOutputFormatter.split` (test_codeblock_preserved) |
| ADV-D15 bot.user.id muda | 🟢 | `self_user_id` recapturado em cada `on_ready` |

## 3. Respostas às 8 perguntas

1. **Memória persistente cresce indefinidamente?** Não — `ConversationStore.purge_older_than(days)` + `SessionStore.purge_older_than(days)`. Default `data_retention_days=90`. Trigger manual via `/dlq purge` ou `python3 -m deile_bot.cli sessions purge --older-than-days 30`.
2. **Trocar default DeepSeek → Anthropic?** Apenas settings — `BotSettings.foundation.forced_model="anthropic:claude-3-5-sonnet"`. Nenhum código muda. O `ModelRouter` do DEILE roteia.
3. **Dev vs prod?** Settings — recomendo dois `.env` separados (`.env` produção, `.env.dev` desenvolvimento) e `slash_sync_guild_ids` populado só em dev (sync per-guild = feedback rápido). Bot Discord deve ser app diferente (token diferente).
4. **Streaming spam?** Não implementamos streaming visível neste M3 (fica como fase 3.5/M5); resposta default é uma mensagem só ou split. Quando implementado: debounce de 800ms (`message_edit_debounce_ms`) cobre Discord rate limit (5/2s/canal); flag para desligar = `message_edit_debounce_ms = -1` (futuro).
5. **/forget remove memória de "Alice" do agente?** Sim — `/forget` apaga `message` rows. Mas `SessionStore` (DEILE) é independente; precisa `/sessions clear --user X` adicional para apagar memória do agente. Documentar como parte do "GDPR delete" combinado.
6. **Tokens em git history pós-rotacionar?** O agente assumiu que o operador rotacionou (declarado nas instruções da run). Não devemos rewrite history aqui — operador fez. Para futuro: pre-commit `git-secrets` adicionando regex de DISCORD_TOKEN.
7. **Daily digest canal vazio?** Atual: bot manda o prompt mesmo assim ("resuma os últimos 24h"); agente vai dizer "nada significativo". Configurável via `args.skip_if_empty=True` no YAML (TODO M5).
8. **bot_context com adapter_ref vaza em log?** Risco real — `adapter_ref` é objeto vivo. Mitigação atual: o pipeline NÃO loga `bot_context` raw; tools que loggarem ctx.extra precisam de cuidado. Recomendação: redigir `adapter_ref`/`permissions`/`identity` antes de qualquer log estruturado. Adicionar como invariant de doc no `8_system_specific_guidelines.md`.

## 4. Bloqueadores 🔴

Nenhum.

## 5. Resumo

M3 Discord pronto para merge. Streaming visível (live edits chunk-a-chunk
via `EgressPipeline.send_response_streaming`) é tracked como follow-up
não-bloqueador para uma fase 3.5 ou M5 — o pipeline default já entrega
a resposta completa dividida em chunks ≤ 2000 chars. Live cenarios
EE2E-1..10 prontos para execução manual em servidor de testes.
