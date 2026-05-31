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

O DEILE pode operar **autonomamente** sobre o repositório GitHub: quando uma issue recebe o label `~workflow:nova` (ou o `deile-one` é atribuído/mencionado), o pipeline a implementa e abre uma PR sem intervenção humana.

### Fluxo completo

```
Discord: "/ideia implementar feature X"
  → DEILE cria issue com ~workflow:nova
  → PipelineMonitor tick
    → Stage 1: DEILE revisa a issue → ~workflow:revisada
    → Stage 2: implementa (claude -p OU despacho ao deile-worker, conforme
               dispatch_mode) na branch auto/issue-N → PR aberta → ~workflow:em_pr
               (retoma sozinho se parar no meio — resume)
    → Stage 3: revisa a PR sob a persona reviewer (quality-gate SOLID/SRP/
               segurança) → merge → ~review:concluida
  → Discord: DM com URL da PR
```

> **Atribuição/menção:** se você atribui o `deile-one` a uma issue, ela entra no fluxo acima; se o marca como **reviewer** de uma PR, ele **revisa e devolve ao autor sem mergear**; se o **menciona** num comentário, ele faz o que foi pedido. Ver `docs/system_design/DECISOES.md` #30–#33.

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

## 🌳 Forge — GitHub & GitLab (issue #297)

O DEILE é **forge-agnóstico**. O mesmo agente, o mesmo pipeline autônomo e os mesmos briefs operam **idênticos** em repos **GitHub** (cloud, GHES) e **GitLab** (cloud, self-hosted). O CLI usado por baixo (`gh` ou `glab`) é detalhe de transporte — você só decide para **onde** o trabalho vai.

### 🎛️ Como decidir qual forge usar

```
            ┌─────────────────────────────────────┐
            │ Onde está o repositório do projeto? │
            └──────────┬──────────────────────────┘
                       │
        ┌──────────────┴───────────────┐
        │                              │
   github.com                     gitlab.com
   ou GHES                        ou self-hosted
        │                              │
        ▼                              ▼
   DEILE_FORGE_KIND=github        DEILE_FORGE_KIND=gitlab
   GITHUB_TOKEN=ghp_…             GITLAB_TOKEN=glpat-…
   (DEILE_GITHUB_HOST=…)          (DEILE_GITLAB_HOST=…)
        │                              │
        └──────────────┬───────────────┘
                       ▼
              ✅ Funciona idêntico
       (mesma máquina de estados, mesmos briefs)
```

| Cenário | `DEILE_FORGE_KIND` | Hosts | Tokens necessários |
|---|---|---|---|
| 🐙 **GitHub cloud** (padrão) | `auto` ou `github` | — (default `github.com`) | `GITHUB_TOKEN` |
| 🦊 **GitLab cloud** | `gitlab` | — (default `gitlab.com`) | `GITLAB_TOKEN` |
| 🏢 **GitHub Enterprise Server** | `github` | `DEILE_GITHUB_HOST=ghe.empresa.com` | `GITHUB_TOKEN` (do seu GHES) |
| 🏠 **GitLab self-hosted** | `gitlab` | `DEILE_GITLAB_HOST=gitlab.empresa.com` | `GITLAB_TOKEN` (do seu GL) |
| 🌐 **Multi-forge na sessão CLI** | `auto` | ambos hosts declarados | **AMBOS** (`GITHUB_TOKEN` + `GITLAB_TOKEN`) |

### 🔑 Onde obter o token e quais escopos

#### 🐙 GitHub (cloud ou GHES)

1. Acesse: **https://github.com/settings/tokens** (em GHES: `https://<seu-ghes>/settings/tokens`)
2. Clique em **"Generate new token (classic)"**
3. Marque os escopos:

| Escopo | Para que serve |
|---|---|
| ✅ `repo` (todo) | Ler/escrever issues, PRs, labels, commits |
| ✅ `workflow` | Pipeline labela PRs e gerencia branches `auto/issue-N` |
| ⬜ `read:org` | **NÃO marque** — o pipeline foi desenhado pra funcionar sem isso |

