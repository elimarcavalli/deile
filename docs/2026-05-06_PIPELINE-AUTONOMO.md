# Pipeline Autônomo de Issues/PRs + Cron Genérico

> **Intents de origem:** [#87](https://github.com/elimarcavalli/deile/issues/87) (pipeline autônomo) e [#86](https://github.com/elimarcavalli/deile/issues/86) (agendador de prompts)
>
> **Versão:** V1 — implementação inicial completa
>
> **Decisões arquiteturais relacionadas:** #18, #19, #20 — ver `docs/system_design/DECISOES.md`

---

## 1. Overview

O DEILE adquiriu a capacidade de **operar autonomamente sobre o repositório GitHub**, sem intervenção humana por mensagem. Quando um operador abre uma issue com o label `~workflow:nova`, o pipeline:

1. Revisa o corpo da issue com DEILE e a transita para `~workflow:revisada`.
2. Cria um worktree isolado, invoca `claude -p <prompt>` (Claude Code one-shot) para implementar, e abre uma PR.
3. Invoca Claude Code na PR para revisar, corrigir e dar merge.

Complementarmente, o **cron genérico** (intent #86) permite que qualquer usuário (ex.: via Discord) agende prompts naturais — "rodar X toda segunda às 9h" ou "executar Y amanhã às 18h" — que o `CronRunner` dispara como novas turns do agente DEILE.

**Problema resolvido:** eliminar o ciclo manual de "usuário pede, DEILE implementa, usuário abre PR, usuário pede revisão". O pipeline automatiza o loop completo; o cron permite agendamento assíncrono de qualquer instrução.

---

## 2. Architectural Decisions

### Patterns escolhidos

| Pattern | Aplicação |
|---|---|
| Polling loop async | `PipelineMonitor._run_forever` usa `asyncio.wait_for` com timeout para poll a cada `poll_interval_seconds` sem threads |
| Strategy de autenticação | `ClaudeDispatcher.prefer_subscription_auth` isola a decisão de qual key usar em um único ponto |
| Hash sharding | `MonitorIdentity.owns(key)` distribui issues/PRs entre instâncias sem coordenador — ver Decisão #18 |
| Separação de stores | `ScheduleStore` (YAML) para pipeline scheduler; `CronStore` (SQLite) para cron genérico — ver Decisão #19 |
| Tool + Command dual surface | Cada funcionalidade exposta tanto como `Tool` (LLM pode invocar) quanto como `/command` (operador pode usar diretamente no REPL) |

### Trade-offs

| Aspecto | Escolha | Alternativa descartada |
|---|---|---|
| Coordenação entre monitores | Labels GitHub (`~batch:<sha>`) como lock otimista | Lock distribuído (Redis, etcd) — dependência operacional pesada para uso local |
| Scheduler do pipeline | YAML por monitor | SQLite compartilhado — YAML é legível/editável manualmente; SQLite seria melhor para N>10 monitores |
| Cron genérico | SQLite | YAML — SQLite suporta escrita concorrente com threading lock; YAML corromperia em acesso paralelo |
| Invocação de Claude Code | subprocess `claude -p` | API direta (anthropic SDK) — Claude Code CLI tem CLAUDE.md e contexto de repositório por design |

### Async/await

- `PipelineMonitor.start/stop/tick` são `async` e usam `asyncio.create_task` para o loop
- `ClaudeDispatcher.run` usa `asyncio.create_subprocess_exec` + `asyncio.wait_for` para timeout
- `CronRunner.tick/_fire` são `async`; `CronStore` é síncrono (SQLite) mas protegido com `threading.Lock` para ser seguro em contexto multithread-over-asyncio

### Segurança

- `ClaudeDispatcher` strip de `ANTHROPIC_API_KEY` por default — ver Decisão #20
- Labels `~batch:<sha>` com SHA de 8 chars evitam colisões de claim entre monitores
- `PIDLockFile` (`lockfile.py`) impede dois monitores com o mesmo `monitor_id` no mesmo host

---

## 3. Component Architecture

### Core Components

| Módulo | Responsabilidade |
|---|---|
| `orchestration/pipeline/monitor.py` | `PipelineMonitor` — driver do loop; orquestra os 3 estágios; mantém `_Stats` |
| `orchestration/pipeline/github_client.py` | `GitHubClient` — wrapper de `gh issue list/view/label`, `gh pr list/label/merge` |
| `orchestration/pipeline/worktree_manager.py` | `WorktreeManager` — cria/remove `.worktrees/<branch>` via `git worktree add` |
| `orchestration/pipeline/claude_dispatcher.py` | `ClaudeDispatcher` — subprocess `claude -p <prompt>`, timeout, strip de keys |
| `orchestration/pipeline/notifier.py` | `DiscordNotifier` — DMs de notificação em cada transição de estado |
| `orchestration/pipeline/identity.py` | `MonitorIdentity` — `monitor_id`, `shard_index/count`, `owns(key)`, branch prefix |
| `orchestration/pipeline/lockfile.py` | `acquire/release` — PID lock em arquivo |
| `orchestration/pipeline/scheduler.py` | `ScheduleStore`, `Schedule`, `RecurringEntry`, `OneshotEntry`, `compute_pending` |
| `orchestration/pipeline/cron.py` | `next_after(expr, anchor)` — parser de expressões cron 5-field |
| `orchestration/pipeline/labels.py` | Constantes de labels; `is_batch_label`, `make_batch_label` |
| `cron/store.py` | `CronStore` (SQLite), `CronEntry`, `make_id` |
| `cron/runner.py` | `CronRunner` — poll loop 30s, `fire_callback`, DM de resultado |

### Tools registradas

| Tool | Módulo | Categoria | SecurityLevel |
|---|---|---|---|
| `pipeline` | `tools/pipeline_tool.py` | SYSTEM | MODERATE |
| `pipeline_schedule` | `tools/pipeline_schedule_tool.py` | SYSTEM | MODERATE |
| `cron_create` | `tools/cron_create_tool.py` | SYSTEM | MODERATE |
| `cron_list` | `tools/cron_list_tool.py` | SYSTEM | SAFE |
| `cron_delete` | `tools/cron_delete_tool.py` | SYSTEM | MODERATE |

### Comandos slash

| Comando | Módulo |
|---|---|
| `/pipeline` | `commands/builtin/pipeline_command.py` |

### Storage

| Artefato | Caminho | Formato |
|---|---|---|
| Schedule do pipeline (por monitor) | `config/pipeline_schedule_<monitor_id>.yaml` | YAML |
| CronStore | `data/cron.db` (ou `DEILE_CRON_DB_PATH`) | SQLite |
| Worktrees | `.worktrees/<branch>` (ou `.worktrees/<monitor_id>/<branch>`) | git worktree |

---

## 4. Implementation Details

```
PipelineMonitor
├── config: PipelineConfig
│   ├── repo: str
│   ├── base_repo_path: Path
│   ├── poll_interval_seconds: int (default 60)
│   ├── main_branch: str (default "main")
│   ├── branch_prefix: str (default "auto/issue-")
│   ├── notify_user_id: Optional[str]
│   ├── enable_review/implement/pr_review: bool
│   └── use_pid_lock: bool
├── identity: MonitorIdentity
│   ├── monitor_id: str
│   ├── shard_index: int
│   ├── shard_count: int
│   ├── owns(key: str) -> bool  # SHA-256 % shard_count
│   └── branch_prefix(action) -> str
├── stats: _Stats
│   ├── ticks, issues_reviewed, issues_implemented
│   ├── prs_reviewed, errors, catchup_runs, scheduled_runs
├── async start() → acquires PID lock → catch_up_pending → create_task(_run_forever)
├── async stop() → set stop_event → wait task → release lock
├── async tick() → check schedule → run pending or run all stages
├── Stage 1: _review_one_new_issue()
│   ├── list issues with ~workflow:nova
│   ├── identity.owns(issue.title) filter + shard
│   ├── claim_with_batch → add ownership label
│   └── transition nova → em_revisao → revisada
├── Stage 2: _implement_one_reviewed_issue()
│   ├── list issues with ~workflow:revisada owned by this monitor
│   ├── WorktreeManager.create_branch_worktree(branch)
│   ├── ClaudeDispatcher.run(implement_prompt, cwd=worktree)
│   └── transition revisada → em_pr
└── Stage 3: _review_one_open_pr()
    ├── list open PRs not draft, no ~review:concluida, owned branch
    ├── claim_with_batch → transition pendente → em_andamento
    ├── ClaudeDispatcher.run(review_prompt, cwd=worktree)
    └── transition em_andamento → concluida

CronRunner
├── store: CronStore
├── fire_callback: async (CronEntry) -> str
├── poll_interval_seconds: int (default 30)
├── notify_dm: Optional[async (user_id, msg) -> dict]
├── async start() → create_task(_run_forever)
├── async tick() → store.list_due() → _fire(entry) for each
└── async _fire(entry)
    ├── await fire_callback(entry)
    ├── store.mark_fired(entry.id, result=summary)
    └── notify_dm if entry.notify_user_id
```

---

## 5. API Specification

### PipelineMonitor

| Method | Parameters | Return | Async | Notes |
|---|---|---|---|---|
| `start()` | — | `None` | Yes | Idempotente; levanta `LockHeldError` se PID lock já ocupado |
| `stop()` | — | `None` | Yes | Wait 5s, cancela se timeout |
| `tick()` | — | `None` | Yes | Um ciclo completo dos 3 estágios (ou entradas de schedule) |
| `stats` | — | `_Stats` | No (property) | Contadores acumulativos |

### ClaudeDispatcher

| Method | Parameters | Return | Async | Notes |
|---|---|---|---|---|
| `run(prompt, cwd, env=None, extra_args=())` | `str`, `Path` | `ClaudeRunResult` | Yes | Timeout default 1800s |

### CronStore

| Method | Parameters | Return | Notes |
|---|---|---|---|
| `add(entry)` | `CronEntry` | `None` | Lança `CronStoreError` se id já existe |
| `list_all(only_enabled=False)` | `bool` | `List[CronEntry]` | Order by next_fire_at |
| `list_due(now=None)` | `datetime` | `List[CronEntry]` | enabled=1 AND next_fire_at <= now |
| `mark_fired(id, when=None, result=None)` | `str`, `datetime`, `str` | `None` | Avança next_fire_at ou desabilita one-shot |
| `remove(id)` | `str` | `bool` | True se linha deletada |
| `set_enabled(id, enabled)` | `str`, `bool` | `bool` | True se linha atualizada |

### ScheduleStore

| Method | Parameters | Return | Notes |
|---|---|---|---|
| `load()` | — | `Schedule` | Retorna `Schedule()` vazio se arquivo ausente |
| `save(schedule)` | `Schedule` | `None` | Cria `config/` se necessário |

### Schedule

| Method | Parameters | Return | Notes |
|---|---|---|---|
| `compute_pending(now=None)` | `datetime` | `List[PendingRun]` | Coalesça misseds por padrão; `replay_all=True` replica cada slot |
| `mark_run(run, when=None)` | `PendingRun` | `None` | Atualiza last_run_at (recurring) ou completed (oneshot) |

---

## 6. Configuration Schema

### `config/pipeline_schedule_<monitor_id>.yaml`

```yaml
recurring:
  - id: review_loop           # str, alphanum + _- obrigatório
    action: review            # review | implement | pr_review
    cron: "*/5 * * * *"       # 5-field cron expression em UTC
    enabled: true             # false congela a entrada sem deletar
    last_run_at: null         # ISO-8601 UTC; null = nunca rodou
    replay_all: false         # true = replay cada slot missed no downtime

oneshot:
  - id: oneshot-impl-99
    action: implement
    target_issue: 99          # contexto; não modifica o pipeline hoje
    run_at: "2026-05-06T18:00:00Z"
    completed: false
```

### `CronEntry` (SQLite)

```
id TEXT PRIMARY KEY
prompt TEXT NOT NULL
cron TEXT                    -- NULL para one-shot
run_at TEXT                  -- NULL para recorrente
next_fire_at TEXT            -- ISO-8601 UTC; index para list_due
last_fired_at TEXT
created_by TEXT              -- e.g. "discord:1234567890"
notify_user_id TEXT          -- Discord snowflake para DM de resultado
enabled INTEGER NOT NULL DEFAULT 1
created_at TEXT NOT NULL
last_result TEXT             -- resumo da última execução (max 1000 chars)
```

### Variáveis de ambiente

Ver seção "Pipeline + Cron — variáveis de ambiente" em `docs/system_design/09-CONFIGURACAO.md`.

---

## 7. Security Implementation

| Aspecto | Implementação |
|---|---|
| Key isolation | `ClaudeDispatcher` strip de `ANTHROPIC_API_KEY` (Decisão #20) — o subprocess `claude` usa assinatura do operador |
| PID lock | `lockfile.py` impede dois monitores com mesmo `monitor_id` no mesmo host; levanta `LockHeldError` com PID do holder |
| Label-based optimistic lock | `~batch:<sha8>` adicionado atomicamente via GitHub API antes de processar; se outro monitor já adicionou, o claim falha silenciosamente |
| Worktree isolation | Cada issue/PR tem seu próprio worktree; Claude Code opera num subdiretório isolado sem acesso ao repo raiz |
| Prompt injection | Os prompts (`IMPLEMENT_PROMPT_TEMPLATE`, `REVIEW_PROMPT_TEMPLATE`) truncam o issue body em 6000 chars; não injetam conteúdo não-sanitizado em flags de shell |
| Tool SecurityLevel | Todas as tools de pipeline/cron classificadas como `MODERATE` (exceto `cron_list`: `SAFE`); passam por `ApprovalSystem` se configurado |
| Sem PII no cron | `created_by` e `notify_user_id` são opcionais; a decisão de logar esses campos cabe ao operador |

---

## 8. Testing Strategy

### Unit Tests

```python
# deile/tests/orchestration/pipeline/
async def test_pipeline_monitor_tick_dispatches_stage1():
    github = MockGitHubClient(issues=[IssueRef(number=1, labels=[WORKFLOW_NEW])])
    monitor = PipelineMonitor(config, github=github)
    await monitor.tick()
    assert monitor.stats.issues_reviewed == 1

async def test_cron_store_add_and_list_due():
    store = CronStore(Path(tmp_path) / "cron.db")
    entry = CronEntry(id="x", prompt="test", cron="* * * * *")
    store.add(entry)
    due = store.list_due(now=datetime.now(timezone.utc) + timedelta(minutes=2))
    assert len(due) == 1

async def test_monitor_identity_shard():
    id1 = MonitorIdentity(monitor_id="a", shard_index=0, shard_count=2)
    id2 = MonitorIdentity(monitor_id="b", shard_index=1, shard_count=2)
    # every key is owned by exactly one monitor
    keys = ["issue-1", "issue-2", "issue-3", "issue-4"]
    for k in keys:
        assert id1.owns(k) != id2.owns(k)
```

### Integration Tests

| Cenário | Cobertura |
|---|---|
| Pipeline tick end-to-end (mocked `gh`) | Todos os 3 estágios; transições de label corretas |
| `CronRunner.tick` com fire_callback | Entry marked_fired após disparo; one-shot desabilitado |
| `PipelineScheduleTool` add_recurring + list | Persistência YAML roundtrip |
| `CronCreateTool` + `CronListTool` + `CronDeleteTool` | CRUD completo via tool interface |
| Sharding: 2 monitors com shard_count=2 | Particionamento sem sobreposição |

### Segurança

| Cenário | Verificação |
|---|---|
| Monitor sem PID lock / lock já ocupado | `LockHeldError` levantado com PID correto |
| `CronEntry` com `cron` e `run_at` ao mesmo tempo | `CronStoreError` levantado em `__post_init__` |
| Prompt vazio em `cron_create` | `ToolResult.error_result("MISSING_PROMPT")` |
| `MonitorIdentity` com `shard_index >= shard_count` | `IdentityError` |

---

## 9. Usage Examples

### CLI (REPL)

```
# Iniciar o pipeline
/pipeline start

# Verificar status
/pipeline status

# Forçar um tick para depuração
/pipeline tick

# Parar
/pipeline stop
```

### Agendamento via LLM (ex.: Discord → DEILE)

```
Usuário: "Revise as issues novas a cada 5 minutos"
DEILE invoca: pipeline_schedule(action="add_recurring",
              trigger_action="review", cron="*/5 * * * *")

Usuário: "Implemente a issue 99 amanhã às 18h"
DEILE invoca: pipeline_schedule(action="add_oneshot",
              trigger_action="implement", run_at="2026-05-07T18:00:00Z",
              target_issue=99)

Usuário: "Agende um relatório de custos toda segunda às 9h"
DEILE invoca: cron_create(prompt="Gere um relatório de custo das últimas
              24h e envie via DM", cron="0 9 * * 1",
              notify_user_id="1234567890")
```

### Programático (bootstrap do daemon)

```python
# No deilebot / daemon startup
from deile.orchestration.pipeline.monitor import PipelineMonitor, PipelineConfig
from deile.cron.store import CronStore
from deile.cron.runner import CronRunner

config = PipelineConfig(
    repo=os.environ["DEILE_PIPELINE_REPO"],
    base_repo_path=Path(os.environ["DEILE_PIPELINE_BASE_PATH"]),
    notify_user_id=os.environ.get("DEILE_PIPELINE_NOTIFY_USER_ID"),
    use_pid_lock=True,
)
monitor = PipelineMonitor(config)
await monitor.start()   # dispara o loop

cron_store = CronStore(Path("data/cron.db"))
cron_runner = CronRunner(
    cron_store,
    fire_callback=my_agent_fire,  # async (entry) -> str
    notify_dm=my_discord_dm,      # async (user_id, msg) -> dict
)
await cron_runner.start()
```

---

## 10. Performance Characteristics

| Aspecto | Característica |
|---|---|
| Poll interval | 60s por padrão; configurável em `PipelineConfig.poll_interval_seconds` |
| Cron runner poll | 30s por padrão; configurável em `CronRunner.poll_interval_seconds` |
| Claude Code timeout | 1800s (30 min) por invocação; configurável em `ClaudeDispatcher.timeout_seconds` |
| GitHub API | Cada tick faz no máximo 3 chamadas de listagem + N operações de label (N ≤ 3 por issue/PR) |
| Worktrees | Criados em disco; removidos manualmente (worktree cleanup não é automático — ver § Rollback) |
| `CronStore.list_due` | `O(log N)` via índice `(enabled, next_fire_at)` |
| Schedule YAML | Parse a cada tick; aceitável para schedules com < 100 entradas |
| Concorrência | Um monitor por identidade por host; shards rodam em processos/máquinas separadas |

---

## 11. Monitoring & Observability

### Logs

| Logger | Eventos |
|---|---|
| `deile.orchestration.pipeline.monitor` | Tick #{n}, stage transitions, errors |
| `deile.orchestration.pipeline.claude_dispatcher` | `invoking Claude Code: ... in <cwd>` |
| `deile.orchestration.pipeline.github_client` | gh CLI calls + stderr |
| `deile.orchestration.pipeline.notifier` | DM sent / DM failed |
| `deile.cron.runner` | Entry fired / fire failed |

### Métricas (via `PipelineMonitor.stats`)

| Contador | Significado |
|---|---|
| `ticks` | Total de ticks executados desde start |
| `issues_reviewed` | Issues que passaram pelo estágio 1 com sucesso |
| `issues_implemented` | Issues que passaram pelo estágio 2 com sucesso |
| `prs_reviewed` | PRs que passaram pelo estágio 3 com sucesso |
| `errors` | Ticks que levantaram exceção não-esperada |
| `catchup_runs` | Runs de catch-up executados no startup |
| `scheduled_runs` | Runs de schedule executados em ticks normais |

### Discord DMs

`DiscordNotifier` envia DM para `DEILE_PIPELINE_NOTIFY_USER_ID` nos seguintes eventos:
- Issue picked up (nome + URL)
- Issue reviewed
- Implementation started (branch name)
- Implementation finished (PR URL)
- PR picked up
- PR reviewed (merged: true/false)
- Error (stage name + mensagem truncada em 1500 chars)

---

## 12. Migration & Deployment

### Backwards-compat

- **Monitor único (sem env vars):** comportamento idêntico ao pré-#87. `MonitorIdentity.is_default=True`, branch prefix `auto/issue-`, worktrees em `.worktrees/<branch>`.
- **Sem `config/pipeline_schedule_<id>.yaml`:** monitor cai para modo legacy "todas as ações a cada tick".
- **Sem `DEILE_CRON_DB_PATH`:** CronStore criado em `data/cron.db`; diretório auto-criado.
- **Tools não registradas automaticamente:** `pipeline_tool`, `pipeline_schedule_tool`, `cron_*` requerem registro explícito via `register_tool(...)`. O `auto_discover()` existente não os cobre — o daemon/bootstrap deve registrá-los.

### Deploy de múltiplos monitores

```bash
# Monitor 0 de 2
DEILE_PIPELINE_MONITOR_ID=monitor-0 \
DEILE_PIPELINE_SHARD_INDEX=0 \
DEILE_PIPELINE_SHARD_COUNT=2 \
python3 deile.py

# Monitor 1 de 2 (outra máquina ou outro processo)
DEILE_PIPELINE_MONITOR_ID=monitor-1 \
DEILE_PIPELINE_SHARD_INDEX=1 \
DEILE_PIPELINE_SHARD_COUNT=2 \
python3 deile.py
```

### Autostart via deilebot

Configurar no `config/deilebot.yaml`:
```yaml
# (seção a ser definida no deilebot)
pipeline:
  autostart: true   # equivalente a DEILE_PIPELINE_AUTOSTART=1
cron:
  autostart: true   # equivalente a DEILE_CRON_AUTOSTART=1
```

Ou via env vars no daemon:
```
DEILE_PIPELINE_AUTOSTART=1
DEILE_CRON_AUTOSTART=1
```

---

## 13. Troubleshooting Guide

| Problema | Diagnóstico | Solução |
|---|---|---|
| Monitor não inicia | Log `another monitor with id=X is already running` | Matar o processo holder (PID no log) ou usar `monitor_id` diferente |
| Issue não é claimed | `batch_id` já presente ou `identity.owns()` retorna False | Verificar `DEILE_PIPELINE_SHARD_*`; inspecionar labels da issue via `gh issue view` |
| `claude -p` timeout | Log `claude -p timed out after 1800s` | Issue/PR muito complexo; aumentar `ClaudeDispatcher.timeout_seconds` |
| PR não aberta após implementação | `_extract_pr_url` não encontrou URL no stdout | Claude Code não abriu PR; inspecionar logs do subprocess em `result.stderr` |
| Worktrees acumulando em disco | Não há cleanup automático | Rodar `git worktree prune` + `git worktree list` manualmente na raiz do repo |
| `CronStore` falha ao abrir | `data/` não existe ou sem permissão | Verificar `DEILE_CRON_DB_PATH`; criar diretório manualmente |
| Entry cron nunca dispara | `enabled=0` ou `next_fire_at` no passado sem advance | Usar `cron_list` tool para inspecionar; `cron_delete` + `cron_create` para re-agendar |
| DMs não chegam | `DEILE_PIPELINE_NOTIFY_USER_ID` não configurado ou `deilebot` offline | Verificar env var; checar log do notifier |

---

## 14. Future Considerations

| Aspecto | Nota |
|---|---|
| Cleanup automático de worktrees | `WorktreeManager` poderia listar e remover worktrees de branches já mergeadas |
| Retry com backoff | `ClaudeDispatcher` atualmente não retenta; um resultado `ok=False` encerra o estágio sem retry |
| Dashboard de pipeline | Stats em `PipelineMonitor.stats` são efêmeros (em memória); persistir em SQLite permitiria queries históricas |
| Cron com suporte a timezones | `CronStore` e `next_after` operam em UTC; UI de Discord pode querer aceitar "às 9h BRT" e converter |
| `CronRunner` multi-host | Sem lock distribuído, dois runners no mesmo `CronStore` disparariam a mesma entry; adicionar `~batch:`-style locking no SQLite |
| `replay_all` no cron genérico | `CronEntry` não tem `replay_all`; se o runner ficar offline por horas, ele coalesce o catchup (fire uma vez) |
| Scheduler do pipeline em SQLite | Para N > ~20 monitores ou schedules dinâmicos, migrar de YAML para SQLite por consistência |
| Débito técnico | `PluginSandbox` em issue #54 poderia isolar o Claude Code subprocess em vez de rodar com mesmo usuário |
