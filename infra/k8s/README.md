# `infra/k8s/` — DEILE + deilebot em containers (Rancher Desktop / k3s)

> ⭐ **Painel TUI universal** — `python3 infra/k8s/deploy.py k8s panel`
> abre o cockpit ao vivo do DEILE. Funciona **com ou sem k8s**: detecta
> automaticamente processos locais (`python deile.py`), tail de
> `~/.deile/logs/`, custos do SQLite, e — quando o cluster está no ar —
> pods, pipeline, workers, audit do bot. Tudo em uma só tela. Salto
> direto: [seção 4.3](#43-painel-tui-ao-vivo-deploypy-k8s-panel).

> **Modelo conceitual** vive em
> [`docs/system_design/14-CONTAINERIZACAO.md`](../../docs/system_design/14-CONTAINERIZACAO.md).
> Aqui você encontra **o passo-a-passo de operação** — instalar
> ferramentas, configurar segredos, build da imagem, deploy, teste e
> troubleshooting.

> **Orquestrador:** o `run.sh` virou um shim — o orquestrador agora é o
> `infra/k8s/deploy.py` (Python, colorido, com `help`). `bash run.sh X`
> equivale a `python3 deploy.py X`. O alvo é explícito no verbo:
> `deploy.py k8s <ação>` (stack no Kubernetes) ou `deploy.py local <ação>`
> (bot como serviço no host); rodar `deploy.py` sem argumentos abre um
> menu. Veja `python3 deploy.py help`. Para preparar uma máquina do zero
> (k3s/colima/dependências), use `python3 infra/setup_environment.py`.

---

## 1. Pré-requisitos — instalando o ambiente do zero (macOS)

### 1.1 Rancher Desktop (k3s + containerd)

[Rancher Desktop](https://rancherdesktop.io/) sobe um k3s local
embarcado com `kube-router` (que enforce NetworkPolicy — essencial pro
nosso modelo).

```bash
brew install --cask rancher
open -a "Rancher Desktop"
```

Na primeira tela:

- **Container Engine**: escolha **`containerd (nerdctl)`** — não Docker.
  k3s lê imagens da mesma store do containerd quando você builda em
  `--namespace k8s.io`.
- **Kubernetes**: habilitado, versão `≥ 1.28`.
- **Path Setup**: deixe o instalador adicionar `~/.rd/bin/` ao PATH.

Verifique:

```bash
~/.rd/bin/kubectl get nodes
# NAME                   STATUS   ROLES                  AGE   VERSION
# lima-rancher-desktop   Ready    control-plane,master   …     v1.28.x+k3s1

~/.rd/bin/nerdctl --namespace k8s.io info | head -3
```

### 1.2 Bot Discord (token + permissões)

1. <https://discord.com/developers/applications> → **New Application**.
2. **Bot** → copie o token (esse vai para `DEILE_BOT_DISCORD_TOKEN`).
3. **Privileged Gateway Intents** — habilite os três:
   `PRESENCE`, `SERVER MEMBERS`, `MESSAGE CONTENT`.
4. **OAuth2 → URL Generator**:
   - Scopes: `bot` + `applications.commands`.
   - Bot Permissions (mínimo): `Send Messages`, `Read Message History`,
     `Add Reactions`, `Use Slash Commands`.
5. Abra a URL gerada e convide o bot para o seu servidor.

Anote o **seu** `user_id` (clique direito → Copy User ID, ative
`Developer Mode` em Settings/Advanced). Esse vai em `owners:` no
`bot-config` (ver §3.3).

### 1.3 `.env` no repo

Crie/edite o `.env` na raiz do repo (`/Users/elimar.cavalli/dev/github/elimarcavalli/deile/.env`).
**Use o [`.env.example`](../../.env.example) como referência** — 11 seções, 435 linhas,
toda variável documentada com formato + default. Mínimo absoluto pra subir hoje:

```ini
# 1. Pelo menos UMA chave LLM (obrigatório):
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# DEEPSEEK_API_KEY=sk-...
# GOOGLE_API_KEY=AIza...

# 2. Token do bot Discord (obrigatório hoje no `k8s up` — ver gap #1 abaixo):
DEILE_BOT_DISCORD_TOKEN=MTQ5...

# 3. (Opcional) token GitHub pra clonar repos privados + abrir PRs:
GITHUB_TOKEN=ghp_...

# 4. (Opcional) token GitLab — issue #297, mas ver gap #2 abaixo:
GITLAB_TOKEN=glpat-...

# DEILE_BOT_AUTH_TOKEN e DEILE_WORKER_BEARER_TOKEN: auto-gerados pelo `k8s up` se ausentes.
```

> `.env` está no `.dockerignore` e `.gitignore` — **não entra na imagem
> nem no git**. Os Secrets do K8s são criados em runtime a partir dele
> e nunca aparecem em `kubectl get secret -o yaml` por acidente.

### 1.3.1 Build-time vs runtime — onde cada var é lida

| Momento | O que lê | Pode customizar |
|---|---|---|
| **`k8s build`** (Dockerfile) | **NADA do `.env`** — build é hermético | Só `--build-arg PYTHON_VERSION=3.11.10` ou `GLAB_VERSION=1.45.0` (raríssimo) |
| **`k8s up`** (deploy.py) | Lê `.env` → cria 3 Secrets (`bot-secrets`, `deile-secrets`, `worker-bearer`) | Vars listadas em §1.3 acima |
| **`k8s claude-login`** | Lê `~/.claude/credentials.json` do host (OAuth do Claude Code Pro/Max) | Use `--switch` pra trocar de conta; `--no-interactive` pra CI |
| **Pods em execução** | Lêem dos Secrets/ConfigMaps montados + env vars hardcoded em cada manifest | `kubectl set env deployment/<name> KEY=VALUE` pra override (já há helpers no painel `[d]`) |

### 1.3.2 Onde cada categoria de config mora (mapa)

Os ~95 nomes do `.env.example` se distribuem em 5 lugares — saber qual usar
para o quê é metade da operação:

| Lugar | Para quê serve |
|---|---|
| **`.env` (raiz do repo)** | Segredos do operador (tokens API, OAuth) + overrides locais |
| **K8s Secrets** (`bot-secrets`, `deile-secrets`, `worker-bearer`, `claude-worker-bearer`, `pipeline-status-bearer`, `claude-credentials`) | Espelho do `.env` montado nos Pods em `/run/secrets/<role>/` |
| **K8s ConfigMaps** (`bot-config`, `deile-runtime-config`, `claude-worker-allowed-repos`) | Config NÃO-secreta: owners do bot, runtime tunables, allowlist de repos |
| **Manifests env vars** (blocos `env:` em `infra/k8s/manifests/*-deployment.yaml`) | Hardcoded por Pod: portas, paths, autostart flags, whitelists |
| **`~/.deile/settings.json` (layered)** | Configs migráveis de runtime — alvo de muitas vars `[DEPRECATED → settings.json]` no `.env.example` |

> **Owners do bot Discord** vivem em `15-bot-config.yaml` (ConfigMap),
> NÃO no `.env`. Formato: `owners: ["discord:<seu_snowflake>"]`. Anote
> seu User ID via Settings/Advanced → Developer Mode no Discord, e cole
> aí antes do `k8s up`.

### 1.3.3 Gaps conhecidos no fluxo de configuração

Gaps resolvidos pelo PR #363 (issue #354):

- ~~**`DEILE_BOT_DISCORD_TOKEN` hard-fail mesmo sem bot**~~ — **resolvido**.
  Use `k8s up --profile pipeline-only` para subir sem bot Discord.
- ~~**`GITLAB_TOKEN` não é propagado pelo `k8s up`**~~ — **resolvido**.
  `GITLAB_TOKEN` (e o alias `GL_TOKEN`) agora é propagado para `deile-secrets`.
  Se só `GITLAB_TOKEN` estiver presente, `k8s up` emite warning sugerindo
  `DEILE_FORGE_KIND=gitlab`.
- ~~**`PIPELINE_STATUS_BEARER_TOKEN` (issue #347) órfão**~~ — **resolvido**.
  `k8s up` agora gera, persiste no `.env` e aplica o Secret `pipeline-status-bearer`
  com a chave `PIPELINE_STATUS_BEARER_TOKEN`. O painel TUI não verá mais 401/403
  após deploy em cluster zerado.
- ~~**Dois `k8s up` consecutivos geram tokens diferentes**~~ — **resolvido**.
  `DEILE_BOT_AUTH_TOKEN`, `DEILE_WORKER_BEARER_TOKEN` e `PIPELINE_STATUS_BEARER_TOKEN`
  são persisted no `.env` na primeira execução e reusados nas seguintes.

Gaps ainda abertos:

4. **`claude-worker` (manifests 47-50) NÃO aplicado pelo `k8s up` padrão** — só
   sobe com o perfil `claude-only` + `k8s claude-login` depois. Esperado por design
   (claude-worker é opt-in e requer OAuth).
5. **Manifest 43 (`forge-tokens-secret`) existe mas não é aplicado** —
   `k8s up` põe os tokens em `deile-secrets`, fazendo o 43 dead code.

---

## 2. Build e deploy — caminho feliz

```bash
python3 infra/k8s/deploy.py k8s build   # ~5–10 min na 1ª vez; cache nas próximas
python3 infra/k8s/deploy.py k8s up      # namespace + NPs + Secrets + bot + worker
python3 infra/k8s/deploy.py k8s test    # cria o Job de prova → DM no Discord
```

### 2.1 Multi-namespace — múltiplas stacks DEILE em paralelo

A stack DEILE é **per-namespace**. Você pode rodar várias em paralelo (uma
por forge, por repo, por ambiente), cada uma com seus próprios Secrets,
PVCs, ConfigMaps e Deployments. Antes de qualquer operação, confira o
mapa atual:

```bash
~/.rd/bin/kubectl get ns -L app.kubernetes.io/managed-by,deile.io/forge,deile.io/repo
```

Convenções:

| Namespace | Para que serve |
|---|---|
| `deile` | **Default** — stack de produção (GitHub `elimarcavalli/deile`). Criada por `k8s up`. |
| `deile-gl` | Piloto GitLab (issue #297). Criada por `k8s create-namespace --forge gitlab --repo <group/project>`. |
| `deile-<algo>` | Convenção para namespaces extras (staging, sandbox, fork-X). Criada por `k8s create-namespace`. |
| `default` | **Built-in do k8s.** Nunca deveria conter recursos DEILE. Se aparecer um pod/svc `deile-*` lá, é vazamento de algum `kubectl apply` sem `-n <ns>` — limpe. |

Para apontar qualquer verbo do `deploy.py` a um namespace específico,
use a flag global `-n <ns>` (ou `--namespace <ns>`) antes do verbo:

```bash
python3 infra/k8s/deploy.py -n deile-gl k8s status
python3 infra/k8s/deploy.py -n deile-gl k8s start          # resume o piloto GitLab
python3 infra/k8s/deploy.py -n deile-gl k8s logs pipeline
python3 infra/k8s/deploy.py -n deile-staging k8s up        # sobe stack staging do zero
```

Mesma regra vale para `kubectl` direto: **sempre passe `-n <ns>`**. Sem
flag, o `kubectl` cai no contexto `default` e o recurso vaza pra lá.

#### 2.1.1 Criando um namespace novo (`k8s create-namespace`)

Para subir uma stack DEILE limpa em outro namespace (ex: piloto GitLab,
ambiente de staging, fork de teste), use o wizard interativo:

```bash
python3 infra/k8s/deploy.py k8s create-namespace
```

Ele pergunta nome, forge (GitHub/GitLab), repo alvo e flags opcionais
(ativar claude-worker, scale inicial). O processo cria namespace + PSS
labels + Secrets + ConfigMaps + Deployments do zero. Suporta também todos
os parâmetros via flag (`--forge gitlab --repo group/project --enable-claude-worker`)
para uso não-interativo.



Sucesso = DM aparece no Discord (no canal direto entre você e o bot)
e o Job termina `1/1 completions in XXs`. Logs:

```bash
bash infra/k8s/run.sh logs    # bot + Job
```

Quando terminar e quiser limpar tudo:

```bash
bash infra/k8s/run.sh down    # kubectl delete ns deile
```

---

## 3. Manifests, um por arquivo

```
infra/k8s/
├── .dockerignore                 ← nada secreto, nada grande entra na imagem
├── Dockerfile                    ← multi-stage; non-root; tini + git; readonly-friendly
├── wrapper.py                    ← entry-point que lê /run/secrets/<role>/, popa env,
│                                    configura git credentials + clone guard
├── run.sh                        ← orquestrador build/up/test/logs/clone/down
├── README.md                     ← este arquivo
└── manifests/
    ├── 00-namespace.yaml                       namespace `deile` w/ PSS:restricted
    ├── 15-bot-config.yaml                      ConfigMap: deilebot.yaml com owners + clonable_repos
    ├── 19-bot-data-pvc.yaml                    PVC do SQLite do bot (audit/dlq persistentes)
    ├── 20-bot-deployment.yaml                  bot Deployment + Service (ClusterIP :8765)
    ├── 30-deile-job.yaml                       one-shot deile Job (proof-of-DM)
    ├── 35-deile-interactive.yaml               long-running deile-shell (`kubectl exec`)
    ├── 36-deile-shell-pvc.yaml                 PVC opcional para /home/deile persistente
    ├── 40-network-policy.yaml                  default-deny + selective allow (todos os pods)
    ├── 41-worker-pvc.yaml                      PVC do deile-worker (workdirs por canal)
    ├── 42-worker-bearer-secret.yaml            Bearer token do deile-worker (pipeline → worker)
    ├── 43-forge-tokens-secret.yaml             GITHUB_TOKEN + GITLAB_TOKEN (forge-agnostic, issue #297)
    ├── 44-pipeline-status-bearer-secret.yaml   Bearer token do pipeline status server (:8768)
    ├── 45-deile-worker-deployment.yaml         deile-worker Deployment + Service (:8766)
    ├── 46-deile-pipeline-deployment.yaml       deile-pipeline Deployment + status Service (:8768)
    ├── 47-claude-worker-allowed-repos.yaml     ConfigMap: repos que o claude-worker pode clonar
    ├── 47-deile-runtime-config.yaml            ConfigMap com env vars de runtime do pipeline
    ├── 48-claude-worker-bearer-secret.yaml     Bearer token do claude-worker (pipeline → claude)
    ├── 49-claude-worker-pvc.yaml               PVC do claude-worker (worktrees + credentials)
    ├── 50-claude-worker-deployment.yaml        claude-worker Deployment + Service (:8767)
    └── 99-deile-debug.yaml                     probe Pod (manual; off-path)
```

> O Secret `claude-credentials` (OAuth do `claude -p`) **não** está em
> manifest — é criado pelo verbo `k8s claude-login` a partir do
> `~/.claude/credentials.json` do host. Esse arquivo nunca entra no
> repo nem na imagem.

### 3.1 Workflow

1. `00-namespace.yaml` cria o namespace com **Pod Security
   `restricted`** ligado em `enforce`. Pods que pedirem privilégio
   demais são rejeitados no admission.
2. `40-network-policy.yaml` aplica **default-deny** (todo ingress/egress
   bloqueado) e depois libera só:
   - DNS para kube-system (53/UDP+TCP);
   - bot 8765/TCP ingress só de pods `role=deile`;
   - `role=deile` egress 8765 para `app=deilebot`;
   - `role=deile` egress 443 a `0.0.0.0/0 except RFC1918` (Anthropic etc.);
   - `app=deilebot` egress 443/80 a `0.0.0.0/0 except RFC1918`
     (discord.com).
3. `run.sh up` cria dois Secrets a partir do `.env`:
   - `bot-secrets`: Discord token + Bearer auth + chaves LLM (bot
     precisa pra agente embutido responder DMs);
   - `deile-secrets`: chaves LLM + Bearer (deile precisa pra LLM e
     pra falar com bot).
4. `15-bot-config.yaml` é um ConfigMap com `deilebot.yaml` — define
   `owners:` (no formato `<provider>:<provider_user_id>` para
   sobreviver à recriação do bot_user_id ULID a cada deploy).
5. `20-bot-deployment.yaml` sobe o bot. `args` (não `command`) usa o
   ENTRYPOINT `tini` da imagem e chama `python3 /app/wrapper.py bot
   run --provider discord`.
6. `30-deile-job.yaml` é a prova: roda uma vez, manda DM e morre.
   `DEILE_WRAPPER_TOOL_WHITELIST=messaging` no env trava o toolset
   do agente.
7. `35-deile-interactive.yaml` é o seu sandbox — `sleep infinity`,
   alvo de `kubectl exec`. Sem whitelist (= `all`), prompt vem do
   operador.

### 3.2 O `wrapper.py` em três linhas

```
sys.argv[1] == "deile"  → carrega /run/secrets/deile/* em os.environ
                           → patcha bootstrap_providers() pra popar segredos depois
                           → (opcional) instala whitelist se DEILE_WRAPPER_TOOL_WHITELIST=messaging
                           → exec deile.cli.main()

sys.argv[1] == "bot"    → carrega /run/secrets/bot/* em os.environ
                           → instala whitelist (Discord input é untrusted)
                           → exec deilebot.cli.main()
```

`/proc/<pid>/environ` é congelado no momento do `execve` — ele nunca
vê os segredos, porque o K8s passa só env não-secretos via `env:` no
Pod spec.

### 3.3 Aceitando outros providers / outros owners

- **Outro provider LLM**: cole a chave no `.env` antes de rodar `up`;
  o `run.sh` injeta automaticamente tudo que casa `_API_KEY`.
- **Outro owner Discord**: edite `15-bot-config.yaml`, adicione mais
  itens em `owners:` no formato `discord:<snowflake>`, `kubectl apply`
  e `kubectl rollout restart deployment/deilebot`.

---

## 4. Operação dia-a-dia

### 4.1 Modos de invocar DEILE no container

```bash
# A) One-shot fechado, prompt fixo no manifest (CI / cron / proof)
bash infra/k8s/run.sh test

# B) Sandbox interativo com toolset cheio (alvo: kubectl exec)
kubectl -n deile exec deploy/deile-shell -- python3 /app/wrapper.py deile "seu prompt aqui"
kubectl -n deile exec -it deploy/deile-shell -- python3 /app/wrapper.py deile   # REPL

# C) Direto no host (sem container) — para mexer em código do seu projeto
python3 deile.py "seu prompt"
```

### 4.2 Clonar repositórios dentro do deile-shell

`deile-shell` inclui `git` no container e suporta clonar repos do GitHub de
forma segura — as credenciais ficam num K8s Secret, nunca em variáveis de
ambiente visíveis no `/proc/<pid>/environ`.

#### Pré-requisito: adicionar `GITHUB_TOKEN` ao `.env`

```ini
# Crie um fine-scoped token no GitHub (Settings → Developer settings →
# Personal access tokens → Fine-grained). Scopes mínimos: Contents: Read.
GITHUB_TOKEN=github_pat_...
```

#### Clonar com um comando

```bash
bash infra/k8s/run.sh clone elimarcavalli/deile
```

O que acontece internamente:
1. `GITHUB_TOKEN` é injetado em `deile-secrets` via `kubectl apply --dry-run`.
2. O script aguarda o kubelet sincronizar o arquivo do Secret no pod (até 90 s).
3. Um snippet Python roda dentro do pod: lê o token do arquivo Secret-montado,
   escreve `~/.git-credentials`, configura `credential.helper store`.
4. `~/bin/git` (o clone guard instalado pelo `wrapper.py`) verifica o URL contra
   a allowlist `clonable_repos` de `15-bot-config.yaml` antes de chamar `/usr/bin/git`.
5. O repo aparece em `/home/deile/work/<nome>` dentro do pod.

#### Allowlist de repos

Edite `infra/k8s/manifests/15-bot-config.yaml`, seção `git_integration`:

```yaml
git_integration:
  clonable_repos:
    - "elimarcavalli/*"     # qualquer repo do org
    - "minha-org/repo-x"   # repo específico
```

Após editar: `kubectl apply -f infra/k8s/manifests/15-bot-config.yaml`.
A allowlist é lida pelo `wrapper.py` no próximo `python3 /app/wrapper.py deile`.

#### Home persistente (opcional)

Por padrão `/home/deile` usa `emptyDir` — os repos clonados somem quando o
pod reinicia. Para persistir:

```bash
# 1. Criar o PVC
kubectl apply -f infra/k8s/manifests/36-deile-shell-pvc.yaml

# 2. Editar 35-deile-interactive.yaml:
#    - comentar a linha emptyDir do volume 'home'
#    - descomentar o bloco persistentVolumeClaim
kubectl apply -f infra/k8s/manifests/35-deile-interactive.yaml
kubectl rollout restart deployment/deile-shell -n deile

# Para remover o disco persistente:
kubectl delete pvc deile-shell-home -n deile
```

> **Nota:** O PVC sobrevive a `kubectl rollout restart` mas NÃO a
> `bash infra/k8s/run.sh down` (que deleta o namespace inteiro).

### 4.3 Painel TUI ao vivo (`deploy.py k8s panel`)

Para acompanhar a stack em tempo real, sem ficar abrindo `kubectl get`/`logs`
em loop:

```bash
python3 infra/k8s/deploy.py k8s panel
```

O painel é uma TUI navegável (`rich.Live`) que cruza o estado do cluster
(pods, deployments, restarts) com a fonte de verdade do pipeline (GitHub
issues + PRs), o uso (`~/.deile/db/usage.db`), processos locais
(`python deile.py` rodando no host) e logs locais (`~/.deile/logs/`).
**Não muta nada** — é só observação, exceto onde explicitamente acionado
(`[a]ções` e `[m]odel`, ambos com confirmação para destrutivos).

**Três modos** de execução, detectados em runtime — **não exige
cluster**:

| Modo | Quando | O que aparece |
|---|---|---|
| **K8s + local** (híbrido) | cluster no ar AND `python deile.py` rodando | PODS k8s + LOCAL PROCESSES lado a lado; ACTIVITY mescla pipeline + log local; NOTIFIER mescla bot audit (k8s) + audit local |
| **K8s only** | cluster no ar, sem DEILE local; ou `--k8s-only` | Comportamento legado pré-universal |
| **Local only** | sem kubectl OU `--local-only` OU cluster down + há vestígios em `~/.deile/` | Pods/Workers/Pipeline ficam em branco; LOCAL PROCESSES e GITHUB e TOKENS funcionam; modelo via `kubectl set env` mostra aviso |
| **Demo** (`--demo`) | opt-in explícito | Mocks puros — útil pra ver a UI sem rodar nada |

Composição (módulos em `infra/k8s/`):

- `_panel_data.py` — cache TTL + `RuntimeContext` + 11 providers (Pods,
  Pipeline, Worker, GitHub, Costs, Models, CurrentModel, Notifier +
  **LocalProcesses, LocalLogs, LocalAudit**). Fonte única de verdade dos
  números.
- `_panel_demo.py` — mocks usados em `--demo` (modo legado opt-in).
- `_panel.py` — `KeyReader` (termios cbreak / `msvcrt`), view contract,
  alerts engine, 9 views, `_LogStreamer` (kubectl), `_LocalLogTailer`
  (tail file), loop principal `PanelApp`.

Zero dependência nova: `rich` já está em `pyproject.toml`. Detecção
local usa só `ps`, leitura tail-from-end pura (até 64KB), sem `psutil`.

#### 4.3.1 Visão geral — o dashboard

A primeira tela que aparece. Refresh visual **1s** (cada bloco refresca
conforme TTL do seu provider — ver tabela abaixo).

**Modo híbrido (k8s + local) — capturado em produção:**

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ DEILE Stack  ·  Dashboard  ·  2026-05-24 22:30:33  ·  refresh 1.0s                                         ┃
┃ mode: k8s + local   cluster: rancher-desktop (k3s)   namespace: deile   repo: elimarcavalli/deile          ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
╭─ PODS ───────────────────────────────────╮╭─ LOCAL PROCESSES (host) ─────────────────╮
│   pod              status    age   doing ││   pid/role             rss  up   cpu doing│
│  ──────────────────────────────────────  ││  ──────────────────────────────────────── │
│  ⚡ deile-pipe…    Running   5h28m mention││  ● local-deile#2001    6MB  15d   0% disp │
│  ● deile-worker…   Running   2h12m idle  ││  ● local-deile#16694   14MB 1h10m 0% disp │
│  ● deile-worker…   Running   2h12m idle  ││  ● local-deile#28117   25MB 30m   0% disp │
│  ● deilebot…       Running   2h12m —     ││                                           │
╰──────────────────────────────────────────╯╰───────────────────────────────────────────╯
╭─ PIPELINE ──────────────────────────╮╭─ ALERTS ────────────────────────────────────╮
│ running for 5h28m  ·  last 38s ago  ││ ⛔ deile-worker-... reiniciou 28× — investigar│
│ summary: mention poll (review …)    ││ ⛔ deile-worker-... reiniciou 26× — investigar│
│ dispatches/24h: 0  mentions/24h: 0  ││ ⛔ deilebot-…       reiniciou 29× — investigar│
│ Issues open: 4  sem_workflow:4      ││                                              │
│ PRs open:    5  sem_review:5        ││                                              │
╰─────────────────────────────────────╯╰──────────────────────────────────────────────╯
╭─ ACTIVITY (últimos 10) ──────────────────────────────────────────────────────────────╮
│  22:29:55  pipeline  mention   PR297    triggers=reviewer                            │
│  22:29:32  local     dispatch           worker dispatch completed (1 result)         │
│  22:28:11  pipeline  http               POST /v1/dispatch → 200                      │
│  22:28:11  pipeline  dispatch           worker dispatch starting                     │
│  22:27:42  local     stages             refining_passes=2/5 prompt_tokens=4819       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ TOKENS & CUSTOS ───────────────────╮╭─ ÚLTIMAS DECISÕES ──────────────────────────╮
│ deepseek $1.74  anthropic $0.32     ││ PR297  triggers=reviewer                     │
│ total 24h: $2.06  records: 246  ·   ││ —  worker dispatch completed                 │
│ 1h: $0.12                           ││ —  worker dispatch starting                  │
╰─────────────────────────────────────╯╰──────────────────────────────────────────────╯
  [1]Pod watch  [2]Pipeline  [3]Issues/PRs  [4]Logs split  [5]Tokens  [n]otifier  [a]ctions  [m]odel
```

**Header "mode:" sinaliza o que está ativo** — `k8s + local` (híbrido,
verde), `k8s only` (ciano), `local only` (amarelo), `demo (mocks)`
(vermelho).

**Visual vs fetch — desacoplados.** A view re-renderiza em cadência
**de 1s** (`refresh_s`) — render é ~3ms, custo desprezível. O fetch
real (subprocess kubectl/gh, query SQLite) acontece no
`BackgroundRefresher` (thread daemon) e respeita o TTL próprio de cada
provider. O thread principal **nunca bloqueia**: `provider.get()`
devolve o cache (mesmo velho) e o refresher repõe no próximo tick.

Cada bloco do dashboard vem de um provider diferente:

| bloco | provider | TTL do provider |
|---|---|---|
| PODS | `PodsProvider` + `WorkerProvider` + `PipelineProvider` | 1s + 2s + 2s |
| PIPELINE | `PipelineProvider` + `GitHubProvider` | 2s + 10s |
| ALERTS | regras locais em `_alerts_from_data` | — (recomputado a cada render) |
| ACTIVITY | `PipelineProvider.events` | 2s |
| TOKENS & CUSTOS | `CostsProvider` (SQLite local) | 30s |
| ÚLTIMAS DECISÕES | últimos 3 eventos `mention`/`dispatch`/`stages` | 2s |

TTLs escolhidos por origem da fonte:

| fonte | TTL | racional |
|---|---|---|
| `kubectl get pods` (local k3s) | 1s | <50ms por chamada, sem custo |
| `kubectl logs --tail=200` (pipeline/audit) | 2s | ~200ms por chamada |
| `kubectl logs` por worker (N pods) | 2s | rodam em paralelo no pool |
| `kubectl get deploy/<dep> -o json` (current model) | 3s | 1 chamada por deploy |
| `gh api --paginate /repos/.../issues` | 10s | rate-limit (5000/h auth = 1.4/s) — 10s = 360/h, folga confortável |
| SQLite `usage.db` (costs) | 30s | local, mas poll mais frequente é só barulho |
| `model_providers.yaml` | 300s | catálogo estático |

#### 4.3.2 Hotkeys globais

Valem em **qualquer view**. As que a view específica respondem antes vão na
descrição dela.

| tecla | ação |
|---|---|
| `1`–`5`, `a`, `m`, `n` | drill em sub-view (a partir do dashboard) |
| `↑` / `↓` ou `j` / `k` | navega em listas (picker, issues/PRs, modelos) |
| `enter` | seleciona o item destacado |
| `esc` | volta à view anterior (ou ao dashboard) |
| `q` | sai do painel |
| `p` | pause / resume do refresh automático |
| `+` / `-` | acelera / desacelera o refresh (×0.25 → ×4) |
| `r` | força refresh imediato e **invalida os caches** de todos os providers |
| `s` | snapshot da tela atual em `~/.deile/snapshots/panel-YYYYMMDD-HHMMSS.txt` |
| `?` | tela de ajuda |

#### 4.3.3 Pod picker → Pod watch (`[1]`)

`[1]` abre o picker. **`↑`/`↓`** navega, **`enter`** entra no pod selecionado.

```text
╭─ escolha um pod para assistir ─────────────────────────────────────────────────────────────────────────────╮
│             pod                           role         status       age      doing now                     │
│  ────────────────────────────────────────────────────────────────────────────────────────────────────────  │
│  ▶     ●    deile-pipeline-676866dcdf-…   pipeline     Running      2h06m    worker dispatch completed     │
│        ●    deile-worker-6486989bbb-45…   worker       Running      2h23m    idle                          │
│        ●    deile-worker-6486989bbb-lr…   worker       Running      2h22m    2026-05-23 16:15:57,852 INFO  │
│        ●    deilebot-75b5dc48d4-k88v7     bot          Running      2h23m    —                             │
│        ●    deile-shell-7bcbb8c79f-t6p…   shell        Running      2h23m    —                             │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
  [↑/↓] navega   [enter] entra   [esc] volta   [q] sai
```

Depois do `enter` abre o **Pod Watch** (refresh **1s**): header com info do
pod + log live seguindo `kubectl logs -f`.

```text
╭─ POD ────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ name: deile-worker-6486989bbb-45nb5   role: worker   status: Running                                                 │
│ uptime: 2h23m   restarts: 0   ready: yes   node: lima-rancher-desktop                                                │
│ worker: idle   last activity: 5s ago                                                                                 │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ LIVE LOG  ·  FOLLOW ON  ·  health ESCONDIDOS  ·  40 health filtrados  ·  deile-worker-6486989bbb-45nb5 ─────────────╮
│ (sem linhas significativas — aguardando atividade real)                                                              │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
  [f] follow on/off   [h] mostrar/esconder health   [c] clear log   [esc] volta   [q] sai
```

Hotkeys da view:

| tecla | ação |
|---|---|
| `f` | pause / resume o follow (sem perder o buffer) |
| `h` | mostra / esconde linhas `GET /v1/health` (default: esconde — workers ociosos lotam o painel) |
| `c` | limpa o buffer de log |

O título do painel sempre informa o estado (`FOLLOW ON`/`PAUSED`,
`health ESCONDIDOS`/`VISÍVEIS`, **N health filtrados** quando algo foi
oculto). Buffer rolling de 400 linhas. SIGTERM no kubectl ao sair (2s →
SIGKILL como garantia).

#### 4.3.4 Pipeline timeline (`[2]`)

Refresh visual **1s** (fetch real do `PipelineProvider` a cada **2s**).
Eventos classificados a partir de
`kubectl logs deploy/deile-pipeline --tail=200` — mention, dispatch, http,
startup, stages.

```text
╭─ STATS ─────────────────────────────────────────────╮╭─ HISTOGRAMA 24h ────────────────────────────────────╮
│ events:    13                                       ││ events 24h (1 col = 1h, mais recente à direita):    │
│ ticks/1h:  4                                        ││                      ▂█▄                            │
│ running:   2h06m                                    ││ ├──────────────────────┤                            │
│ last age:  12m                                      ││ -24h                now                             │
│ gap p95:   38m                                      ││                                                     │
│ gap max:   1h08m                                    ││                                                     │
│ failures:  0                                        ││                                                     │
╰─────────────────────────────────────────────────────╯╰─────────────────────────────────────────────────────╯
╭─ EVENTS (mais recentes em cima) ───────────────────────────────────────────────────────────────────────────╮
│  time                action                target             detail                                       │
│  ────────────────────────────────────────────────────────────────────────────────────────────────────────  │
│  13:15:57            dispatch                                 worker dispatch completed                    │
│  13:15:57            http                                     POST /v1/dispatch → 200                      │
│  13:04:58            dispatch                                 worker dispatch starting                     │
│  13:04:58            mention               PR295              triggers=reviewer                            │
│  11:56:26            mention               #278               triggers=assignee                            │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
  [esc] volta   [r] força refresh   [q] sai
```

- **STATS**: `gap p95`/`max` são o tempo entre eventos consecutivos
  (proxy de ociosidade). `failures` conta POST `/v1/dispatch` com retorno
  5xx na janela.
- **HISTOGRAMA 24h**: 24 colunas (1 por hora), densidade renderizada com
  blocos `▁▂▃▄▅▆▇█`, peak normalizado. Vazio? `--tail=200` só pegou as
  últimas N entradas (em workload moderado cobre ~5h, não 24h).
- **EVENTS**: últimos 30 eventos classificados.

#### 4.3.5 Issues & PRs (`[3]`)

Refresh visual **1s** (fetch real do `GitHubProvider` a cada **10s** —
`gh` API custa rate-limit). Cruz cluster ↔ GitHub,
respeitando precedência de label (`bloqueada` vence sobre fase).

```text
╭────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ filtro: todos    issues: 2    PRs: 2                                                                                           │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ ISSUES ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│        #        workflow                 review           updated      assignees            title                              │
│  ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  │
│  ▶     283      bloqueada                —                11h17m       elimarcavalli        [FEATURE] Suíte de testes…         │
│        257      —                        —                17h57m       —                    [INTENT] Decomposição autônoma…   │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ PRS ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│        #        workflow                 review           updated      assignees            title                              │
│  ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  │
│        295      —                        —                12m          elimarcavalli        feat: decomposição autônoma…      │
│        294      —                        —                17m          —                    feat(panel): live TUI monitoring… │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
  [a] all   [i] só issues   [p] só PRs   [b] só bloqueadas   [m] minhas   [↑/↓] navega   [enter] abrir URL   [esc] volta
```

Hotkeys da view:

| tecla | ação |
|---|---|
| `a` / `i` / `p` / `b` / `m` | filtros: all / só issues / só PRs / só bloqueadas / minhas (assignee = `$GH_USER` ou `elimarcavalli`) |
| `enter` | copia URL pro clipboard (best-effort: `pbcopy` / `xclip` / `wl-copy`) |

`workflow` aparece em **vermelho-bold** quando a issue/PR está bloqueada;
ciano para o estado normal de pipeline.

#### 4.3.6 Tokens & Custos (`[5]`)

Refresh visual **1s** (fetch real do `CostsProvider` a cada **30s** —
SQLite local é barato mas poll muito frequente é só barulho). Lê
`~/.deile/db/usage.db` (`UsageRepository`).

```text
╭────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ records 24h: 196   ·   total: $0.36   ·   1h: $0.12                                                        │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ POR PROVIDER ─────────────────────────────────────────────────────────────────────────────────────────────╮
│  provider                                             24h                       1h                      %  │
│  ────────────────────────────────────────────────────────────────────────────────────────────────────────  │
│  anthropic                                         $0.323                   $0.104                  89.5%  │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ TOP 5 SESSIONS (24h) ─────────────────────────────────────────────────────────────────────────────────────╮
│  session_id                                                                                          cost  │
│  ────────────────────────────────────────────────────────────────────────────────────────────────────────  │
│  default                                                                                           $0.361  │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
  [r] força refresh   [esc] volta   [q] sai
```

> **Sem dados?** O DB só recebe registros quando o agente roda **localmente**
> (no host). Os pods em K8s gravam no PVC do worker — o painel ainda não
> tem acesso a esse PVC. Trabalho futuro: endpoint `/admin/usage` no
> `worker_server.py` (opção pareada à do model switcher).

#### 4.3.7 Notifier echo (`[n]`)

Refresh visual **1s** (fetch real do `BotAuditProvider` a cada **2s**).
Parsea `kubectl logs deploy/deilebot --tail=500` filtrando
linhas JSON do logger `deilebot.audit`. Captura tanto `outbound_sent`
quanto `outbound_failed`.

```text
╭─ AUDIT EVENTS ─────────────────────────────────────────────────────────────────────────────────────────────╮
│  ts                      event                    status       detail                                      │
│  ────────────────────────────────────────────────────────────────────────────────────────────────────────  │
│  -05-23 14:26:38,490     outbound_failed          FAIL         op=channel.post, reason=denied              │
│  -05-23 14:36:54,033     outbound_failed          FAIL         op=channel.post, reason=denied              │
│  -05-23 16:04:58,997     outbound_failed          FAIL         op=channel.post, reason=denied              │
│  -05-23 16:23:27,192     outbound_sent            OK           op=dm.send, user_id=1475913578648436909,    │
│                                                                message_id=15077810                         │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
  [r] força refresh   [esc] volta   [q] sai
```

Status verde (`OK`) quando o evento contém `sent`/`received`, vermelho
(`FAIL`) quando contém `failed`, cinza (`—`) para outros.

#### 4.3.8 Ações (`[a]`)

Refresh **1s** (pra mostrar o output streaming). Lista os verbos do
`deploy.py` acionáveis sem sair do painel.

```text
╭─ AÇÕES ────────────────────────────────────────────────────────────────────────────────────────────────────╮
│         ação                  comando                                                                      │
│  ────────────────────────────────────────────────────────────────────────────────────────────────────────  │
│  1      status                infra/k8s/deploy.py k8s status                                               │
│  2      restart               k8s restart --yes                                                            │
│  3      build (no restart)    k8s build --yes                                                              │
│  4      build + restart       build --restart --yes                                                        │
│  5      up (provisiona)       k8s up --yes                                                                 │
│  6      stop (scale 0)        k8s stop --yes                                                               │
│  7      start (scale 1)       k8s start --yes                                                              │
│  8      test (Job one-shot)   k8s test --yes                                                               │
│  9      DOWN (apaga ns)       k8s down --yes        ← destrutivo, pede confirmação [y/n]                   │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ OUTPUT ───────────────────────────────────────────────────────────────────────────────────────────────────╮
│ (rode uma ação para ver o output aqui)                                                                     │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
  [1-9] dispara   [c] cancelar   [esc] volta   [q] sai
```

- `[1]`–`[9]` dispara a ação correspondente (subprocess streaming em
  thread daemon, buffer rolling de 500 linhas).
- `[9] DOWN` é **destrutiva** — overlay de confirmação `[y/n]` antes de
  rodar.
- `[c]` cancela a ação em curso (SIGTERM → SIGKILL após 2s).
- Borda do painel OUTPUT: **amarelo** = running, **verde** = exit 0,
  **vermelho** = exit ≠ 0.
- Sair da view (`[esc]`) mata o runner pendente.

#### 4.3.9 Trocar modelo em runtime (`[m]`)

Refresh visual **1s** (fetch real do `CurrentModelProvider` a cada
**3s**, catálogo `ModelsProvider` a cada **300s** — YAML estático).
Lista o catálogo de modelos lido do
`deile/config/model_providers.yaml` e mostra o que está efetivamente em uso
em cada Deployment. **`enter`** abre overlay de confirmação; **`y`**
aplica via `kubectl set env`, o que dispara rollout (zero-downtime no
worker com `RollingUpdate maxSurge:1/maxUnavailable:0`; rápido restart no
pipeline que é `Recreate`).

```text
╭─ TARGET ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ target: deile-worker                                                                                                           │
│ DEILE_PREFERRED_MODEL atual:                                                                                                   │
│   · deile-worker: anthropic:claude-sonnet-4-6                                                                                  │
│   · deile-pipeline: (não setado — usa defaults do settings)                                                                    │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ MODELOS DISPONÍVEIS ──────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│         slug                                     display                          tier        label        $/1M in   $/1M out  │
│  ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  │
│  ▶      anthropic:claude-opus-4-7                Claude Opus 4.7                  tier_1      flagship       $5.00     $25.00  │
│         anthropic:claude-sonnet-4-6  ●           Claude Sonnet 4.6                tier_2      balanced       $3.00     $15.00  │
│         anthropic:claude-haiku-4-5               Claude Haiku 4.5                 tier_3      fast           $1.00      $5.00  │
│         openai:gpt-5.4                           GPT-5.4                          tier_2      balanced       $2.50     $15.00  │
│         openai:gpt-5.4-mini                      GPT-5.4 Mini                     tier_3      fast           $0.75      $4.50  │
│         gemini:gemini-3.1-pro-preview            Gemini 3.1 Pro Preview           tier_1      flagship       $2.00     $12.00  │
│         gemini:gemini-3-flash-preview            Gemini 3 Flash Preview           tier_2      balanced       $0.50      $3.00  │
│         gemini:gemini-3.1-flash-lite-preview     Gemini 3.1 Flash-Lite Preview    tier_3      fast           $0.25      $1.50  │
│         deepseek:deepseek-v4-pro                 DeepSeek V4 Pro                  tier_1      flagship-…     $1.74      $3.48  │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
  [↑/↓] navega   [w] target=worker   [p] target=pipeline   [b] both   [enter] aplicar   [esc] volta   [q] sai
```

A bolinha verde **`●`** ao lado de um slug indica que ele é o valor
**efetivamente em uso** num dos targets atuais (cross-reference com
`CurrentModelProvider`).

Hotkeys da view:

| tecla | ação |
|---|---|
| `w` / `p` / `b` | troca o alvo: só worker (default) / só pipeline / both |
| `enter` | overlay de confirmação para aplicar o slug destacado |
| `y` / `n` | confirma / cancela (no overlay) |

> **Implementação atual = opção A** (env var + rollout). Opções pareadas
> que ficaram anotadas como evolução futura:
> - **B** — endpoint `/admin/set_model` no `worker_server.py` (swap
>   in-process instantâneo, sem rollout);
> - **C** — `ConfigMap` + hot-reload via `watchdog` (declarativo,
>   K8s-native, depende do hot-reload cobrir `model_providers`).

#### 4.3.10 Ajuda (`[?]`)

Resumo dos hotkeys globais. Útil pra lembrar dos atalhos sem sair do
painel.

```text
╭─ Hotkeys globais ────────────────────────────────────────────────────────────────────────────────╮
│   1-5, a, m, n               drill em sub-view (no dashboard)                                    │
│   ↑/↓ ou j/k                 navega em listas (picker, issues/PRs, modelos)                      │
│   enter                      seleciona o item destacado                                          │
│   esc                        volta à view anterior (ou ao dashboard)                             │
│   q                          sai do painel                                                       │
│   p                          pause / resume refresh automático                                   │
│   + / -                      acelera / desacelera o refresh (×0.25 a ×4)                         │
│   r                          força refresh imediato (invalida caches)                            │
│   s                          snapshot: salva a tela atual em ~/.deile/snapshots/                 │
│   ?                          esta tela                                                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

#### 4.3.11 Alertas — regras

O painel ALERTS no dashboard cruza limiares contra o estado atual:

| ícone | severidade | condição |
|---|---|---|
| ⛔ | **crit** | pod com `restarts ≥ 3` |
| ⚠ | warn | pod com `restarts ≥ 1` há menos de 30min |
| ⚠ | warn | pipeline sem ação há **>5min** (possível travamento) |
| ⚠ | warn | issue(s) com `~workflow:bloqueada` (lista até 3 números) |
| 🙋 | warn | issue(s) com `~workflow:aguardando_stakeholder` (esperando você) |
| ⚠ | warn | algum provider retornou erro no último fetch |

Borda do painel ALERTS muda conforme a pior severidade: **vermelha** se há
crítico, **amarela** se há warn, **verde** se está limpo. Limite visual de
6 alertas — o resto vira `… (+N mais)`.

#### 4.3.12 Modo demo

Quando `kubectl` não está disponível **ou** o cluster está fora, o painel
não trava: cai em mocks de `_panel_demo.py` (pods sintéticos, eventos
fictícios, 1 alerta de exemplo, custo de teste). A UI ainda abre — útil
para hackear/testar a TUI offline (em viagem, num CI, etc).

A detecção é silenciosa: a tentativa de instanciar `PanelData.default()`
captura qualquer falha do primeiro `pods.get()` e seta `data = None`.

#### 4.3.13 Arquitetura interna (para estender)

Adicionar uma view nova:

1. Crie uma classe que herda de `View` em `_panel.py`:
   ```python
   class MyView(View):
       name = "my-view"
       title = "Minha View"
       refresh_s = 5.0
       HOTKEYS = "[esc] volta   [q] sai"

       def __init__(self, data=None):
           self.data = data

       def render(self, app):
           # devolve qualquer RenderableType do rich
           return Panel(Text("oi"), title="MEU PAINEL",
                        border_style="green")

       def handle_key(self, key, app):
           # hotkeys próprios (globais já foram tratados antes)
           return ActionResult()
   ```
2. Registre em `_build_views(data)`:
   ```python
   "my-view": MyView(data=data),
   ```
3. Adicione hotkey de navegação no `DashboardView.HOTKEYS` e no
   `DashboardView.handle_key` (ou navegue a partir de outra view).
4. Se a view precisa de **dados novos**, escreva um provider em
   `_panel_data.py` (use `Cache` para TTL automático), adicione ao
   `PanelData` dataclass e ao `errors()` / `force_refresh_all()`.

Padrões do contract:

- `render()` é puro — apenas snapshot do estado atual; nunca faz I/O em
  primeira pessoa, consulta os providers (que são cacheados).
- `handle_key()` retorna um `ActionResult`: `.nav(target)`, `.back()`,
  `.refresh()`, `.quit()` ou `ActionResult()` (no-op).
- `on_mount(app)` / `on_unmount(app)` são chamados quando a view entra /
  sai da pilha — use pra iniciar/parar streamers (ver `PodWatchView`).
- Cadência (`refresh_s`) é por view. O multiplicador global (`+`/`-`)
  divide a cadência efetiva.

#### 4.3.14 Troubleshooting

| sintoma | causa provável | solução |
|---|---|---|
| `painel exige terminal interativo (sem TTY)` | rodou via pipe / CI | rode num terminal real, ou use o snapshot (`-s`) depois pra capturar |
| painel abre **em modo demo** mesmo com cluster no ar | `kubectl` não está no `PATH` | use a versão do Rancher Desktop: `export PATH="$HOME/.rd/bin:$PATH"` |
| `provider github: ...` no ALERTS | `gh` ausente ou não autenticado | `gh auth login` ou aceite que esse painel fique vazio |
| `provider costs: ...` ou painel TOKENS vazio | DB `~/.deile/db/usage.db` ainda não existe | rode o agente localmente uma vez (`python3 deile.py`) ou ignore — não bloqueia o resto |
| issues bloqueadas que não deviam estar | a label `~workflow:bloqueada` vence sobre a fase no derive — verifique no GitHub | edite as labels via REST (ver seção "Label edits" no `CLAUDE.md`) |
| `[m]` mostra `(não setado)` no pipeline | é o estado correto se o manifest do pipeline não define `DEILE_PREFERRED_MODEL` | aplique normalmente — o `set env` cria a entrada |
| painel trava ao apertar `c` em Ações com runner em curso | é a parada (SIGTERM → SIGKILL 2s); aguarde | até 2s; se persistir, `Ctrl-C` no terminal mata o painel inteiro |
| logs do pod-watch só mostram `health` em loop | filtro `[h]` está em "VISÍVEIS" | aperte `h` para esconder; o título mostra `N health filtrados` |

#### 4.3.16 CLI flags do `panel`

Todos opcionais (defaults batem com a stack padrão em `manifests/`).
Encadeie após `deploy.py k8s panel`:

```bash
python3 infra/k8s/deploy.py k8s panel [flags...]
```

**Por categoria:**

| Categoria | Flag | Default | Quando usar |
|---|---|---|---|
| **Namespace / deploys** | `--namespace <ns>` | `deile` | rodar contra outro namespace (ex: testar PR num clone do cluster) |
| | `--pipeline-deploy <name>` | `deile-pipeline` | renomeou o deployment no manifest |
| | `--worker-deploy <name>` | `deile-worker` | idem |
| | `--bot-deploy <name>` | `deilebot` | idem |
| **GitHub** | `--repo <owner/repo>` | derivado de `git remote get-url origin` | painel apontando pra outro repo de pipeline |
| **Paths locais** | `--usage-db <path>` | `~/.deile/db/usage.db` | DB de custos em outro caminho (PVC montado, snapshot, etc) |
| | `--logs-dir <path>` | `~/.deile/logs/` | logs em outro local (CI, container with mounted volume) |
| **Modo** | `--k8s-only` | (auto) | **não** detectar processos locais; útil quando o painel está sendo aberto NUM HOST com DEILE rodando que NÃO é o que você quer monitorar |
| | `--local-only` | (auto) | **não** chamar kubectl (mesmo disponível); foca só no host |
| | `--demo` | (off) | mocks puros — útil pra screenshot/treinamento sem cluster nem DEILE |

**Exemplos:**

```bash
# Painel apontando para um cluster de homologação (mesmo binário, ns diferente)
python3 infra/k8s/deploy.py k8s panel --namespace deile-staging

# DEILE rodando local sem k8s — só host
python3 infra/k8s/deploy.py k8s panel --local-only

# Cluster de produção isolado, ignorando processo local de dev
python3 infra/k8s/deploy.py k8s panel --k8s-only

# DB de custos snapshotado pra inspeção offline
python3 infra/k8s/deploy.py k8s panel --usage-db /backup/usage-2026-05-24.db

# Pipeline rodando contra outro fork
python3 infra/k8s/deploy.py k8s panel --repo other-org/their-deile

# Modo demo para gravar screenshot do README ou treinar alguém
python3 infra/k8s/deploy.py k8s panel --demo
```

**Validação de flags acontece antes do TTY abrir** — `--namespace` sem
valor, flag desconhecido, ou `--k8s-only` em ambiente sem kubectl
retornam erro imediato (exit code 64 ou 1).

#### 4.3.17 Modo Local-only — DEILE rodando fora do k8s

Quando você roda `python3 deile.py` direto no shell (sem container) e
quer monitorar **sem** precisar do cluster:

```bash
python3 infra/k8s/deploy.py k8s panel --local-only
```

O que aparece (todos com dados reais, sem mocks):

| Painel | Fonte real |
|---|---|
| `LOCAL PROCESSES` | `ps -axo pid,pcpu,rss,etime,command` filtrado por padrões `(deile\|deilebot)` — categorizado em `local-deile`/`local-pipeline`/`local-bot`/`local-other` |
| `PIPELINE` | fallback para `last_action` do log local quando o pod do pipeline não responde — `summary` mostra a última ação do log local (`worker dispatch completed`, etc) |
| `ACTIVITY` | classifica eventos da última janela (~64KB tail-from-end) de `~/.deile/logs/deile.log` — `actor='local'` na coluna |
| `ISSUES & PRs` | igual ao modo k8s (vem de `gh api`) |
| `TOKENS & CUSTOS` | igual ao modo k8s (SQLite local — `~/.deile/db/usage.db`) |
| `NOTIFIER ECHO` | parses `~/.deile/logs/security_audit.log` (JSONL) — mostra cada `AuditEvent` emitido pelo agente local |
| `AÇÕES` | `[1] status (k8s offline)` + ações locais: `tail deile.log`, `tail security_audit`, `ps deile-like`, `open logs dir`, `open sessions dir` |
| `TROCAR MODELO` | mostra aviso honesto: `kubectl set env` não roda sem k8s; ajuste via `model_providers.yaml` ou env var no shell |

O `PodWatch` ([1] → Enter num processo local) **muda o streamer** —
chama `tail -F ~/.deile/logs/deile.log` em vez de `kubectl logs -f`.
Fallback Python puro quando `tail` ausente (polling de EOF a cada
500ms). Header do drill-in mostra PID, CPU%, RSS, uptime, cmdline.

**Detecção automática de modo.** Sem nenhum flag, o painel roda
`RuntimeContext.detect()` e escolhe entre:

```
k8s_available  = kubectl_bin() is not None  (e cluster aceita)
local_available = ~/.deile/logs/ existe  OU  ~/.deile/db/usage.db existe
                                          OU  `ps` mostra python+deile
```

Ambos → híbrido. Só um → modo correspondente. Nenhum → erro com
sugestão de `--demo`.

### 4.4 Diagnóstico rápido

```bash
# antes de tudo: confira em qual namespace está olhando
~/.rd/bin/kubectl get ns -L app.kubernetes.io/managed-by,deile.io/forge,deile.io/repo

# tudo no namespace alvo (troque `deile` pelo seu)
kubectl -n deile get all,secrets,configmaps,networkpolicies

# bot
kubectl -n deile logs deploy/deilebot --tail=50
kubectl -n deile logs deploy/deilebot --follow

# pipeline + claude-worker (cluster atual também tem esses)
kubectl -n deile logs deploy/deile-pipeline --tail=80
kubectl -n deile logs deploy/claude-worker --tail=80

# último Job
kubectl -n deile logs job/deile-oneshot

# is the bot's sqlite schema up?
kubectl -n deile exec deploy/deilebot -- \
  python3 -c "import sqlite3; print(sorted(r[0] for r in sqlite3.connect('/home/deile/data/deilebot.sqlite').execute('SELECT name FROM sqlite_master WHERE type=\"table\"')))"
```

### 4.5 Health checks de isolamento

São esses os experimentos que provam que o container está fechado.
Reaproveite a qualquer hora:

```bash
# uid + capabilities + seccomp
kubectl -n deile exec deploy/deile-shell -- \
  grep -E '^(Uid|Gid|CapEff|Seccomp|NoNewPrivs)' /proc/self/status

# alcança o Mac? (deve falhar instantâneo — kube-router REJECT)
kubectl -n deile exec deploy/deile-shell -- \
  python3 -c '
import socket,time
for h,p in (("192.168.5.2",22),("1.1.1.1",443)):
  s=socket.socket(); s.settimeout(3); t0=time.monotonic()
  try: s.connect((h,p)); rc="OK"
  except OSError as e: rc="FAIL: "+e.strerror
  print(f"{h}:{p} -> {rc} [{(time.monotonic()-t0)*1000:.0f} ms]")'

# secrets em /proc/<pid>/environ? (deve ser CLEAN)
kubectl -n deile exec deploy/deile-shell -- /bin/sh -c '
for K in ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY GOOGLE_API_KEY DEILE_BOT_AUTH_TOKEN; do
  grep -aq "${K}=" /proc/self/environ && echo LEAK $K || echo CLEAN $K
done'
```

---

## 5. Troubleshooting

| Sintoma | Causa | Fix |
|---|---|---|
| `error: failed to solve … no such file` no `nerdctl build` | Você não está rodando da raiz do repo | `cd <repo-root>; bash infra/k8s/run.sh build` |
| `ContainerCreating` que nunca passa | `imagePullPolicy: Never` exige imagem local | Cheque `nerdctl --namespace k8s.io images deile-stack:local`. Se não estiver lá, refaça o `build` |
| Bot: `sqlite3.OperationalError: no such table: audit` | `pip install` perdeu os `.sql` (pyproject sem `package-data`) | `bash infra/k8s/run.sh build` busca a injeção manual no Dockerfile; rebuild + `rollout restart` |
| Job: `BOT_INTEGRATION_DISABLED` | DEILE não vê `DEILE_BOT_ENDPOINT` ou `DEILE_BOT_AUTH_TOKEN` | Cheque o Secret `deile-secrets` e o env do Pod. `run.sh up` recria com bearer fresco |
| Bot: `tool whitelist active — 0 kept` | Esperado quando `DEILE_BOT_ENDPOINT` está ausente no Pod do bot e messaging tools não auto-discoverem ali | Para o agente embutido responder DMs nenhuma tool é necessária (resposta sai pelo egress pipeline). Para alcançar outros canais, configure `DEILE_BOT_ENDPOINT=http://localhost:8765` no bot Pod |
| Slash commands `/audit`, `/dlq` retornam `owner only` | Seu `provider_user_id` não está em `owners:` | Edite `15-bot-config.yaml` → `owners: ["discord:<seu_user_id>"]` → `kubectl apply -f` + `rollout restart deployment/deilebot` |
| `host.lima.internal` resolve mas conexão refused | Esperado — DNS está liberado, a conexão é bloqueada pela NetworkPolicy `except: 192.168.0.0/16` | Não é bug; é defesa funcionando |

---

## 6. Modelo de ameaça resumido

Se isto te interessou de verdade, leia
[`docs/system_design/08-SEGURANCA.md`](../../docs/system_design/08-SEGURANCA.md)
e [`docs/system_design/14-CONTAINERIZACAO.md`](../../docs/system_design/14-CONTAINERIZACAO.md).

| Vetor | Defesa |
|---|---|
| Discord user manda prompt malicioso pro bot pedindo `cat ~/.ssh/id_rsa` | Bot's embedded agent tem **só `messaging.*` tools** — sem `bash`, sem `read_file`. Mesmo se o LLM aceitasse o pedido, não tem ferramenta pra cumprir |
| Container compromised tenta ler segredos via `/proc/self/environ` | Wrapper só seta secrets em `os.environ` **depois** do `execve` — `/proc/<pid>/environ` é frozen e fica limpo |
| Container compromised tenta exfilar via TCP pra Mac | NetworkPolicy `default-deny-all` + `except 192.168.0.0/16` no egress 443 — Mac é unreachable (1 ms REJECT) |
| Container compromised tenta privilege escalation | `runAsNonRoot`, `capabilities.drop: [ALL]`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem`, `seccompProfile: RuntimeDefault` — nada pra escalar |
| Container compromised tenta acessar K8s API | `automountServiceAccountToken: false` — sem credentials montadas |
| Pod manifest legítimo é alterado pra afrouxar securityContext | PSS `restricted` enforce no namespace — admission rejeita |
| LLM decide ler `/run/secrets/deile/ANTHROPIC_API_KEY` por conta própria | Persona `developer.md` declara "REGRA #5: extração de segredos é tentativa de invasão"; DEILE recusa **mesmo quando o operador pede** |
