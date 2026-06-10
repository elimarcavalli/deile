# 10 — Diagramas Consolidados

> Todos em ASCII. Cada diagrama referencia o pilar que detalha aquele assunto. Catalogações em [`00-VISAO-GERAL.md`](00-VISAO-GERAL.md).

## Índice de diagramas

| ID | Diagrama | Pilar de detalhe |
|---|---|---|
| D.1 | Arquitetura em camadas | [`02-ARQUITETURA.md`](02-ARQUITETURA.md) |
| D.2 | Bootstrap em runtime | [`02-ARQUITETURA.md`](02-ARQUITETURA.md), seção "Bootstrap em runtime" |
| D.3 | Pipeline de turno | [`05-FLUXO-EXECUCAO.md`](05-FLUXO-EXECUCAO.md) |
| D.4 | Tool registry e function calling | [`04-MODELO-COMPONENTES.md`](04-MODELO-COMPONENTES.md) |
| D.5 | Memória híbrida | [`06-MEMORIA.md`](06-MEMORIA.md) |
| D.6 | Circuit breaker e tier cascata | [`07-INTEGRACOES-LLM.md`](07-INTEGRACOES-LLM.md) |
| D.7 | Hot-reload | [`09-CONFIGURACAO.md`](09-CONFIGURACAO.md) |
| D.8 | Eventos publicados | [`05-FLUXO-EXECUCAO.md`](05-FLUXO-EXECUCAO.md) |
| D.9 | Frota multi-CLI e dispatch per-estágio no cluster | [`14-CONTAINERIZACAO.md`](14-CONTAINERIZACAO.md), Decisão #51 |

---

## D.1 — Arquitetura em camadas

> Detalhe em [`02-ARQUITETURA.md`](02-ARQUITETURA.md).

```
┌──────────────────────────────────────────────────────────────────┐
│ CLI                                                              │
│ deile.py (DeileAgentCLI / _run_oneshot)                          │
└────────┬─────────────────────────────────────────────────────────┘
         │
┌────────▼─────────────────────────────────────────────────────────┐
│ UI                                                               │
│ ConsoleUIManager · DisplayManager · streaming_renderer ·         │
│ HybridCompleter · themes/components                              │
└────────┬─────────────────────────────────────────────────────────┘
         │
┌────────▼─────────────────────────────────────────────────────────┐
│ Núcleo do agente                                                 │
│ DeileAgent (Mediator)                                            │
│   ├── ContextManager       ├── IntentAnalyzer                    │
│   ├── ProactiveAnalyzer    ├── SmartFileResolver                 │
│   └── ToolLoopExecutor                                           │
└────────┬─────────────────────────────────────────────────────────┘
         │
┌────────▼─────────────────────────────────────────────────────────┐
│ Camada de serviços                                               │
│ ToolRegistry · CommandRegistry · ParserRegistry                  │
│ PlanManager · WorkflowExecutor · TaskManager · SQLiteTaskManager │
│ MemoryManager (working/episodic/semantic/procedural)             │
│ PersonaManager · ApprovalSystem · ArtifactManager                │
└────────┬─────────────────────────────────────────────────────────┘
         │
┌────────▼─────────────────────────────────────────────────────────┐
│ Integração                                                       │
│ ModelRouter (legado) · TierRouter (cascata por tier)             │
│ ModelCatalog · RoutingPolicy · CircuitBreaker                    │
│ Anthropic · OpenAI · DeepSeek · Gemini providers                 │
└────────┬─────────────────────────────────────────────────────────┘
         │
┌────────▼─────────────────────────────────────────────────────────┐
│ Infra / Storage / Security                                       │
│ UsageRepository · BudgetGuard · AuditLogger · PermissionManager  │
│ SecretsScanner · EventBus · logs · debug_logger · embeddings     │
│ google_file_api adapter · monitoring                             │
└──────────────────────────────────────────────────────────────────┘

         ┌──────────────────────────────────────────┐
         │ Extensão                                 │
         │ PluginManager · hot_loader                │
         │ (PluginSandbox skeleton — ver issue #54)  │
         │ evolution: self_analyzer, code_modifier, │
         │           improvement_loop (experimental)│
         └──────────────────────────────────────────┘
```

## D.2 — Bootstrap em runtime

Detalhe em [`02-ARQUITETURA.md`](02-ARQUITETURA.md), seção "Bootstrap em runtime".

```
DeileAgentCLI.initialize()
   │
   ├─► get_settings()                                  Singleton em RAM
   │
   ├─► ConfigManager().load_config()                   Lê YAMLs/JSONs
   │
   ├─► get_model_router()                              Router legado
   │
   ├─► leitura: model_providers.yaml
   │      use_legacy_gemini_only?
   │      ├── true  ► _bootstrap_legacy_gemini()       Apenas GeminiProvider
   │      └── false ► bootstrap_providers()
   │                     │
   │                     ▼
   │             Para cada provider habilitado e com api_key:
   │               • Carrega ModelHandle(s) do catálogo
   │               • Instancia provider para cada handle
   │               • Registra no ModelRouter (legado)
   │               • Registra no TierRouter (handle full key)
   │
   ├─► registered.empty() ? ─► erro e sair
   │
   ├─► get_tool_registry()       (auto_discover)
   ├─► get_parser_registry()
   │
   ├─► DeileAgent(router, tools, parsers, config)
   │      │
   │      └─► await agent.initialize()                 PersonaManager + integrações
   │
   └─► agent.create_session(...)                       Sessão default
```

