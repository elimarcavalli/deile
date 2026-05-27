# 02 — Arquitetura de alto nível

> Camadas, subpacotes e dependências do `deile/`. Itens individuais (tools, comandos, parsers) descritos em [`04-MODELO-COMPONENTES.md`](04-MODELO-COMPONENTES.md).

## Camadas (vista lógica)

```
┌─────────────────────────────────────────────────────────────────┐
│  CLI / UI                                                       │
│  Rich Terminal UI, autocompletion, temas, status, streaming     │
│  → deile/ui/, deile.py (DeileAgentCLI)                          │
└──────────────────────┬──────────────────────────────────────────┘
                       │ chamadas síncronas + asyncio.to_thread
┌──────────────────────▼──────────────────────────────────────────┐
│  Núcleo do agente                                               │
│  Mediator (DeileAgent), análise de intenção, gestão de contexto │
│  e sessão, file resolver, executor de tool-loop                 │
│  → deile/core/                                                  │
└──────────────────────┬──────────────────────────────────────────┘
                       │ orquestração de componentes
┌──────────────────────▼──────────────────────────────────────────┐
│  Camada de serviços                                             │
│  Tool Registry, Command Registry, Parser Registry,              │
│  Plan Manager, Workflow Executor, Memory Manager,               │
│  Persona Manager, Approval System                               │
│  → deile/tools, deile/commands, deile/parsers,                  │
│    deile/orchestration, deile/memory, deile/personas            │
└──────────────────────┬──────────────────────────────────────────┘
                       │ ports → adapters
┌──────────────────────▼──────────────────────────────────────────┐
│  Integração / Infraestrutura                                    │
│  Multi-provider router (legado + tier), providers concretos,    │
│  storage SQLite, security, scanner de segredos, event bus       │
│  → deile/core/models/, deile/storage, deile/security,           │
│    deile/events, deile/infrastructure                           │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Extensão                                                       │
│  Plugins, evolution (auto-melhoria), personas customizadas      │
│  → deile/plugins, deile/evolution                               │
└─────────────────────────────────────────────────────────────────┘
```

## Subpacotes do `deile/`

