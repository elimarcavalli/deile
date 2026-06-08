# 04 — Modelo de Componentes

> Interfaces e registries dos tipos de componentes plugáveis: **Tools, Commands, Parsers, Personas, Skills** (descobertos dentro de `deile/`) e os **CLI Adapters** da frota de workers (descobertos em `infra/k8s/`, lado de deployment). Catalogações e contagens em [`00-VISAO-GERAL.md`](00-VISAO-GERAL.md). Templates em [`12-PADROES-CODIGO.md`](12-PADROES-CODIGO.md).

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

## Skills

> Unidade composável de expertise — arquivo Markdown com frontmatter YAML que pode entrar no prompt automaticamente (via `triggers`) **ou** ser invocada explicitamente pelo LLM (tool `invoke_skill`) **ou** disparada manualmente pelo usuário (slash command `/<name>`). Decisão #34 em [`DECISOES.md`](DECISOES.md).

### Interface (em `deile/skills/base.py` e `deile/skills/loader.py`)

| Símbolo | Tipo | Papel |
|---|---|---|
| `Skill` | dataclass | `name`, `description`, `body`, `triggers`, `priority`, `source`, `kind`, `source_path`. Campo `content` é alias de `body` |
| `SkillTrigger` | dataclass frozen | `file_globs`, `code_block_langs`, `keywords`, `file_content_patterns`. Vazio = skill só responde a invocação explícita |
| `parse_skill_text(text, path, *, source, kind, force_uppercase_name)` | função | Parser tolerante — retorna `Skill` ou `None` com warning. Aceita CRLF |
| `SkillLoader` | classe | `load_file(path)` (lança `SkillLoadError`) + `load_directory(dir)` (skip-with-warning) |
| `SkillSelectionContext` | dataclass frozen | Entrada do router por turno: `user_input` + `file_references` |
| `BootstrapResult` | dataclass | `router` + `watcher` (opcional) — devolvido por `bootstrap_skills_with_handle` |

### Registry (`SkillRegistry` em `deile/skills/registry.py`)

| Aspecto | Detalhe |
|---|---|
| Acessor singleton | `get_skill_registry()` — thread-safe (double-checked locking) |
| Thread-safety | Cada mutação/leitura é guardada por `RLock`. `replace_all(skills)` faz swap atômico — readers nunca veem estado parcial durante hot-reload |
| Localização das skills | Cinco diretórios em ordem de prioridade crescente: `deile/skills/library/` (bundled) → `~/.deile/skills/` → `~/.claude/commands/` (UPPERCASE) → `<cwd>/.deile/skills/` → `<cwd>/.claude/commands/` (UPPERCASE) + extras de `SettingsManager` |
| Override | Source posterior substitui anterior em colisão de nome — projeto pode sobrescrever bundled, usuário pode sobrescrever projeto |
| Auto-discovery | `bootstrap_skills(...)` em `deile/skills/bootstrap.py` resolve via `default_scan_order()` |

### Router (`SkillRouter` em `deile/skills/router.py`)

| Operação | Método | Notas |
|---|---|---|
| Seleção por turno | `select_skills(SkillSelectionContext)` | Avalia os 4 tipos de trigger; ordena por `(-priority, name)`; corta em `max_skills_per_turn` (default 4, configurável em `skills.yaml`) |
| Renderização do bloco ativo | `render_block(skills)` | Gera `## Active Skills` + body de cada skill, anexado ao system prompt |
| Renderização do catálogo | `render_catalog(exclude_names)` | Gera `## Available Skills` com diretiva imperativa + exemplo concreto + listagem (`name`/`description`/trigger hint) das skills não-disparadas |
| Triggers suportados | — | `file_globs` (glob fnmatch), `code_block_langs` (case-insensitive), `keywords` (word-boundary regex), `file_content_patterns` (regex MULTILINE, 4 KiB de sample por arquivo, cache por turno, **path-traversal containment** via `_resolve_within`) |

### Hot-reload (`SkillsWatcher` em `deile/skills/watcher.py`)

