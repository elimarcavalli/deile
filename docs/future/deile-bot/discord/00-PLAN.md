# 00 — Plano completo: `deile_bot/providers/discord/`

## 1. Motivação

O Discord é o primeiro alvo. O `discord_bot/` atual tem 3 problemas estruturais que justificam um pacote novo (não um patch):

1. **Silo:** não importa nada do `deile/` (sem tools, sem memória multi-camada, sem multi-provider).
2. **Vulnerabilidades agudas:** tokens hardcoded, identidade por display_name, jailbreak no system prompt (S1-S4 da auditoria).
3. **Ausência de hooks:** sem slash sync, sem `d!help`, sem rate limit, sem DLQ, sem audit estruturado.

A escolha é **reescrever em `deile_bot/providers/discord/` consumindo a foundation** e migrar gradualmente o que valer (memória → SQLite). O legado vira `archive/`.

## 2. Princípios

| # | Princípio | Por quê |
|---|---|---|
| DI1 | **Tudo o que pode ser foundation, é foundation.** Adapter só faz I/O Discord-específico. | Reuso para Telegram/WhatsApp/Meta. |
| DI2 | **Slash commands são primeiros cidadãos.** Toda interação tem variante `/<cmd>` além do `d!<cmd>` (compat). | UX moderna do Discord. |
| DI3 | **Identidade SEMPRE por `user.id`.** Display_name é cosmético. | Corrige S3 da auditoria. |
| DI4 | **Persona não vive em `.py`.** Markdown em `deile/personas/instructions/` ou `deile_bot/providers/discord/personas/instructions/`. | Alinha com padrão do projeto e permite hot-reload. |
| DI5 | **Streaming é default na resposta longa** (>200 chars de previsão). Editar a mensagem chunk-a-chunk. | Sensação de "agente pensando". |
| DI6 | **Toda chamada que pode falhar tem fallback graceful.** Sem stack trace para o usuário. | UX. |
| DI7 | **Comandos admin são gateados por `PermissionGate`.** Owners vêm de settings, não de string literal. | Segurança. |
| DI8 | **Operação observável.** `/metrics`, `/dlq`, `/sessions`, `/audit recent` para owners. | Diagnóstico em produção. |

## 3. Decisões

| # | Decisão | Motivo |
|---|---|---|
| D1 | Pacote `deile_bot/providers/discord/` reaproveita a ABC `ProviderAdapter` | Foundation |
| D2 | Lib: `discord.py` ≥ 2.3 (mantém o atual) | Comunidade ativa, conhecido pelo time |
| D3 | Slash commands sincronizados em `setup_hook` via `await self.tree.sync()` (e per-guild em desenvolvimento via `--guild-id`) | Resolve B2 |
| D4 | Identidade Discord → `BotUser` mapeia `(provider="discord", provider_user_id=str(message.author.id))` | DI3 |
| D5 | DM é detectada por `isinstance(message.channel, discord.DMChannel)` → `ChannelScope.DM` | Sem ambiguidade |
| D6 | Threads → `ChannelScope.THREAD` com `parent_channel_id` populado; histórico do parent é injetado no prompt da thread | Resolve A12 |
| D7 | Reaction-trigger: emoji 🤖 (configurável) reagindo a uma mensagem chama o agente DEILE com aquela mensagem como prompt | Atalho natural |
| D8 | `/deile <prompt>` é o comando canônico; `@bot <texto>` também invoca; reply a msg do bot também | Múltiplas afordâncias |
| D9 | Streaming via `message.edit` em chunks acumulados (debounce 800ms para não rate-limitar a Edit API) | Sensação de tempo real |
| D10 | Resposta > 2000 chars (limite Discord) → split via `OutputFormatter.split` respeitando codeblocks | Resolve B8 |
| D11 | Tools novas que o agente DEILE pode invocar via bot: `send_dm`, `get_user_profile`, `react_to_message`, `pin_message`, `start_thread`, `mention_role` | Expandir alcance |
| D12 | Owner detectado por `user.id` em `BotSettings.permissions.owners`; nada no system prompt | Resolve S3+S4 |
| D13 | `d!help` é cog auto-gerado lendo `bot.tree.get_commands()` + `bot.cogs` | Resolve B3 |
| D14 | `unban` aceita user.id (recomendado) ou username (legacy) | Resolve B9 |
| D15 | Scheduler genérico em `deile_bot/runtime/scheduler.py` (foundation futura) com YAML de cron jobs; deprecia `scheduler_333.py` | Resolve A7 parcial |
| D16 | Comando admin `/forget --user <id> --before <date>` apaga histórico do usuário; usa `ConversationStore.purge_*` | Privacidade |
| D17 | Comando admin `/dlq list/replay/purge` opera sobre DLQ da foundation | Operação |
| D18 | Comando admin `/sessions list/clear/purge` opera sobre `SessionStore` do DEILE | Operação |
| D19 | Comando admin `/metrics` devolve snapshot do `MetricsCollector` | Observabilidade |
| D20 | Logging estruturado JSON em `data/logs/discord_bot.log` com rotação | Operação |

## 4. Capability matrix do Discord

```python
DISCORD_CAPABILITIES = ProviderCapabilities(
    can_edit_message=True,
    can_react=True,
    can_send_dm=True,
    can_threads=True,
    can_polls=True,
    can_inline_keyboards=False,        # Discord usa Components/Buttons, semântica diferente
    can_slash_commands=True,
    can_voice_messages=False,           # bots não enviam voice msgs
    can_send_typing=True,
    can_fetch_user_profile=True,        # parcial: avatar/banner/accent/nick/joined_at — bio não-pública
    has_conversation_window=False,
    max_message_chars=2000,
    max_attachments_per_message=10,
    supported_attachment_kinds=frozenset({IMAGE, VIDEO, AUDIO, FILE}),
)
```