4. Copie o token (formato `ghp_…`). **Você só vê uma vez.**

#### 🦊 GitLab (cloud ou self-hosted)

1. Acesse: **https://gitlab.com/-/user_settings/personal_access_tokens** (self-hosted: `https://<seu-gitlab>/-/user_settings/personal_access_tokens`)
2. Clique em **"Add new token"**, dê um nome (ex.: `deile-pipeline`) e data de expiração
3. Marque os escopos:

| Escopo | Para que serve |
|---|---|
| ✅ `api` | Acesso completo à API (issues, MRs, notes, merge, labels) |
| ✅ `read_repository` | Clone via HTTPS + leitura de arquivos (templates `.gitlab/issue_templates/`) |
| ✅ `write_repository` | Push de commits + branches `auto/issue-N` |
| ⬜ `read_user` / `read_registry` | Não necessários |

4. Copie o token (formato `glpat-…`). **Você só vê uma vez.**

### 🔐 Autenticação git: HTTPS+token vs SSH vs GPG

Para o DEILE em produção (containerizado), a recomendação é **clara**:

| Opção | Recomendação | Por quê |
|---|---|---|
| 🟢 **HTTPS + Personal Access Token** | ✅ **USE ISTO** | É o que o `wrapper.py` configura automaticamente; o token vai pro `~/.git-credentials` em mode `0600`; sem prompt interativo; mesmo padrão pra os dois forges; rotação simples (substitui no Secret). |
| 🟡 **SSH key** | ❌ Evite | Exige montar `~/.ssh/id_*` como Secret separado, `known_hosts` pré-populado, agent socket no Pod — toda essa cerimônia só pra alcançar o mesmo resultado do HTTPS+token. |
| 🟠 **GPG signing** | ➖ Opcional | É **ortogonal** à autenticação — GPG só assina o conteúdo do commit, não autentica `git push`. Se você quer commits assinados, pode adicionar `signingkey` no `~/.gitconfig` separadamente, mas não substitui o token. Default do DEILE é não assinar. |

> 💡 **TL;DR**: gere um PAT no forge, joga no K8s Secret, esquece. SSH só faz sentido se o seu time TEM uma política que proíbe HTTPS — o que é raro hoje em dia.

### 🪛 Configuração — escolha sua via

#### 🏠 Local (laptop / dev)

```bash
# 1. Adicione ao seu .env (gitignored):
DEILE_FORGE_KIND=gitlab                    # ou "github" / "auto"
DEILE_FORGE_REPO=group/sub/projeto         # owner/repo no GH, group/.../proj no GL
DEILE_GITLAB_HOST=gitlab.com               # ou seu self-hosted
GITLAB_TOKEN=glpat-XXXXXXXXXXXXXXXXXXXX    # token gerado acima

# Para multi-forge na sessão CLI, defina TAMBÉM:
GITHUB_TOKEN=ghp_XXXXXXXXXXXXXXXXXXXXXX

# 2. Rode normalmente
python3 deile.py
```

#### ☸️ Kubernetes (deile-pipeline / deile-worker / deile-shell)

```bash
# 1. Patch no Secret existente (NÃO recrie o Secret inteiro pra preservar o GITHUB_TOKEN/LLM keys):
GL_PAT='glpat-XXXXXXXXXXXXXXXXXXXX'
kubectl -n deile patch secret deile-secrets \
  -p "{\"stringData\":{\"GITLAB_TOKEN\":\"${GL_PAT}\"}}"

# 2. Defina as env vars no Deployment do pipeline:
kubectl -n deile set env deploy/deile-pipeline \
  DEILE_FORGE_KIND=gitlab \
  DEILE_FORGE_REPO=group/sub/projeto \
  DEILE_GITLAB_HOST=gitlab.com   # (omita se for gitlab.com)

# 3. Reconstrua e reinicie (deploy.py faz tudo idempotente):
python3 infra/k8s/deploy.py k8s build --restart --yes
```