| Subpacote | Responsabilidade |
|---|---|
| `core/` | Agente principal (`DeileAgent`), análise de intenção (`IntentAnalyzer`), gestão de contexto (`ContextManager`), exceções, file resolver (`SmartFileResolver`), proactive analyzer (`ProactiveAnalyzer`), tool-loop executor (`ToolLoopExecutor`), mapeador intent→tier (`classify_tier`) |
| `core/models/` | Multi-provider: `ModelRouter` (legado), `TierRouter`, `ModelCatalog`, `RoutingPolicy`, `CircuitBreaker`, `bootstrap`, providers concretos (Anthropic, OpenAI, DeepSeek, Gemini) |
| `tools/` | Interface `Tool`, `ToolSchema`, `ToolContext`, `ToolResult`, `ToolRegistry` com auto-discovery; tools concretas como siblings em `deile/tools/*.py` |
| `commands/` | Slash commands, `CommandRegistry`, builtins em `deile/commands/builtin/` |
| `parsers/` | Pipeline de parsing: comando, arquivo, diff, parser inteligente; `ParserRegistry` |
| `orchestration/` | `PlanManager`, `WorkflowExecutor`, `TaskManager`, `SQLiteTaskManager`, `ApprovalSystem`, `ArtifactManager`, `RunManager` |
| `orchestration/pipeline/` | Pipeline autônomo de issues/PRs/menções: `PipelineMonitor` (loop de polling; estágios `classify` → `review` → `implement` (+`resume`) → `pr_review` → `follow_ups`, mais `process_mentions` como roteador por papel); `PipelineImplementer` (estratégia plugável — `ClaudeImplementer` via `claude -p` em worktree **ou** `WorkerImplementer` que despacha ao `deile-worker` por HTTP, selecionada por `dispatch_mode`); `ResumeTracker` (`resume_state.py` — cadência/fingerprint/tentativa por item, issue #254); `briefs.py` (templates de prompt: implement/review/review-only/address/mention + variantes de resume; **tooling-agnostic via placeholders `{forge_*_cmd}` preenchidos pelo `forge/cli_renderer`** — Decisão #41); `github_client.py` (shim deprecated que re-exporta de `forge/`, mantido por 1 release); `WorktreeManager` (`_ensure_forge_remote` + alias `github` para repos GH — Decisão #41), `ClaudeDispatcher` (`claude -p` subprocess), `DiscordNotifier`, `MonitorIdentity` (sharding hash-based), `LockFile` (PID lock), `ScheduleStore`/`Schedule`/`RecurringEntry`/`OneshotEntry` (scheduler YAML por monitor), `cron.py` (parser de expressões 5-field) |
| `orchestration/forge/` | Camada de adapter forge-agnóstica (Decisão #41): `ForgeClient` ABC (28+ métodos cobrindo issues, PR/MR, labels, comentários, merge, CI), implementada por `GitHubForge` (port do legado `GitHubClient`, via `gh`) e `GitLabForge` (nova, via `glab` + REST v4); `ForgeConfig`/`ForgeKind` (dataclass + enum); `IssueRef`/`PrRef`/`MrRef`/`CommentRef`/`MentionTrigger` (refs frias com `from_gh_json`/`from_gl_json`); `parse_forge_url`/`find_first_pr_url`/`find_last_pr_url` (URL parser que reconhece os dois forges + hosts customizados); `detect_forge_kind`/`build_forge_config`/`build_forge` (detecção em 3 camadas: env override → URL host → path heuristic, com fallback p/ GH compat); `cli_renderer.render_brief_cmds` (gera snippets `gh`/`glab` por verbo, consumido pelos briefs); `ForgeRouter` (singleton com cache `(host, project)→client` para sessões CLI multi-repo); erros tipados (`ForgeError`, `ForgeDetectionError`, `ForgeCliNotFound`, `ForgeCommandError`, `MergeBlocked`, `MergeBlockedByPipeline`). |
| `cron/` | Agendador genérico de prompts: `CronStore` (SQLite, `data/cron.db`), `CronEntry` (recurring + one-shot), `CronRunner` (poll loop 30s, dispara `fire_callback`) |
| `memory/` | `MemoryManager` + 4 camadas (`WorkingMemory`, `EpisodicMemory`, `SemanticMemory`, `ProceduralMemory`) + `MemoryConsolidator` |
| `runtime/` | Estado vivo por-processo publicado em `~/.deile/run/<id>.json` (`InstanceState`, `get_instance_state`, `pid_alive`). Separado da memória (camadas em `deile/memory/`) porque é volátil, por-processo, e expõe metadados de execução para introspecção externa (painel TUI, futura observabilidade). Issue #303 (Fase 1). |
| `security/` | `PermissionManager`, `AuditLogger`, `SecretsScanner` |
| `personas/` | `BasePersona`, `BaseAutonomousPersona`, `PersonaManager`, `PersonaLoader`, `instruction_loader`, `builder`, `context`, `library/` (YAMLs), `instructions/` (MDs), `memory/integration.py` |
| `config/` | `Settings` (singleton via `get_settings()`), `ConfigManager`, YAMLs (`api_config`, `commands`, `intent_patterns`, `model_providers`, `persona_config`, `system_config`), `profiles/` |
| `events/` | `EventBus`, `Event`, `EventType`, `EventPriority`; handlers de eventos |
| `storage/` | `logs`, `debug_logger`, `embeddings`, `usage_repository` (com `BudgetGuard` e `BudgetExceeded`), `aio_fileio` (helpers `read_json`/`write_json`/`write_text` que envolvem `asyncio.to_thread` — usados por `orchestration/` para não bloquear o event loop) |
| `infrastructure/` | Adapters externos (ex.: `google_file_api`), monitoring |
| `plugins/` | `plugin_manager`, `dependency_resolver`, `hot_loader` (via `watchdog`), `marketplace`, `sandbox` (`PluginSandbox` — skeleton, não isola; ver issue #54) |
| `evolution/` | `self_analyzer`, `code_modifier`, `improvement_loop` (gated atrás de `experimental=True`), `benchmarker`, `rollback_manager` |
| `ui/` | `ConsoleUIManager`, `DisplayManager`, `streaming_renderer`, `emoji_support`, `themes/`, `components/`, `completers/` (`hybrid_completer`) |
| `ui/panel/observability/` | Painel TUI ao-vivo (Decisão #44, issue #347): `ClaudeJsonlParser` (parser incremental do `~/.claude/projects/<hash>/<sid>.jsonl` produzido por `claude -p`, tolerante a JSON malformado, marca `tool_use` órfão como `in_progress`); `ClusterObservabilityClient` (aiohttp wrapper sobre os endpoints do `pipeline_status_server` e do `claude_worker_server`, com timeouts curtos e `ApiError` fallback — painel não trava se um pod estiver down); 3 screens Rich-based (`ClusterStatusScreen`/`LiveSessionScreen`/`HistoryScreen`) renderizando adaptativamente a `console.width`. Convive com o painel legado (`infra/k8s/_panel.py`) durante a transição. |
| `tests/` | Suíte pytest + scripts standalone (ver `CLAUDE.md` para convenção dual) |

## Dependências entre subpacotes (direção real, observada nos imports)

| De → Para | Natureza |
|---|---|
| `core/agent.py` → `tools/`, `parsers/`, `commands/`, `personas/`, `orchestration/`, `core/models/`, `ui/`, `storage/`, `config/` | Mediator orquestra todos |
| `core/models/bootstrap.py` → `core/models/{router, tier_router, catalog, provider_config}` + providers concretos | Registro condicional |
| `core/agent.py` → `storage/usage_repository.BudgetExceeded` | Tratamento de erro de budget |
| `tools/registry.py` → `tools/base.py`, `core/exceptions.py` | Registry depende da interface |
| `memory/memory_manager.py` → 4 camadas + `memory_consolidation` | Composição |
| `orchestration/plan_manager.py` → `tools/base.ToolResult`, `security/` | Steps de plano executam tools sob política |
| `personas/manager.py` → `personas/loader.py`, `personas/base.py`, configs | Hot-reload de instruções |
| `config/manager.py` → `watchdog` (lazy import) | Hot-reload de configuração |

## Pontos de entrada

| Componente | Singleton/Factory | Arquivo |
|---|---|---|
| Configuração | `get_settings()` | `deile/config/settings.py` |
| Configuração estruturada (YAML/JSON) | `get_config_manager()` / `ConfigManager` | `deile/config/manager.py` |
| Tools | `get_tool_registry()` | `deile/tools/registry.py` |
| Comandos | `get_command_registry()` | `deile/commands/registry.py` |
| Parsers | `get_parser_registry()` | `deile/parsers/registry.py` |
| Router (legado) | `get_model_router()` | `deile/core/models/router.py` |
| Router por tier | `get_tier_router()` | `deile/core/models/tier_router.py` |
| Memória | `MemoryManager` instanciado pelo agente | `deile/memory/memory_manager.py` |
| Permissões | `get_permission_manager()` | `deile/security/permissions.py` |
| Audit | `get_audit_logger()` | `deile/security/audit_logger.py` |
| Event bus | `get_event_bus()` | `deile/events/event_bus.py` |
| Logger | `get_logger()` | `deile/storage/logs.py` |
| Plan manager | `get_plan_manager()` | `deile/orchestration/plan_manager.py` |
| Workflow executor | `get_workflow_executor()` | `deile/orchestration/workflow_executor.py` |
| Repositório de uso | `get_usage_repository()` | `deile/storage/usage_repository.py` |
| Intent analyzer | `get_intent_analyzer()` | `deile/core/intent_analyzer.py` |
| Runtime state (state file + heartbeat) | `get_instance_state()` | `deile/runtime/instance_state.py` |

## Padrões arquiteturais aplicados

> Detalhamento e regras inegociáveis em [`03-PRINCIPIOS-ARQUITETURAIS.md`](03-PRINCIPIOS-ARQUITETURAIS.md).

| Padrão | Onde está |
|---|---|
| Mediator | `DeileAgent` orquestra registries, memory, orquestração e providers |
| Registry | Descoberta/extensão para Tools, Commands, Parsers e Personas |
| Strategy | Roteamento via `RoutingPolicy` (em `tier_router.py`) e `RoutingStrategy` (em `router.py`) |
| Observer / Hot-reload | `watchdog` em `config/manager.py` e `plugins/hot_loader.py` |
| Command | `SlashCommand.execute()` e `Tool.execute()` encapsulam ações |
| Circuit Breaker | `_ProviderBreaker` + `CircuitBreaker` em `tier_router.py` |

## Fronteira CLI ↔ Registry (decisão #24)

A fronteira `deile/cli.py` lê o `CommandRegistry` para decidir quais flags expor — não duplica lógica de comandos. Cada subclasse de `SlashCommand` declara metadados `cli_flag`/`cli_extra_flags` e `deile/commands/cli_flags.py:build_cli_flag_specs()` produz a lista de `CLIFlagSpec` consumida por `add_command_flags_to_parser()`. Adicionar flag = adicionar atributo na classe do comando.

| Componente | Arquivo | Responsabilidade |
|---|---|---|
| Spec dataclass | `deile/commands/cli_flags.py:CLIFlagSpec` | Mapeia uma flag CLI a um nome de comando + sub-comando opcional + se aceita argumento + se exige provider de LLM |
| Builder | `deile/commands/cli_flags.py:build_cli_flag_specs(registry)` | Percorre `registry.get_all_commands()`, lê `cli_flag` e `cli_extra_flags`, produz lista ordenada de specs |
| Bridge para argparse | `deile/commands/cli_flags.py:add_command_flags_to_parser(parser, specs)` | Transforma cada spec em `parser.add_argument(...)` (`store_true` ou positional) |
| Dispatcher | `deile/cli.py:_run_command_flag()` | Aceita `command_name` + `command_args` + `requires_provider`; bootstrappa providers só se necessário; renderiza `CommandResult` |
| Help expandido | `deile/cli.py:_format_help_with_commands()` | Anexa o catálogo de slash commands à saída padrão de `--help`, lendo do registry |

Flags com `cli_requires_provider=False` (default) não exigem nenhuma `*_API_KEY` — `--version`, `--status`, `--tools` etc. funcionam offline.

## Bootstrap em runtime

Sequência implementada em `DeileAgentCLI.initialize` (ou `_run_oneshot`):

| # | Passo |
|---|---|
| 1 | `get_settings()` — carrega o singleton |
| 2 | `ConfigManager().load_config()` — carrega YAMLs/JSONs e configura hot-reload (se habilitado) |
| 3 | `get_model_router()` — obtém o router legado |
| 4a | Se `feature_flags.use_legacy_gemini_only=true` em `model_providers.yaml`, registra apenas `GeminiProvider` via `_bootstrap_legacy_gemini` |
| 4b | Caso contrário, `bootstrap_providers(router=...)` registra todos os providers cuja `api_key_env` estiver definida no ambiente; cada handle (`provider:model_id`) também é registrado no `TierRouter` |
| 5 | Se nenhum provider foi registrado, a CLI exibe erro e retorna sem subir o agente |
| 6 | `get_tool_registry()` e `get_parser_registry()` — instanciam (se necessário) e auto-descobrem |
| 7 | Construção do `DeileAgent` recebendo router, registries e `ConfigManager` |
| 8 | `agent.initialize()` — inicializa `PersonaManager` e dependências |
| 9 | `agent.create_session(...)` — cria sessão de trabalho com `working_directory` |

> Diagrama de bootstrap em [`10-DIAGRAMAS.md`](10-DIAGRAMAS.md).
