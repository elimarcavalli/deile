# 05 — Fluxo de Execução

> Descreve o que acontece entre uma entrada do usuário e a resposta final. Diagramas em [`10-DIAGRAMAS.md`](10-DIAGRAMAS.md). Componentes em [`04-MODELO-COMPONENTES.md`](04-MODELO-COMPONENTES.md).

## Sessões

A classe `AgentSession` (em `deile/core/agent.py`) carrega:

| Campo | Conteúdo |
|---|---|
| `session_id` | Identificador da sessão |
| `working_directory` | Substitui CWD para tools de filesystem |
| `context_data` | Estado mutável da sessão |
| Histórico conversacional | Mensagens trocadas durante a sessão |

| Tipo de sessão | Quando | Identificador |
|---|---|---|
| Interativa | Modo REPL da CLI | `default_cli_session` |
| One-shot | Modo single-shot da CLI | `oneshot_cli_session` |

## Modo de processamento: streaming vs. legado

`DeileAgent` expõe três entradas principais:

| Método | Quando é usado | Saída |
|---|---|---|
| `process_input(user_input, session_id)` | Modo legado (não-streaming) e one-shot CLI | `AgentResponse` síncrona com `content`, `status`, `execution_time`, `metadata`, `tool_results` |
| `process_input_stream(user_input, session_id)` | Modo interativo da CLI quando `Settings.streaming_enabled=True` | `AsyncIterator` de eventos (texto delta, tool events, etc.) |
| `process_stream(...)` | Variante usada por integrações | `AsyncIterator` |

> A CLI interativa sempre tenta primeiro o caminho streaming (cf. `deile.py`).

## Pipeline de turno (alto nível)

> Diagrama equivalente em [`10-DIAGRAMAS.md`](10-DIAGRAMAS.md), seção D.3.

| Etapa | Componente | Detalhe |
|---|---|---|
| 1. Roteamento por prefixo | `DeileAgent` | Se começa com `/` → comando slash. Caso contrário → pipeline normal |
| 2. Parsing | `ParserRegistry` | `CommandParser`, `FileParser`, `IntelligentFileParser`, `DiffParser`. Resolução de `@arquivo` via `SmartFileResolver` |
| 3. Análise de intenção | `IntentAnalyzer` | Match em `intent_patterns.yaml`, cálculo de confidence + complexity, mapeamento intent → tier (`classify_tier`) |
| 4. Decisão de workflow | `_should_create_workflow` / `_legacy_workflow_detection` | Se `intent.requires_workflow` → orquestração via Plan/Workflow |
| 5a. Caminho direto | `_process_iterative_function_calling`, `_execute_tools`, `_apply_validation_gate` | Single tool / chat; streaming events emitidos |
| 5b. Caminho de orquestração | `PlanManager.create_plan`, `_execute_plan_steps`, `_perform_security_checks` | `ApprovalSystem` para steps de risco; rollback handlers em falha |
| 6. Provider selection | `RoutingPolicy` + `TierRouter` + `CircuitBreaker` (ou `ModelRouter` legado) | Força modelo específico se `session.context_data["forced_model"]` estiver setado |
| 7. Geração de resposta | Provider concreto | Function calling: agent ↔ provider em loop até stopping condition; `_BudgetExceeded` é tratada com mensagem estruturada |
| 8. Memory store | `MemoryManager.store_interaction(...)` | Multi-camada |
| 9. Saída | — | `AgentResponse` ou stream events |

## Comandos slash

`_process_slash_command` (em `deile/core/agent.py`) roda quando a entrada começa com `/`:

| # | Passo |
|---|---|
| 1 | `CommandParser` extrai nome e argumentos |
| 2 | `CommandRegistry.execute_command(...)` chama `SlashCommand.execute(context)` |
| 3 | Resultado é convertido para `AgentResponse` ou injetado no stream via `_render_to_text` (renderiza Rich → texto plano para a pipeline de streaming, com console fixo de 120 colunas) |

## Function calling iterativo

`_process_iterative_function_calling` orquestra o loop de tool calls com o provider:

| # | Ação |
|---|---|
| 1 | Constrói declarações de função para o provider escolhido (`get_anthropic_tools` / `get_openai_functions` / `get_gemini_functions`) |
| 2 | Recebe tool_calls do provider; executa via `_execute_tools` (async, com `ToolContext`) |
| 3 | Aplica `_apply_validation_gate` quando configurado |
| 4 | Re-injeta resultados como mensagens de tool |
| 5 | Termina quando provider retorna sem mais tool calls ou ao atingir limite (constante de iteração máxima vive em `deile/core/agent.py`) |