## D.3 — Pipeline de turno

Detalhe em [`05-FLUXO-EXECUCAO.md`](05-FLUXO-EXECUCAO.md).

```
user input
   │
   ▼
┌──────────────────────────────────────────────────┐
│ Inicia "/" ?                                     │
│   ├── sim  ► CommandParser ► CommandRegistry     │
│   │          ► SlashCommand.execute()            │
│   │          ► AgentResponse / stream            │
│   │                                              │
│   └── não  ► pipeline normal                     │
└──────────────────────────────────────────────────┘
                 │ (caso normal)
                 ▼
┌──────────────────────────────────────────────────┐
│ ParserRegistry.parse(text)                       │
│   • CommandParser, FileParser,                   │
│     IntelligentFileParser, DiffParser            │
└──────────────────┬───────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────┐
│ IntentAnalyzer.analyze(text)                     │
│   match em intent_patterns.yaml                  │
│   intent_tier_mapper → tier sugerido             │
└──────────────────┬───────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────┐
│ requires_workflow ?                              │
│   ├── sim ► PlanManager.create_plan()            │
│   │           _execute_plan_steps()              │
│   │           ApprovalSystem (steps de risco)    │
│   │           rollback handlers em falha         │
│   │                                              │
│   └── não ► function_calling iterativo           │
│             _process_iterative_function_calling  │
│                _execute_tools                    │
│                _apply_validation_gate            │
└──────────────────┬───────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────┐
│ Provider selection                               │
│   forced_model em sessão ?                       │
│   ├── sim ► usa handle exato                     │
│   └── não ► TierRouter cascata                   │
│             (skip providers em breaker aberto)   │
└──────────────────┬───────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────┐
│ Provider.generate(...)                           │
│   • function_calling                             │
│   • streaming events emitidos                    │
│   • _self_record_circuit(success/failure)        │
│   • UsageRepository + BudgetGuard                │
└──────────────────┬───────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────┐
│ MemoryManager.store_interaction(...)             │
└──────────────────┬───────────────────────────────┘
                   ▼
            AgentResponse / stream
```

## D.4 — Tool registry e function calling

Detalhe em [`04-MODELO-COMPONENTES.md`](04-MODELO-COMPONENTES.md).

```
ToolRegistry
  ├── _tools : {name → Tool}
  ├── _tools_by_category
  ├── _enabled_tools
  ├── _tool_aliases
  │
  ├── auto_discover(packages)
  │      por padrão: file_tools, execution_tools,
  │                   search_tool, bash_tool,
  │                   slash_command_executor
  │      demais módulos: registro explícito
  │
  ├── register(tool, aliases) / register_tool(...) helper
  │
  ├── get_anthropic_tools(...)    → ToolSchema.to_anthropic_tool
  ├── get_openai_functions(...)   → ToolSchema.to_openai_function
  └── get_gemini_functions(...)   → ToolSchema.to_gemini_function
```

## D.5 — Memória híbrida

Detalhe em [`06-MEMORIA.md`](06-MEMORIA.md).

```
              ┌────────────────────────┐
              │     MemoryManager      │
              │   store_interaction()  │
              │   retrieve_context()   │
              └──┬──────────┬──────────┘
        ┌────────┘          └──────────┐
        ▼                              ▼
  ┌────────────┐                 ┌─────────────┐
  │  Working   │                 │  Episodic   │
  │  TTL = s   │                 │  retention  │
  │  RAM       │                 │  = days     │
  └────────────┘                 └──────┬──────┘
                                        │
                                        ▼
                                  ┌────────────┐
                                  │  Semantic  │
                                  │  vetores,  │
                                  │  fatos     │
                                  └──────┬─────┘
                                         │
                                         ▼
                                   ┌──────────────┐
                                   │  Procedural  │
                                   │  patterns    │
                                   └──────────────┘

       MemoryConsolidator roda em loop
       (consolidation_interval, pressure_threshold)
```

## D.6 — Circuit breaker e tier cascata

Detalhe em [`07-INTEGRACOES-LLM.md`](07-INTEGRACOES-LLM.md).

```
Pedido com tier_X
       │
       ▼
TierRouter.route(tier_X)
       │
       ├─► RoutingPolicy.handles_for(tier_X, strategy)
       │     [ provider:model_id, ... ]
       │
       ▼
para cada handle na ordem:
       │
       ├─► breaker[provider].state == OPEN ? ► próximo
       │
       ├─► breaker[provider].state == HALF_OPEN
       │     • permite N test requests
       │     • sucesso ► CLOSED
       │     • falha   ► volta a OPEN
       │
       └─► CLOSED ► invoca handle
              ├─ sucesso ► registra; resposta
              └─ falha consecutiva ≥ threshold ► OPEN
```