## 5. Migração do `discord_bot/` legado

| Item | Ação | Quando |
|---|---|---|
| `bot.py`, `cogs/`, `memory.py`, `llm_generate.py`, `discord_utils.py`, `scheduler_333.py`, `disparar_agora.py`, `demo.py`, `nuke.py`, `salve_tiago.py`, `send_dm.py` | Mover para `archive/discord_bot_legacy/` em PR único na fase 1 (mantém histórico git) | Fase 1 |
| `memory.json` | Migrar via `scripts/migrate_memory_json_to_sqlite.py` para o `ConversationStore` da foundation | Fase 2 |
| `.settings.json` | Mapear chaves para `BotSettings` (foundation) e `DiscordBotSettings` (provider) | Fase 1 |
| `.env` | `DISCORD_TOKEN`, `DEEPSEEK_API_KEY` continuam; adicionar `DEILE_BOT_*` para nova foundation | Fase 1 |
| Tokens hardcoded em `send_dm.py`/`salve_tiago.py` | **REVOGAR no Discord Developer Portal** antes de tocar no código (operação humana) | Antes da fase 1 |
| `start.sh` | Reescrito para `python3 -m deile_bot.cli run --provider discord` | Fase 4 |
| `bot.log`, `scheduler.log` | Substituídos por logs estruturados em `data/logs/` | Fase 4 |

A pasta `discord_bot/` é **deletada** no merge final da fase 4. Antes disso, o `archive/discord_bot_legacy/` permanece como referência histórica.

## 6. Riscos

| Risco | Prob | Impacto | Mitigação |
|---|---|---|---|
| Tokens vazados no `archive/` continuarem expostos no histórico | alta | crítico | Rotacionar tokens **antes** de qualquer push; adicionar git-secrets pre-commit |
| `discord.py` 2.x mudar API entre patches | baixa | médio | Pin `discord.py>=2.3,<3.0` |
| Streaming via `message.edit` exceder rate limit do Discord (5/2s por canal) | média | médio | Debounce 800ms + buffer de chunks |
| Slash sync per-guild durante dev poluir guilda de produção | baixa | baixo | Settings separados por env |
| Reaction-trigger 🤖 abusado (alguém reage 100 msgs) | média | médio | Rate limit por usuário + cool-down por canal |
| Migração `memory.json` perder dados | baixa | baixo | Script é dry-run-default; backup automático |
| `send_dm` tool usada por agente para spam | média | crítico | Permission gate `SEND_DM` allowlist; rate limit dedicado por destinatário |

## 7. Critérios de "feito" do plano inteiro

1. `archive/discord_bot_legacy/` substituiu `discord_bot/`; tokens rotacionados.
2. Bot novo conecta ao Discord, sincroniza slash, responde menção, responde reply, ignora ruído.
3. Agente DEILE invocável via `/deile`, `@bot`, e reaction 🤖.
4. Streaming progressivo funciona em mensagens longas (visível visualmente).
5. `/help`, `/capabilities`, `/dlq`, `/forget`, `/sessions`, `/metrics` operacionais.
6. on_member_join saudação personalizada; threads herdam contexto.
7. Todos os testes E2E (fase 5) passam contra servidor Discord de teste.
8. Revisão cética concluída.

## 8. Mapa de fases

| Fase | Bloqueia | Esforço |
|---|---|---|
| 01 — Emergência + shell adapter | tudo abaixo | 2 dias |
| 02 — Pipeline completa + tools | fase 03 | 4 dias |
| 03 — Bridge agente DEILE + streaming + comandos canônicos | fase 04 | 4 dias |
| 04 — Eventos proativos + scheduler + admin | fase E2E | 3 dias |
| E2E — Bateria completa contra Discord real | revisão | 2 dias |
| Revisão | release | 1 dia |

Total: ~16 dias de dev sênior.

## 9. Dependências externas

| Dependência | Versão | Onde |
|---|---|---|
| `discord.py` | `>=2.3,<3.0` | `requirements.txt` (provider Discord) |
| `python-dotenv` | já no projeto | reuso |
| Foundation completa (fases 1-3) | mergeada | bloqueante |
| DEILE hooks (fases 1-3) | mergeadas | bloqueante para fase 3 deste plano |

## 10. Configuração runtime

```yaml
# config/deile_bot.yaml
foundation:
  sqlite_path: data/deile_bot.sqlite
  intent_classifier: heuristic
  agent_bridge_mode: in_process
  default_persona: developer

permissions:
  owners:
    - "01HZ-elimar-bot-user-id"
  per_action:
    EXECUTE_TOOL: { mode: owner_only }
    SEND_DM: { mode: allowlist, list: ["01HZ-elimar-bot-user-id"] }

personas:
  default: developer
  rules:
    - when: { provider: discord, scope: DM }
      use: developer
    - when: { provider: discord, scope: GROUP, channel_name_in: ["geral"] }
      use: host

providers:
  enabled_providers: ["discord"]

discord:
  token_env: DISCORD_TOKEN
  intents:
    message_content: true
    members: true
    presences: false
  command_prefix: "d!"
  slash_sync_guild_ids: []           # vazio = global; em dev: ["123..."]
  reaction_trigger_emoji: "🤖"
  message_edit_debounce_ms: 800
  on_member_join_enabled: true
  daily_digest:
    enabled: true
    cron: "0 9 * * *"                # 09:00 todo dia
    channels: ["geral"]
```