## Eventos publicados durante o turno

| Helper | Quando dispara |
|---|---|
| `_emit_router_event` | Sinaliza troca/queda de provider |
| `_publish_tool_event` | Tool start/end (definido dentro do escopo do streaming) |

> Bus alvo: `EventBus` em `deile/events/event_bus.py`.

### Eventos do ciclo autônomo do pipeline

O ciclo autônomo (`stages.py` + `monitor.py`) emite eventos estruturados via `pipeline_logger` ao logger `deile.pipeline.events` — separado do `EventBus` acima. Esses eventos cobrem refinamento, decomposição, batch, mudanças de label, reaper e autenticação.

> Ver [`15-PIPELINE-LOGGER.md`](15-PIPELINE-LOGGER.md) para o formato canônico, API completa das 15 funções e garantias formais.

### Roteamento per-stage do dispatch (frota multi-CLI)

Cada etapa do ciclo autônomo (`classify`/`refine`/`implement`/`pr_review`/`follow_ups`) é despachada a um worker resolvido independentemente — o pipeline não está mais preso a um único alvo. Os três eixos de configuração por stage são resolvidos por módulos dedicados em `deile/orchestration/pipeline/`, cada um com a mesma cadeia de fallback (env var per-stage → settings.json per-stage → global → default):

| Eixo | Resolver | Env var per-stage / global | Default |
|---|---|---|---|
| Worker (qual pod recebe o `POST /v1/dispatch`) | `dispatch_resolver.resolve_stage_dispatcher` | `DEILE_PIPELINE_DISPATCH_<STAGE>` / `DEILE_PIPELINE_DISPATCH_MODE` | `deile-worker` |
| Modelo | `model_resolver.resolve_stage_model` (formato `provider:model` do deile/claude-worker) e `resolve_stage_cli_model` (id nativo livre dos CLI workers) | `DEILE_PIPELINE_MODEL_<STAGE>` / `DEILE_PREFERRED_MODEL` | sem override (worker decide) |
| Reasoning | `reasoning_resolver.resolve_stage_reasoning` (defaults opinados por stage — ver [`07-INTEGRACOES-LLM.md`](07-INTEGRACOES-LLM.md)) | `DEILE_PIPELINE_REASONING_<STAGE>` / `DEILE_REASONING_EFFORT` | default por stage |

> O conjunto de workers válidos (`dispatch_resolver.get_valid_dispatchers`) é **derivado em runtime** do registro de adapters (`infra/k8s/cli_adapters/`) — os dois workers núcleo (`deile-worker`, `claude-worker`) somados à frota CLI plugável. Nenhuma lista é hardcodada. Ver Decisão #51 e [`07-INTEGRACOES-LLM.md`](07-INTEGRACOES-LLM.md) para a camada de execução CLI.

### Scale-to-zero on-demand dos CLI workers

Os workers da frota CLI nascem `replicas: 0` (custo zero ocioso). Antes de despachar a um deles, o pipeline **garante ≥1 réplica** via `cli_worker_scaler.ensure_replica` (`kubectl scale --replicas=1`, com cooldown in-memory anti-flapping, reusando a SA do pipeline pod). Os workers núcleo nascem com 1 réplica e são `NOT_APPLICABLE` para o scaler. Falha de scale (sem `kubectl`/RBAC) vira erro tipado instruindo o scale manual.

### Resume nativo por worker (anti-sangria, issue #445)

Quando há trabalho começado (workdir reusado + sessão anterior), o worker **retoma a sessão nativa do CLI no MESMO workdir** em vez de re-gastar tokens do zero (cada adapter conhece a flag de resume do seu CLI; ver `cli_adapters/base.py:ResumeCtx`). O pipeline consulta `GET /v1/dispatches/{task_id}/resume-info` (liveness + `session_id` capturado) para decidir *resume vs fresh vs skip*.

A peça anti-sangria é a classificação de **corte por provider**: `_worker_core.classify_provider_error` varre stdout/stderr e, ao detectar `INSUFFICIENT_CREDIT`/`RATE_LIMIT`/`PROVIDER_ERROR`/`PROVIDER_CONN` (402/429/5xx/conexão), faz o adapter marcar `ok=False` (INCOMPLETO) em vez de "conclusão limpa" — os CLIs frequentemente saem com rc=0 mesmo cortados no meio. Isso leva o pipeline a retomar o trabalho parcial em vez de dar a task como completa pela metade.

