# 🤖 DEILE — Development Environment Intelligence & Learning Engine

<div align="center">

![Versão](https://img.shields.io/badge/versão-5.1.0-blue.svg?style=for-the-badge)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg?style=for-the-badge)
![Licença](https://img.shields.io/badge/licença-MIT-green.svg?style=for-the-badge)

**Agente de IA autônomo, multi-provedor, executado via CLI, voltado ao desenvolvimento de software.**

</div>

---

## 🚀 Visão geral e propósito

DEILE é um agente conversacional autônomo distribuído como aplicação de linha de comando (`python3 deile.py`). Não há servidor HTTP, API REST, nem interface web — toda a interação acontece em um shell interativo provido pela classe `DeileAgentCLI` (ponto de entrada `deile.py`), com o restante do código encapsulado no pacote `deile/`.

A versão atual, declarada em `deile/__version__.py`, é **5.1.0**. O sistema integra múltiplos provedores de LLM, despacha chamadas de função (function calling) para uma camada interna de ferramentas, e apresenta a saída ao usuário com renderização de Markdown em streaming progressivo.

### 🎯 Casos de uso principais

| Caso de uso | Como o DEILE atende |
|---|---|
| 💬 Conversa técnica multi-turno | Sessão interativa com histórico em memória working/episodic |
| 🛠️ Edição e geração de código | Ferramentas `read_file`, `write_file`, `list_files`, `delete_file` |
| ⚙️ Execução de comandos do sistema | Ferramentas `bash_execute`, `python_execute`, `pip_install`, `run_tests` |
| 🔍 Busca e análise de repositório | Ferramenta `find_in_files` com matching configurável |
| 📋 Orquestração de tarefas | `sqlite_task_manager` com listas, dependências e estados persistidos |
| 💰 Telemetria de custo | `usage_repository` agrega tokens, custo USD e latência por requisição |

---

## ✨ Funcionalidades reais implementadas

| Funcionalidade | Localização no código |
|---|---|
| 🌐 Roteamento entre 4 provedores de LLM | `deile/core/models/router.py`, `tier_router.py` |
| 🔄 Streaming unificado de eventos | `deile/core/models/stream_events.py` (UnifiedStreamEvent) |
| 🖼️ Renderização incremental de Markdown | `deile/ui/streaming_renderer.py` |
| 🔁 Loop iterativo de function calling | `deile/core/tool_loop_executor.py` |
| 📨 Barramento assíncrono de eventos | `deile/events/event_bus.py` |
| 🛠️ Registro extensível de ferramentas | `deile/tools/registry.py` |
| 📜 Comandos slash registráveis | `deile/commands/registry.py`, `commands/builtin/` |
| 🎭 Personas dinâmicas via YAML+Markdown | `deile/personas/library/`, `deile/personas/instructions/` |
| 🧠 Quatro camadas de memória | `deile/memory/*.py` |
| 🔒 Permissões, auditoria e scan de segredos | `deile/security/*.py` |
| 💾 Persistência SQLite (tasks + uso) | `deile/orchestration/sqlite_task_manager.py`, `deile/storage/usage_repository.py` |
| 🔌 Plugins, hot-reload e marketplace | `deile/plugins/*.py` |
| 🧬 Módulo de auto-evolução experimental | `deile/evolution/*.py` |

---

## 🏗️ Arquitetura e camadas

DEILE adota um pacote em camadas com registries para os artefatos extensíveis (tools, commands, parsers, personas). Os módulos de mais alto nível dentro de `deile/` são:

| Camada | Pacote | Responsabilidade |
|---|---|---|
| 🧩 Núcleo | `deile/core/` | Agente principal, contexto, análise de intenções, file resolver |
| 🤖 Modelos LLM | `deile/core/models/` | Providers, router, tiers, stream events, catálogo |
| 🔁 Loop de ferramentas | `deile/core/tool_loop_executor.py` | Itera function calls até término ou limite |
| 📨 Eventos | `deile/events/` | EventBus assíncrono, EventType enum, handlers |
| 🛠️ Ferramentas | `deile/tools/` | Tools concretos + registry + schemas JSON |
| 📜 Comandos | `deile/commands/` | Slash commands builtin + registry |
| 🧱 Parsers | `deile/parsers/` | Detecção de menções `@arquivo`, comandos, diffs |
| 🎭 Personas | `deile/personas/` | Loader, builder, manager, instruções MD, library YAML |
| 🧠 Memória | `deile/memory/` | Working, episodic, semantic, procedural + consolidação |
| 🔒 Segurança | `deile/security/` | AuditLogger, PermissionsManager, SecretsScanner |
| 💾 Armazenamento | `deile/storage/` | Logs, embeddings, usage_repository, debug_logger |
| 🎯 Orquestração | `deile/orchestration/` | Tasks SQLite, planos, runs, aprovação, artifacts, workflow |
| 🖥️ UI | `deile/ui/` | ConsoleUI, StreamingRenderer, completers, componentes |
| 🧬 Evolução | `deile/evolution/` | Self-analyzer, code modifier, sandbox, rollback, benchmarker |
| 🔌 Plugins | `deile/plugins/` | Plugin manager, hot loader, sandbox, marketplace, deps |
| ⚙️ Infra | `deile/infrastructure/` | Google File API, cost tracker |
| 🛠️ Configuração | `deile/config/` | Settings singleton, manager, YAML de providers/comandos/intents |

### Fluxo de uma mensagem do usuário

1. `DeileAgentCLI` (em `deile.py`) lê a entrada e a entrega ao `DeileAgent` (`deile/core/agent.py`).
2. Parsers extraem menções de arquivo e comandos slash.
3. Se for slash, o `CommandRegistry` despacha para o builtin correspondente.
4. Caso contrário, o `ModelRouter` seleciona um provider conforme tier e estratégia.
5. O provider produz `UnifiedStreamEvent`s (`TEXT_DELTA`, `TOOL_USE`, etc.).
6. Quando aparece um `TOOL_USE`, o `ToolLoopExecutor` aciona o `ToolRegistry`, executa a ferramenta e re-injeta o `TOOL_RESULT` na conversa.
7. O `StreamingRenderer` consome os eventos e atualiza a tela via `rich.live.Live`.
8. O `EventBus` publica eventos transversais (telemetria, persona, tool invocation).

---

## 🌐 Provedores de LLM suportados

Quatro providers concretos vivem em `deile/core/models/` e são registrados condicionalmente por `bootstrap.py` apenas se a respectiva chave de API estiver presente.

| 🔌 Provider | Classe | Variável de ambiente | SDK |
|---|---|---|---|
| Anthropic | `AnthropicProvider` | `ANTHROPIC_API_KEY` | `anthropic` |
| OpenAI | `OpenAIProvider` | `OPENAI_API_KEY` | `openai` |
| DeepSeek | `DeepSeekProvider` (estende OpenAI) | `DEEPSEEK_API_KEY` | `openai` (endpoint compatível) |
| Gemini | `GeminiProvider` | `GOOGLE_API_KEY` | `google-genai` |

### Catálogo de modelos (do `deile/config/model_providers.yaml`)

| Provider | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| Anthropic | `claude-opus-4-7` | `claude-sonnet-4-6` | `claude-haiku-4-5` |
| OpenAI | `gpt-5.3-codex` | `gpt-5.4` | `gpt-5.4-mini` |
| Gemini | `gemini-3.1-pro-preview` | `gemini-3-flash-preview` | `gemini-3.1-flash-lite-preview`, `gemini-2.5-flash-lite` |
| DeepSeek | `deepseek-v4-pro` | — | `deepseek-v4-flash`, `deepseek-reasoner` |

> ℹ️ Os identificadores acima são os literais carregados do YAML; a aplicação real depende de o nome ser válido no SDK do provedor.

### Estratégias de roteamento

O `ModelRouter` consulta o `TierRouter` para escolher um modelo conforme a tier solicitada e a estratégia configurada, aplicando fallback entre providers em caso de erro.

---

## 🛠️ Ferramentas integradas

Todas residem em `deile/tools/` e implementam `Tool` (subclasses `SyncTool`/`AsyncTool` em `base.py`). O registro é feito por `register_tool` (`tools/registry.py`).

| 🧰 Tool | Arquivo | Objetivo |
|---|---|---|
| `read_file` | `file_tools.py` | Ler arquivo com detecção de encoding e limites de tamanho |
| `write_file` | `file_tools.py` | Escrever arquivo de forma atômica |
| `list_files` | `file_tools.py` | Listar diretório com filtros e recursão |
| `delete_file` | `file_tools.py` | Remover arquivo respeitando políticas |
| `find_in_files` | `search_tool.py` | Buscar padrões em árvore de diretórios |
| `bash_execute` | `bash_tool.py` | Executar comando shell com níveis de segurança |
| `execute_command_enhanced` | `execution_tools.py` | Execução de comando com PTY/streaming |
| `python_execute` | `execution_tools.py` | Executar bloco/arquivo Python isolado |
| `pip_install` | `execution_tools.py` | Instalar dependência Python |
| `run_tests` | `execution_tools.py` | Disparar suíte de testes |
| `git` | `git_tool.py` | Operações Git via GitPython |
| `http` | `http_tool.py` | Requisição HTTP genérica |
| `lint_format` | `lint_tool.py` | Lint/format (issues + formatação) |
| `secrets_scanner` | `secrets_tool.py` | Detectar/redigir segredos em conteúdo |
| `archive_tool` | `archive_tool.py` | Compactar/descompactar arquivos |
| `process_tool` | `process_tool.py` | Inspecionar processos do sistema |
| `tokenizer` | `tokenizer_tool.py` | Estimar tokens e analisar contexto |
| `slash_command_executor` | `slash_command_executor.py` | Despachar comandos slash a partir do agente |

JSON Schemas formais em `deile/tools/schemas/` (8 arquivos: `bash_execute`, `delete_file`, `find_in_files`, `list_files`, `pip_install`, `python_execute`, `read_file`, `write_file`).

---

## 📊 Comandos slash

Em `deile/commands/builtin/` (24 arquivos `*_command.py`). Os identificadores reais (verificados no código) são:

| ⌨️ Comando | Arquivo | Finalidade resumida |
|---|---|---|
| `help` | `help_command.py` | Lista comandos disponíveis |
| `status` | `status_command.py` | Estado do sistema, sessão e métricas |
| `config` | `config_command.py` | Visualizar/ajustar configurações |
| `context` | `context_command.py` | Inspecionar contexto da sessão |
| `cls` | `clear_command.py` | Limpar tela |
| `compact` | `compact_command.py` | Compactar histórico |
| `cost` | `cost_command.py` | Custo acumulado por requisição |
| `debug` | `debug_command.py` | Alternar modo de debug |
| `diff` | `diff_command.py` | Mostrar diffs |
| `export` | `export_command.py` | Exportar transcrições/artefatos |
| `logs` | `logs_command.py` | Consultar logs |
| `memory` | `memory_command.py` | Operar camadas de memória |
| `model` | `model_command.py` | Selecionar modelo/estratégia de roteamento |
| `permissions` | `permissions_command.py` | Gerenciar permissões |
| `plan` | `plan_command.py` | Operar planos de execução |
| `run` | `run_command.py` | Executar runs orquestrados |
| `tools` | `tools_command.py` | Listar e descrever ferramentas |
| `stop` | `stop_command.py` | Cancelar operação corrente |
| `approve` | `approve_command.py` | Aprovar etapa pendente |
| `patch-apply` | `apply_command.py` | Aplicar patch gerado |
| `patch-generate` | `patch_command.py` | Gerar patch a partir de mudanças |
| `sandbox` | `sandbox_command.py` | Operar sandbox Docker |
| `welcome` | `welcome_command.py` | Tela de boas-vindas |

---

## 🎭 Sistema de personas

| Camada | Local | Conteúdo |
|---|---|---|
| 📚 Library (descritor) | `deile/personas/library/` | `architect.yaml`, `debugger.yaml`, `developer.yaml` |
| 📝 Instruções | `deile/personas/instructions/` | `developer.md`, `fallback.md` |
| 🧠 Memória de persona | `deile/personas/memory/` | Integração com `MemoryManager` |
| 🛠️ Infra | `deile/personas/` | `loader.py`, `builder.py`, `manager.py`, `context.py`, `error_recovery.py` |

Personas são MD-driven: alterar a instrução em `instructions/*.md` modifica o comportamento sem editar Python. Os YAMLs declaram nome, `persona_id`, capacidades e especializações.

---

## 🧠 Camadas de memória

Quatro arquivos em `deile/memory/`, agregados por `memory_manager.py`. A consolidação periódica vive em `memory_consolidation.py`.

| 🧠 Camada | Arquivo | Propósito | Tempo de vida |
|---|---|---|---|
| Working | `working_memory.py` | Estado transitório do turno | TTL curto |
| Episodic | `episodic_memory.py` | Eventos da sessão | Sessão |
| Semantic | `semantic_memory.py` | Fatos persistentes | Persistente |
| Procedural | `procedural_memory.py` | Padrões/skills aprendidos | Persistente, evolui |

---

## 🔒 Segurança e auditoria

| Componente | Arquivo | Função |
|---|---|---|
| 📜 Audit logger | `deile/security/audit_logger.py` | Registro estruturado de ações sensíveis |
| 🛡️ Permissions | `deile/security/permissions.py` | Política de permissões (config em `config/permissions.yaml`) |
| 🔍 Secrets scanner | `deile/security/secrets_scanner.py` | Detecção/redação de credenciais |

Tools possuem `SecurityLevel` (`tools/base.py`); `bash_tool.py` define `BashSecurityLevel` adicional para classificar comandos shell.

---

## 💾 Persistência

DEILE persiste dados em **dois bancos SQLite distintos**:

| Banco | Caminho default | Origem da definição |
|---|---|---|
| Tarefas e listas | `./deile_tasks.db` | `deile/orchestration/sqlite_task_manager.py` |
| Telemetria de uso | `./data/usage.db` | `deile/storage/usage_repository.py` |

Demais armazenamentos:

- 🧮 Embeddings: `deile/storage/embeddings.py`
- 📑 Logs textuais: `deile/storage/logs.py`
- 🐛 Debug logger: `deile/storage/debug_logger.py`

### DER ASCII (modelo de dados — bancos SQLite)

```
┌─────────────────────────┐         ┌───────────────────────────┐
│ task_lists              │ 1     N │ tasks                     │
│─────────────────────────│─────────│───────────────────────────│
│ id (PK)                 │◄────────│ list_id (FK → task_lists) │
│ title                   │         │ id (PK)                   │
│ description             │         │ title                     │
│ created_at              │         │ description               │
│ sequential_mode         │         │ status                    │
│ auto_start_next         │         │ priority                  │
│ stop_on_failure         │         │ depends_on (JSON)         │
│ active                  │         │ blocks (JSON)             │
│ current_task_id         │         │ created_at                │
│ total_tasks             │         │ started_at                │
│ completed_tasks         │         │ completed_at              │
│ failed_tasks            │         │ estimated_duration        │
│ updated_at              │         │ tags (JSON)               │
└─────────────────────────┘         │ metadata (JSON)           │
   (deile_tasks.db)                 │ success                   │
                                    │ result_data (JSON)        │
                                    │ error_message             │
                                    │ updated_at                │
                                    └───────────────────────────┘

┌─────────────────────────────────────┐
│ usage_records  (data/usage.db)      │
│─────────────────────────────────────│
│ id (PK, AUTOINC)                    │
│ timestamp        REAL               │
│ provider_id      TEXT               │
│ model_id         TEXT               │
│ tier             TEXT               │
│ session_id       TEXT               │
│ prompt_tokens    INTEGER            │
│ completion_tokens INTEGER           │
│ cached_tokens    INTEGER            │
│ total_tokens     INTEGER            │
│ cost_usd         REAL               │
│ latency_ms       INTEGER            │
│ success          INTEGER (0/1)      │
│ error_type       TEXT (nullable)    │
└─────────────────────────────────────┘
```

> ⚠️ Não há scripts SQL versionados em `*.sql`. As tabelas são criadas em runtime por `CREATE TABLE IF NOT EXISTS`. Conforme política do projeto, qualquer script SQL futuro será de responsabilidade do operador humano executar.

---

## 📡 Sistema de eventos

`deile/events/event_bus.py` define `EventBus` assíncrono e o enum `EventType` com os grupos abaixo (categorias confirmadas no código):

| 📂 Grupo | Eventos representativos |
|---|---|
| Sistema | `SYSTEM_STARTED`, `SYSTEM_STOPPED` |
| Persona | `PERSONA_ACTIVATED`, `PERSONA_DEACTIVATED`, `PERSONA_SWITCHED` |
| Tarefas | `TASK_CREATED`, `TASK_STARTED`, `TASK_COMPLETED`, `TASK_FAILED`, `TASK_CANCELLED` |
| Código | `CODE_GENERATED`, `CODE_EXECUTED`, `CODE_TESTED`, `FILE_MODIFIED` |
| Ferramentas | `TOOL_INVOKED`, `TOOL_COMPLETED`, `TOOL_FAILED` |

Handlers ficam em `deile/events/event_handlers.py`.

---

## 🔄 Streaming UI

Pipeline recém-introduzido (branch `feature/streaming-ui`):

| 🧩 Componente | Local | Papel |
|---|---|---|
| `UnifiedStreamEvent` | `deile/core/models/stream_events.py` | Evento canônico independente de provider |
| `ToolLoopExecutor` | `deile/core/tool_loop_executor.py` | Itera function calls (limite `MAX_TOOL_ITERATIONS = 10`) |
| `StreamingRenderer` | `deile/ui/streaming_renderer.py` | Acumula `TEXT_DELTA`, renderiza Markdown progressivo via `rich.live.Live` |

Características confirmadas no código:

- 🧱 **Padrão acumulador** — re-renderiza texto completo a cada delta para que `rich.markdown.Markdown` lide com cercas/inline parciais.
- 🎞️ **Live region com diff** — `rich.live.Live` repinta apenas linhas alteradas.
- ⏱️ **Refresh throttling** — `refresh_per_second` desacopla velocidade de rede da do terminal (default 12 Hz).
- 🪟 **Fallback legado** — terminais sem ANSI confiável recebem renderização em lote pelo mesmo parâmetro.
- 🧪 **Testabilidade** — desacoplado do `ConsoleUIManager`, pode usar `Console(file=StringIO())`.

---

## ⚙️ Configuração

### Variáveis de ambiente reconhecidas

Chaves declaradas em `.env.example` (ao menos uma é obrigatória no startup, conforme `bootstrap_providers`):

| 🔑 Variável | Provider |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic |
| `OPENAI_API_KEY` | OpenAI |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `GOOGLE_API_KEY` | Gemini |

### Arquivos de configuração

| 📁 Local | Conteúdo |
|---|---|
| `./config/settings.json` | Configurações runtime |
| `./config/permissions.yaml` | Política de permissões |
| `./config/search.yaml` | Defaults de busca |
| `./config/display.yaml` | Preferências de exibição |
| `deile/config/system_config.yaml` | Configuração do sistema |
| `deile/config/api_config.yaml` | Configuração de APIs |
| `deile/config/model_providers.yaml` | Catálogo de modelos e tiers |
| `deile/config/intent_patterns.yaml` | Padrões de detecção de intenção |
| `deile/config/persona_config.yaml` | Defaults de personas |
| `deile/config/commands.yaml` | Defaults de comandos |
| `deile/config/profiles/autonomous_agent.yaml` | Perfil "autonomous" |
| `deile/config/profiles/enterprise.yaml` | Perfil "enterprise" |

> ℹ️ Há **dois diretórios `config/`**: o de runtime (`./config/`) e o do pacote (`deile/config/`). Não devem ser confundidos.

---

## 📋 Requisitos do sistema

### 🐍 Linguagem e plataforma

| Item | Valor |
|---|---|
| Python | 3.9+ |
| SO | Linux, macOS, Windows |
| Entrada | `python3 deile.py` |

### 📦 Dependências de produção (selecionadas — completo em `requirements.txt`)

| Domínio | Pacotes |
|---|---|
| 🤖 LLM SDKs | `anthropic>=0.40.0`, `openai>=1.50.0`, `google-genai==1.46.0` |
| 🖥️ UI/CLI | `rich==14.1.0`, `prompt_toolkit==3.0.52`, `colorama==0.4.6`, `Pygments==2.19.2` |
| ⚡ Async I/O | `aiofiles==24.1.0`, `aiosqlite==0.19.0` |
| ✅ Validação/config | `pydantic==2.12.5`, `PyYAML==6.0.3`, `python-dotenv==1.1.1` |
| 🌐 Rede/utilidades | `requests==2.32.5`, `httplib2==0.31.0` |
| 🔧 Sistema | `psutil==6.1.0`, `docker==7.1.0`, `chardet==5.2.0`, `GitPython==3.1.46` |
| 📚 Outras | `numpy==2.2.6`, `pathspec==0.12.1`, `watchdog==6.0.0`, `py7zr==1.1.0` |

### 🧪 Dependências de desenvolvimento (de `dev-requirements.txt`)

| Categoria | Pacotes |
|---|---|
| Testes | `pytest==8.4.2`, `pytest-asyncio==1.2.0`, `pytest-mock==3.15.0`, `pytest-cov==6.3.0`, `pytest-xdist==3.8.0`, `pytest-benchmark==5.1.0` |
| Qualidade | `coverage==7.11.0`, `isort==6.0.1`, `radon==6.0.1`, `black==25.9.0` |
| Segurança | `safety==3.6.1`, `bandit==1.9.3` |

---

## 🧪 Testes

Configuração em `pytest.ini`:

| Item | Valor |
|---|---|
| `testpaths` | `deile/tests` |
| `asyncio_mode` | `auto` |
| `python_files` | `test_*.py *_test.py` |
| Cobertura mínima exigida | `--cov-fail-under=80` |
| Markers registrados | `unit`, `integration`, `security`, `orchestration`, `bash`, `ui`, `slow`, `perf` |

### 📊 Volume real (verificado via `git ls-files`)

| Métrica | Valor |
|---|---|
| Arquivos `test_*.py` / `*_test.py` rastreados em `deile/tests/` | **56** |
| Arquivos Python totais em `deile/tests/` (inclui `__init__.py` e helpers) | **66** |
| Subdiretórios | `core/`, `core/models/`, `integration/`, `perf/`, `tools/`, `ui/`, `might/` |
| Testes "might" (consomem tokens reais) | em `deile/tests/might/` — fora do fluxo padrão de `pytest` |

> ⚠️ A cobertura percentual real não foi medida nesta documentação (o ambiente de geração não dispunha de `pytest` instalado). O valor está sujeito ao gate de 80% definido em `pytest.ini`.

### Convenções de teste

| Tipo | Padrão de nome | Como rodar |
|---|---|---|
| Pytest tests | `test_*.py` | Coletados automaticamente |
| Scripts standalone | `*_test.py`, `smoke_test_*.py`, `proactive_final_test.py` | `python deile/tests/<nome>.py` ou `python deile/tests/all.py` |
| Testes empíricos com LLM | `deile/tests/might/<nickname>/` | Manual; gastam tokens reais |

---

## 🚦 Operação

### Inicialização

| Passo | Comando |
|---|---|
| 1. Clonar | `git clone <repo>` |
| 2. Instalar deps | `pip install -r requirements.txt` |
| 3. Configurar `.env` | Definir ao menos uma das 4 chaves de API |
| 4. Executar | `python3 deile.py` |

### Política de SQL

Conforme política do projeto: scripts SQL são executados pelo operador humano. Se um erro de banco surgir em runtime, o agente deve parar e informar o operador.

### Troubleshooting

| Sintoma | Provável causa | Ação |
|---|---|---|
| `bootstrap_providers` registra 0 providers | Nenhuma API key no ambiente | Definir uma das 4 variáveis em `.env` |
| Erros `--strict-markers` no pytest | Marker novo não registrado | Adicionar em `pytest.ini` |
| Cobertura abaixo de 80% | `--cov-fail-under=80` ativo | Adicionar testes ou ajustar gate |
| Ferramenta não encontrada pelo agente | Não registrada via `register_tool` | Garantir registro no import path |

---

## 💪 Pontos fortes / diferenciais técnicos

| 💪 Diferencial | Por que importa |
|---|---|
| 🔁 ToolLoopExecutor único e provider-agnóstico | Elimina duplicação que existia em `chat_with_tools` por provider |
| 🌐 Fallback automático entre 4 providers | Resiliência ao indisponibilizar uma vendor |
| 💵 Telemetria de custo persistida em SQLite | Rastreia tokens, latência e USD por requisição |
| 🖼️ Streaming Markdown com diff de Live region | UX próxima a editores ricos sem reflow do terminal |
| 🧠 Quatro camadas de memória explícitas | Separa estado transitório, sessão e conhecimento persistente |
| 🎭 Personas MD-driven | Mudança de comportamento sem alteração de código |
| 🔌 Plugins com hot-reload e sandbox | Extensibilidade controlada |
| 🔒 Auditoria + scan de segredos no core | Postura de segurança incorporada à arquitetura |

---

## ⚠️ Limitações conhecidas

| ⚠️ Limitação | Detalhe |
|---|---|
| Sem servidor HTTP/REST | DEILE é exclusivamente CLI; integrações externas devem ser construídas pelo consumidor |
| IDs de modelo no YAML são literais | Devem corresponder aos nomes válidos no SDK do provedor — não há validação contra catálogo remoto |
| Limite de iterações de tool-loop | `MAX_TOOL_ITERATIONS = 10` em `tool_loop_executor.py` |
| Cobertura efetiva não medida no README | O gate é 80%; o número real depende da execução local de `pytest --cov` |
| Módulo de evolução é experimental | Pacote `deile/evolution/` existe e é importável, mas não é caminho operacional padrão |
| Sandbox Docker exige Docker disponível | Comando `sandbox` depende do daemon Docker no host |

---

## 📄 Licença

Projeto licenciado sob **MIT License**. Veja `LICENSE`.

## 👤 Autoria

- **Elimar Cavalli** — criador e mantenedor — [@elimarcavalli](https://github.com/elimarcavalli)
- **DEILE AGENT** - este repositório - [@DEILE](https://github.com/elimarcavalli/deile)

---

<div align="center">

**DEILE 5.1.0** — `python3 deile.py`

</div>