### 🔄 Como funciona por dentro (decisão run-time)

```
┌──────────────────────────────────────────────────────────────────┐
│                  Pipeline tick (ou agente CLI)                  │
└──────────────────────────────┬───────────────────────────────────┘
                               ▼
            ┌──────────────────────────────────┐
            │  build_forge(project_path, env)  │
            └──────────────┬───────────────────┘
                           ▼
       ┌───────────────────────────────────────────┐
       │  detect_forge_kind() — 3 camadas:         │
       │  1. DEILE_FORGE_KIND="github"|"gitlab"?   │ ✅ Override explícito
       │  2. URL host bate "github.com"/"gitlab.com"│ ✅ Detecção por URL
       │     ou DEILE_*_HOST declarado?            │
       │  3. project_path 3+ segmentos → GitLab    │ ✅ Heurística de path
       │     2 segmentos → GitHub (compat)         │
       └──────────────┬────────────────────────────┘
                      ▼
        ┌─────────────────────────────┐
        │  Resolve ForgeClient:       │
        │  github → GitHubForge(gh)   │
        │  gitlab → GitLabForge(glab) │
        └─────────────┬───────────────┘
                      ▼
       ┌─────────────────────────────────┐
       │ Briefs forge-aware:             │
       │   {forge_create_pr_cmd}         │ → "gh pr create …" OU "glab mr create …"
       │   {pr_url_pattern}              │ → "github.com/…/pull/N" OU "gitlab.com/…/-/merge_requests/N"
       │   {merge_cmd}                   │ → "gh api PUT …" OU "glab api PUT …"
       └─────────────────────────────────┘
```

### 🗺️ Mapeamento GitHub ↔ GitLab (vocabulário canônico)

| Conceito interno | GitHub | GitLab |
|---|---|---|
| Mudança proposta | **PR** (Pull Request) | **MR** (Merge Request) |
| Comentário | `comment` | `note` |
| Thread de review | review comment | discussion |
| Reviewer | `requested_reviewers[]` | `reviewers[]` |
| Numeração | `pr.number` | `mr.iid` (interno ao projeto) |
| URL pública | `/<owner>/<repo>/pull/N` | `/<group>/.../<proj>/-/merge_requests/N` |
| Templates de issue | `.github/ISSUE_TEMPLATE/*.md` | `.gitlab/issue_templates/*.md` |
| API base | `api.github.com` (cloud) ou `<host>/api/v3` (GHES) | `<host>/api/v4` |
| CLI | `gh` | `glab` |
| Cross-ref de PR/MR | `#N` (mesmo namespace de issue) | `!N` (namespace separado) |

### 🧩 Multi-forge na sessão CLI (modo dual)

Numa mesma sessão `python3 deile.py` ou `deile-shell`, você pode trabalhar com repos GitHub **e** GitLab. O `ForgeRouter` cacheia um cliente por `(host, project)` — você só precisa que **os dois tokens** estejam configurados:

```bash
export GITHUB_TOKEN=ghp_...
export GITLAB_TOKEN=glpat-...
# DEILE_FORGE_KIND fica "auto" (default) — cada URL escolhe seu adapter
```

