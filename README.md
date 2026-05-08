# 🤖 DEILE — Development Environment Intelligence & Learning Engine

<p align="center">
  <img src="docs/img/banner.png" alt="DEILE" width="480">
</p>

![Version](https://img.shields.io/badge/version-5.1.0-blue.svg?style=for-the-badge)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg?style=for-the-badge)
![License](https://img.shields.io/badge/license-MIT-green.svg?style=for-the-badge)

**Agente de IA autônomo, multi-provedor, executado via CLI, voltado ao desenvolvimento de software.**

---

## 🤖 Pipeline autônomo

O DEILE pode operar **autonomamente** sobre o repositório GitHub: quando uma issue recebe o label `~workflow:nova`, o pipeline a implementa e abre uma PR sem intervenção humana.

### Fluxo completo

```
Discord: "/ideia implementar feature X"
  → DEILE cria issue com ~workflow:nova
  → PipelineMonitor tick
    → Stage 1: DEILE revisa issue → ~workflow:revisada
    → Stage 2: Claude Code implementa em worktree → PR aberta → ~workflow:em_pr
    → Stage 3: Claude Code revisa PR → merge → ~review:concluida
  → Discord: DM com URL da PR
```

### Quick start do pipeline

Configure as variáveis de ambiente:

```bash
export DEILE_PIPELINE_REPO=owner/repo
export DEILE_PIPELINE_BASE_PATH=/caminho/para/repo
export DEILE_PIPELINE_NOTIFY_USER_ID=123456789012345678  # Discord snowflake (opcional)
```

Inicie de dentro do REPL:

```
> /pipeline start          # inicia o loop de polling (1 min)
> /pipeline status         # mostra contadores
> /pipeline stop           # para o loop
```

Ou configure `DEILE_PIPELINE_AUTOSTART=1` para iniciar automaticamente com o daemon.

### Agendamento de tarefas

```
# Agendar revisão de issues a cada 10 minutos
→ pipeline_schedule(action="add_recurring", trigger_action="review", cron="*/10 * * * *")

# Agendar prompt natural toda segunda às 9h
→ cron_create(prompt="Gere relatório de custos", cron="0 9 * * 1")

# One-shot: implementar issue 99 amanhã às 18h
→ pipeline_schedule(action="add_oneshot", trigger_action="implement",
    run_at="2026-05-07T18:00:00Z", target_issue=99)
```

Documentação completa: [`docs/2026-05-06_PIPELINE-AUTONOMO.md`](docs/2026-05-06_PIPELINE-AUTONOMO.md)

---

## 🚀 Visão geral

DEILE é um **agente de IA autônomo para desenvolvimento de software**, executado diretamente no terminal. Você conversa com ele em linguagem natural — em português ou inglês — e ele lê, escreve e edita arquivos do seu projeto, roda comandos, instala pacotes, executa testes, busca trechos no repositório, planeja tarefas e acompanha custo de uso, tudo dentro do diretório de trabalho atual.

O DEILE **pensa, decide e resolve**: aciona ferramentas reais (function calling) para entender o problema, planejar e concluir o que foi pedido, mostra o que está fazendo em tempo real e mantém memória da conversa entre turnos.

As ferramentas disponíveis incluem `list_files`, `read_file`, `write_file`, `delete_file`, `find_in_files`, `bash_execute`, `python_execute`, `pip_install`, `run_tests`, `git`, `http`, `lint_format`, `secrets_scanner`, `archive_tool` e `process_tool`. É distribuído como aplicação de linha de comando interativa, com modo one-shot opcional para uso não interativo.

### 🎯 Para quem é


| Perfil                        | O que ganha                                                        |
| ----------------------------- | ------------------------------------------------------------------ |
| 👩‍💻 Pessoa desenvolvedora   | Um par de programação que executa, não só sugere                   |
| 🛠️ Engenharia de plataforma  | Automação de tarefas repetitivas dentro do repositório             |
| 🔍 Revisão de código          | Leitura guiada do projeto com perguntas em linguagem natural       |
| 📋 Pequenos projetos pessoais | Geração e refatoração de código com baixo custo (modelos por tier) |
| 🎓 Aprendizado                | Observar passo a passo como um agente decide e usa ferramentas     |


### ✨ O que o DEILE faz hoje


| CHAVE                  | Descrição                                                                |
| ---------------------- | ------------------------------------------------------------------------ |
| 💬 CONVERSA            | Conversa multi-turno com contexto e histórico de sessão.                 |
| 🖼️ STREAMING UI       | Resposta em streaming com renderização incremental no terminal.          |
| 🔁 LOOP DE FERRAMENTAS | Function calling iterativo até concluir a tarefa (com limite seguro).    |
| 🛠️ EDIÇÃO DE CÓDIGO   | Lê, cria, edita, deleta e busca arquivos no repositório.                 |
| ⚙️ EXECUÇÃO LOCAL      | Executa shell/Python, instala pacotes e roda testes/lint.                |
| 🌐 ROTEAMENTO LLM      | Roteia entre 4 providers com fallback e seleção por tier.                |
| 🧠 MEMÓRIA             | Memória em 4 camadas: working, episodic, semantic e procedural.          |
| 📋 ORQUESTRAÇÃO        | Planeja tarefas, gerencia dependências e executa workflows com rollback. |
| ⌨️ CCOMANDOS SLASH     | omandos para custo, contexto, plano, permissões, modelo, logs e mais.    |
| 🎭 PERSONAS            | Personas MD/YAML com troca dinâmica de comportamento.                    |
| 📨 EVENTOS             | Event bus assíncrono para progresso, ferramentas, tarefas e sistema.     |
| 💰 TELEMETRIA          | Mede tokens, latência e custo em USD com persistência SQLite.            |
| 🔒 SEGURANÇA           | Permissões, aprovação por risco, auditoria e scanner de segredos.        |
| 🔌 PLUGINS HOT RELOAD  | Extensões com ciclo de vida e recarga dinâmica (sem isolamento — ver §segurança). |
| 🚀 MODOS CLI           | Modo interativo (REPL) e modo one-shot para automação.                   |


---

## ⚡ Quick start

Pré-requisito: **Python 3.9+** e ao menos uma chave de API entre Anthropic, OpenAI, DeepSeek e Gemini.

> 🧭 **Cobertura por chave única**: `OPENAI_API_KEY` ou `DEEPSEEK_API_KEY` cobrem todas as tiers em ambas as estratégias de roteamento. `GOOGLE_API_KEY` cobre tiers 1–3, mas não o tier_4. `ANTHROPIC_API_KEY` cobre tiers 1–3 na estratégia `task_optimized`. Para cobertura plena e fallback entre providers, use pelo menos duas chaves.

### 1️⃣ Clonar o repositório

```sh
git clone https://github.com/elimarcavalli/deile.git   # Clonar repositório
cd deile                                               # Entrar no diretório
```

### 2️⃣ Início rápido (recomendado)

O próprio `deile.py` faz todo o setup inicial: cria `.venv` se não existir, pergunta as chaves de API (input oculto), gera o `.env`, instala dependências e já sobe a CLI. Nas próximas execuções, detecta o ambiente virtual e inicia o DEILE direto.

```sh
python3 deile.py   # Executar o DEILE (tudo automático na 1ª execução)
```

### 3️⃣ Início manual (passo a passo)

Caso prefira cada etapa no controle manual:

```sh
python3 -m venv .venv                  # Criar ambiente virtual
source .venv/bin/activate              # Ativar o .venv (macOS/Linux)
# .venv\Scripts\activate               # Ativar o .venv (Windows)
pip install -r requirements.txt        # Instalar dependências
cp .env.example .env                   # Copiar .env de exemplo
# Edite .env e preencha pelo menos uma das chaves: ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, GOOGLE_API_KEY
python3 deile.py                       # Executar
```

Pronto — o prompt interativo abre e você já pode conversar. Use `/help` para listar comandos disponíveis.

> 💡 Para uso não interativo (one-shot, só uma mensagem e resposta), o DEILE aceita argumentos diretos pela linha de comando.
>
> ⚠️ **Compatibilidade:** só homologado para Unix-like (Linux/macOS). Windows pode funcionar, mas é experimental.

---

### 🌍 Instalar globalmente (versão local)

Para rodar o comando `deile` em qualquer diretório a partir do **clone local** do repositório:

**Forma recomendada (one-shot):** a partir da raiz do projeto:

```sh
python3 deile.py --install
```

O instalador roda `pip install --user -e .` no Python que você invocou. Em Python gerenciado pelo sistema (ex.: Homebrew no macOS, PEP 668), ele tenta de novo com `--break-system-packages` quando necessário. O executável costuma ficar em `~/.local/bin/deile` — confira se esse diretório está no seu `PATH`.

Rodar de novo **atualiza** a instalação editável para apontar ao diretório atual do repositório.

**Equivalente manual** (se preferir digitar o pip você mesmo):

```sh
python3 -m pip install --user -e .
# Em ambiente PEP 668, se o comando acima falhar:
python3 -m pip install --user --break-system-packages -e .
```

Depois:

```sh
deile                     # modo interativo
deile "resuma a arquitetura do repositório"   # one-shot
deile --version           # imprime a versão do DEILE
deile --status            # painel de saúde do sistema (sem precisar de API key)
deile --tools             # lista as tools registradas
deile --model-list        # tabela de modelos disponíveis
deile --pipeline-status   # status do pipeline autônomo
deile --export ./BACKUP   # exporta dados da sessão
deile --help              # lista TODAS as flags + catálogo de slash commands
```

> Cada comando slash do REPL tem sua flag CLI correspondente — geradas automaticamente a partir do `CommandRegistry` (decisão #24, issue #126). Adicionar uma nova flag é só declarar `cli_flag = "--foo"` na classe do comando.

Para isolar o app sem mexer no Python do sistema, use [pipx](https://pipx.pypa.io/): `brew install pipx` e depois `pipx install -e .` na raiz do repo.

> Para desenvolvimento no dia a dia, um venv dedicado (`.venv`, conda) continua sendo a opção mais limpa para dependências; `--install` é o atalho para ter o comando `deile` globalmente no usuário.

---

## ✨ Funcionalidades reais implementadas


| CHAVE            | Descrição                     | Fonte                                                                             |
| ---------------- | ----------------------------- | --------------------------------------------------------------------------------- |
| 🌐 ROTEAMENTO    | Entre 4 LLM providers         | `deile/core/models/router.py`, `tier_router.py`                                   |
| 🔄 STREAMING     | Unificado de eventos          | `deile/core/models/stream_events.py`                                              |
| 🖼️ RENDERIZAÇÃO | Incremental de Markdown       | `deile/ui/streaming_renderer.py`                                                  |
| 🔁 LOOP          | Iterativo de function calling | `deile/core/tool_loop_executor.py`                                                |
| 📨 BARRAMENTO    | Assíncrono de eventos         | `deile/events/event_bus.py`                                                       |
| 🛠️ REGISTRO     | Extensível de ferramentas     | `deile/tools/registry.py`                                                         |
| 📜 COMANDOS      | Slash registráveis            | `deile/commands/registry.py`, `commands/builtin/`                                 |
| 🎭 PERSONAS      | Dinâmicas via YAML+Markdown   | `deile/personas/library/`, `deile/personas/instructions/`                         |
| 🧠 MEMÓRIA       | Quatro camadas                | `deile/memory/*.py`                                                               |
| 🔒 PERMISSÕES    | Auditoria e scan de segredos  | `deile/security/*.py`                                                             |
| 💾 PERSISTÊNCIA  | SQLite (tasks + uso)          | `deile/orchestration/sqlite_task_manager.py`, `deile/storage/usage_repository.py` |
| 🔌 PLUGINS       | Hot-reload e marketplace      | `deile/plugins/*.py`                                                              |
| 🧬 AUTO-EVOLUÇÃO | Módulo experimental           | `deile/evolution/*.py`                                                            |


---

## 🏗️ Arquitetura e camadas

DEILE segue arquitetura por camadas, com registries para artefatos extensíveis (tools, commands, parsers, personas).


| Camada                 | Pacote                             | Responsabilidade                                     |
| ---------------------- | ---------------------------------- | ---------------------------------------------------- |
| 🧩 Núcleo              | `deile/core/`                      | Lógica central, integração com modelos               |
| 🤖 Modelos LLM         | `deile/core/models/`               | Provedores, roteamento, streaming                    |
| 🔁 Loop de ferramentas | `deile/core/tool_loop_executor.py` | Iteração de function calls                           |
| 📨 Eventos             | `deile/events/`                    | Event bus assíncrono e handlers                      |
| 🛠️ Ferramentas        | `deile/tools/`                     | Registry e implementação de tools                    |
| 📜 Comandos            | `deile/commands/`                  | Slash commands e despacho                            |
| 🧱 Parsers             | `deile/parsers/`                   | Parsing de entrada (arquivos, diffs, refs)           |
| 🎭 Personas            | `deile/personas/`                  | Instruções MD/YAML e manager                         |
| 🧠 Memória             | `deile/memory/`                    | Quatro camadas: working/episodic/semantic/procedural |
| 🔒 Segurança           | `deile/security/`                  | Permissões, audit, secrets scanner                   |
| 💾 Armazenamento       | `deile/storage/`                   | Logger, debug, embeddings, uso/custo                 |
| 🎯 Orquestração        | `deile/orchestration/`             | Planos, workflows, tarefas, aprovações               |
| 🖥️ UI                 | `deile/ui/`                        | Renderização, streaming, display                     |
| 🧬 Evolução            | `deile/evolution/`                 | Auto-learning experimental                           |
| 🔌 Plugins             | `deile/plugins/`                   | Plugin manager, hot-reload                           |
| ⚙️ Infra               | `deile/infrastructure/`            | Adapters externos (SDKs, drivers)                    |
| 🛠️ Configuração       | `deile/config/`                    | Settings singleton, YAML, profiles                   |

---

**Fluxo de uma mensagem do usuário:**

1. `DeileAgentCLI` lê a entrada, encaminha para `DeileAgent`.
2. Parsers extraem menções a arquivos/comandos.
3. Se for slash command, despacha via `CommandRegistry`.
4. Se não, `ModelRouter` escolhe o provider/modelo.
5. Provider emite `UnifiedStreamEvent` (`TEXT_DELTA`, `TOOL_USE`, ...).
6. Em `TOOL_USE`, `ToolLoopExecutor` executa a ferramenta e retorna o resultado na conversa.
7. `StreamingRenderer` acompanha eventos e atualiza terminal (live).
8. `EventBus` publica eventos (telemetria, persona, tool invocation).

---

## 🌐 Provedores de LLM suportados

| Provider    | Classe                | Chave Ambiente         | SDK / Endpoint         |
|-------------|-----------------------|------------------------|------------------------|
| Anthropic   | `AnthropicProvider`   | `ANTHROPIC_API_KEY`    | `anthropic`            |
| OpenAI      | `OpenAIProvider`      | `OPENAI_API_KEY`       | `openai`               |
| DeepSeek    | `DeepSeekProvider`*   | `DEEPSEEK_API_KEY`     | `openai` (endpoint)    |
| Gemini      | `GeminiProvider`      | `GOOGLE_API_KEY`       | `google-genai`         |

> **DeepSeekProvider** estende **OpenAIProvider**

> Os providers só são registrados se a respectiva chave de ambiente estiver setada.

### Catálogo de modelos (`deile/config/model_providers.yaml`):

| Provider   | Modelos disponíveis                                              |
|------------|------------------------------------------------------------------|
| Anthropic  | `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`      |
| OpenAI     | `gpt-5.3-codex`, `gpt-5.4`, `gpt-5.4-mini`                      |
| Gemini     | `gemini-3.1-pro-preview`, `gemini-3-flash-preview`, outros      |
| DeepSeek   | `deepseek-v4-pro`, `deepseek-v4-flash`, `deepseek-reasoner`     |

> O nome do modelo deve ser válido no SDK do provider.

O **ModelRouter** faz fallback entre providers conforme o tier e a estratégia.

---

## 🛠️ Ferramentas integradas

Todas em `deile/tools/` e registradas via `register_tool`:

| Tool                   | Arquivo               | Função                                             |
|------------------------|-----------------------|----------------------------------------------------|
| `read_file`            | file_tools.py         | Ler arquivo (encoding, limites de tamanho)         |
| `write_file`           | file_tools.py         | Escrever arquivo de forma atômica                  |
| `list_files`           | file_tools.py         | Listar arquivos em diretório                       |
| `delete_file`          | file_tools.py         | Remover arquivo com política                       |
| `find_in_files`        | search_tool.py        | Buscar padrões em árvore                           |
| `bash_execute`         | bash_tool.py          | Executar comando shell (níveis de segurança)       |
| `execute_command_enhanced` | execution_tools.py| Comando com PTY/streaming                          |
| `python_execute`       | execution_tools.py    | Executar bloco/arquivo Python isolado              |
| `pip_install`          | execution_tools.py    | Instalar dependência Python                        |
| `run_tests`            | execution_tools.py    | Rodar testes                                       |
| `git`                  | git_tool.py           | Operações Git via GitPython                        |
| `http`                 | http_tool.py          | Requisição HTTP genérica                           |
| `lint_format`          | lint_tool.py          | Lint/format                                        |
| `secrets_scanner`      | secrets_tool.py       | Detectar/redigir segredos                          |
| `archive_tool`         | archive_tool.py       | Compactar/descompactar arquivos                    |
| `process_tool`         | process_tool.py       | Inspecionar processos                              |
| `tokenizer`            | tokenizer_tool.py     | Estimar tokens/analisar contexto                   |
| `slash_command_executor`| slash_command_executor.py | Disparar comandos slash                      |

## 📊 Comandos slash

Os comandos slash do DEILE (em `deile/commands/builtin/`):

```sh
/help             # Lista comandos disponíveis
/status           # Estado do sistema, sessão e métricas
/config           # Visualizar/ajustar configurações
/context          # Inspecionar contexto da sessão
/cls              # Limpar tela
/compact          # Compactar histórico
/cost             # Mostrar custo acumulado por requisição
/debug            # Ativar/desativar debug
/diff             # Mostrar diffs de arquivos
/export           # Exportar transcrições/artefatos
/logs             # Consultar logs
/memory           # Operar camadas de memória
/model            # Selecionar modelo/roteamento
/permissions      # Gerenciar permissões
/plan             # Operar planos de execução
/run              # Executar run orquestrado
/tools            # Listar ferramentas disponíveis
/stop             # Cancelar operação corrente
/approve          # Aprovar etapa pendente
/patch-apply      # Aplicar patch gerado
/patch-generate   # Gerar patch das mudanças
/sandbox          # Status do toggle de sandbox (informativo)
/welcome          # Tela de boas-vindas
```

---

## 🎭 Sistema de personas

Personas MD-driven:

- 📚 Library (`deile/personas/library/`): `architect.yaml`, `debugger.yaml`, `developer.yaml`
- 📝 Instruções (`deile/personas/instructions/`): `developer.md`, `fallback.md`
- 🧠 Memória de persona (`deile/personas/memory/`)
- 🛠️ Infra (`deile/personas/`): loader, builder, manager, etc.

Modificar `instructions/*.md` altera o comportamento sem tocar em Python. Os YAMLs definem nome/persona_id/capacidades.

---

## 🧠 Camadas de memória

Quatro camadas (em `deile/memory/`, agregadas via `memory_manager.py`):

- Working: `working_memory.py` — Estado transitório do turno
- Episodic: `episodic_memory.py` — Eventos da sessão
- Semantic: `semantic_memory.py` — Fatos persistentes (longa duração)
- Procedural: `procedural_memory.py` — Padrões/skills aprendidos

---

## 🔒 Segurança e auditoria

- 📜 Audit logger: `deile/security/audit_logger.py` — Registro de ações sensíveis
- 🛡️ Permissions: `deile/security/permissions.py` — Permissões (política em `config/permissions.yaml`)
- 🔍 Secrets scanner: `deile/security/secrets_scanner.py` — Detecção/redação de credenciais

Tools têm `SecurityLevel` (`tools/base.py`). Além disso, `bash_tool.py` possui classificação própria (`BashSecurityLevel`).

---

## 💾 Persistência

- **Tarefas e listas:** `./deile_tasks.db` (`deile/orchestration/sqlite_task_manager.py`)
- **Telemetria de uso:** `./data/usage.db` (`deile/storage/usage_repository.py`)
- Outras: embeddings (`deile/storage/embeddings.py`), logs/texto/debug.

**DER ASCII resumido:**

```
task_lists 1:N tasks (deile_tasks.db)
usage_records (data/usage.db)
```

> As tabelas SQLite são auto-criadas em runtime. Não há script SQL versionado.

---

## 📡 Sistema de eventos

Definido em `deile/events/event_bus.py` (async EventBus, enum `EventType`):

- Sistema: `SYSTEM_STARTED`, `SYSTEM_STOPPED`
- Persona: `PERSONA_ACTIVATED`, `PERSONA_DEACTIVATED`, `PERSONA_SWITCHED`
- Tarefas: `TASK_CREATED`, `TASK_STARTED`, `TASK_COMPLETED`, `TASK_FAILED`, `TASK_CANCELLED`
- Código: `CODE_GENERATED`, `CODE_EXECUTED`, `CODE_TESTED`, `FILE_MODIFIED`
- Ferramentas: `TOOL_INVOKED`, `TOOL_COMPLETED`, `TOOL_FAILED`

Handlers em `deile/events/event_handlers.py`.

---

## 🔄 Streaming UI

- `UnifiedStreamEvent` (`deile/core/models/stream_events.py`) — evento canônico, provider-agnóstico
- `ToolLoopExecutor` (`deile/core/tool_loop_executor.py`) — itera function calls (até `MAX_TOOL_ITERATIONS=25`)
- `StreamingRenderer` (`deile/ui/streaming_renderer.py`) — Markdown incremental no console (via `rich.live.Live`)

Características:

- Padrão acumulador — re-renderiza o texto a cada delta (cercas e inline OK).
- Live region/diff — só linhas alteradas são repintadas.
- Refresh throttling (12Hz default).
- Fallback de batch para terminais sem ANSI confiável.
- Testável sem UI real — pode usar Console(file=StringIO).

---

## ⚙️ Configuração

### Variáveis de ambiente reconhecidas

```sh
# Uma das chaves obrigatórias para uso:
export ANTHROPIC_API_KEY=...
export OPENAI_API_KEY=...
export DEEPSEEK_API_KEY=...
export GOOGLE_API_KEY=...
```

Veja exemplos em `.env.example` — defina pelo menos uma.

### Arquivos de configuração

Principais caminhos:

- `./config/settings.json` — Configurações runtime
- `./config/permissions.yaml` — Permissões
- `./config/search.yaml` — Busca
- `./config/display.yaml` — Exibição
- `deile/config/system_config.yaml` — Config do sistema
- `deile/config/api_config.yaml` — Config de APIs
- `deile/config/model_providers.yaml` — Catálogo de modelos/tiers
- `deile/config/intent_patterns.yaml` — Padrões de intenção
- `deile/config/persona_config.yaml` — Defaults de persona
- `deile/config/commands.yaml` — Defaults de comandos
- `deile/config/profiles/autonomous_agent.yaml` — Perfil autonomous
- `deile/config/profiles/enterprise.yaml` — Perfil enterprise

> Obs: o repo tem dois `config/`: um na raiz, um em `deile/`. Não confundir.

---

## 📋 Requisitos do sistema

### 🐍 Linguagem e plataforma

- Python >= 3.9
- Sistema: Linux, macOS, Windows*
- Execução: `python3 deile.py`

### 📦 Dependências de produção principais (`requirements.txt`)

- 🤖 LLM SDKs: anthropic, openai, google-genai
- 🖥️ UI/CLI: rich, prompt_toolkit, colorama, Pygments
- ⚡ Async I/O: aiofiles, aiosqlite
- ✅ Validação/config: pydantic, PyYAML, python-dotenv
- 🌐 Rede: requests, httplib2
- 🔧 Sistema: psutil, chardet, GitPython
- 📚 Outras: numpy, pathspec, watchdog, py7zr

### 🧪 Dependências de desenvolvimento (`dev-requirements.txt`)

- Testes: pytest, pytest-asyncio, pytest-mock, pytest-cov, pytest-xdist, pytest-benchmark
- Qualidade: coverage, isort, radon, black
- Segurança: safety, bandit

---

## 🧪 Testes

Configuração (ver `pytest.ini`):

```sh
# Diretório de testes
deile/tests/

# Cobertura exigida (gate): 80%
# Arquivos coletados: test_*.py, *_test.py
# Markers padrões: unit, integration, security, orchestration, bash, ui, slow, perf

# Exemplos de execução:
pytest                               # Roda todos os testes
pytest --cov deile/ --cov-fail-under=80   # Roda com cobertura mínima exigida
```

**Volume real:**  

- 56 arquivos de teste `test_*.py` ou `*_test.py`  
- 66 arquivos Python totais em `deile/tests/` (incluindo helpers, **init**, etc.)  
- Subdiretórios: core/, core/models/, integration/, perf/, tools/, ui/, might/  
- Testes de consumo real de token em `deile/tests/might/` (fora do fluxo padrão)

---

## 🚦 Operação

**Veja o "Quick start" acima para inicialização.**

- Política SQL: scripts SQL são executados pelo operador manualmente. Se der erro, o agente para e reporta.
- Troubleshooting rápido:

```sh
# Se bootstrap_providers não encontra providers:
# → Nenhuma API key definida — edite .env

# Erro --strict-markers no pytest:
# → Marker novo não registrado — registre em pytest.ini

# Cobertura baixa (<80%):
# → Adicione testes ou ajuste o gate

# Ferramenta não encontrada:
# → Garanta que está registrada via register_tool no import path
```

---

## 💪 Pontos fortes / diferenciais técnicos

- 🔁 ToolLoopExecutor único e provider-agnóstico: elimina duplicidade por provider
- 🌐 Fallback automático entre 4 providers: resiliência máxima
- 💵 Telemetria de custo persiste em SQLite
- 🖼️ Streaming Markdown incremental de altíssima UX no terminal
- 🧠 Quatro memórias explícitas: separação clara de estados
- 🎭 Personas MD-driven: editáveis sem mexer no core Python
- 🔌 Plugins hot-reload (sem sandbox — só carregue plugins auditados)
- 🔒 Auditoria + scan de segredos nativo

---

## ⚠️ Limitações conhecidas

- Sem servidor HTTP/REST — apenas CLI
- IDs de modelo no YAML são literais: precisa ser válido no provider
- Limite de iteração no tool-loop: MAX_TOOL_ITERATIONS = 25
- Cobertura real não reportada no README (verificar local via pytest)
- Módulo de evolução ainda experimental
- `/sandbox` é apenas um toggle informativo — não fornece isolamento real (ver issues #54/#55/#57)

---

## 🤝 Como contribuir

Contribuições são bem-vindas! Corrija typos, crie ferramentas, comandos, personas, providers ou memórias.

### ⚡ Fluxo recomendado com `gh` (para o DEILE e contribuidores)

O fluxo abaixo é o que o DEILE segue (e é indicado seguir) ao trabalhar com GitHub. Os comandos `gh` são os mais relevantes em cada etapa.


| #   | Etapa                      | Ação                                              | Comandos `gh` principais                                                           |
| --- | -------------------------- | ------------------------------------------------- | ---------------------------------------------------------------------------------- |
| 1   | **Explorar issues**        | Listar e visualizar issues abertas                | `gh issue list` · `gh issue view <id>`                                             |
| 2   | **Criar issue**            | Registrar bug, feature ou discussão               | `gh issue create --title "..." --body "..."`                                       |
| 3   | **Criar branch vinculada** | Criar branch com rastreio automático da issue     | `gh issue develop <id> --checkout`                                                 |
| 4   | **Implementar**            | Editar código, rodar testes e lint                | `pytest` · `ruff check deile/` · `isort --check-only deile/`                       |
| 5   | **Commitar**               | Registrar mudanças (Conventional Commits)         | `git add -p` · `git commit -m "feat(scope): ..."`                                  |
| 6   | **Push**                   | Enviar branch ao remoto                           | `git push -u origin <branch>`                                                      |
| 7   | **Abrir PR**               | Criar pull request com título e corpo descritivos | `gh pr create --title "..." --body "..."` · `gh pr create --fill`                  |
| 8   | **Inspecionar PR**         | Ver diff, status de checks e detalhes             | `gh pr view <id>` · `gh pr diff <id>` · `gh pr checks <id>`                        |
| 9   | **Comentar**               | Deixar comentário em PR ou issue                  | `gh pr comment <id> --body "..."` · `gh issue comment <id> --body "..."`           |
| 10  | **Revisar**                | Aprovar ou solicitar mudanças                     | `gh pr review <id> --approve` · `gh pr review <id> --request-changes --body "..."` |
| 11  | **Merge**                  | Integrar branch aprovada                          | `gh pr merge <id> --squash` · `gh pr merge <id> --merge`                           |
| 12  | **Fechar issue**           | Encerrar issue resolvida                          | `gh issue close <id> --comment "Resolvida via PR #..."`                            |


> **Dica:** `gh issue develop <id> --checkout` cria a branch já vinculada à issue e faz checkout automaticamente. O PR aberto com `gh pr create` detecta a branch e associa ao issue correspondente.

---

### 🔁 Fluxo padrão (fork + PR)

```sh
# 1. Faça o fork no GitHub
# 2. Clone seu fork:
git clone https://github.com/<seu-usuario>/deile.git
cd deile

# 3. Adicione o remoto original
git remote add upstream https://github.com/elimarcavalli/deile.git

# 4. Sincronize com upstream
git fetch upstream && git checkout main && git merge upstream/main

# 5. Crie branch para sua mudança
git checkout -b feature/nome-feature

# 6. Configure ambiente (cuida de venv, deps e .env)
python3 deile.py

# 7. Implemente e teste
pytest
ruff check deile/

# 8. Commits no padrão Conventional
git commit -m "feat(tools): adiciona nova ferramenta foo"

# 9. Push no seu fork
git push origin feature/nome-feature

# 10. Abra o PR no GitHub (do seu fork → main do upstream)
```

### ✅ Checklist antes do PR

| ✔️  | Item                                                               |
|-----|--------------------------------------------------------------------|
| [ ] | Testes verdes (`pytest`)                                          |
| [ ] | Código passa `ruff check deile/`                                  |
| [ ] | `isort --check-only deile/` sem pendências                        |
| [ ] | Commits no padrão Conventional Commits                            |
| [ ] | Atualize o AGENTS/README/CHANGELOG se necessário                  |
| [ ] | Nunca commitar arquivos sensíveis ou residuais                    |

### 🧩 Como adicionar extensões

| 🧩 O quê                | 📁 Onde                                                          | 📝 Observação                                    |
|-------------------------|-------------------------------------------------------------------|--------------------------------------------------|
| 🛠️ Tool                 | `deile/tools/<nome>.py`                                           | Decore com `@register_tool`                      |
| ⌨️ Slash command        | `deile/commands/builtin/<nome>.py`                                | Registre no `CommandRegistry`                    |
| 🗂️ Parser               | `deile/parsers/<nome>.py`                                         | Siga o contrato base                             |
| 🧑‍🎤 Persona              | `personas/instructions/` e `personas/library/`                    | MD/YAML                                          |
| 🧠 Provider de LLM      | `core/models/`                                                    | Registre em `bootstrap.py` + YAML dos modelos    |

### 🐛 Reportando bugs/features/refatorações

| #  | 📝 Ação                                             | 🚩 Template                                             |
|----|-----------------------------------------------------|-------------------------------------------------------| 
| 1️⃣ | 🐞 Bug: crie issue                                  | [Bug Report Template](.github/ISSUE_TEMPLATE/bug_report.md) |
| 2️⃣ | ✨ Feature request: crie issue                      | [Feature Request Template](.github/ISSUE_TEMPLATE/feature_request.md) |
| 3️⃣ | 🧠 Refatoração: crie issue                          | [Refactoring Proposal Template](.github/ISSUE_TEMPLATE/refactoring_proposal.md) |


> 💡 PRs grandes ou que mexam em arquitetura: recomendado abrir issue de discussão primeiro.

---

## 📄 Licença

Projeto licenciado sob [**MIT License**](LICENSE).

## 👤 Construtores

| Nome                | GitHub                                                       | Site                                                 | E-mail                                              |
|---------------------|--------------------------------------------------------------|------------------------------------------------------|-----------------------------------------------------|
| Elimar Cavalli      | [@elimarcavalli](https://github.com/elimarcavalli)           | [elimar.dev](https://elimar.dev)                     | [elimar.dev@gmail.com](mailto:elimar.dev@gmail.com) |
| DEILE               | [@DEILE](https://github.com/elimarcavalli/deile)             | [elimar.dev/deile](https://elimar.dev/deile)         | [deile@elimar.dev](mailto:deile@elimar.dev)         |
| Open DEILE          | [@OpenDEILE](https://github.com/elimarcavalli/opendeile)     | [elimar.dev/opendeile](https://elimar.dev/opendeile) | [opendeile@elimar.dev](mailto:opendeile@elimar.dev) |

---

**DEILE 5.1.0** — `python3 deile.py`