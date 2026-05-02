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
| `memory/` | `MemoryManager` + 4 camadas (`WorkingMemory`, `EpisodicMemory`, `SemanticMemory`, `ProceduralMemory`) + `MemoryConsolidator` |
| `security/` | `PermissionManager`, `AuditLogger`, `SecretsScanner` |
| `personas/` | `BasePersona`, `BaseAutonomousPersona`, `PersonaManager`, `PersonaLoader`, `instruction_loader`, `builder`, `context`, `library/` (YAMLs), `instructions/` (MDs), `memory/integration.py` |
| `config/` | `Settings` (singleton via `get_settings()`), `ConfigManager`, YAMLs (`api_config`, `commands`, `intent_patterns`, `model_providers`, `persona_config`, `system_config`), `profiles/` |
| `events/` | `EventBus`, `Event`, `EventType`, `EventPriority`; handlers de eventos |
| `storage/` | `logs`, `debug_logger`, `embeddings`, `usage_repository` (com `BudgetGuard` e `BudgetExceeded`) |
| `infrastructure/` | Adapters externos (ex.: `google_file_api`), monitoring |
| `plugins/` | `plugin_manager`, `dependency_resolver`, `hot_loader` (via `watchdog`), `marketplace`, `sandbox` (`PluginSandbox`) |
| `evolution/` | `self_analyzer`, `code_modifier`, `improvement_loop`, `benchmarker`, `safety_sandbox`, `rollback_manager` |
| `ui/` | `ConsoleUIManager`, `DisplayManager`, `streaming_renderer`, `emoji_support`, `themes/`, `components/`, `completers/` (`hybrid_completer`) |
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