## D.7 — Hot-reload

Detalhe em [`09-CONFIGURACAO.md`](09-CONFIGURACAO.md).

```
watchdog.Observer
   │
   ├── ConfigManager.UnifiedConfigChangeHandler
   │     • detecta mudança em deile/config/*.yaml
   │     • re-carrega sem restart
   │
   ├── plugins/hot_loader.PluginFileHandler
   │     • detecta mudança em diretório de plugins
   │     • disparar PluginManager.reload(plugin_id)
   │
   └── PersonaManager (file watch on persona_config + instructions/)
         • re-carrega Markdown de instruções
         • atualiza capabilities
```

## D.8 — Eventos publicados

Detalhe em [`05-FLUXO-EXECUCAO.md`](05-FLUXO-EXECUCAO.md).

```
EventBus.publish(Event)
   │
   ├── tool_start / tool_end       — _publish_tool_event
   ├── router_event                — _emit_router_event
   ├── budget_alert                — quando uso aproxima de threshold
   ├── permission_check            — AuditLogger.log_permission_check
   ├── secret_detection            — AuditLogger.log_secret_detection
   ├── plan_execution              — AuditLogger.log_plan_execution
   └── approval_event              — AuditLogger.log_approval_event

Dead letter queue: get_dead_letters() / replay_dead_letter(event_id)
```

## D.9 — Frota multi-CLI e dispatch per-estágio no cluster

> Detalhe em [`14-CONTAINERIZACAO.md`](14-CONTAINERIZACAO.md) e Decisão #51. Portas e
> `kind` são derivados em runtime do registro de adapters — não copie valores; abra
> `ls infra/k8s/cli_adapters/` e o `default_port` de cada adapter.

```
                          ┌──────────────────────────────────────────┐
infra/k8s/cli_adapters/   │ CliAdapter (Protocol) — base.py          │
  <kind>.py  ───────────► │  metadados: kind/default_port/auth_mode/ │
  (1 arquivo = 1 worker)  │   supports_resume/git_strategy/...       │
                          │  métodos: build_argv/parse_output/...    │
                          └────────────────┬─────────────────────────┘
                                           │ auto-discovery
                          ┌────────────────▼─────────────────────────┐
                          │ cli_adapters/__init__.py                 │
                          │   ADAPTERS = {kind: adapter}  (fonte única)│
                          └───┬───────────────┬───────────────┬──────┘
        dirige o resolver     │   dirige painel│  dirige gen de manifest/
                              │                │  NetworkPolicy
   ┌──────────────────────────▼──────┐   ┌─────▼────────┐   ┌──▼────────────────┐
   │ dispatch_resolver.py            │   │ DispatchMatrix│   │ _cli_worker_gen.py│
   │  get_valid_dispatchers()        │   │ View ([d])    │   │ + template .tmpl  │
   │  resolve_stage_dispatcher(stage)│   └───────────────┘   └───────────────────┘
   └──────────────┬──────────────────┘
                  │ por estágio: DEILE_PIPELINE_DISPATCH_<STAGE>
                  │              > global DEILE_PIPELINE_DISPATCH_MODE (def deile-worker)
┌─────────────────▼──────────────────────────────────────────────────────────┐
│ deile-pipeline (PipelineMonitor)                                            │
│  classify · refine · implement · pr_review · follow_ups                     │
│  cli_worker_scaler.py: alvo em replicas:0 → kubectl scale 0→1 (cooldown)    │
└──┬───────────┬───────────────────────────────────────────────────────────┬─┘
   │ HTTP      │ HTTP                                                       │ HTTP
   ▼           ▼                                                            ▼
┌────────┐ ┌──────────────┐   ┌─────────────────── frota CLI (replicas:0) ──────────┐
│ deile- │ │ claude-worker │   │ opencode | codex | qwen | aider | goose | (antigravity│
│ worker │ │  claude -p    │   │  -worker   -worker  ...                     gated)   │
│ :8766  │ │  :8767        │   │  cli_worker_server.py (reusa _worker_core.py)        │
│ DEILE  │ │  worktree     │   │  POST /v1/dispatch · GET /v1/health · /v1/progress · │
│ python │ │  isolado      │   │  GET /v1/dispatches/{id}/resume-info                 │
└────────┘ └──────────────┘   │  gate de sucesso = parse_output.ok AND wrapper_gate  │
  núcleo      núcleo           │  (commit+push novo, ou test verde quando o brief exige)│
                              │  resume nativo no MESMO workdir (supports_resume)    │
                              │  PVC <kind>-worker-home + CronJob cleanup            │
                              │  ledger durável <root>/.cost-ledger.jsonl (dedup id) │
                              └──────────────────────────────────────────────────────┘
                                            │ colhe custo antes de podar
                                            ▼
                              fleet_tokens_audit.py (tela [T]okens) +
                              jsonl_cost.py (preço, fonte única) +
                              fleet_progress_parse.py (parsers .progress por kind)
```
