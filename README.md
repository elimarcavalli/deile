# 🤖 DEILE — Development Environment Intelligence & Learning Engine

<p align="center">
  <img src="docs/img/banner.png" alt="DEILE" width="480">
</p>

![Version](https://img.shields.io/badge/version-5.1.0-blue.svg?style=for-the-badge)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg?style=for-the-badge)
![License](https://img.shields.io/badge/license-MIT-green.svg?style=for-the-badge)

**Um *agent harness* multi-provedor — e a plataforma de orquestração autônoma construída sobre ele — para desenvolvimento de software.**

O DEILE tem **duas faces** que compartilham o mesmo núcleo Python:

1. 🧑‍💻 **CLI interativo** — você conversa em linguagem natural no terminal e ele lê, escreve e edita arquivos, roda comandos, executa testes, busca no repositório, planeja tarefas e acompanha custo — tudo no diretório de trabalho atual.
2. ☸️ **Pipeline autônomo em Kubernetes** — uma frota de pods que monitora um repositório **GitHub ou GitLab**, refina issues, implementa em branches `auto/issue-N`, abre PRs/MRs e revisa-as **sem intervenção humana**.

### 🧩 Onde o DEILE se encaixa

No **núcleo**, o DEILE é um **agent harness** — o runtime que transforma uma API de LLM em agente de verdade: loop de tool-calling, roteamento com fallback, streaming, memória e permissões. É a mesma categoria de Claude Code, aider ou Codex CLI — só que sobre **4 provedores** em vez de um.

Em **volta** desse harness há uma **plataforma de orquestração autônoma** (o pipeline + a frota K8s) que coordena agentes em paralelo sobre um forge. E aqui está o detalhe que define a categoria: o DEILE **pode dirigir outro harness** — o pod `claude-worker` executa `claude -p` (o próprio Claude Code) como um dos workers despacháveis **por etapa do pipeline**, lado a lado com o `deile-worker` (o motor próprio do DEILE). Ou seja: **harness no núcleo, meta-harness/orquestrador na borda.**

> Este README descreve **apenas o que existe no código**. Cada afirmação foi conferida na fonte. Onde algo é experimental, parcial ou um gap conhecido, está sinalizado.

---

## 🗺️ Índice

| Seção | O quê |
|---|---|
| [🚀 Visão geral](#-visão-geral) | O que o DEILE faz, para quem |
| [⚡ Quick start](#-quick-start) | Subir o CLI em minutos |
| [🧰 Ferramentas](#-ferramentas-integradas) | As tools que o agente aciona |
| [📊 Comandos slash](#-comandos-slash) | O REPL e suas flags CLI |
| [🌐 Provedores & modelos](#-provedores-de-llm-roteamento-e-modelos) | LLMs, tiers, fallback |
| [🧠 Reasoning effort](#-reasoning-effort-esforço-de-raciocínio) | Esforço de raciocínio por etapa |
| [🎭 Personas](#-sistema-de-personas) · [🧩 Skills](#-sistema-de-skills) · [🧠 Memória](#-camadas-de-memória) | Componentes plugáveis |
| [🧩 Sub-DEILEs paralelos](#-sub-deiles-paralelos-decomposição-em-sessão-cli) | Decomposição concorrente |
| [🤖 Pipeline autônomo](#-pipeline-autônomo-de-issue-a-pr) | A máquina de estados de issue→PR |
| [🌳 Forge GitHub + GitLab](#-forge--github--gitlab) | Forge-agnóstico |
| [☸️ Stack Kubernetes](#-stack-kubernetes) | Os 6 pods, deploy.py, painel |
| [🔭 Observabilidade](#-observabilidade-e-eventos) | OTLP, runtime state, status servers |
| [🔒 Segurança](#-segurança-e-auditoria) · [💾 Persistência](#-persistência) | Permissões, custo, SQLite |
| [🏗️ Arquitetura](#-arquitetura-e-camadas) · [⚙️ Configuração](#-configuração) | Camadas e settings |
| [📋 Requisitos](#-requisitos-do-sistema) · [🧪 Testes](#-testes) · [🚦 Operação](#-operação-e-troubleshooting) | Engenharia |
| [⚠️ Limitações](#-limitações-conhecidas) · [🤝 Contribuir](#-como-contribuir) | Realidade & contribuição |

---

## 🚀 Visão geral

DEILE **pensa, decide e resolve**: aciona ferramentas reais (function calling) para entender o problema, planejar e concluir o que foi pedido, mostra o que está fazendo em tempo real (streaming) e mantém memória da conversa entre turnos. Funciona em **português ou inglês**.

### 🎯 Para quem é

| Perfil | O que ganha |
|---|---|
| 👩‍💻 Pessoa desenvolvedora | Um par de programação que **executa**, não só sugere |
| 🛠️ Engenharia de plataforma | Automação de tarefas repetitivas dentro do repositório |
| 🔍 Revisão de código | Leitura guiada do projeto em linguagem natural |
| 🤖 Operação autônoma | Frota que processa issues → PRs/MRs 24/7 num cluster |
| 🎓 Aprendizado | Observar passo a passo como um agente decide e usa ferramentas |

### ✨ O que o DEILE faz hoje

| Capacidade | Descrição |
|---|---|
| 💬 Conversa multi-turno | Contexto e histórico de sessão persistentes |
| 🖼️ Streaming UI | Resposta em streaming com renderização incremental de Markdown no terminal |
| 🔁 Loop de ferramentas | Function calling iterativo até concluir a tarefa (limite configurável) |
| 🧩 Sub-DEILEs paralelos | Decompõe pedidos complexos em N sub-agentes em paralelo, com painel ao vivo |
| 🛠️ Edição de código | Lê, cria, edita, deleta e busca arquivos no repositório |
| ⚙️ Execução local | Shell/Python, instala pacotes, roda testes |
| 🌐 Roteamento LLM | Roteia entre **4 provedores** com fallback e seleção por tier |
| 🧠 Reasoning effort | Esforço de raciocínio ajustável (`low`…`ultracode`), global e por etapa do pipeline |
| 🧠 Memória | Quatro camadas: working, episodic, semantic, procedural |
| 📋 Orquestração | Planeja tarefas, gerencia dependências, workflows com rollback |
| ⌨️ Comandos slash | ~40 comandos no REPL; vários também expostos como flag CLI |
| 🎭 Personas | Comportamento via Markdown + YAML, sem mudar Python |
| 🧩 Skills | Unidades de expertise em Markdown com hot-reload e 4 gatilhos de auto-injeção |
| 🤖 Pipeline autônomo | Issue → refino → implementação → PR → review → merge, em GitHub **ou** GitLab |
| 💰 Telemetria | Tokens, latência e custo em USD com persistência SQLite + ledger durável |
| 🔭 Observabilidade | OpenTelemetry (traces/métricas) + runtime state por processo + painel TUI |
| 🔒 Segurança | Permissões, aprovação por risco, auditoria tipada e scanner de segredos |

---

## ⚡ Quick start

**Pré-requisitos:** Python **3.9+** e ao menos uma chave de API entre Anthropic, OpenAI, DeepSeek e Gemini.

> 🧭 **Cobertura por chave:** `OPENAI_API_KEY` ou `DEEPSEEK_API_KEY` cobrem todas as tiers (1–4). `GOOGLE_API_KEY` cobre tiers 1–3. `ANTHROPIC_API_KEY` cobre tiers 1–3. Para cobertura plena e fallback entre provedores, use pelo menos duas chaves.

### 1️⃣ Clonar

```sh
git clone https://github.com/elimarcavalli/deile.git
cd deile
```

### 2️⃣ Início rápido (recomendado)

O próprio `deile.py` faz o setup na 1ª execução: cria `.venv`, pergunta as chaves de API (input oculto), gera o `.env`, instala dependências e sobe a CLI. Nas execuções seguintes, detecta o ambiente e inicia direto.

```sh
python3 deile.py
```

### 3️⃣ Início manual

```sh
python3 -m venv .venv
source .venv/bin/activate              # macOS/Linux  (.venv\Scripts\activate no Windows)
pip install -r requirements.txt
cp .env.example .env                   # ~450 linhas, seções comentadas
# preencha ao menos uma chave: ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY / GOOGLE_API_KEY
python3 deile.py
```

Use `/help` para listar os comandos. Para uma única mensagem (one-shot), passe o prompt como argumento.

> ⚠️ **Compatibilidade:** homologado para Unix-like (Linux/macOS). Windows pode funcionar, mas é experimental (o status server por Unix-socket e o flock viram no-op).

### 🌍 Instalar globalmente (a partir do clone local)

```sh
python3 deile.py --install                          # pergunta o modo (global/local) e instala
python3 deile.py --install --install-mode global    # venv isolado em ~/.deile/venv/ (pula o prompt)
python3 deile.py --install --install-mode local     # venv no próprio repo (<repo>/.venv/)
```

O `--install` cria um **venv isolado** (não toca o Python do sistema — contorna o PEP 668 sem `--break-system-packages`), instala o DEILE editável lá dentro e cria um **shim** `deile` em `~/.local/bin/` (tentando ajustar o `PATH` no rc do shell). Depois:

```sh
deile                                   # REPL interativo
deile "resuma a arquitetura do repo"    # one-shot
deile --version                         # versão
deile --status                          # painel de saúde (não exige API key)
deile --tools                           # lista tools registradas
deile --model-list                      # tabela de modelos
deile --pipeline-status                 # status do pipeline autônomo
deile --help                            # TODAS as flags + catálogo de slash commands
```

> **Vários** comandos slash têm uma flag CLI correspondente, **gerada automaticamente** a partir do `CommandRegistry` (issue #126) — declarar `cli_flag = "--foo"` na classe do comando cria a flag (nem todos os ~40 comandos expõem uma).

---

## 🧰 Ferramentas integradas

O LLM aciona ferramentas reais via function calling. O conjunto-padrão é auto-descoberto pelo `ToolRegistry` (`deile/tools/discovery.py`); demais tools são registradas explicitamente.

### 📁 Arquivos & busca
| Tool | Função |
|---|---|
| `read_file` | Ler arquivo (encoding, limites de tamanho) |
| `write_file` | Escrever arquivo de forma atômica |
| `edit_file` | Edição cirúrgica (substituição de trecho) |
| `delete_file` | Remover arquivo com política |
| `list_files` | Listar arquivos em diretório |
| `find_in_files` | Buscar padrões na árvore do projeto |

### ⚙️ Execução
| Tool | Função |
|---|---|
| `bash_execute` | Executar comando shell (com níveis de segurança próprios) |
| `python_execute` | Executar bloco/arquivo Python |
| `pip_install` | Instalar dependência Python |
| `run_tests` | Rodar a suíte de testes |
| `vision_describe_image` | Descrever/analisar uma imagem |

### 🤖 Orquestração & autonomia
| Tool | Função |
|---|---|
| `dispatch_parallel_subagents` | Decompõe o pedido em 2–5 sub-DEILEs paralelos (sessões limpas) |
| `dispatch_deile_task` | Despacha uma tarefa a outro DEILE (worker) |
| `worktree` | Cria/gerencia git worktrees isolados |
| `pipeline` | Controla o pipeline autônomo (start/stop/status) |
| `pipeline_schedule` | Agenda ações do pipeline (recorrente/one-shot, cron) |
| `cron_create` · `cron_list` · `cron_delete` | Agenda prompts naturais via cron genérico |

### 🧠 Skills & preferências
| Tool | Função |
|---|---|
| `list_skills` | Catálogo machine-readable das skills carregadas |
| `invoke_skill` | Carrega o body de uma skill por nome (sob demanda) |
| `remember_preference` · `list_preferences` · `forget_preference` | Memória de preferências do usuário |

### 📨 Mensageria (opcional — só com `deilebot` instalado)
`discord_send_message`, `discord_send_dm`, `discord_edit_message`, `discord_react`, `discord_pin_message`, `discord_start_thread`, `discord_mention_role`, `discord_get_user_profile`, `whatsapp_send_template`.

> 🔒 Tools de **DM** e **menção de cargo** são `SecurityLevel.DANGEROUS` e passam pelo `ApprovalSystem` por design. Elas só se registram quando `import deilebot` funciona **e** `DEILE_BOT_ENDPOINT` + `DEILE_BOT_AUTH_TOKEN` estão configurados.

---

## 📊 Comandos slash

Todos em `deile/commands/builtin/`. Aliases entre parênteses.

| Grupo | Comandos |
|---|---|
| 🧭 Sessão | `/help` · `/status` · `/welcome` · `/context` · `/compact` · `/clear` (`/cls`) · `/stop` · `/rename` · `/resume` · `/rewind` (`/rw`) · `/fork` |
| 💰 Custo & métricas | `/cost` · `/loc` (`/estatisticas`) · `/standup` |
| 🧠 Config & runtime | `/config` · `/settings` · `/env` · `/model` · `/reasoning` (`/effort`) · `/memory` · `/permissions` · `/debug` |
| 🛠️ Trabalho | `/tools` · `/skills` · `/plan` · `/run` · `/todo` · `/diff` · `/patch` (`/patch-generate`) · `/apply` (`/patch-apply`) · `/approve` · `/logs` · `/export` |
| 🤖 Pipeline & cluster | `/pipeline` · `/pipeline-schedule` · `/backlog` · `/pods` · `/panel` |
| ℹ️ Outros | `/sandbox` · `/version` (`/ver`) |

> Além dos built-ins, **toda skill carregada vira `/<name>`** automaticamente (exceto as *bundled* em `deile/skills/library/`, que ficam só via auto-trigger e `invoke_skill`). Skills de `~/.claude/commands/` registram em UPPERCASE.

---

## 🌐 Provedores de LLM, roteamento e modelos

Definição em `deile/config/model_providers.yaml`. Provedores só são registrados quando a respectiva chave de ambiente está setada (`bootstrap_providers()` em `deile/core/models/bootstrap.py`).

| Provider | Classe | Chave de ambiente | SDK / endpoint |
|---|---|---|---|
| Anthropic | `AnthropicProvider` | `ANTHROPIC_API_KEY` | `anthropic` |
| OpenAI | `OpenAIProvider` | `OPENAI_API_KEY` | `openai` |
| DeepSeek | `DeepSeekProvider` ⟵ estende `OpenAIProvider` | `DEEPSEEK_API_KEY` | `openai` (endpoint `api.deepseek.com/v1`) |
| Gemini | `GeminiProvider` | `GOOGLE_API_KEY` | `google-genai` |

### 🧮 Roteamento — dois roteadores coexistem

- **`ModelRouter`** (`router.py`) — roteador legado baseado em estratégia.
- **`TierRouter`** (`tier_router.py`) — roteamento por **tier** (`tier_1` complexo → `tier_4` bulk/barato), com um **circuit breaker por provider** injetado (`CircuitBreaker`, estados em `BreakerState`) e fallback automático na cascata do tier.
- **Estratégias** (`routing_strategies.py`): `task_optimized` (default) e `cost_optimized`, além de `round_robin` / `least_busy`.
- **Budget guard** (`BudgetGuard` em `deile/storage/usage_repository.py`) — enforcement de orçamento por **sessão** e por **provider (diário e mensal)**.

### 📚 Catálogo de modelos (verificado em `model_providers.yaml`)

| Provider | Modelos (tier) |
|---|---|
| Anthropic | `claude-opus-4-8` (1) · `claude-sonnet-4-6` (2) · `claude-haiku-4-5` (3) |
| OpenAI | `gpt-5.5` (1) · `gpt-5.4` (2) · `gpt-5.4-mini` (3) · `gpt-5.4-nano` (4) |
| Gemini | `gemini-3.1-pro-preview` (1) · `gemini-2.5-pro` (1) · `gemini-3.5-flash` (2) · `gemini-3-flash-preview` (2) · `gemini-3.1-flash-lite` · `gemini-2.5-flash` · `gemini-2.5-flash-lite` |
| DeepSeek | `deepseek-v4-pro` (1) · `deepseek-v4-flash` (3) |

> Cada entrada do YAML declara pricing (input/output/cached por 1M tokens) e `context_window` — base para a telemetria de custo. O nome do modelo precisa ser válido no SDK do provider.

---

## 🧠 Reasoning effort (esforço de raciocínio)

Fonte única em `deile/core/models/reasoning.py`. O DEILE adota o **vocabulário do Claude Code** — `low` · `medium` · `high` · `xhigh` · `max` · `ultracode` · `auto` — e o traduz para o parâmetro nativo de cada provider (Anthropic `effort`, OpenAI `reasoning_effort`, Gemini `thinking_config`, DeepSeek). A tradução é **fail-open**: nunca quebra o turno.

Configurável em três níveis: **global** (`DEILE_REASONING_EFFORT` / `/reasoning` no CLI), **por etapa do pipeline** (`DEILE_PIPELINE_REASONING_<STAGE>`) e na coluna *Reasoning* do painel TUI.

---

## 🎭 Sistema de personas

Personas são MD-driven: editar o Markdown muda o comportamento, sem tocar em Python.

- 📚 **Library** (`deile/personas/library/*.yaml`): `analyst`, `architect`, `debugger`, `developer`, `reviewer` (nome/id/capacidades).
- 📝 **Instruções** (`deile/personas/instructions/*.md`): `analyst`, `architect`, `debugger`, `developer`, `reviewer`, `discord_developer`, `monitor`, `fallback` (a prosa que entra no system prompt).

O pipeline escolhe a persona pelo tipo da issue/etapa — `analyst` refina intents, `architect` arquiteta features/refactors, `debugger` investiga bugs e `reviewer` revisa PRs. A persona `monitor` fica **fora** desse despacho por tipo: supervisiona o namespace K8s na Fase B do pod `deile-monitor` (o tick mecânico do pipeline é determinístico, sem LLM).

---

## 🧩 Sistema de skills

Skills são unidades composáveis de expertise em **Markdown puro** (sem código Python). O loader varre as fontes abaixo em ordem de prioridade crescente — em colisão de nome o source mais alto vence (`INFO` log):

| Origem | Caminho | Comportamento |
|---|---|---|
| Bundled | `deile/skills/library/**/*.md` | Vai no pacote; PR no repo. Bundled out-of-the-box: `python`, `typescript`, `tdd` |
| Usuário pessoal | `~/.deile/skills/*.md` | Visível em qualquer projeto seu |
| Usuário (Claude compat) | `~/.claude/commands/*.md` | Nome registrado em UPPERCASE (`kind=command`) |
| Projeto | `<cwd>/.deile/skills/*.md` | Versionada no git, viaja com o repo |
| Projeto (Claude compat) | `<cwd>/.claude/commands/*.md` | UPPERCASE (`kind=command`) |
| Configurada | `library_paths:` em `deile/config/skills.yaml` ou `/skills add` | Paths extras (escopo global/project) |

### Como o LLM usa cada skill (três caminhos independentes)

- **Auto-injeção no system prompt** quando um `trigger` casa (até `max_per_turn=4`, ordenadas por `(-priority, name)`):
  - `file_globs` — `fnmatch` no basename/path
  - `code_block_langs` — fence ` ```python ` no input (case-insensitive)
  - `keywords` — match **literal** (escapado) com word-boundary, case-insensitive (não confunde "rust" com "trust")
  - `file_content_patterns` — regex em 4 KiB de cada arquivo referenciado, **contido ao `project_root`** (segurança)
- **Function-calls** `invoke_skill(name)` e `list_skills` — o LLM puxa skills que não dispararam por trigger.
- **Slash `/<name>`** — invocação explícita pelo usuário, com argumentos opcionais.

🔁 **Hot-reload via `watchdog`**: dropar/editar/remover um `.md` reflete em ~0,5 s, sem restart (swap atômico no `SkillRegistry`).

---

## 🧠 Camadas de memória

Quatro camadas em `deile/memory/`, agregadas via `MemoryManager` (`memory_manager.py`). A regra: estado cross-turn **não** vive em globals de módulo — vai para a camada certa.

| Camada | Módulo | Propósito |
|---|---|---|
| Working | `working_memory.py` | Estado transitório do turno (TTL) |
| Episodic | `episodic_memory.py` | Eventos da sessão — persistido em SQLite (`aiosqlite`, `episodes.db`) |
| Semantic | `semantic_memory.py` | Fatos/conhecimento de longa duração |
| Procedural | `procedural_memory.py` | Padrões/skills aprendidos |

> Nenhuma camada guarda segredos ou PII; toda escrita é assíncrona. A consolidação entre camadas fica em `memory_consolidation.py`.

---

## 🧩 Sub-DEILEs paralelos (decomposição em sessão CLI)

Numa conversa interativa, o DEILE pode identificar sub-tarefas **independentes e substanciais** e dispará-las em paralelo — cada uma num sub-DEILE com **sessão limpa** (contexto/histórico próprios). Você vê o progresso ao vivo num painel multipanel.

✅ "Refator módulo A E módulo B (não-acoplados)" · "Gere testes pra X, Y e Z"
❌ Tarefas sequenciais · micro-tarefas (<30s) · mesmo arquivo

O LLM chama `dispatch_parallel_subagents` com 2–5 sub-tarefas. O `SubAgentOrchestrator` dispara em paralelo (semaphore `DEILE_SUBAGENT_MAX_PARALLEL`, default 3), respeitando o budget global. Cada sub-DEILE vai direto ao tool-loop, então o painel mostra `⚙ bash_execute(...)`, `✓ write_file: 412 bytes`, `✎ texto-em-curso` em tempo real.

```
🧩 Decomposto em 2 frentes paralelas · 1 ok · 1/2 concluídas · 00:08

╭─ ▶ sub-DEILE #1 · refatorar auth.py ──────────────────────────── 00:08 ──╮
│ ⚙ bash_execute(pytest deile/tests/auth -q)                               │
│ ✓ bash_execute: 5 passed in 0.04s                                        │
│ ✎ aplicando guard clauses (3/5 funções)                                  │
╰──────────────────────────────────────────────────────────────────────────╯
╭─ ✅ sub-DEILE #2 · doc do módulo X ──────────────────────────────────────╮
│ ✅ concluído · docs/x.md                                                  │
╰──────────────────────────────────────────────────────────────────────────╯
(toque 1-9 para focar · ESC: fecha painel)
```

**Runners pluggable:** `LocalSubAgentRunner` (default, in-process via `asyncio`) ou `WorkerSubAgentRunner` (delega ao `deile-worker` por HTTP — `DEILE_SUBAGENT_RUNNER=worker`). Falha de uma frente **não** cancela as siblings; o resumo final é gravado no histórico e o `/resume` reconstrói o painel.

| Variável | Default | Uso |
|---|---|---|
| `DEILE_SUBAGENT_RUNNER` | `local` | `local` \| `worker` |
| `DEILE_SUBAGENT_MAX_PARALLEL` | `3` | Teto de concorrência por chamada |
| `DEILE_SUBAGENT_BUDGET_S` | `600` | Teto global de tempo (s) |
| `DEILE_SUBAGENT_POLL_INTERVAL_S` | `0.8` | Polling do worker runner |

---

## 🤖 Pipeline autônomo (de issue a PR)

Quando uma issue recebe `~workflow:nova` (ou o bot é atribuído/mencionado), o pipeline a leva sozinho até a PR/MR — refinando o escopo antes de escrever uma linha de código. Roda no pod `deile-pipeline` (`PipelineMonitor.tick()` em loop).

### 🔁 Máquina de estados de issues

```
🆕 ~workflow:nova
      │
      ▼  Stage 1 — crítica de escopo (persona por tipo: analyst/architect/debugger)
🔍 ~workflow:em_revisao
      │
      ├─ VEREDITO: CLARO ──────────────► ✅ ~workflow:revisada
      │                                        │
      │                            ┌───────────┴────────────┐
      │                         (code-type)             (intent)
      │                            ▼                        ▼
      │                    🚀 em_implementacao        🧩 decomposta
      │                            ▼                  (architect abre N derivadas)
      │                    📬 em_pr  →  PR/MR aberta
      │
      └─ VEREDITO: VAGO ─► 🏷️ refinar + estado de refino:
                              🧠 em_refinamento  (intent → analyst)
                              🏛️ em_arquitetura  (feature/bug/refactor → architect/debugger)
                                     │
                                     ↕ ⏸️ aguardando_stakeholder (humano remove p/ retomar)
                                     │
                              volta p/ 🆕 ~nova  (até 5 voltas; estourou → ⛔ bloqueada)
```

- **Portão de refinamento** — toda issue nova é **criticada por escopo** por uma persona escolhida pelo tipo: `intent → analyst`, `feature`/`refactor` → `architect`, `bug → debugger`. Issues **VAGAS** ganham `refinar` + estado de refino e são reescritas (corpo, comentários, bracket `[TIPO]` do título) **até 5 voltas** (contador durável via label `~refine:N`). Um gap de alto impacto pode pausar em `~workflow:aguardando_stakeholder` com 2–3 opções sugeridas — o humano remove a label para retomar.
- **Decomposição** — intents que passam são quebrados em issues derivadas pelo `architect` (default anti-flood: **agregar numa única** issue com checklist `- [ ]`; só dividir com independência provada).
- **PRs/MRs** — `~review:pendente` → `~review:em_andamento` → `~review:concluida`.
- **Locks & marcadores** — `~batch:<sha8>` (claim, só quando há mais de um monitor); `~mention:processado` (idempotência); `~workflow:bloqueada` (bloqueio duro, exclui do auto-resume).

### 🧭 As 5 etapas — cada uma roteável de forma independente

`classify` · `refine` · `implement` · `pr_review` · `follow_ups`. Para cada etapa você escolhe **três eixos** independentes (com fallback global e persistência por env var **ou** `~/.deile/settings.json`):

| Eixo | Por etapa | Global | Onde |
|---|---|---|---|
| 👷 **Worker** | `DEILE_PIPELINE_DISPATCH_<STAGE>` | `DEILE_PIPELINE_DISPATCH_MODE` (default `deile-worker`) | `dispatch_resolver.py` |
| 🧠 **Modelo** | `DEILE_PIPELINE_MODEL_<STAGE>` | `DEILE_PREFERRED_MODEL` | `model_resolver.py` |
| 🤔 **Reasoning** | `DEILE_PIPELINE_REASONING_<STAGE>` | `DEILE_REASONING_EFFORT` | `reasoning_resolver.py` |

São dois "workers" possíveis por etapa:

- 🐍 **`deile-worker`** (`:8766`) — roda o **DEILE Python in-process**, usando seus próprios provedores de LLM.
- 🤖 **`claude-worker`** (`:8767`) — roda o **`claude -p`** (Claude Code CLI) em worktrees isolados sob PVC, com credenciais OAuth.

### 📌 Roteamento de menção/atribuição (`process_mentions` é um roteador)

| Trigger | Ação |
|---|---|
| **Issue** + assignee/menção no corpo | Injeta `~workflow:nova` → o pipeline assume |
| **Qualquer trigger sobre uma PR/MR** | Brief **unificado** `pr_unified`: o worker abre a PR, descobre o estado real (papel autor/assignee/reviewer; HEAD vs último review; threads abertas; comentários dirigidos a mim) e age — revisa, comenta, atende thread ou mergeia. PR **open** → push direto; **merged/closed** → branch derivada `auto/<orig>-followup-<sha>` + nova PR |
| **Issue** + comentário | Faz o que o comentário pede (one-shot sob persona `developer`); se a issue está num gate ativo, o próprio gate relê o comentário |

> 🔒 **Quality-gate:** o **brief unificado de PR** exige a **suíte completa verde** (`python3 -m pytest deile/tests/ -q`) antes de approve/merge; o **implementador** roda só os testes impactados (`-p no:cov`) e delega a suíte completa ao revisor. O brief confronta entrega vs pedido — testes verdes não substituem requisito faltante.

### ♻️ Resume sob demanda

Despacho **fresh é o default**; resume só é solicitado (`resume=True`) quando há trabalho-em-curso real registrado no `DispatchLedger` (anti-double-dispatch). Sessões `claude -p` acima do orçamento de tokens (`DEILE_CLAUDE_RESUME_TOKEN_BUDGET`, ~100K) são **promovidas para fresh** em vez de rejeitadas. Falhas de auth recorrentes entram em **backoff exponencial** por target. O brief lê `.deile-progress.md` no PASSO 0 — então "fresh com contexto natural" cobre a maioria dos casos sem inflar o JSONL da sessão.

### 🏁 Quick start do pipeline (REPL)

```sh
export DEILE_FORGE_REPO=owner/repo          # env var canônico do repositório (no settings.json: pipeline.repo)
export DEILE_PIPELINE_BASE_PATH=/caminho/para/repo
> /pipeline start          # inicia o loop de polling
> /pipeline status         # contadores
> /pipeline stop           # para
```

Ou `DEILE_PIPELINE_AUTOSTART=1`. Agendamento via `pipeline_schedule(...)` (recorrente/one-shot, cron) e `cron_create(prompt=..., cron=...)`.

---

## 🌳 Forge — GitHub & GitLab

O DEILE é **forge-agnóstico** (issue #297). O mesmo agente, pipeline e briefs operam **idênticos** em **GitHub** (cloud, GHES) e **GitLab** (cloud, self-hosted). A camada `deile/orchestration/forge/` esconde a diferença sob `ForgeClient` (ABC); o pipeline nunca chama `gh`/`glab` direto.

| Cenário | `DEILE_FORGE_KIND` | Hosts | Tokens |
|---|---|---|---|
| 🐙 GitHub cloud (padrão) | `auto` ou `github` | default `github.com` | `GITHUB_TOKEN` (escopos `repo` + `workflow`; **sem** `read:org`) |
| 🦊 GitLab cloud | `gitlab` | default `gitlab.com` | `GITLAB_TOKEN` (escopos `api`, `read_repository`, `write_repository`) |
| 🏢 GitHub Enterprise Server | `github` | `DEILE_GITHUB_HOST=ghe.empresa.com` | `GITHUB_TOKEN` do GHES |
| 🏠 GitLab self-hosted | `gitlab` | `DEILE_GITLAB_HOST=gitlab.empresa.com` | `GITLAB_TOKEN` do GL |
| 🌐 Multi-forge na sessão CLI | `auto` | ambos declarados | **AMBOS** |

### 🔄 Detecção em 3 camadas (`detect_forge_kind`)

```
1. DEILE_FORGE_KIND="github"|"gitlab"?      ✅ override explícito (vence sempre)
2. host da URL bate github.com/gitlab.com   ✅ detecção por URL
   ou DEILE_*_HOST declarado?                  (HTTP probe opt-in via DEILE_FORGE_PROBE=1)
3. project_path com 3+ segmentos → GitLab   ✅ heurística de path
   2 segmentos (owner/repo) → GitHub           (compat retroativa)
```

`GitHubForge` opera via `gh`; `GitLabForge` via `glab` + REST v4. O `ForgeRouter` (singleton) cacheia um cliente por `(host, project)`, permitindo **GH e GL na mesma sessão CLI** — basta os dois tokens configurados.

### 🗺️ Vocabulário canônico GitHub ↔ GitLab

| Interno | GitHub | GitLab |
|---|---|---|
| Mudança proposta | **PR** | **MR** |
| Comentário | `comment` | `note` |
| Reviewer | `requested_reviewers[]` | `reviewers[]` |
| Numeração | `pr.number` | `mr.iid` |
| URL | `/<owner>/<repo>/pull/N` | `/<group>/.../<proj>/-/merge_requests/N` |
| Templates de issue | `.github/ISSUE_TEMPLATE/*.md` | `.gitlab/issue_templates/*.md` |
| CLI | `gh` | `glab` |

> ⚠️ **Pipeline ≠ CLI:** o `deile-pipeline` autônomo é **per-repo, per-forge** (uma instância serve um repo). Para GH e GL simultâneos em modo autônomo, rode **duas instâncias** (cada uma com sua `DEILE_FORGE_REPO`). A sessão CLI interativa não tem essa restrição.

**Garantias verificadas:** tokens **nunca** ficam em `/proc/self/environ` — o `wrapper.py` move pra `~/.git-credentials` (mode `0600`) + config do `gh`/`glab` e remove de `os.environ` antes do agente subir; o `secrets_scanner` detecta tokens GitHub (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/`github_pat_`) **e** GitLab (`glpat-`/`gldt-`/`glptt-`/`glsoat-`); back-compat 100% com setups GitHub existentes.

---

## ☸️ Stack Kubernetes

Quem prefere que **o agente não toque o filesystem do host** (input não-confiável, sandbox descartável, ou operação autônoma 24/7) sobe a stack em [`infra/k8s/`](infra/k8s/) — testada em **Rancher Desktop (k3s/containerd)**.

Todos os pods compartilham uma **única imagem** `deile-stack:local` (`imagePullPolicy: Never`); `/app` é **baked no build** (`COPY`, não montado) — **mudança de código só vai ao ar após rebuild + restart**.

### 🐳 Os 6 pods

| Pod | Porta | Papel |
|---|---|---|
| `deilebot` | `:8765` | Bridge de I/O (Discord e outros canais) — vive num repo separado |
| `deile-worker` | `:8766` | Roda **DEILE Python in-process**; alvo de dispatch HTTP do pipeline |
| `claude-worker` | `:8767` | Roda **`claude -p`** em worktrees isolados; OAuth no PVC `claude-worker-home` |
| `deile-pipeline` | `:8768` (status, in-process) | O **monitor de forge** — não recebe dispatch (sem Service de ingestão de tarefas); expõe só o Service read-only `deile-pipeline-status` p/ o painel. No mais, só "chama pra fora" |
| `deile-monitor` | `:8769` | **Supervisor determinístico do cluster** (vigias V1–V8); distinto do pipeline. Expõe um control-plane on-demand (status sem-LLM, ordens, Q&A read-only) consumido pelo `deilebot` |
| `deile-shell` | — | Sandbox `kubectl exec`-only, toolset cheio; prompt vem do humano |

> **`deile-monitor`** roda um tick em duas fases: **Fase A** é uma varredura mecânica **sem LLM** (8 vigias: saúde OAuth, pods em erro, issues órfãs, PRs `auto/*` com tentativa N/3, aguardando-stakeholder, Jobs falhos, saúde do pipeline, coleta de follow-ups); **Fase B** só aciona a persona `monitor` quando candidatos sobrevivem à Fase A. Em regime estável, **não gasta token**. Tick default a cada 30 min. Tem RBAC dedicado (`deile-monitor-sa`).

### 🎛️ Orquestrador: `infra/k8s/deploy.py`

Imprime um plano antes de qualquer ação mutante; `--yes` pula o prompt, `--dry-run` só mostra o plano. **Flag global `-n <ns>`** seleciona o namespace (default `deile`). *(`infra/k8s/run.sh` ainda existe, mas é só um shim que delega ao `deploy.py`.)*

| Objetivo | Comando |
|---|---|
| Menu interativo / lista de verbos | `python3 infra/k8s/deploy.py` / `... help` |
| **Rebuild + restart** (deploy de código) | `python3 infra/k8s/deploy.py k8s build --restart --yes` |
| Provisionar/atualizar a stack (idempotente) | `python3 infra/k8s/deploy.py k8s up` |
| Criar namespace do zero (interativo) | `... k8s create-namespace` / `... k8s setup` |
| Escalar workers | `... k8s scale --worker 2 --claude-worker 1` |
| Pausar / retomar (scale 0/1; preserva dados) | `... k8s stop` / `... k8s start` |
| Status / painel TUI / logs | `... k8s status` / `... k8s panel` / `... k8s logs [bot\|worker\|pipeline\|claude-worker]` |
| Bootstrap / renovar OAuth do claude-worker | `... k8s claude-login [--switch\|--no-interactive]` / `... k8s claude-renew` |
| Clonar repo no `deile-shell` | `... k8s clone <owner/repo>` |
| Listar namespaces DEILE | `... k8s list` |
| **Teardown** (apaga o namespace + dados) | `... k8s down` |

### 🧱 A imagem

Multi-stage sobre **Python 3.11 (slim)**. Instala, em camadas verificáveis: `gh`, **`glab` 1.45.0 (SHA256 conferido)**, `kubectl` v1.31.4, `procps`, `tini` (reaper de zumbis) e o **`claude` CLI pinado em `2.1.158`** (via npm). O DEILE Python e os servidores (`wrapper.py`, `worker_server.py`, `claude_worker_server.py`) são copiados como `0555` (read-only em runtime).

### 🛡️ Defesas aplicadas em todos os pods

`runAsNonRoot uid 10001` · `capabilities drop ALL` · `readOnlyRootFilesystem` · `allowPrivilegeEscalation: false` · `seccompProfile RuntimeDefault` · `automountServiceAccountToken: false` (exceto os pods que precisam renovar OAuth via `kubectl exec`) · PSS `restricted` no namespace · **NetworkPolicy `default-deny-all`** + opt-ins explícitos por porta · **secrets montados como arquivos** em `/run/secrets/<role>/` (nunca via `env:`, então `/proc/<pid>/environ` fica limpo) · `bootstrap_providers()` popa as API keys de `os.environ` após instanciar os providers.

> 🚫 **Honestidade:** a "whitelist de egress" do `claude-worker` (`api.anthropic.com`, `github.com`, `gitlab.com`, granularidade de repo) é **enforçada na aplicação** (`wrapper.py` + ConfigMap `claude-worker-allowed-repos`, fail-closed), **não** em L3/L4 — a NetworkPolicy libera TCP 443 genérico. É um controle de aplicação, não firewall de rede.

### 🌐 Multi-namespace

O DEILE roda **múltiplas stacks lado a lado**, uma por namespace (`deile` é a default; `deile-gl` é o piloto GitLab). Os 6 deployments core não fixam namespace — o `-n <ns>` do `deploy.py` os aplica em qualquer namespace. **Gap conhecido:** alguns manifests auxiliares (NetworkPolicy, claude-worker, certos PVCs/CronJobs) ainda fixam `namespace: deile` — multi-namespace é pleno para o core, parcial para os auxiliares.

### 🖥️ Painel TUI & auditoria de custo

`deploy.py k8s panel` abre um cockpit Rich navegável (não fecha após a escolha): pods (k8s + processos locais), timeline do pipeline, backlog de issues/PRs, feed de atividade e sessões `claude -p` ao vivo (parser incremental de JSONL). Hotkeys: `[1-4]` views · `[t]` auditoria de tokens (suspende e roda `session_tokens_audit.py`) · `[M]` monitor · `[d]` **matriz de dispatch** (editar por etapa: Worker × Model × Timeout × Retries × Cost-cap × Reasoning; `[L]` login claude, `[I]` install, `[s]` scale, `[c]` cleanup, `[p]` max_parallel, `[J]` retenção JSONL) · `[?]` ajuda · `[q]` sair.

---

## 🔭 Observabilidade e eventos

### 📨 Event bus
`deile/events/event_bus.py` — `EventBus` assíncrono com enum `EventType`: sistema (`SYSTEM_STARTED/STOPPED`), persona (`PERSONA_ACTIVATED/...`), tarefas (`TASK_CREATED/STARTED/COMPLETED/FAILED/CANCELLED`), código (`CODE_GENERATED/EXECUTED/TESTED`, `FILE_MODIFIED`) e ferramentas (`TOOL_INVOKED/COMPLETED/FAILED`). Handlers em `deile/events/event_handlers.py`.

### 📈 OpenTelemetry (`deile/observability/`)
Tracer + métricas CNCF, com **fallback no-op** quando o SDK está ausente, `DEILE_OTLP_ENDPOINT` vazio ou `DEILE_OBSERVABILITY_DISABLED=true`. Toda chamada é best-effort — **nunca quebra o turno**.

- **Spans:** `deile.turn` (1 por interação), `deile.tool.<name>` (1 por execução), `deile.llm.call` (1 por chamada de provider). Adapter de dispatch: root span `deile.dispatch` + eventos `dispatch.*` e child spans `git.commit` / `git.push` / `forge.pr_open` / `forge.pr_review`.
- **Métricas:** `deile.tokens.total`, `deile.cost.usd.total`, `deile.tool.duration_ms`, `deile.turn.duration_ms`, `deile.errors.total`.
- **Sem segredos** em atributos — apenas tamanhos, tokens, custo e IDs opacos (redação automática de tokens). `session_id` deliberadamente **não** é label (controle de cardinalidade).
- **Env:** `DEILE_OTLP_ENDPOINT` (vazio = off), `DEILE_OTLP_HEADERS`, `DEILE_OTLP_INSECURE`, `DEILE_OTLP_SERVICE_NAME`, `DEILE_OTLP_SAMPLE_RATIO`, `DEILE_OBSERVABILITY_DISABLED`.

### 🩺 Runtime state por processo (`deile/runtime/`)
Cada processo DEILE publica seu estado vivo em `~/.deile/run/<instance_id>.json` (escrita atômica + cleanup no `atexit`), com heartbeat a cada 2 s. O `current_action` é um enum (`idle`/`starting`/`tool_execution`/`llm_call`/`shutting_down`) e o state file acumula tokens/custo/turns/tool_calls/errors — **sem segredos, prompts ou tool_args**. Um **status server por Unix-socket** (`<id>.sock`, `chmod 0600`) responde `STATUS`/`METRICS`/`FLUSH` em protocolo de linha; um `registry.json` (lock `fcntl.flock`, GC de PIDs mortos) dá visão de frota. No Windows, vira no-op.

### 🔌 Endpoints de control-plane (internos ao cluster)

> Não há servidor HTTP **público** — o agente interativo é puro CLI. Estes endpoints são o control-plane **interno** do cluster, protegidos por Bearer (exceto `/health` e o fluxo OAuth).

**`deile-worker` (`:8766`)** — `GET /v1/health` · `POST /v1/dispatch` · `GET /v1/result/{task_id}` · `GET /v1/progress/{task_id}`

**`claude-worker` (`:8767`)** — `GET /v1/health` · `GET /v1/auth/start` · `GET /v1/auth/status` · `GET /v1/pod-status` · `POST /v1/dispatch` · `GET /v1/progress/{task_id}` · `GET /v1/dispatches/{task_id}/resume-info` · `GET /v1/sessions` · `GET /v1/sessions/{id}/{command,chat,stdout}` · `POST /v1/sessions/{id}/kill` · `DELETE /v1/sessions/{id}/cleanup` · `GET\|POST /v1/cleanup`

**`deile-pipeline` status (`:8768`)** — `GET /v1/health` · `GET /v1/pipeline-status[/backlog\|/recent\|/ledger\|/reaper-preview]` · `POST /v1/pipeline/force-tick`

**`deile-monitor` (`:8769`)** — `GET /v1/health` · `GET /v1/monitor-status` · `POST /v1/command` · `POST /v1/ask` · `GET /v1/ask/{request_id}`

---

## 🔒 Segurança e auditoria

| Componente | Onde | Papel |
|---|---|---|
| 🛡️ Permissões | `deile/security/permissions.py` (`PermissionManager`) | Verifica permissão antes de ação privilegiada |
| 📜 Audit log | `deile/security/audit_logger.py` (`AuditLogger` + `AuditEvent` tipado) | Registro tipado de ações sensíveis |
| 🔍 Scanner de segredos | `deile/security/secrets_scanner.py` | Detecta/redige credenciais (tokens GitHub e GitLab, entre outros) |
| ✅ Aprovação por risco | `deile/orchestration/approval_system.py` (`ApprovalSystem`) | Gate de ações de alto risco (ex.: DM, menção de cargo) |

Toda tool declara um `SecurityLevel` (`deile/tools/base.py`); o `bash_tool` tem catálogo de risco próprio (`assess_risk` em `deile/tools/_shell_security.py`, classificando `safe`/`moderate`/`dangerous` sobre o mesmo enum `SecurityLevel`). O gate de aprovação interativa das tools de **mensageria** (DM / menção de cargo) pode ser auto-dispensado para um operador confiável via `approval.auto: true` em `~/.deile/settings.json` (`bot_approval_auto`).

---

## 💾 Persistência

Os stores relacionais são **SQLite auto-criados em runtime** (o ledger de custo é JSONL append-only) — **não há script SQL versionado**;

| Store | Arquivo | Dono |
|---|---|---|
| Tarefas & listas | `./.deile/db/tasks.db` (legacy: `./deile_tasks.db`) | `deile/orchestration/sqlite_task_manager.py` |
| Telemetria de uso/custo | `~/.deile/db/usage.db` | `deile/storage/usage_repository.py` |
| Memória episódica | `episodes.db` | `deile/memory/episodic_memory.py` (`aiosqlite`) |
| Cron genérico | `data/cron.db` | `deile/cron/store.py` (`CronStore`) |
| Ledger de custo durável | `~/.claude/cost-ledger.jsonl` | `claude-worker` (JSONL append-only, dedup por `session_id`) |

```
.deile/db/tasks.db          ── task_lists ──1:N── tasks   (legacy: ./deile_tasks.db)
~/.deile/db/usage.db        ── usage_records  (tokens, custo USD, provider/model)
episodes.db                 ── episódios da sessão
data/cron.db                ── cron entries  (CronStore + CronRunner)
~/.claude/cost-ledger.jsonl ── custo por sessão claude -p (JSONL; colhido antes de podar o transcript)
```

> 💰 **Ledger de custo (issue #445):** os transcripts do `claude -p` acoplam continuidade `--resume` (volumoso, efêmero) e auditoria de custo (minúsculo, permanente). No cleanup, o custo de cada sessão órfã é **colhido para o ledger durável antes** de o transcript ser podado — custo histórico permanente em escala de KB, transcripts podam livremente. A ferramenta `infra/k8s/session_tokens_audit.py` lê o ledger (sessões podadas) + o JSONL vivo (recentes) com custo idêntico (mesma tabela de preços em `jsonl_cost.py`).

---

## 🏗️ Arquitetura e camadas

Arquitetura hexagonal por camadas, com **registries** para artefatos extensíveis (tools, commands, parsers, personas, skills) — adicionar um artefato **não** exige tocar no núcleo (Open/Closed). I/O é **async-first**.

| Camada | Pacote | Responsabilidade |
|---|---|---|
| 🧩 Núcleo | `deile/core/` | Lógica central, integração com modelos, tool-loop |
| 🤖 Modelos LLM | `deile/core/models/` | Provedores, roteamento, streaming, reasoning |
| 📨 Eventos | `deile/events/` | Event bus assíncrono e handlers |
| 🛠️ Ferramentas | `deile/tools/` | Registry e implementação de tools (+ mensageria) |
| 📜 Comandos | `deile/commands/` | Slash commands e despacho |
| 🧱 Parsers | `deile/parsers/` | Parsing de entrada (arquivos, diffs, refs, comandos) |
| 🎭 Personas | `deile/personas/` | Instruções MD/YAML e manager |
| 🧩 Skills | `deile/skills/` | Discovery, registry, hot-reload |
| 🧠 Memória | `deile/memory/` | Quatro camadas |
| 🔒 Segurança | `deile/security/` | Permissões, audit, secrets scanner |
| 💾 Armazenamento | `deile/storage/` | Logger, usage/custo, budget guard, embeddings |
| 🎯 Orquestração | `deile/orchestration/` | Planos, workflows, tarefas, aprovações, pipeline, forge |
| 🩺 Runtime | `deile/runtime/` | State file por processo, status server, registry de frota |
| 🔭 Observabilidade | `deile/observability/` | Tracer/métricas OTLP, adapter de dispatch |
| ⏰ Cron | `deile/cron/` | `CronStore` (SQLite) + `CronRunner` |
| 🖥️ UI | `deile/ui/` | Renderização, streaming, painel, sub-agent panel |
| 🧬 Evolução | `deile/evolution/` | Auto-learning **experimental** |
| 🔌 Plugins | `deile/plugins/` | Plugin manager, hot-reload (sem sandbox — ver Limitações) |
| ⚙️ Infra | `deile/infrastructure/` | Adapters externos (SDKs, drivers) |
| 🛠️ Configuração | `deile/config/` | `Settings` singleton, YAML, profiles |
| 🔗 Integrações | `deile/integrations/` | Cliente HTTP do control-plane (flecha reversa agente → bot) |
| 🪵 Log mgmt | `deile/log_mgmt/` | Análise, rotação e dispatch de logs |
| 🧰 Preferências | `deile/preferences/` | Backing store das tools `remember`/`list`/`forget_preference` |

**Fluxo de uma mensagem do usuário:**

1. `_DeileCLI` (`deile/cli.py`) lê a entrada e encaminha ao `DeileAgent` — `deile.py` é só o launcher que prepara o venv e delega a `deile.cli.main()`.
2. Parsers extraem menções a arquivos/comandos.
3. Slash command → despacha via `CommandRegistry`; senão segue para o modelo.
4. `ModelRouter`/`TierRouter` escolhe provider/modelo conforme tier e estratégia.
5. O provider emite `UnifiedStreamEvent` (`TEXT_DELTA`, `TOOL_USE_START`/`TOOL_USE_END`, …).
6. Num evento de tool use, o `ToolLoopExecutor` executa a tool e devolve o resultado à conversa (até `max_tool_iterations`, default **100**, ajustável via `DEILE_MAX_TOOL_ITERATIONS`).
7. O `StreamingRenderer` acompanha eventos e atualiza o terminal ao vivo (`rich.live.Live`).
8. O `EventBus` publica eventos (telemetria, persona, tool).

---

## ⚙️ Configuração

### 🔑 Variáveis de ambiente
A referência canônica é o [`.env.example`](.env.example) (~450 linhas, seções comentadas): chaves de LLM, forges, bot, workers, pipeline (dispatch/model/reasoning por etapa), subagents, cron, OpenTelemetry, status server, etc. **A leitura de config deve passar por `get_settings()`** (`deile/config/settings.py`) — é um princípio arquitetural; alguns módulos do pipeline ainda leem `os.environ` direto (gap conhecido).

### 🗂️ Settings em camadas
`~/.deile/settings.json` é resolvido em três camadas com precedência **project > user > profile**: profile (preset) → user (`~/.deile/settings.json`) → project (`<cwd>/.deile/settings.json`, com opt-in via `trust.project_layer_dirs`). Ajuste por `/settings set <chave> <valor>` no CLI.

### 📄 Arquivos de configuração
- `deile/config/` (código + YAML): `model_providers.yaml`, `intent_patterns.yaml`, `persona_config.yaml`, `commands.yaml`, `skills.yaml`, `system_config.yaml`, `api_config.yaml` + `profiles/`.
- `config/` (raiz, runtime): `settings.json`, `deilebot.yaml`, `pipeline_schedule_*.yaml`, `persona_config.yaml`.

> Há **dois** diretórios `config/` (raiz e `deile/`). Não confundir.

---

## 📋 Requisitos do sistema

### 🐍 Linguagem e plataforma
- Python **≥ 3.9** · Linux/macOS (Windows experimental) · entrada `python3 deile.py`.

### 📦 Dependências de produção (`requirements.txt`)
- 🤖 LLM SDKs: `anthropic`, `openai`, `google-genai`
- 🖥️ UI/CLI: `rich`, `prompt_toolkit`, `colorama`, `Pygments`
- ⚡ Async I/O: `aiofiles`, `aiosqlite`
- ✅ Validação/config: `pydantic`, `pydantic-settings`, `PyYAML`, `python-dotenv`
- 🌐 Rede/sistema: `requests`, `httplib2`, `psutil`, `chardet`, `GitPython`, `tenacity`
- 📚 Outras: `numpy`, `pathspec`, `watchdog`

### 🧩 Extras opcionais (`pyproject.toml`)
| Extra | Para quê |
|---|---|
| `bot` | `deilebot` (git URL — repo separado `elimarcavalli/deilebot`) |
| `otel` | OpenTelemetry (api/sdk/exporter OTLP gRPC) |
| `ui` | `textual` |
| `scheduler` · `webhook` · `test` | APScheduler · FastAPI/uvicorn · pytest & cia. |

### 🧪 Dependências de desenvolvimento (`dev-requirements.txt`)
Testes (`pytest`, `pytest-asyncio`, `pytest-mock`, `pytest-cov`, `pytest-xdist`, `pytest-benchmark`), qualidade (`coverage`, `isort`, `radon`, `black`) e segurança (`safety`, `bandit`). *(o `pytest-timeout` usado pelo `pytest.ini` vem do extra `[test]` do `pyproject.toml`.)*

---

## 🧪 Testes

Configuração em `pytest.ini`:
- `testpaths = deile/tests`; coleta `test_*.py` e `*_test.py`.
- `asyncio_mode = auto` (testes async dispensam `@pytest.mark.asyncio`).
- `--strict-markers` + `--strict-config` — markers novos precisam ser registrados antes de usar.
- Timeout de 300 s por teste (`pytest-timeout`, modo thread).
- Markers registrados: `unit`, `integration`, `security`, `orchestration`, `bash`, `ui`, `slow`, `perf`, `e2e`, `e2e_discord_live`, `e2e_telegram_live`, `e2e_whatsapp_live`, `e2e_meta_live`, `manual`, `llm`.

```sh
python3 -m pytest deile/tests/ -q          # suíte completa (resumo)
python3 -m pytest deile/tests/path/test_x.py -v   # um arquivo
```

> ℹ️ O `deile/tests/` mistura **pytest tests** (`test_*.py`, coletados) e **scripts standalone** (`*_test.py`, `smoke_test_*.py`) rodados manualmente. Testes que consomem token real ficam em `deile/tests/might/` (opt-in, fora da suíte padrão). **Não há `--cov-fail-under` ativo no `pytest.ini`** — a cobertura é medida sob demanda, não como gate bloqueante na config atual.

---

## 🚦 Operação e troubleshooting

| Sintoma | Causa provável / ação |
|---|---|
| `bootstrap_providers` não acha providers | Nenhuma API key definida — edite o `.env` |
| Erro `--strict-markers` no pytest | Marker novo não registrado — registre em `pytest.ini` |
| Tool "não encontrada" | Garanta que está em `DEFAULT_TOOL_PACKAGES` ou registrada via `register_tool` |
| Mudança de código no cluster sem efeito | `/app` é baked — rode `deploy.py k8s build --restart` |
| Pod em erro de auth / `WORKER_AUTH_EXPIRED` | OAuth do claude-worker expirou — `deploy.py k8s claude-renew` |
| Erro de banco durante uma tarefa | O agente **para e reporta** — scripts SQL -> humano |

---

## 💪 Pontos fortes / diferenciais técnicos

- 🔁 **Tool-loop único e provider-agnóstico** — elimina duplicação por provider.
- 🌐 **Fallback automático entre 4 provedores** com circuit breaker por provider.
- 🧭 **Forge-agnóstico** — o mesmo pipeline opera GitHub e GitLab idênticos.
- 🤖 **Loop autônomo DEILE-a-DEILE OU DEILE-a-Claude** — escolha o worker por etapa.
- 🧠 **Portão de refinamento** — critica o escopo da issue antes de escrever código.
- 💵 **Telemetria de custo persistente** + ledger durável que sobrevive à poda de transcripts.
- 🔭 **Observabilidade enterprise** (OTLP) + runtime state por processo + painel TUI.
- 🎭 **Personas e skills MD-driven** — editáveis sem tocar no Python; hot-reload.
- 🔒 **Auditoria tipada + scanner de segredos + aprovação por risco** nativos.
- 🐳 **Hardening de container** — non-root, drop ALL caps, RO rootfs, secrets como arquivos, NetworkPolicy default-deny.

---

## ⚠️ Limitações conhecidas

- O **agente interativo** não expõe servidor HTTP público — é puro CLI. Os endpoints HTTP existem só como **control-plane interno** do cluster (workers/pipeline-status, com Bearer).
- IDs de modelo no YAML são literais — precisam ser válidos no SDK do provider.
- Tool-loop tem teto (`max_tool_iterations`, default **100**, configurável).
- Módulo de **evolução** é experimental.
- **Plugins não têm sandbox real** — carregue apenas plugins auditados.
- `/sandbox` é um toggle **informativo** — não fornece isolamento (use a stack K8s para isolamento de verdade).
- **Multi-namespace é pleno para os 6 deployments core, parcial para manifests auxiliares** (alguns ainda fixam `namespace: deile`).
- A whitelist de egress do `claude-worker` é controle de **aplicação** (`wrapper.py`), não de rede (L3/L4) — gap documentado; FU prioritária é um sidecar de proxy de credenciais.

---

## 🤝 Como contribuir

Contribuições são bem-vindas — corrija typos, crie tools, comandos, parsers, personas, skills ou providers.

### ⚡ Fluxo recomendado com `gh`

| # | Etapa | Comandos `gh` principais |
|---|---|---|
| 1 | Explorar issues | `gh issue list` · `gh issue view <id>` |
| 2 | Criar issue (siga o template apropriado) | `gh issue create --title "..." --body "..."` |
| 3 | Branch vinculada | `gh issue develop <id> --checkout` |
| 4 | Implementar + testar | `python3 -m pytest deile/tests/ -q` · `ruff check deile/` · `isort --check-only deile/` |
| 5 | Commitar (Conventional Commits) | `git add -p` · `git commit -m "feat(scope): ..."` |
| 6 | Abrir PR | `gh pr create --fill` |
| 7 | Revisar / merge | `gh pr review <id> --approve` · `gh pr merge <id> --squash` |

> ⚠️ Edições de **label** devem usar o endpoint REST (`gh api .../labels`), **não** `gh issue edit --add-label` — este último dispara uma query GraphQL que exige o escopo `read:org` (que o token do pipeline não tem).

### 🧩 Onde adicionar extensões

| O quê | Onde | Observação |
|---|---|---|
| 🛠️ Tool | `deile/tools/<nome>.py` | Adicione ao `DEFAULT_TOOL_PACKAGES` ou registre via `register_tool` |
| ⌨️ Slash command | `deile/commands/builtin/<nome>.py` | Registre no `CommandRegistry`; ganha flag CLI automática |
| 🗂️ Parser | `deile/parsers/<nome>.py` | Siga o contrato base + `priority` |
| 🎭 Persona | `personas/instructions/` + `personas/library/` | MD/YAML, sem Python |
| 🧩 Skill | `~/.deile/skills/`, `.deile/skills/` ou `deile/skills/library/` | MD com frontmatter; hot-reload |
| 🧠 Provider | `deile/core/models/` | Registre em `bootstrap.py` + YAML dos modelos |

### ✅ Checklist antes do PR

| ✔️ | Item |
|---|---|
| [ ] | Suíte completa verde (`python3 -m pytest deile/tests/ -q`) |
| [ ] | `ruff check deile/` e `isort --check-only deile/` sem pendências |
| [ ] | Commits no padrão Conventional Commits |
| [ ] | README/docs atualizados se necessário |
| [ ] | Nenhum arquivo sensível ou residual commitado |

Para detalhes de arquitetura, abra a base de conhecimento em [`docs/system_design/00-VISAO-GERAL.md`](docs/system_design/00-VISAO-GERAL.md) (índice dos 14 pilares + registro de decisões).

---

## 📄 Licença

Projeto licenciado sob [**MIT License**](LICENSE).

## 👤 Construtores

| Nome | GitHub | Site | E-mail |
|---|---|---|---|
| Elimar Cavalli | [@elimarcavalli](https://github.com/elimarcavalli) | [elimar.dev](https://elimar.dev) | [elimar.dev@gmail.com](mailto:elimar.dev@gmail.com) |
| DEILE-One | [@DEILE-One](https://github.com/deile-one) | [elimarcavalli/deile](https://github.com/elimarcavalli/deile) | [deile-one@elimar.dev](mailto:deile-one@elimar.dev) |

---

**DEILE 5.1.0** — `python3 deile.py`

> O monitor do pipeline faz polling do forge a cada **120 segundos** por padrão (ajustável via `DEILE_PIPELINE_POLL_INTERVAL`).
