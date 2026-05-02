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

## Tratamento de erros estruturados

Erros que devem aparecer ao usuário com formatação especial são marcados via `metadata` em `AgentResponse`:

| Flag de metadata | Significado | Origem |
|---|---|---|
| `budget_exceeded` | Limite de gasto atingido | `BudgetExceeded` em `deile/storage/usage_repository.py` |
| `forced_model_not_registered` | `forced_model` aponta para handle não registrado em runtime | Sessão com `context_data["forced_model"]` setado |

> A CLI renderiza esses casos com Rich `Panel` específico.

## Memória ao longo do turno

| Camada | Uso típico durante o turno |
|---|---|
| Working memory | Estado transitório do turno |
| Episodic memory | Registra o turno como episódio da sessão |
| Semantic memory | Pode armazenar correções (`store_correction(interaction_id, correction_data)`) e conhecimento (`store_knowledge(...)`) |
| Procedural memory | Atualiza/consulta padrões via `analyze_interaction(...)` e `get_relevant_patterns(query)` |

> Detalhamento em [`06-MEMORIA.md`](06-MEMORIA.md).
