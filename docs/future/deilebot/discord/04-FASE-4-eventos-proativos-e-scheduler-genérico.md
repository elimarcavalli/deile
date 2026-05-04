# Fase 4 — Eventos proativos + scheduler genérico + admin

> Sai da reatividade pura. Bot ouve member_join, on_thread_create, on_reaction_add (não-trigger), edits. Daily digest agendado. Scheduler genérico de cron jobs. Comandos admin: `/dlq`, `/forget`, `/sessions`, `/metrics`, `/audit recent`.

## Pré-requisitos

- Fases 1, 2, 3 mergeadas.
- Branch: `feat/discord-proactive-and-admin`.

## Entregáveis

### 4.1. Eventos passivos no Discord

Cog `events_cog.py`:

| Evento | Reação |
|---|---|
| `on_member_join(member)` | Se `on_member_join_enabled`, envelope sintético: "novo membro {nick}, dê boas-vindas curtas e pergunte de onde veio". Persona "host". Saída no canal `welcome_channel_name` da settings. |
| `on_thread_create(thread)` | Registra thread no `ConversationStore` com `parent_channel_id` populado. Não responde. |
| `on_message_edit(before, after)` | Persiste atualização (campo `edited_at`); reavalia se a edição muda significativamente o sentido (Levenshtein > 30% E msg original já gerou resposta) → envia "Atualizei minha resposta dada sua edição." opcionalmente. Configurável. |
| `on_message_delete(message)` | Marca `deleted_at` no SQLite (não apaga registro). Útil para audit. |
| `on_raw_reaction_add` (não-trigger) | Conta como "engajamento". Se reage 👍 a uma resposta do bot, métrica `bot_engagement_positive_total++`. Se reage 👎, `bot_engagement_negative_total++`. |
| `on_typing` | Ignorado por default (overhead alto). |

### 4.2. Inheritance de contexto em threads

Quando uma thread nasce sob um canal, o `IngressPipeline` para mensagens nessa thread injeta no histórico **as últimas 5 mensagens do canal pai** antes das mensagens da thread. Implementação: `ConversationStore.get_recent_messages_with_parent(channel)` que faz UNION ordenado.

### 4.3. Scheduler genérico

`deilebot/runtime/scheduler.py` (nova foundation, mas vive em runtime para não acoplar a pipeline):

```python
class CronJob(BaseModel):
    name: str
    cron: str                       # ex.: "0 9 * * *"
    handler: str                    # módulo.func
    args: dict = {}
    enabled: bool = True

class Scheduler:
    def __init__(self, jobs: list[CronJob]): ...
    async def start(self): ...
    async def stop(self): ...
```

Configuração:

```yaml
# config/deilebot.yaml
scheduler:
  jobs:
    - name: daily_digest
      cron: "0 9 * * *"
      handler: deilebot.providers.discord.jobs.daily_digest:run
      args: { channels: ["geral"], lookback_hours: 24 }
      enabled: true
```

`daily_digest:run`:

```python
async def run(adapter: DiscordAdapter, ingress: IngressPipeline, channels: list[str], lookback_hours: int):
    for ch_name in channels:
        ch = adapter.find_channel_by_name(ch_name)
        history = await store.get_messages_in_window(ch, hours=lookback_hours)
        env = synth_envelope("Resuma os últimos {} ocorridos em #{} em até 5 bullets.".format(lookback_hours, ch_name))
        env = with_force_respond(env)
        await ingress.handle(env, adapter)
```

`scheduler_333.py` legado vira deprecation no `archive/`; migração documentada (config `cron: "33 3 * * *"`).

### 4.4. Comandos admin

Cog `admin_cog.py` — todos com `@app_commands.checks.has_role(...)` ou check customizado via `PermissionGate.is_owner`.

| Comando | Função |
|---|---|
| `/dlq list [provider]` | Lista até 25 entradas pendentes na DLQ |
| `/dlq replay [provider]` | Replay todas as pendentes |
| `/dlq purge --older-than-days N` | Apaga DLQ antiga |
| `/forget --user <bot_user_id> [--before <date>]` | Apaga histórico do usuário no `ConversationStore` |
| `/sessions list` | Lista sessões persistidas no `SessionStore` (DEILE) |
| `/sessions clear --user <bot_user_id>` | Apaga sessão de um usuário |
| `/sessions purge --older-than-days N` | Limpa sessões antigas |
| `/metrics` | Snapshot do `MetricsCollector` em embed |
| `/audit recent [--type X] [--user Y] [--limit 25]` | Últimas entradas do audit log |
| `/persona override --user <id> --persona <name>` | Define persona específica para um usuário (sobrescreve regras) |
| `/persona reset --user <id>` | Remove override |

### 4.5. CLI completo

`python3 -m deilebot.cli`:

```
deilebot run --provider discord [--guild-id ID]      # roda o bot
deilebot dlq list [--provider X]                      # CLI espelhando /dlq
deilebot sessions purge --older-than-days N
deilebot metrics
deilebot migrate-memory-json --source <path>
deilebot persona list
```

### 4.6. Logs estruturados

`deilebot/foundation/logging.py`:

```python
def setup_logging(settings: BotSettings):
    """JSON-friendly stdout + rotating file em data/logs/deilebot.log."""
```

Substitui `bot.log` plain text. Cada linha é JSON com `ts, level, logger, event, ...campos`.

### 4.7. Testes desta fase

| Caso | Cobertura |
|---|---|
| `on_member_join` mockado → envelope sintético chega no pipeline | Unit |
| `on_message_edit` significativo → resposta atualizada | Unit |
| Thread herda contexto do parent | Integration |
| Scheduler dispara `daily_digest` no horário simulado (`pytest-freezegun`) | Integration |
| `/dlq list` mostra entradas; `/dlq replay` esvazia | Integration |
| `/forget --user X` apaga registros de X no `ConversationStore` | Integration |
| `/persona override` muda persona usada para o usuário | Integration |
| Logs JSON têm campos esperados | Unit |

## Critérios de aceitação

| # | Verificar |
|---|---|
| AC-1 | Member join produz boas-vindas (smoke real) |
| AC-2 | Threads herdam contexto (smoke real) |
| AC-3 | Daily digest dispara no cron (manual ou freezegun) |
| AC-4 | `/dlq`, `/forget`, `/sessions`, `/metrics`, `/audit`, `/persona` operacionais |
| AC-5 | `archive/discord_bot_legacy/scheduler_333.py` deprecated; novo scheduler cobre |
| AC-6 | Logs estruturados em `data/logs/deilebot.log` |
| AC-7 | `python3 -m deilebot.cli --help` mostra todos os subcomandos |

## Pontos de atenção

- **`on_typing` desligado** por default — overhead alto, valor baixo.
- **`on_message_delete`** preserva registro (compliance), só marca como deletado.
- **Scheduler lib**: `apscheduler` ou `croniter` + asyncio. Documentar a escolha em `DECISOES.md`.
- **`/forget`** é destrutivo; pedir confirmação interativa (component button "Confirmar/Cancelar").
- **Cuidado com privilégio**: comandos admin são `owner_only`. `PermissionGate` precisa estar 100% honrando.

## Estimativa

3 dias.