## Tratamento de erros estruturados

Erros que devem aparecer ao usuário com formatação especial são marcados via `metadata` em `AgentResponse`:

| Flag de metadata | Significado | Origem |
|---|---|---|
| `budget_exceeded` | Limite de gasto atingido | `BudgetExceeded` em `deile/storage/usage_repository.py` |
| `forced_model_not_registered` | `forced_model` aponta para handle não registrado em runtime | Sessão com `context_data["forced_model"]` setado |

> A CLI renderiza esses casos com Rich `Panel` específico.

## Skills no system prompt por turno

`ContextManager._build_skills_block(parse_result, session)` é chamado em dois caminhos (com-persona e fallback) durante a construção do system prompt do turno. Bootstrap é lazy: a primeira chamada popula a singleton `SkillRegistry` via `bootstrap_skills()`; chamadas subsequentes reusam o router.

| # | Passo |
|---|---|
| 1 | Extrai o último `user` do `session.conversation_history` (para detectar code blocks e keywords no input) |
| 2 | Lê `parse_result.file_references` (do `FileParser`/`IntelligentFileParser`) |
| 3 | `SkillRouter.select_skills(SkillSelectionContext)` avalia 4 triggers por skill: `file_globs`, `code_block_langs` (incluindo extensões inferidas do file_ref via `LanguageDetector`), `keywords`, `file_content_patterns` (regex em 4 KiB de cada arquivo referenciado, **contained ao `project_root`** via `_resolve_within`) |
| 4 | Skills disparadas ordenadas por `(-priority, name)`, cortadas em `max_per_turn` (default 4) |
| 5 | `render_block(selected)` → `## Active Skills\n### Skill: <name>\n<body>` anexado |
| 6 | `render_catalog(exclude_names=selected)` → `## Available Skills` com diretiva imperativa, exemplo concreto e listagem das skills NÃO-disparadas (para o LLM puxar via `invoke_skill` se aplicável) |
| 7 | Bloco final colado **depois** da camada persona/DEILE.md, **antes** de `extra_system_prompt` |

Falhas no bootstrap ou na seleção são logadas (warning) e não interrompem o turno — `_build_skills_block` retorna string vazia e a turn continua sem skills.

### Ordem final do system prompt (com persona ativa)

```
┌─ DEILE.md Core         (prepend, não negociável)  ┐
├─ DEILE.md User         (prepend)                   │ via _prepend_deile_md_layers
├─ DEILE.md CWD          (prepend, regras do projeto)┘
├─ Persona instructions  (base, persona.build_system_instruction)
├─ ## Active Skills      (append, skills auto-disparadas)
│  ### Skill: <name>
│  <body>
├─ ## Available Skills   (append, catálogo das demais)
├─ 📁 [ARQUIVOS DISPONÍVEIS NO PROJETO] (append, listagem do cwd)
└─ extra_system_prompt   (append, modo bot via _merge_bot_extra)
```

No caminho de fallback (sem `PersonaManager`), `instruction_loader.load_fallback_instruction()` substitui a camada Persona; o resto da ordem é idêntico.

### Feedback visual ao usuário

| Evento | Onde aparece |
|---|---|
| Skills carregadas no startup | `logger.info` em `agent.py:_auto_discover_components` com `total + breakdown por source + invocáveis como /<nome>` |
| Skill auto-injetada no turno | STAGE event `"🧩 Skill ativa: <names>"` (chave `skills_active` em `stage_messages.py`) emitido pelo agent logo após `build_context`, surgindo no spinner antes da chamada ao LLM. Source: `session.context_data["_active_skills"]` populado por `_build_skills_block` |
| Skill invocada via `invoke_skill` | Aparece como tool call no transcript (renderização padrão de tool result) |
| Skill invocada via `/<name>` | Aparece como execução de slash command |
| Hot-reload do watcher | `logger.info` `"skills: hot-reload — registry now holds N skill(s)"` |

## Memória ao longo do turno

| Camada | Uso típico durante o turno |
|---|---|
| Working memory | Estado transitório do turno |
| Episodic memory | Registra o turno como episódio da sessão |
| Semantic memory | Pode armazenar correções (`store_correction(interaction_id, correction_data)`) e conhecimento (`store_knowledge(...)`) |
| Procedural memory | Atualiza/consulta padrões via `analyze_interaction(...)` e `get_relevant_patterns(query)` |

> Detalhamento em [`06-MEMORIA.md`](06-MEMORIA.md).