| Aspecto | Detalhe |
|---|---|
| Tecnologia | `watchdog.observers.Observer` (mesma stack de `deile/plugins/hot_loader.py`) |
| Diretórios observados | Os mesmos resolvidos por `default_scan_order()` que existirem em disco |
| Debounce | `threading.Timer` 0,5 s coalesce bursts de write do editor |
| Serialização | `_RELOAD_LOCK` impede reloads sobrepostos (manual + watcher, ou dois eventos durante rescan) |
| Atomicidade | `reload_registry` → `SkillRegistry.replace_all(skills)` em um único lock |
| Ciclo | `start()` retorna `False` + warning quando watchdog indisponível ou nenhum diretório existe; `stop()` é idempotente e dá join no observer |

### Acesso pelo LLM — function-call tools (`deile/tools/skill_tools.py`)

| Tool | Propósito |
|---|---|
| `list_skills` | Retorna catálogo machine-readable (todas as skills + descrição + dica de trigger) |
| `invoke_skill(name)` | Retorna o body da skill nomeada. Erro com lista de até 25 nomes disponíveis (hint para `list_skills` se mais) quando o nome não existe |

Ambas são auto-descobertas via `DEFAULT_TOOL_PACKAGES` — `deile/tools/skill_tools.py` está na lista.

### Slash commands

`deile/commands/skill_loader.py` é um **shim de backward-compat**. Delega para `deile.skills` mas:

- Filtra skills `source=="bundled"` antes do bridge — bundled não vira `/<name>` (só auto-trigger + tool)
- Pre-detecta colisões com built-ins antes do bridge para que o warning saia do logger deste módulo (testes legados patcham `sl_mod.logger.warning`)
- Exporta `SkillDefinition` (alias de `Skill`), `_normalize_name`, `_parse_skill_file`, `_VALID_NAME_RE`, `_FRONTMATTER_RE`, `_list_md_files` que testes antigos importam diretamente

### Comando `/skills` (já existia)

`deile/commands/builtin/skills_command.py` gerencia paths extras via `SettingsManager.add_skills_path` / `remove_skills_path` (escopo `global` ou `project`). O comando aciona `agent.reload_skills()` para hot-reload pós-mudança.

## CLI Adapters

