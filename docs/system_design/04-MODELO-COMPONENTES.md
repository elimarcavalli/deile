# 04 — Modelo de Componentes

> Interfaces e registries dos quatro tipos de componentes plugáveis: **Tools, Commands, Parsers, Personas**. Catalogações e contagens em [`00-VISAO-GERAL.md`](00-VISAO-GERAL.md). Templates em [`12-PADROES-CODIGO.md`](12-PADROES-CODIGO.md).

## Tools

### Interface (em `deile/tools/base.py`)

| Símbolo | Tipo | Papel |
|---|---|---|
| `Tool` (ABC) | classe abstrata | Base para todas as tools. Métodos abstratos: `name`, `description`, `category`, `execute(context)` |
| `AsyncTool` | classe | Tool que implementa `execute_with_timeout` em cima do `execute` |
| `SyncTool` | classe | Wrapper que implementa `execute_sync` e expõe `execute()` async via `asyncio.to_thread` |
| `ToolSchema` | dataclass | JSON Schema interno + conversores para Anthropic/OpenAI/Gemini |
| `ToolContext` | dataclass | `user_input`, `parsed_args`, `session_data`, `working_directory`, `file_list`, `metadata` |
| `ToolResult` | dataclass | `status`, `data`, `message`, `error`, `metadata`, `execution_time`, `display_policy`, `show_cli`, `artifact_path`, `display_data` |
| `ToolStatus` | enum | `pending`, `running`, `success`, `error`, `cancelled` |
| `ToolCategory` | enum | `file`, `execution`, `search`, `system`, `analysis`, `network`, `database`, `messaging`, `other` |
| `SecurityLevel` | enum | `safe`, `moderate`, `dangerous` |
| `DisplayPolicy` | enum | `system`, `agent`, `both`, `silent` |
| `ShowCliPolicy` | enum | `always`, `parameter`, `never` |

### Registry (`ToolRegistry` em `deile/tools/registry.py`)

| Aspecto | Detalhe |
|---|---|
| Acessor singleton | `get_tool_registry()` |
| Localização | `deile/tools/*.py` (siblings de `base.py` e `registry.py`); **não existe** `deile/tools/builtin/` |
| Comando para inventariar | ver [`00-VISAO-GERAL.md`](00-VISAO-GERAL.md) |

| Operação | Método | Notas |
|---|---|---|
| Registro explícito | `register(tool, aliases=None)` | Lança `ToolError` em duplicidade de nome |
| Helper top-level | `register_tool(tool, aliases=None)` | Função módulo-level que delega ao singleton. **Não** é um decorator |
| Auto-discovery | `auto_discover(package_names=None)` | Cobre por padrão: `file_tools`, `execution_tools`, `search_tool`, `bash_tool`, `slash_command_executor`. Após o conjunto-padrão, chama `register_messaging_tools()` (registra 7 tools `messaging.discord_*` quando `deilebot` está instalado **e** `DEILE_BOT_ENDPOINT`/`AUTH_TOKEN` configurados) |
| Demais módulos | `git_tool`, `http_tool`, `lint_tool`, `archive_tool`, `process_tool`, `secrets_tool`, `tokenizer_tool` | Precisam de registro explícito ou descoberta passando o nome do módulo |
| Tools de mensageria | `deile/tools/messaging/` | Categoria `MESSAGING`. Cada tool herda `MessagingTool` (`_base.py`), que centraliza permission/audit/approval e mapeia erros do `BotControlClient` para `ToolResult.error_result(error_code=...)` tipados |
| Tools de pipeline/cron | `deile/tools/pipeline_tool.py`, `pipeline_schedule_tool.py`, `cron_create_tool.py`, `cron_list_tool.py`, `cron_delete_tool.py` | Categoria `SYSTEM`. Descritas abaixo. Precisam de registro explícito |
| Conversores para LLMs | `get_anthropic_tools(...)`, `get_openai_functions(...)`, `get_gemini_functions(...)` | Geram declarações nativas para function calling |

#### Tools do pipeline autônomo (intent #87 e #86)

As cinco tools abaixo expõem o pipeline e o agendador para o LLM, permitindo que o DEILE as invoque quando o usuário pede em linguagem natural (ex: via Discord).