> ⚠️ **Pipeline ≠ CLI**: o `deile-pipeline` (autônomo) é **per-repo, per-forge** — uma instância serve UM repo. Para operar GH e GL simultaneamente em modo autônomo, rode **duas instâncias** do pipeline (Decisão #18 — `MonitorIdentity` + shard), cada uma com sua `DEILE_FORGE_REPO`.

### ✅ Garantias

- 🔒 Tokens **nunca** ficam em `/proc/self/environ` — `wrapper.py` move pra `~/.git-credentials` (mode `0600`) + `~/.config/gh/hosts.yml` + `~/.config/glab-cli/config.yml` e remove de `os.environ` antes do agente subir.
- 🛡️ `secrets_scanner` detecta vazamento de tokens GitHub (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`) **e** GitLab (`glpat-`, `gldt-`, `glptt-`, `glsoat-`).
- 🔁 **Backwards-compat 100%**: se você já rodava com GitHub, **nada muda** — `DEILE_PIPELINE_REPO` continua aceito como alias, `monitor.github` continua funcionando, `_setup_gh_auth` continua existindo. O `glab` fica dormente até você definir `DEILE_FORGE_KIND=gitlab`.

> 🚫 **Honestidade**: rate-limit sleep ativo e HTTP probe opt-in estão documentados mas **não implementados** nesta release — o cliente lê e propaga erros de rate limit do CLI, mas não dorme até reset automaticamente. Se o piloto GitLab esbarrar no limite (`gitlab.com`: 600 req/min/usuário), abriremos issue derivada. Mitigação imediata: `DEILE_PIPELINE_POLL_INTERVAL` mais conservador.

---

## 🧩 Sub-DEILEs paralelos (decomposição em sessão CLI)

Durante uma conversa interativa, o DEILE pode identificar autonomamente sub-tarefas **independentes e substanciais** dentro do seu pedido e dispará-las em paralelo — cada uma rodando num sub-DEILE com **sessão limpa** (contexto/histórico próprios). Você vê o progresso ao vivo num painel multipanel discreto.

### Quando usar

✅ "Refator módulo A E módulo B (não-acoplados)"  ·  "Gere testes pra X, Y e Z"  ·  "Escreva doc do módulo A e implemente feature em B"

❌ Tarefas sequenciais ("primeiro X, depois Y") · micro-tarefas (<30s cada) · mesmo arquivo

### Como funciona

O LLM principal chama a tool `dispatch_parallel_subagents` com 2-5 sub-tarefas (description + prompt auto-contido + persona/model opcionais). O `SubAgentOrchestrator` dispara em paralelo (semaphore de `subagent_max_parallel`, default 3), respeitando budget global. Cada sub-DEILE roda no chat-with-tools loop direto (skip de autonomous/workflow paths) — então o painel mostra `⚙ bash_execute(...)`, `✓ write_file: 412 bytes`, `✎ texto-em-curso` em tempo real.

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

Teclas: `1`-`9` foca uma frente (ficha completa com prompt, persona, model, task_id, files_touched + tail do stream); `ESC` volta ou fecha. Falha de uma frente NÃO cancela siblings. Stdout dos sub-DEILEs é capturado (não polui o terminal). O resumo final é gravado no histórico — `/resume` reconstrói o painel.

### Runners pluggable

- **`LocalSubAgentRunner`** (default) — in-process via `asyncio.create_task` + `wait FIRST_COMPLETED`. Funciona em qualquer ambiente (laptop, CI, pod), sem depender de infra.
- **`WorkerSubAgentRunner`** — delega ao `deile-worker` via HTTP (`wait=False` + polling de `GET /v1/progress/{task_id}`). Isolamento real por processo separado. Habilite com `DEILE_SUBAGENT_RUNNER=worker`.

Ambos herdam de `_BaseRunner` (template-method): o ciclo de vida (`running → STARTED → cancel/exception handling → mark_*`) é compartilhado.

### Settings

```bash
DEILE_SUBAGENT_RUNNER=local|worker          # default: local
DEILE_SUBAGENT_MAX_PARALLEL=3               # teto de concorrência por chamada
DEILE_SUBAGENT_BUDGET_S=600                 # teto global de tempo (10min)
DEILE_SUBAGENT_POLL_INTERVAL_S=0.8          # polling do WorkerSubAgentRunner
```

Detalhes técnicos completos: Decisão #34 em [`docs/system_design/DECISOES.md`](docs/system_design/DECISOES.md). Issue: [#257](https://github.com/elimarcavalli/deile/issues/257). Demo end-to-end: [`test-your-might/issue-257-demo/`](test-your-might/issue-257-demo/).

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
| 🧩 SUB-DEILES PARALELOS | Decompõe pedidos complexos em N sub-DEILEs em paralelo (sessões limpas), com painel multipanel ao vivo + foco por tecla — issue #257. |
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

### 🐳 Rodar em container (Kubernetes / Rancher Desktop)

Para quem prefere que **o agente não toque o filesystem do host** (ex.:
o bot Discord que recebe input não-confiável, ou um sandbox descartável
pra experimentos), há uma stack completa em [`infra/k8s/`](infra/k8s/)
que sobe DEILE e o daemon deilebot em Pods isolados num k3s local
(Rancher Desktop).

Caminho feliz, do zero ao primeiro DM em ~10 min:

```bash
brew install --cask rancher    # 1) instala k3s + containerd local
open -a "Rancher Desktop"      #    escolha container engine = containerd

# 2) edite .env do repo: pelo menos uma chave LLM + DEILE_BOT_DISCORD_TOKEN
#    (mesmo .env do modo local — nada novo)

bash infra/k8s/run.sh build    # 3) builda a imagem (~5–10 min na 1ª vez)
bash infra/k8s/run.sh up       # 4) namespace + NetworkPolicies + Secrets + bot
bash infra/k8s/run.sh test     # 5) one-shot deile → DM no Discord (prova de E2E)
```

Três modos de invocar DEILE no container, todos com host inalcançável:

```bash
# A) One-shot pré-programado (Job; prompt fixo no manifest)
bash infra/k8s/run.sh test

# B) Sandbox interativo, toolset cheio (bash, file ops, python_execute…)
kubectl -n deile exec deploy/deile-shell -- python3 /app/wrapper.py deile "explore /home/deile"
kubectl -n deile exec -it deploy/deile-shell -- python3 /app/wrapper.py deile   # REPL

# C) Host (sem container) — pra mexer em código do SEU projeto
python3 deile.py "..."
```

Defesas aplicadas em todos os modos containerizados: `runAsNonRoot uid 10001`,
`capabilities drop ALL`, `readOnlyRootFilesystem`, `seccompProfile RuntimeDefault`,
`automountServiceAccountToken: false`, PSS `restricted` no namespace,
NetworkPolicy `default-deny-all` + `except: 192.168.0.0/16` no egress 443 (Mac
inalcançável via `host.lima.internal`/`192.168.5.x` — REJECT em 1 ms), secrets
montados como **arquivos** em `/run/secrets/<role>/` (nunca via `env:`, portanto
`/proc/<pid>/environ` fica limpo no `execve`), e `bootstrap_providers()`
monkey-patched para popar API keys de `os.environ` após instanciar providers
(subprocesses herdam env limpo).

Tutorial passo-a-passo, troubleshooting, modelo de ameaça e operação:
[`infra/k8s/README.md`](infra/k8s/README.md). Design e decisões:
[`docs/system_design/14-CONTAINERIZACAO.md`](docs/system_design/14-CONTAINERIZACAO.md).

---

## ✨ Funcionalidades reais implementadas


| CHAVE            | Descrição                     | Fonte                                                                             |
| ---------------- | ----------------------------- | --------------------------------------------------------------------------------- |
| 🌐 ROTEAMENTO    | Entre 4 LLM providers         | `deile/core/models/router.py`, `tier_router.py`                                   |
| 🔄 STREAMING     | Unificado de eventos          | `deile/core/models/stream_events.py`                                              |
| 🖼️ RENDERIZAÇÃO | Incremental de Markdown       | `deile/ui/streaming_renderer.py`                                                  |
| 🔁 LOOP          | Iterativo de function calling | `deile/core/tool_loop_executor.py`                                                |
| 🧩 SUB-DEILES    | Paralelos em sessão CLI       | `deile/orchestration/subagents/`, `deile/tools/dispatch_parallel_subagents.py`, `deile/ui/subagent_panel.py` |
| 📨 BARRAMENTO    | Assíncrono de eventos         | `deile/events/event_bus.py`                                                       |
| 🛠️ REGISTRO     | Extensível de ferramentas     | `deile/tools/registry.py`                                                         |
| 📜 COMANDOS      | Slash registráveis            | `deile/commands/registry.py`, `commands/builtin/`                                 |
| 🎭 PERSONAS      | Dinâmicas via YAML+Markdown   | `deile/personas/library/`, `deile/personas/instructions/`                         |
| 🧩 SKILLS        | MD-driven, hot-reload, 4 triggers, `invoke_skill` tool | `deile/skills/`, `deile/tools/skill_tools.py`              |
| 🧠 MEMÓRIA       | Quatro camadas                | `deile/memory/*.py`                                                               |
| 🔒 PERMISSÕES    | Auditoria e scan de segredos  | `deile/security/*.py`                                                             |
| 💾 PERSISTÊNCIA  | SQLite (tasks + uso)          | `deile/orchestration/sqlite_task_manager.py`, `deile/storage/usage_repository.py` |
| 🔌 PLUGINS       | Hot-reload e marketplace      | `deile/plugins/*.py`                                                              |
| 🧬 AUTO-EVOLUÇÃO | Módulo experimental           | `deile/evolution/*.py`                                                            |


---

## 🏗️ Arquitetura e camadas

DEILE segue arquitetura por camadas, com registries para artefatos extensíveis (tools, commands, parsers, personas, skills).


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
| Anthropic  | `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5`      |
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
| `list_skills`          | skill_tools.py        | Catálogo machine-readable de todas as skills carregadas |
| `invoke_skill`         | skill_tools.py        | Carrega o body de uma skill por nome (LLM puxa sob demanda) |

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
/skills           # list/add/remove paths de skills (ver seção 🧩)
/tools            # Listar ferramentas disponíveis
/stop             # Cancelar operação corrente
/approve          # Aprovar etapa pendente
/patch-apply      # Aplicar patch gerado
/patch-generate   # Gerar patch das mudanças
/sandbox          # Status do toggle de sandbox (informativo)
/welcome          # Tela de boas-vindas
```

> Além dos built-ins acima, **toda skill carregada vira `/<name>`** automaticamente (exceto as bundled em `deile/skills/library/`, que ficam disponíveis só via auto-trigger e `invoke_skill`). Skills de `~/.claude/commands/` ficam UPPERCASE.

---

## 🎭 Sistema de personas

Personas MD-driven:

- 📚 Library (`deile/personas/library/`): `architect.yaml`, `debugger.yaml`, `developer.yaml`
- 📝 Instruções (`deile/personas/instructions/`): `developer.md`, `fallback.md`
- 🧠 Memória de persona (`deile/personas/memory/`)
- 🛠️ Infra (`deile/personas/`): loader, builder, manager, etc.

Modificar `instructions/*.md` altera o comportamento sem tocar em Python. Os YAMLs definem nome/persona_id/capacidades.

---

## 🧩 Sistema de skills

Skills são unidades composáveis de expertise em Markdown puro (sem código). O loader escaneia todas as fontes abaixo em ordem de prioridade crescente — em colisão de nome o source mais alto vence (project sobrescreve user que sobrescreve bundled, com `INFO` log):

| Origem | Caminho | Comportamento |
|---|---|---|
| Bundled | `deile/skills/library/**/*.md` | Vai no pacote DEILE; PR no repo |
| Usuário pessoal | `~/.deile/skills/*.md` | Visível em qualquer projeto seu |
| Usuário (Claude compat) | `~/.claude/commands/*.md` | Mesmo formato; nome registrado em UPPERCASE (`kind=command`) |
| Projeto | `<cwd>/.deile/skills/*.md` | Versionada no git, viaja junto com o repo |
| Projeto (Claude compat) | `<cwd>/.claude/commands/*.md` | UPPERCASE (`kind=command`) |
| Configurada via YAML | `library_paths:` em `deile/config/skills.yaml` | Lista explícita de paths absolutos ou relativos ao repo |
| Configurada via REPL | `/skills add <path> [--scope global\|project]` | Persistida em `~/.deile/settings.json` (global) ou `.deile/settings.json` (projeto) |

### Como o LLM usa cada skill

Três caminhos simultâneos e independentes:

- **Auto-injeção no system prompt** — quando uma `trigger` casa para o turno (até `max_per_turn=4`, ordenadas por `(-priority, name)`):
  - `file_globs` — match `fnmatch` no basename ou path completo
  - `code_block_langs` — fence ``` ```python ``` no input, case-insensitive
  - `keywords` — word-boundary regex (não confunde "rust" com "trust")
  - `file_content_patterns` — regex em 4 KiB de cada arquivo referenciado, **contido ao `project_root`** (security)
- **Function-call tools** `invoke_skill(name)` e `list_skills` — auto-descobertas via `DEFAULT_TOOL_PACKAGES`. O catálogo no system prompt mostra ao LLM o que existe; ele puxa skills que não dispararam por trigger
- **Slash command `/<name>`** — invocação explícita pelo usuário com argumentos opcionais

### Configuração e ergonomia

- `deile/config/skills.yaml`: `enabled`, `max_per_turn`, `library_paths`, `extension_map`, `basename_map`
- Comando `/skills` no REPL: `list` (mostra paths ativos), `add` e `remove` (gerencia paths extras com escopo `global` ou `project`)
- **Hot-reload** via `watchdog`: dropar/editar/remover um `.md` reflete no agente em 0,5 s, sem restart (debounce coalesce bursts do editor; swap atômico no `SkillRegistry`)

### Formato

```markdown
---
name: rust                          # opcional; default = stem do arquivo (normalizado)
description: |
  Regras do projeto sobre Rust — ownership, async/Tokio. Sobrescreve
  qualquer conselho genérico do treinamento.
triggers:                           # tudo opcional; vazio = só responde a /<name> ou invoke_skill
  file_globs: ["*.rs", "Cargo.toml"]
  code_block_langs: [rust]
  keywords: ["ownership", "tokio"]
  file_content_patterns: ['^use tokio::']
priority: 50                        # int; default 0. Maior aparece primeiro no ranking
---
# Body em Markdown puro — entra no prompt ou é devolvido por invoke_skill.
```

Bundled out-of-the-box: `python`, `typescript`, `tdd`. Detalhes em [`docs/system_design/04-MODELO-COMPONENTES.md`](docs/system_design/04-MODELO-COMPONENTES.md) e template completo em [`docs/system_design/12-PADROES-CODIGO.md`](docs/system_design/12-PADROES-CODIGO.md). Decisão #34 em [`docs/system_design/DECISOES.md`](docs/system_design/DECISOES.md).

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

# Sub-DEILEs paralelos (issue #257) — opcionais
export DEILE_SUBAGENT_RUNNER=local         # local (default) | worker
export DEILE_SUBAGENT_MAX_PARALLEL=3       # teto de concorrência
export DEILE_SUBAGENT_BUDGET_S=600         # teto de tempo global (s)
export DEILE_SUBAGENT_POLL_INTERVAL_S=0.8  # polling do worker runner
```

Veja exemplos em `.env.example` — defina pelo menos uma chave.

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
- `deile/config/skills.yaml` — Sistema de skills (`enabled`, `max_per_turn`, `library_paths`, `extension_map`)
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
| 🧩 Skill                | `~/.deile/skills/`, `.deile/skills/`, ou `deile/skills/library/`  | MD com frontmatter — sem Python; hot-reload automático |
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