> Componente plugável da **frota de workers** (Decisão #51 em [`DECISOES.md`](DECISOES.md)): cada CLI de coding headless (opencode, codex, qwen, aider, goose, …) é integrado por **um** adapter, sem editar nenhum consumidor. Diferente dos cinco componentes acima, vive em `infra/k8s/cli_adapters/` (lado de deployment, fora de `deile/`) porque seu consumidor é o server do worker e o pipeline de dispatch, não o agente CLI. Containerização em [`14-CONTAINERIZACAO.md`](14-CONTAINERIZACAO.md).

### Responsabilidade

Um adapter declara **o que muda entre CLIs** e nada mais. Toda a maquinaria agnóstica (lease, heartbeat, subprocess one-shot, Bearer auth, cleanup, gate pós-run) é compartilhada via `infra/k8s/_worker_core.py` e o server genérico `infra/k8s/cli_worker_server.py`; o adapter especializa apenas os **cinco pontos divergentes** mais os metadados de classe que dirigem registro, painel, geração de manifest e NetworkPolicy.

### Interface (Protocol `CliAdapter` em `infra/k8s/cli_adapters/base.py`)

| Símbolo | Tipo | Papel |
|---|---|---|
| `CliAdapter` | `Protocol` (`runtime_checkable`) | Contrato do adapter — validado por `isinstance` sem herança nominal (basta ter os atributos/métodos) |
| `BaseCliAdapter` | dataclass | Base opcional com defaults conservadores (sem resume/reasoning, auth `env`, `brief_driven`); métodos obrigatórios levantam `NotImplementedError` para falhar cedo |
| `WorkResult` | dataclass frozen | Veredito de `parse_output`: `ok`, `result_text`, `error_code`, `cost_usd` |
| `ResumeCtx` | dataclass frozen | Contexto de retomada (`session_id`, `prev_task_id`) — passado a `build_argv` só quando `supports_resume=True` e o pipeline pediu resume (anti-sangria de custo, issue #445) |
| `ModelInfo` | dataclass frozen | Um modelo suportado, exposto por `GET /v1/models` (alimenta o picker do painel); inclui preço e `auth` por modelo (opcionais, retrocompatíveis) |
| `OAuthSpec` | dataclass frozen | Especificação de OAuth para adapters `auth_mode="oauth_file"` (generaliza o `claude-login`) |
| `AuthMode` / `GitStrategy` / `ModelAuth` | `Literal` | `env`\|`oauth_file`; `cli_autocommit`\|`brief_driven`; `apikey`\|`chatgpt` |

**Metadados de classe** (lidos como dados pelos consumidores): `kind`, `default_port`, `auth_mode`, `supports_resume`, `supports_reasoning`, `git_strategy`, `auth_env_keys`, `egress_hosts`, `writable_dirs`, `oauth`.

**Os cinco pontos do contrato** (especialização por dispatch):

| Método | Responsabilidade |
|---|---|
| `build_argv(...)` | Monta o argv headless do CLI (flags de autonomia, modelo, brief, resume) |
| `env_overlay(home=...)` | Variáveis de ambiente que o CLI exige (HOME/XDG/config); **não** inclui as `auth_env_keys` (vêm do Secret) |
| `parse_output(stdout, stderr, rc)` | Interpreta a saída num `WorkResult` — exit-code não basta; o server ainda aplica o gate pós-run por cima |
| `list_models()` | Catálogo (estático ou dinâmico) que alimenta `GET /v1/models` |
| `extract_session_id(...)` | Deriva o session-id nativo para o resume (CLIs workdir-keyed devolvem o `task_id` sentinela) |

(`provision_auth(...)` é um sexto ponto opcional, no-op por default; sobrescrito só por adapters dual-mode como o codex, em que o modelo escolhido dita a credencial.)

### Registro (auto-discovery em `infra/k8s/cli_adapters/__init__.py`)

| Aspecto | Detalhe |
|---|---|
| Fonte única | O dicionário `ADAPTERS = {kind: adapter}` é montado em import escaneando o pacote; consome-o `dispatch_resolver` (deriva `VALID_DISPATCHERS`), o painel, `deploy.py gen-worker` e a geração de NetworkPolicy |
| Convenção de descoberta | Um módulo `<kind>.py` participa se expõe `ADAPTER` (preferido), `get_adapter()`, ou uma única instância detectável que satisfaça `CliAdapter`; `base.py` e módulos `_privados` nunca são escaneados |
| Tolerância a falha | Um módulo que estoure no import é logado e **pulado** — um adapter quebrado não derruba o registro inteiro |
| Hot-reload em testes | `reload_adapters()` re-escaneia e muta `ADAPTERS` in-place (referências já capturadas seguem válidas) |
| Gate Antigravity | `cli_adapters/antigravity.py` é um **gate documentado**: NÃO exporta `ADAPTER`/`get_adapter` (só a classe-rascunho + sentinela `ANTIGRAVITY_GATED=True`), então o auto-discovery o ignora e `antigravity-worker` não vira dispatcher. Motivo e condição de saída do gate em Decisão #51 |

## Como adicionar um novo componente

> Templates concretos em [`12-PADROES-CODIGO.md`](12-PADROES-CODIGO.md).

| Componente | Passo |
|---|---|
| Tool | Subclasse de `Tool` (ou `AsyncTool`/`SyncTool`), implementar `name`, `description`, `category`, `execute()` e definir `ToolSchema`. Registrar via `register_tool(...)` ou colocar em um dos módulos auto-descobertos |
| Slash command | Subclasse de `SlashCommand` em `deile/commands/builtin/`, definir `name`, `description`, `aliases`, implementar `async execute()`; o registry descobre |
| Parser | Subclasse de `Parser` (ou `RegexParser`) em `deile/parsers/`, implementar `can_parse` (rápido, síncrono) e `parse` (síncrono); opcionalmente `parse_async` para I/O; definir `priority`; registrar |
| Persona | Criar instrução em `deile/personas/instructions/<id>.md` e config em `deile/personas/library/<id>.yaml`; mapeamento em `deile/config/persona_config.yaml` |
| Skill | Criar `*.md` com frontmatter (`name`, `description`, `triggers`, `priority`) num dos 5 diretórios de scan; **nenhum código Python**. Hot-reload pega em 0,5 s |
| CLI Adapter | Criar `infra/k8s/cli_adapters/<kind>.py` satisfazendo o Protocol `CliAdapter` (ou herdando `BaseCliAdapter`), expondo `ADAPTER`/`get_adapter`; declarar metadados (`kind`/`default_port`/…) e implementar os cinco pontos do contrato. O auto-discovery registra; **nenhum consumidor é editado** (Decisão #51) |