| Tool | Arquivo | Propósito |
|---|---|---|
| `pipeline` | `pipeline_tool.py` | Controla o `PipelineMonitor`: start/stop/status/tick. Serve intent "inicie / pare / verifique o pipeline" |
| `pipeline_schedule` | `pipeline_schedule_tool.py` | CRUD de entradas no `ScheduleStore` (YAML por monitor): lista, add_recurring, add_oneshot, remove, enable, disable. Serve intent "agende revisão de issue a cada 5 minutos" |
| `cron_create` | `cron_create_tool.py` | Agenda um prompt natural para execução futura no `CronStore` (SQLite). Serve intent "execute isso todo dia às 9h" ou "execute isso uma vez amanhã às 18h" |
| `cron_list` | `cron_list_tool.py` | Lista entradas do `CronStore` com filtros. Serve intent "o que está agendado?" / "quais tarefas pendentes?" |
| `cron_delete` | `cron_delete_tool.py` | Remove ou desabilita uma entrada do `CronStore` por id. Serve intent "cancele o cron X" |

## Commands (Slash)

### Interface (em `deile/commands/base.py` e `deile/commands/registry.py`)

| Aspecto | Detalhe |
|---|---|
| Hierarquia | `SlashCommand` (base) + variantes especializadas como `LLMCommand`, `DirectCommand` |
| Atributos do comando | `name`, `description`, opcional `aliases` |
| Método principal | `async execute(context: CommandContext) -> CommandResult` |
| Campos de `CommandResult` | `success`, `content`, `content_type` (`text`/`rich`/`json`/`error`), `status` (`CommandStatus`), `metadata`, `execution_time`, `error` |
| Construtores prontos | `CommandResult.success_result(...)` e `CommandResult.error_result(...)` |
| Metadata de CLI flag (decisão #24) | Atributos opcionais de classe lidos pelo CLI builder em `deile/commands/cli_flags.py`: `cli_flag` (ex: `"--status"`), `cli_extra_flags` (dict de sub-flags para um único comando — usado por `ModelCommand` e `PipelineCommand`), `cli_takes_arg` (bool), `cli_arg_metavar` (str), `cli_help` (str), `cli_requires_provider` (bool — default `False`, flag roda sem API key), `cli_dispatch` (bool — default `True`; `False` declara flag como *modifier*, ex: `--debug`, registrada no argparse mas não dispara o slash command) |

### Registry (`CommandRegistry` em `deile/commands/registry.py`)

| Aspecto | Detalhe |
|---|---|
| Acessor singleton | `get_command_registry(config_manager=None)` |
| Descoberta | Comandos em `deile/commands/builtin/` |
| Despacho | Para comandos parseados pelo `CommandParser` |
| Configuração estendida | `deile/config/commands.yaml` |

#### Comandos slash do pipeline (intent #87)

| Comando | Arquivo | Propósito |
|---|---|---|
| `/pipeline` | `pipeline_command.py` | `start \| stop \| status \| tick` — controla o `PipelineMonitor` diretamente da CLI. Idempotente: `start` duas vezes é no-op. Guarda a instância em `agent.pipeline_monitor` |
| `/pipeline-schedule` | (se existir `pipeline_schedule_command.py`) | Alias de linha de comando para as mesmas operações de `pipeline_schedule` tool; útil para operação manual no REPL sem passar pelo LLM |

## Parsers

### Interface (em `deile/parsers/base.py`)

| Aspecto | Detalhe |
|---|---|
| Método de checagem | `def can_parse(input_text: str) -> bool` — **síncrono** e rápido (checagem de padrão) |
| Método de parsing | `def parse(input_text: str) -> ParseResult` — **síncrono** |
| Variante async | `parse_async(input_text)` para parsers que precisam de I/O |
| Atributo de ordem | `priority` (default 0; maior executa primeiro) |
| Subclasses padrão | `RegexParser`, `CompositeParser` |

### Campos de `ParseResult`

| Campo | Tipo | Conteúdo |
|---|---|---|
| `status` | `ParseStatus` | resultado do parsing |
| `commands` | `List[ParsedCommand]` | comandos identificados |
| `file_references` | `List[str]` | referências `@arquivo` |
| `tool_requests` | `List[str]` | tools solicitadas explicitamente |
| `error_message` | `str` | mensagem de erro (se houve) |
| `confidence` | `float` | confiança do parsing |
| `metadata` | `Dict[str, Any]` | contexto adicional |

### Registry (`ParserRegistry` em `deile/parsers/registry.py`)

| Aspecto | Detalhe |
|---|---|
| Acessor singleton | `get_parser_registry()` |

| Operação | Método |
|---|---|
| Descoberta de parser apropriado | `find_suitable_parsers(input_text)` |
| Parse direto (escolhe e roda) | `parse(input_text, working_directory=None)` |
| Sugestões de autocompletar | `get_suggestions(partial_input)` |

### Parsers concretos por responsabilidade

| Arquivo | Responsabilidade |
|---|---|
| `command_parser.py` | Slash commands |
| `file_parser.py` | Referências `@arquivo` |
| `intelligent_file_parser.py` | Parsing de arquivos com contexto |
| `diff_parser.py` | Diffs / patches |

## Personas

### Hierarquia (em `deile/personas/base.py`)

| Símbolo | Papel |
|---|---|
| `BasePersona` (ABC) | Contrato base com `id`, `description`, `capabilities` |
| `BaseAutonomousPersona(BasePersona)` | Adiciona `build_system_instruction(context)`, `process_user_input(...)`, `select_tools(...)`, `build_dynamic_prompt(...)`, `create_tool_orchestration_plan(...)`, `adapt_to_feedback(...)`, `get_diagnostics()` |
| `PersonaConfig` (Pydantic) | Schema da configuração: capabilities, model preferences, behavior settings, tool preferences |
| `AgentCapability`, `CommunicationStyle`, `ToolExecutionStrategy`, `ResponseMode` | Enums de comportamento |
| `AgentMetrics`, `AgentContext` | Telemetria e contexto de execução |

### Loader e Manager

| Componente | Local | Operações principais |
|---|---|---|
| `PersonaLoader` | `deile/personas/loader.py` | `discover_persona_modules`, `load_persona_instructions`, `_get_generic_persona_class` (cria fallback `GenericPersona`) |
| `PersonaManager` | `deile/personas/manager.py` | `initialize(enable_hot_reload=True)`, `_load_available_personas()`, `_create_persona_from_config(...)` |

### Onde vivem os artefatos de persona

| Tipo | Local | Formato |
|---|---|---|
| Instruções (prompt) | `deile/personas/instructions/*.md` | Markdown |
| Configurações de capacidades | `deile/personas/library/*.yaml` | YAML |
| Configuração geral (default, hot-reload, mapeamentos) | `deile/config/persona_config.yaml` | YAML |
| Builders / contextos | `deile/personas/builder.py`, `context.py`, `error_context.py`, `error_recovery.py` | Python |
| Integração com memória | `deile/personas/memory/integration.py` | Python |
| Integração com auditoria | `deile/personas/audit_integration.py` | Python |

## Como adicionar um novo componente

> Templates concretos em [`12-PADROES-CODIGO.md`](12-PADROES-CODIGO.md).

| Componente | Passo |
|---|---|
| Tool | Subclasse de `Tool` (ou `AsyncTool`/`SyncTool`), implementar `name`, `description`, `category`, `execute()` e definir `ToolSchema`. Registrar via `register_tool(...)` ou colocar em um dos módulos auto-descobertos |
| Slash command | Subclasse de `SlashCommand` em `deile/commands/builtin/`, definir `name`, `description`, `aliases`, implementar `async execute()`; o registry descobre |
| Parser | Subclasse de `Parser` (ou `RegexParser`) em `deile/parsers/`, implementar `can_parse` (rápido, síncrono) e `parse` (síncrono); opcionalmente `parse_async` para I/O; definir `priority`; registrar |
| Persona | Criar instrução em `deile/personas/instructions/<id>.md` e config em `deile/personas/library/<id>.yaml`; mapeamento em `deile/config/persona_config.yaml` |
