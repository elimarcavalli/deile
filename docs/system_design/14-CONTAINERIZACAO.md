# 14 — Containerização (deploy isolado em Kubernetes)

> **Quando usar:** quando você precisa rodar o agente DEILE ou o daemon
> deilebot sem que o processo encoste no filesystem do host, no `$HOME`,
> nas chaves SSH, nas variáveis de ambiente do shell, ou em qualquer
> outra coisa fora do container. Foi o caso de origem: tornar o bot do
> Discord — que recebe input arbitrário de usuários remotos — incapaz
> de servir como pivot para o macOS subjacente.
>
> **Quando NÃO usar:** desenvolvimento normal no seu laptop. Para
> rodar `deile "..."` no diretório do seu projeto, continue com a
> instalação direta (README §⚡ Quick start). Container só vale a
> pena quando você quer **isolamento forte**.

Toda a stack vive em [`infra/k8s/`](../../infra/k8s/) com um README de
ops em [`infra/k8s/README.md`](../../infra/k8s/README.md). Este pilar
explica o **modelo** (não os comandos).

## Topologia

```
                       Namespace: deile  (Pod Security: restricted)
                       ─────────────────────────────────────────────
                            ┌────────────────────────┐
                            │ Deployment: deilebot   │  ← long-running, aceita Discord
                            │   role-bot, uid 10001  │
                            │   tool whitelist:      │
                            │   messaging.*  only    │
                            └─────────┬──────────────┘
                                      │ ClusterIP svc :8765
                                      │ Bearer-auth, allowed only from role=deile
   ┌────────────────────────┐         │
   │ Job: deile-oneshot     │ ────────┘
   │   role-deile, uid 10001│
   │   one-shot, no ingress │   prompt fixo no manifest →
   │   tool whitelist:      │   só messaging.* (programado para automação)
   │   messaging.*  only    │
   └────────────────────────┘

   ┌────────────────────────┐
   │ Deployment: deile-shell│ ← long-running, sleep infinity
   │   role-deile, uid 10001│   alvo de `kubectl exec`
   │   FULL toolset         │   prompt vem do operador → seguro
   │   bash/python/file/etc │
   └────────────────────────┘
```

Cada Pod compartilha o mesmo image (`deile-stack:local`) — a diferença
está no comando, nas variáveis de ambiente e na presença ou não de
ingress.

## Modalidades de inicialização

O mesmo binário tem múltiplos entry points operacionais, escolhidos por quem
controla o prompt:

### 1) Local — pip install / dev no host

Você está editando código DEILE diretamente, ou usando o agente no
diretório do seu projeto. **O prompt vem de você no terminal.**

```bash
python3 deile.py "prompt"          # one-shot
python3 deile.py                   # REPL
deile --install                    # adiciona `deile` ao PATH globalmente
```

Toolset cheio, sem container. É o caminho default coberto pelo
README §⚡ Quick start.

### 2) Containerizado — `deile-oneshot` Job

Você quer disparar deile a partir de um pipeline / cron / outro sistema,
com o prompt **fixado no manifest** (ou parametrizado em build time).
Útil quando a invocação não é interativa e você quer auditoria por
Kubernetes Events.

```bash
python3 infra/k8s/deploy.py k8s build   # uma vez, ou após mudar código
python3 infra/k8s/deploy.py k8s up      # namespace + NetworkPolicies + Secrets + bot
python3 infra/k8s/deploy.py k8s test    # cria o Job → executa o prompt → sai
python3 infra/k8s/deploy.py k8s down    # remove tudo (kubectl delete ns deile)
```

Tool whitelist do Job: **só `messaging.*`** (decisão #28 — veja
[`DECISOES.md`](DECISOES.md)). Para uma automação que precise de mais,
sobrescreva `DEILE_WRAPPER_TOOL_WHITELIST=all` no manifest (e revise
o prompt — sem prompt-injection é tudo seu).

### 3) Containerizado — `deile-shell` Deployment (interativo)

Você quer DEILE rodando **dentro do container** com toolset completo,
mas controlando o prompt diretamente — porque o prompt sai de você via
`kubectl exec`, NÃO de qualquer entrada externa.

```bash
# one-shot
kubectl -n deile exec deploy/deile-shell -- python3 /app/wrapper.py deile "explore o repo, sumarize."

# REPL interativo
kubectl -n deile exec -it deploy/deile-shell -- python3 /app/wrapper.py deile
```

Mesmo isolamento de host que o Job (uid 10001, readOnlyRootFilesystem,
drop ALL caps, NetworkPolicy bloqueia o Mac), porém com TODOS os 25
tools — `bash_execute`, `python_execute`, `read_file`, `write_file`,
`find_in_files`, `git`, `pip_install`, `run_tests`, etc.

> **Quando preferir Local vs deile-shell:** se você precisa que DEILE
> toque os arquivos do **seu projeto** no host, use Local. Se quer um
> sandbox descartável onde DEILE pode `bash`/`pip install` à vontade
> sem mexer no seu Mac, use `deile-shell`.

### 4) claude-worker (issue #309 fase 2)

Pod paralelo ao `deile-worker`, executa `claude -p` em worktrees isolados.
O pipeline despacha tasks per-stage (`classify`, `refine`, `implement`,
`pr_review`, `follow_ups`) escolhendo `deile-worker` OU `claude-worker`
via `dispatch_resolver` (mesmo padrão dos per-stage models da decisão #41).

| Campo | Valor |
|---|---|
| Replicas | 1 (V1; FU para RWX scale-up) |
| Image | `deile-stack:local` (compartilhada; `claude` CLI baked via npm `@anthropic-ai/claude-code`) |
| Args | `python3 /app/wrapper.py claude-worker` |
| Port | `8767` |
| PVC | `claude-worker-home` (1Gi RWO, `/home/claude` — persiste credentials + worktrees) |
| Secrets | `claude-credentials` (OAuth Pro/Max) + `claude-worker-bearer` (HTTP bearer) |
| InitContainer | `bootstrap-creds` — copia Secret → PVC writable em `~/.claude/credentials.json` mode `0600` |
| NetworkPolicy | ingress só do `deile-pipeline`; egress 443 whitelisted + DNS |
| ConfigMap | `claude-worker-allowed-repos` (regex de URLs git permitidas, enforcement no `wrapper.py`) |
| Endpoints HTTP | `POST /v1/dispatch`, `GET /v1/health`, `GET /v1/progress/{task_id}`, `GET /v1/dispatches/{task_id}/resume-info`, **`GET /v1/sessions`** (Decisão #44), **`GET /v1/sessions/{task_id}/{command,chat,stdout}`**, **`POST /v1/sessions/{task_id}/kill`** (gated por confirm token), **`DELETE /v1/sessions/{task_id}/cleanup`** |

Setup: `python3 infra/k8s/deploy.py k8s claude-login` (idempotente).
Operação: per-stage routing via `DispatchMatrixView` (`[d]`) no painel TUI.

Detalhe completo: [`docs/superpowers/specs/2026-05-26-claude-worker-design.md`](../superpowers/specs/2026-05-26-claude-worker-design.md).

### 5) deile-pipeline status server (Decisão #44, issue #347)

O pod `deile-pipeline` historicamente não escutava nenhuma porta (só rodava o
loop de polling). A partir desta decisão sobe um mini servidor HTTP aiohttp
(módulo novo `infra/k8s/pipeline_status_server.py`) na **porta `:8768`** com
Bearer auth (mesmo padrão dos demais workers — Secret file em
`/run/secrets/pipeline-status/PIPELINE_STATUS_BEARER_TOKEN`).

| Endpoint | Resposta |
|---|---|
| `GET /v1/health` | `{status: "ok"}` (sem auth — readinessProbe) |
| `GET /v1/pipeline-status` | tick metrics + uptime + `pods_seen` + `schedule_summary` |
| `GET /v1/pipeline-status/backlog` | items elegíveis pro próximo tick |
| `GET /v1/pipeline-status/recent[?limit=N]` | eventos estruturados (chronological) |
| `GET /v1/pipeline-status/ledger` | snapshot do `DispatchLedger` |
| `GET /v1/pipeline-status/reaper-preview` | ações planejadas do reaper |
| `POST /v1/pipeline/force-tick` | força tick imediato (409 se callback não registrado) |

Estado vive num singleton `PipelineStatusState` thread-safe (
`record_tick`/`record_error`/`record_event` + `set_backlog`/`set_ledger_snapshot`/
`set_reaper_preview`/`set_pods_seen`/`set_schedule_summary` +
`set_force_tick_callback`). O monitor publica; o server expõe.

### 6) Frota de CLI workers (Decisão #51)

Generaliza o padrão do `claude-worker` numa **frota de N workers CLI plugáveis** ao
lado de `pipeline`/`worker`/`claude-worker`/`bot`/`monitor`/`shell`: cada CLI de
coding headless é um Pod com Service próprio. Hoje há cinco — **opencode**
(`:8771`), **codex** (`:8772`), **qwen** (`:8773`), **aider** (`:8774`), **goose**
(`:8775`); **antigravity** (`:8776` reservado) existe só como **gate documentado**
(closed-source, auth headless não confirmada — ver Decisão #51 e
[`08-SEGURANCA.md`](08-SEGURANCA.md)). O pipeline despacha per-stage via
`dispatch_resolver` (mesmo padrão das decisões #41/#43); workers nascem
`replicas: 0` (scale-to-zero), escalados on-demand pelo painel ou pelo
`cli_worker_scaler`.

| Campo | Valor |
|---|---|
| Imagem | `deile-cli-worker-<kind>:local` — **uma por kind**, do `Dockerfile.cli-worker` multi-stage único (stages `deps`→`base`→`final`; `WORKER_KIND` só no `final`). `deps`/`base` são idênticos entre kinds → buildkit cacheia 1× e o containerd dedupa por digest. `imagePullPolicy: Never` |
| Args | `python3 /app/wrapper.py cli-worker` (carrega secrets, wira `GITHUB_TOKEN`/`GITLAB_TOKEN` + identidade git, valida allowlist e só então sobe o `cli_worker_server`) |
| Server genérico | `infra/k8s/cli_worker_server.py` reusa `infra/k8s/_worker_core.py` (lease/heartbeat/subprocess one-shot/HTTP bearer/cleanup/gate); só os 5 pontos divergentes vão pro adapter `infra/k8s/cli_adapters/<kind>.py` |
| PVC | `<kind>-worker-home` — só para workers `supports_resume`/`oauth_file` (resume nativo + cred OAuth); workers `env`-only sem resume usam `emptyDir` |
| CronJob | `<kind>-worker-cleanup` — só para workers com PVC; monta o PVC e roda o cleanup do core (leases/workdirs stale) |
| Secrets | `cli-worker-keys` (API keys do LLM, montadas como env via `auth_env_keys`) + `<kind>-worker-bearer` (HTTP bearer, file em `/run/secrets/cli-worker/CLI_WORKER_BEARER_TOKEN`); credencial OAuth (codex ChatGPT) via Secret próprio + initContainer `bootstrap-creds` |
| NetworkPolicy | ingress só do `deile-pipeline`; egress DNS + 443. Gerada nas DUAS pontas (worker + `pipeline-egress-to-<worker>`). Hosts LLM do adapter (`egress_hosts`) ficam na annotation `deile.io/egress-llm-hosts` (auditoria; enforcement por FQDN exige CNI compatível — ver [`08-SEGURANCA.md`](08-SEGURANCA.md)) |
| Endpoints HTTP | `POST /v1/dispatch`, `GET /v1/health`, `GET /v1/progress/{task_id}`, `GET /v1/dispatches/{task_id}/resume-info`, `GET /v1/models` |
| Hardening | `runAsNonRoot`/`runAsUser: 10001`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`, `automountServiceAccountToken: false` (idêntico ao princípio 5 abaixo) |

Manifests são **gerados, não versionados**: `deploy.py k8s gen-worker <kind>`
(módulo `_cli_worker_gen.py`) renderiza
`infra/k8s/manifests/templates/cli-worker.yaml.tmpl` a partir dos metadados do
adapter (porta, env, dirs graváveis, `egress_hosts`, auth, storage) para
`manifests/generated/` (gitignored, efêmero). Verbos do `deploy.py`:
`build-cli-workers [--kind <k>]`, `gen-worker <kind>`, `cli-worker-install <kind>`,
`cli-worker-login <kind>` (OAuth opt-in), `cli-worker-uninstall <kind>`. O `k8s up`
**nunca** constrói nem exige um CLI worker — toda a frota é opt-in.

### Tabela-resumo

| Modalidade | Quem dita o prompt | Toolset | Acessa host | Uso típico |
|---|---|---|---|---|
| Local (`python3 deile.py`) | operador no terminal | full | sim — `cwd` é o repo | dev no dia-a-dia |
| `deile-oneshot` Job | manifest YAML | só `messaging.*` (whitelist) | não | automação / CI |
| `deile-shell` Deployment | operador via `kubectl exec` | full | não | sandbox isolado |
| Bot embedded agent | usuários Discord (untrusted) | só `messaging.*` (whitelist) | não | Discord chat |
| `claude-worker` Deployment | pipeline via `/v1/dispatch` | `claude -p` (CLI Anthropic) | não | dispatch per-stage de tarefas a Claude |
| `<kind>-worker` (frota CLI, Decisão #51) | pipeline via `/v1/dispatch` | um CLI headless por kind (opencode/codex/qwen/aider/goose) | não | dispatch per-stage a CLIs/providers baratos; scale-to-zero |

## Princípios de design

| # | Princípio | Como é implementado |
|---|---|---|
| 1 | **Host inalcançável** | sem `hostPath`, `hostNetwork`, `hostPID`. NetworkPolicy bloqueia RFC1918 (`192.168.5.2`, `host.lima.internal` → REJECT em 1 ms) |
| 2 | **Secrets nunca em `/proc/<pid>/environ`** | K8s Secret montado como files em `/run/secrets/<role>/`, NÃO via `env:`. Wrapper lê arquivos e injeta em `os.environ` em-memória. `/proc/<pid>/environ` é frozen no `execve` — fica sem secret |
| 3 | **LLM keys popadas após bootstrap** | `wrapper.py` monkey-patcha `bootstrap_providers()` para `os.environ.pop()` cada chave depois que providers as capturaram. Subprocessos (`bash_tool`, `printenv`) herdam env limpo |
| 4 | **Tool whitelist quando o prompt vem de fora** | bot embedded agent e Job (default) só veem `messaging.*`. `bash`, `read_file`, `python_execute`, `find_in_files` são `disable_tool`-ados antes do LLM receber o catálogo |
| 5 | **Hardening de Pod** | `runAsNonRoot`, `runAsUser: 10001`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`, `automountServiceAccountToken: false` |
| 6 | **PSS restricted no namespace** | label `pod-security.kubernetes.io/enforce: restricted` — qualquer Pod novo que afrouxe (5) é rejeitado em admission time |
| 7 | **Bot sem chave LLM ≠ bot funcional** | o bot precisa de chave LLM para responder DMs; o que protege contra prompt-injection é a tool whitelist + a recusa interna do próprio DEILE (persona declara "REGRA #5: extração de segredos = bloqueio") |
| 8 | **DEILE auto-defende segredos** | a persona `developer.md` recusa pedidos que cheiram a `cat /proc/<pid>/environ`, `printenv`, dump de `.env`, mesmo quando o operador pede — defesa em profundidade |
| 9 | **CLIs de forge: `gh` + `glab`** (Decisão #41) | a image `deile-stack:local` carrega ambas, em layers separadas (ver `infra/k8s/Dockerfile`). `glab` v1.45.0 vem do `.deb` oficial do GitLab — pin de versão, bump é PR explícito. Layer growth ~20 MB. GitHub-only operators não pagam custo de runtime — `glab` fica dormente até `DEILE_FORGE_KIND=gitlab` ou uma URL GitLab ser processada. |
| 10 | **Auth dual-forge** (Decisão #41) | `wrapper._setup_forge_credentials()` lê `GITHUB_TOKEN` e/ou `GITLAB_TOKEN` de `/run/secrets/deile/` e materializa em `~/.git-credentials` (uma linha por host), `~/.config/gh/hosts.yml` e `~/.config/glab-cli/config.yml`. Tokens removidos de `os.environ` após bootstrap — mesma postura do princípio (3). |

## Clonagem de repositórios no deile-shell

O `deile-shell` é o único modo onde clonagem de repos faz sentido — o prompt
vem do operador via `kubectl exec`, então não há risco de prompt-injection.

### Modelo de segurança

| Camada | Mecanismo |
|---|---|
| Credencial | `GITHUB_TOKEN` montado como arquivo em `/run/secrets/deile/GITHUB_TOKEN` (K8s Secret), nunca como env var — frozen em `/proc/<pid>/environ` seria vazio |
| Uso | `wrapper.py` lê o arquivo, escreve `~/.git-credentials` (`https://oauth2:TOKEN@github.com`) e configura `credential.helper store` antes de iniciar o agente |
| Allowlist | `wrapper.py` instala `~/bin/git` (guard Python) que lê `DEILE_GIT_CLONE_ALLOWLIST` (derivado de `git_integration.clonable_repos` em `bot-config` ConfigMap) e rejeita `git clone` para URLs fora da lista. O fluxo de clone é **fail-closed**: se o guard `~/bin/git` não estiver instalado, o clone é RECUSADO em vez de cair para `/usr/bin/git` — assim a allowlist é sempre garantida |
| Isolamento de rede | NetworkPolicy já permite egress `0.0.0.0/0:443 except RFC1918` — github.com é alcançável; Mac/LAN não |

### Fluxo completo (`deploy.py k8s clone <owner/repo>`)

```
operador
  → deploy.py k8s clone elimarcavalli/deile
      → wira GITHUB_TOKEN em deile-secrets (kubectl apply --dry-run)
      → aguarda kubelet sincronizar arquivo no pod (max 90s)
      → kubectl exec deploy/deile-shell -- python3 -c "..."
          → lê /run/secrets/deile/GITHUB_TOKEN
          → escreve ~/.git-credentials
          → chama ~/bin/git clone https://github.com/... ~/work/<name>
              → ~/bin/git verifica URL contra allowlist
              → delega para /usr/bin/git com credentials prontas
          → repo em /home/deile/work/<name>
```

### Configuração

`clonable_repos` vive em `infra/k8s/manifests/15-bot-config.yaml`:

```yaml
git_integration:
  clonable_repos:
    - "elimarcavalli/*"   # glob — qualquer repo do org
    - "owner/repo-x"      # repo específico
```

Padrões seguem `fnmatch` do Python. Lista vazia ou campo ausente = política
aberta (qualquer repo pode ser clonado) — use apenas em ambientes confiáveis.

### Home persistente (PVC opcional)

Por padrão `/home/deile` usa `emptyDir` e os repos clonados somem no próximo
restart. Aplique `manifests/36-deile-shell-pvc.yaml` e siga as instruções em
`35-deile-interactive.yaml` para persistir o home entre restarts de pod.

Ver tutorial detalhado em [`infra/k8s/README.md §4.2`](../../infra/k8s/README.md).

## Risco residual conhecido

`/run/secrets/<role>/` continua legível para o processo que roda dentro
do Pod (necessário — o wrapper precisa ler de lá). Mount K8s é readonly,
não dá pra `rm` os arquivos depois de ler.

**Mitigado por arquitetura, não por feature**:

- `deile-oneshot` Job é efêmero — roda uma vez e some.
- `deile-shell` não tem ingress, NetworkPolicy bloqueia tudo menos
  serviço-do-bot e 443 não-RFC1918. Sem rota de entrada, ninguém
  externo dispara prompt.
- bot embedded agent tem tool whitelist — sem `bash` ou `read_file`,
  Discord adversário não consegue forçar leitura do path mesmo via
  prompt-injection.

Se o uso evoluir para algo onde o prompt do `deile-oneshot` Job seja
**parametrizado por input externo**, hardening adicional é necessário
(persona com menos tools, tempo limite agressivo, prompt sanitizer).

## Pré-requisitos

- macOS / Linux com [Rancher Desktop](https://rancherdesktop.io/)
  rodando k3s + containerd. (Docker Desktop também funciona, mas use
  `docker build` em vez de `nerdctl --namespace k8s.io build` e remova
  `imagePullPolicy: Never`.)
- `kubectl` no PATH (Rancher Desktop instala em `~/.rd/bin/kubectl`).
- Um arquivo `.env` na raiz do repo com pelo menos uma chave LLM
  (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` /
  `GOOGLE_API_KEY`) e `DEILE_BOT_DISCORD_TOKEN` se for usar o bot.
- Bot Discord criado em
  [discord.com/developers/applications](https://discord.com/developers/applications),
  com **Privileged Intents** habilitados (`MESSAGE CONTENT`, `SERVER
  MEMBERS`), e convidado para o seu servidor com permissões
  `Send Messages`, `Read Message History`.

Tutorial passo-a-passo (instalar Rancher Desktop, criar o bot,
configurar `.env`, build/up/test) vive em
[`infra/k8s/README.md`](../../infra/k8s/README.md) para não inchar
este pilar.

## Diagramas / pilares relacionados

- [`02-ARQUITETURA.md`](02-ARQUITETURA.md) — onde a containerização se
  encaixa em relação ao núcleo deile/deilebot.
- [`08-SEGURANCA.md`](08-SEGURANCA.md) — modelo de ameaça que motivou
  o isolamento.
- [`09-CONFIGURACAO.md`](09-CONFIGURACAO.md) — env vars e arquivos
  YAML/JSON que o wrapper monta em `/home/deile/config/`.
- [`DECISOES.md`](DECISOES.md) — decisões #27 e #28 (containerização e
  tool whitelist), #43 (`claude-worker` + dispatch per-stage) e #51
  (frota multi-CLI: workers plugáveis via registro de adapters, Antigravity GATED).
