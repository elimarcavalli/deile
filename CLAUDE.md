# DEILE — Claude Code Context

## Knowledge base — START HERE

The authoritative project knowledge lives in `docs/system_design/`. The single index/table-of-contents is `docs/system_design/00-VISAO-GERAL.md` — open it first to navigate. The three documents auto-loaded into your context via the `@`-imports below are the minimum set you should always have on hand:

- `docs/system_design/00-VISAO-GERAL.md` — pillars index, single source of truth for counts, decisions table.
- `docs/system_design/03-PRINCIPIOS-ARQUITETURAIS.md` — non-negotiable rules with a fast trigger index.
- `docs/system_design/12-PADROES-CODIGO.md` — concrete templates for tools, commands, parsers, memory, security, tests.

@docs/system_design/00-VISAO-GERAL.md
@docs/system_design/03-PRINCIPIOS-ARQUITETURAIS.md
@docs/system_design/12-PADROES-CODIGO.md

The remaining pillar docs are **read-on-demand**. Open them with the `Read` tool only when the situation demands; never preemptively.

## Mandatory protocol (run before every non-trivial turn)

Before the first `Write`, `Edit`, or mutating `Bash` of each turn:

1. Classify the **action** you are about to perform → consult the trigger index in `03-PRINCIPIOS-ARQUITETURAIS.md`.
2. Classify the **target file path(s)** → match against the subpackage map in `02-ARQUITETURA.md`.
3. Match the **user's keywords** → architecture / scope / capability terms point you to the relevant pillar (see `00-VISAO-GERAL.md`).
4. **Take the union** of pillars implied by the three checks above. `Read` every unread document in that union **before** the first mutation.
5. If the scope grows mid-task, **stop and re-run the protocol** with the expanded scope.

Exemptions: typos, whitespace, single-line cosmetic edits, renaming a strictly-local variable, non-architectural read-only questions, running tests/lint/formatters, editing `.env` or lockfiles, editing `CLAUDE.md` or files under `docs/system_design/`. When uncertain, the action is **not** exempt — run the protocol.

## Pillar map

| # | Pillar | Document |
|---|---|---|
| 0 | Index / counts / decisions table | `docs/system_design/00-VISAO-GERAL.md` |
| 1 | Capabilities | `docs/system_design/01-CAPACIDADES.md` |
| 2 | Architecture | `docs/system_design/02-ARQUITETURA.md` |
| 3 | Architectural principles | `docs/system_design/03-PRINCIPIOS-ARQUITETURAIS.md` |
| 4 | Component model (registries) | `docs/system_design/04-MODELO-COMPONENTES.md` |
| 5 | Execution flow | `docs/system_design/05-FLUXO-EXECUCAO.md` |
| 6 | Memory (4 layers) | `docs/system_design/06-MEMORIA.md` |
| 7 | LLM integrations | `docs/system_design/07-INTEGRACOES-LLM.md` |
| 8 | Security | `docs/system_design/08-SEGURANCA.md` |
| 9 | Configuration | `docs/system_design/09-CONFIGURACAO.md` |
| 10 | Diagrams | `docs/system_design/10-DIAGRAMAS.md` |
| 11 | Development workflow | `docs/system_design/11-WORKFLOW-DESENVOLVIMENTO.md` |
| 12 | Code patterns | `docs/system_design/12-PADROES-CODIGO.md` |
| 13 | Documentation template | `docs/system_design/13-PADRAO-DOCUMENTACAO.md` |
| 14 | Containerization (K8s) | `docs/system_design/14-CONTAINERIZACAO.md` |
| — | Decision records | `docs/system_design/DECISOES.md` |

## Operational quick reference

Entry point: `python3 deile.py` (CLI shell in `DeileAgentCLI`; all logic lives in the `deile/` package).

| Task | Command |
|---|---|
| Run agent | `python3 deile.py` |
| Run tests | `python3 -m pytest deile/tests/ -q 2>&1 \| tail -5` — shows only the final summary line; add `-v` only when debugging a specific failure |
| Single test | `python3 -m pytest deile/tests/path/to/test_x.py -v` |
| Coverage | auto-runs with `pytest`; fails under 80% (`--cov-fail-under=80`) |
| Lint | `ruff check deile/` |
| Imports | `isort --check-only deile/` |
| Complexity | `radon cc deile/ -a` |

## Kubernetes / cluster operations

The cluster runs on **Rancher Desktop (k3s/containerd)** with the single image **`deile-stack:local`** (`imagePullPolicy: Never`). All five pods (`deile-pipeline`, `claude-worker`, `deile-worker`, `deilebot`, `deile-shell`) share that image; `/app` is **baked at build time** (not mounted), so **code changes only go live after a rebuild + pod restart**. `kubectl` lives at `~/.rd/bin/kubectl` (may not be on `PATH`).

### Multi-namespace — DEILE supports many concurrent stacks

DEILE can run **multiple independent stacks side-by-side**, one per namespace. Each namespace gets its own pipeline + workers + bot + shell, with its own Secrets, ConfigMaps, PVCs and forge config. The default namespace is `deile`; create others via `k8s create-namespace`.

**ALWAYS check the namespace landscape first before any cluster op:**

```bash
K=~/.rd/bin/kubectl
$K get ns -L app.kubernetes.io/managed-by,deile.io/forge,deile.io/repo
```

Current namespaces (verify before assuming — drift is real):

| Namespace | Forge | Repo | Status | Notes |
|---|---|---|---|---|
| `deile` | GitHub | `elimarcavalli/deile` | **prod, all running** | default; the production stack |
| `deile-gl` | GitLab | (pilot) | scaled to 0 (paused) | issue #297 multi-forge pilot; `start` to resume |
| `default` | — | — | **must stay empty of DEILE** | k8s built-in. If you see `deile-*` resources here, they leaked from a manifest applied without `-n <ns>` — clean up |
| `kube-system`, `kube-public`, `kube-node-lease` | — | — | k3s internal | never touch |

**Hard rule:** every `kubectl` command MUST carry `-n <ns>`. Every `deploy.py` k8s command MUST receive `--namespace <ns>` (or rely on the default `deile`). Forgetting the namespace flag on `kubectl apply -f manifest.yaml` puts the resource in `default` — that's how the `default` namespace gets polluted.

### Orchestrator: `infra/k8s/deploy.py`

Run from repo root. Prints a plan before any mutating action; `--yes` skips the prompt, `--dry-run` shows the plan only. **Global flag:** `-n <ns>` / `--namespace <ns>` selects the target namespace for every k8s verb (default: `deile`).

| Goal | Command |
|---|---|
| Interactive menu / list all verbs | `python3 infra/k8s/deploy.py` / `... help` |
| **Rebuild image + restart pods** (deploy code changes) | `python3 infra/k8s/deploy.py k8s build --restart --yes` |
| Provision / update the whole stack (idempotent) | `python3 infra/k8s/deploy.py k8s up` |
| Create a brand-new namespace from scratch (interactive) | `python3 infra/k8s/deploy.py k8s create-namespace` |
| Scale workers up/down (`--worker N --claude-worker M`) | `python3 infra/k8s/deploy.py k8s scale --worker 2` |
| Rollout restart (no rebuild) | `python3 infra/k8s/deploy.py k8s restart` |
| Pause / resume (scale 0 / 1; keeps data + Secrets) | `... k8s stop` / `... k8s start` |
| Status (pods, deployments, services) | `python3 infra/k8s/deploy.py k8s status` |
| Live TUI cockpit | `python3 infra/k8s/deploy.py k8s panel` |
| Logs (bot, worker, pipeline, claude-worker) | `python3 infra/k8s/deploy.py k8s logs [bot\|worker\|pipeline\|claude-worker]` |
| One-shot Job (fixed prompt) | `python3 infra/k8s/deploy.py k8s test` |
| Clone a repo into `deile-shell` (allowlisted) | `python3 infra/k8s/deploy.py k8s clone <owner/repo>` |
| Bootstrap claude-worker OAuth (full) | `python3 infra/k8s/deploy.py k8s claude-login [--switch \| --no-interactive]` |
| Renew claude-worker OAuth token (lightweight refresh, no full bootstrap) | `python3 infra/k8s/deploy.py k8s claude-renew` |
| **Teardown** (DELETES the target namespace + all data) | `python3 infra/k8s/deploy.py k8s down` |

Examples targeting a non-default namespace:

```bash
python3 infra/k8s/deploy.py -n deile-gl k8s status
python3 infra/k8s/deploy.py -n deile-gl k8s start          # resume the GitLab pilot
python3 infra/k8s/deploy.py -n deile-gl k8s logs pipeline
python3 infra/k8s/deploy.py -n deile-staging k8s up        # spin up a fresh staging stack
```

### Direct `kubectl` (when you need a specific pod)

```bash
K=~/.rd/bin/kubectl
$K -n deile get pods,deployments,services                  # always pass -n
$K -n deile logs deploy/deile-pipeline --tail=80           # or deile-worker, deilebot, claude-worker
$K -n deile rollout status deployment/deile-pipeline --timeout=180s
$K -n deile exec -it deploy/deile-shell -- python3 /app/wrapper.py deile   # interactive REPL in-cluster
```

### Roles & ports

| Pod | Port | Role |
|---|---|---|
| `deilebot` | `:8765` | Discord (and other channels) I/O bridge |
| `deile-worker` | `:8766` | runs DEILE Python in-process; HTTP dispatch target for the pipeline |
| `claude-worker` | `:8767` | runs `claude -p` subprocess in isolated worktrees; OAuth credentials live in PVC `claude-worker-home` |
| `deile-pipeline-status` | `:8768` | status server (in-process aiohttp inside `deile-pipeline`); read-only telemetry for the panel |
| `deile-pipeline` | — | the forge monitor — no Service for inbound, only "calls out" (dispatches to workers, talks to forge) |
| `deile-shell` | — | `kubectl exec`-only sandbox; full toolset; prompt comes from the human via `kubectl exec` |

**Only `deile-pipeline` runs the autonomous monitor**; the others never autostart it.

## Variáveis de ambiente — onde mora o quê (mapa completo)

A configuração do DEILE vive em **5 lugares distintos** que coexistem. Saber qual usar para o quê é metade do trabalho de operação.

### Os 5 lugares onde a config mora

| Lugar | Para quê serve | Quando muda |
|---|---|---|
| **`.env` (raiz do repo)** | Segredos do operador (tokens API, OAuth) + overrides locais. Lido pelo `deploy.py k8s up` e pelo `python3 deile.py` local. | Cada operador edita o seu — nunca vai pro git (`.dockerignore` + `.gitignore`). |
| **K8s Secrets** (`bot-secrets`, `deile-secrets`, `worker-bearer`, `claude-worker-bearer`, `pipeline-status-bearer`, `claude-credentials`) | Espelho dos segredos do `.env` dentro do cluster. Montados nos Pods como arquivos em `/run/secrets/<role>/`. | Criados/atualizados por `k8s up` (a maioria) ou `k8s claude-login` (OAuth do claude). |
| **K8s ConfigMaps** (`bot-config`, `deile-runtime-config`, `claude-worker-allowed-repos`) | Config NÃO-secreta: owners do bot, runtime tunables, allowlist de repos. | Editar o YAML do manifest + `kubectl apply`. |
| **Manifests env vars** (blocos `env:` em `infra/k8s/manifests/*-deployment.yaml`) | Hardcoded por Pod: portas, paths, autostart flags, whitelists. | PR no repo — só muda quando muda a arquitetura. |
| **`~/.deile/settings.json` (layered)** | Configs migráveis de runtime — alvo de **muitas** vars marcadas `[DEPRECATED → settings.json]` no `.env.example`. Layers: system/user/project. | Via `/settings set <chave> <valor>` no CLI, ou edição direta do JSON. |

### Categorias das ~95 variáveis (inventário macro)

> **A referência canônica e completa de cada variável (descrição, default, formato) é o [`.env.example`](.env.example)** (435 linhas, agrupado em 11 seções). Esta tabela aqui é só o mapa.

| Categoria | Exemplos | Quem consome | Onde se configura |
|---|---|---|---|
| **LLM providers** | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `GOOGLE_API_KEY` | todos os Pods que rodam LLM (pipeline/worker/bot/shell — pelo menos UMA é obrigatória) | `.env` → `bot-secrets` + `deile-secrets` |
| **Forges** | `GITHUB_TOKEN`, `GITLAB_TOKEN`/`GL_TOKEN`, `DEILE_FORGE_KIND`, `DEILE_FORGE_REPO`, `DEILE_GITHUB_HOST`, `DEILE_GITLAB_HOST` | pipeline + worker + claude-worker (forge agnostic via #297) | `.env` → `deile-secrets` (tokens); manifests/settings.json (hosts/repo) |
| **Discord bot** | `DEILE_BOT_DISCORD_TOKEN`, `DEILE_BOT_ENDPOINT`, `DEILE_BOT_AUTH_TOKEN`, `DEILE_BOT_DISABLED` | só `deilebot` e quem fala com ele (worker/pipeline para notificar) | `.env` → `bot-secrets` + `deile-secrets` (bearer compartilhado) |
| **Owners do bot** | (não é env — `owners: ["discord:<snowflake>"]` em YAML) | `deilebot` | ConfigMap `infra/k8s/manifests/15-bot-config.yaml` |
| **deile-worker** | `DEILE_WORKER_BEARER_TOKEN`, `DEILE_WORKER_ENDPOINT`, `DEILE_WORKER_TASK_TIMEOUT_S` (2h default), `DEILE_WORKER_HOST/PORT`, `DEILE_WORKER_ROOT` | pipeline (dispatch) + worker (servidor) | `.env` (bearer) → Secret `worker-bearer`; resto em manifest 45 |
| **claude-worker** | `DEILE_CLAUDE_WORKER_AUTH_TOKEN`, `DEILE_CLAUDE_WORKER_ENDPOINT`, `DEILE_CLAUDE_WORKER_TASK_TIMEOUT_S`, `DEILE_CLAUDE_RESUME_TOKEN_BUDGET` (500k) | pipeline (dispatch) + claude-worker (servidor) | Secret `claude-worker-bearer` (populado por `k8s claude-login`); resto em manifest 50 |
| **claude OAuth** | `CLAUDE_OAUTH_ACCESS_TOKEN`, conteúdo de `~/.claude/credentials.json` | só `claude-worker` | Secret `claude-credentials` (criado por `k8s claude-login`) |
| **Pipeline (monitor)** | `DEILE_PIPELINE_AUTOSTART`, `DEILE_PIPELINE_POLL_INTERVAL`, `DEILE_PIPELINE_REPO`, `DEILE_PIPELINE_SHARD_INDEX/COUNT` | só `deile-pipeline` | manifest 46 + DEPRECATED → settings.json |
| **Pipeline resume** | `DEILE_PIPELINE_RESUME_ENABLED/INTERVAL/MAX_ATTEMPTS/BUDGET` (issue #254) | pipeline | DEPRECATED → settings.json (`pipeline.resume_*`) |
| **Dispatch routing** | `DEILE_PIPELINE_DISPATCH_MODE` (global) + `_CLASSIFY/REFINE/IMPLEMENT/PR_REVIEW/FOLLOW_UPS` (per-stage, issue #309 fase 2) | pipeline | manifest 46 / painel `[d]` |
| **Models per-stage** | `DEILE_PREFERRED_MODEL` (global) + `DEILE_PIPELINE_MODEL_<STAGE>` (per-stage, issue #305) | pipeline → worker | manifest 46 / painel `[d]` |
| **Subagents paralelos** | `DEILE_SUBAGENT_RUNNER`, `_MAX_PARALLEL`, `_BUDGET_S`, `_POLL_INTERVAL_S`, `_CAPTURE_BUFFER_MAX_BYTES` (issue #257) | qualquer DEILE invocando `dispatch_parallel_subagents` | DEPRECATED → settings.json |
| **Loop guard** | `DEILE_LOOP_GUARD_DISABLE/MAX_CALLS/REPEAT_THRESHOLD/WINDOW_SIZE/WINDOW_THRESHOLD/NO_PROGRESS`, `DEILE_MAX_TOOL_ITERATIONS` | core agent | DEPRECATED → settings.json |
| **Cron** | `DEILE_CRON_DB_PATH`, `DEILE_CRON_POLL_INTERVAL`, `DEILE_CRON_AUTOSTART` | só `deilebot` (cron roda lá) | manifest 20 + DEPRECATED → settings.json |
| **OpenTelemetry** | `DEILE_OTLP_ENDPOINT/HEADERS/INSECURE/SERVICE_NAME/SAMPLE_RATIO`, `DEILE_OBSERVABILITY_DISABLED` (issue #303 fase 4) | todos os Pods (opcional) | `.env` (vazio = no-op) |
| **Pipeline status server** | `DEILE_PIPELINE_STATUS_HOST/PORT/AUTH_TOKEN/LOG_LEVEL/ENDPOINT`, `PIPELINE_STATUS_BEARER_TOKEN` (issue #347) | só `deile-pipeline` (server) + painel (cliente) | manifest 46 + Secret `pipeline-status-bearer` |
| **Wrapper/whitelist** | `DEILE_WRAPPER_TOOL_WHITELIST` (`all`/`messaging`/CSV), `DEILE_DEFAULT_PERSONA` | wrapper.py → todos os Pods | manifests por Pod (`messaging` no Job, `all` no shell) |
| **K8s namespace** | `DEILE_K8S_NAMESPACE` | só `deploy.py` (default da flag `-n`) | `.env` ou flag CLI |
| **Runtime state** | `DEILE_RUNTIME_DIR` (default `~/.deile/run/`) | issue #303 fase 1 (state files) | `.env` (raramente) |
| **Internos/debug** | `DEILE_DEBUG`, `DEILE_LOG_LEVEL`, `DEILE_HARNESS_MODEL`, `DEILE_SMOKE_MODEL`, `DEILE_STREAM_TEST_MODEL` | testes/debug | só ad-hoc |

### Obrigatórias para subir o cluster do zero (HOJE — pré-correções)

**Hard-fail** em `k8s up` se ausentes:
- `DEILE_BOT_DISCORD_TOKEN` ⚠️ **bug: hard-fail mesmo se você não quer rodar o bot**. Reportado e a corrigir.
- Pelo menos UMA de `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `GOOGLE_API_KEY`.

**Auto-geradas** se ausentes (`secrets.token_urlsafe(32)`):
- `DEILE_BOT_AUTH_TOKEN`, `DEILE_WORKER_BEARER_TOKEN`.

**Opcionais propagadas se presentes:**
- `GITHUB_TOKEN`.

### Bugs/gaps conhecidos no fluxo de configuração (a corrigir)

1. **`DEILE_BOT_DISCORD_TOKEN` é obrigatório no `k8s up` mesmo pra setups sem bot** — deveria ser opt-in (`--with-bot` ou detecção automática).
2. **`GITLAB_TOKEN`/`GL_TOKEN` não é propagado pelo `k8s up`** — só o `GITHUB_TOKEN` vai pro Secret. GitLab puro exige `kubectl patch` manual. (O `k8s setup` interativo já trata, mas o `up` não.)
3. **`PIPELINE_STATUS_BEARER_TOKEN` (issue #347) nem é gerado nem aplicado pelo `k8s up`** — o manifest 44 (`44-pipeline-status-bearer-secret.yaml`) é stub vazio e não entra na lista do `k8s_up`. Status server fica sem auth válido ao clonar do zero.
4. **`claude-worker-bearer`, manifests 47–50** não são aplicados pelo `k8s up` — assume-se que `k8s claude-login` cuida. Isso é OK para a OAuth, mas significa que `claude-worker` não sobe sem chamar `claude-login` antes.
5. **Manifest 43 (`43-forge-tokens-secret.yaml`) existe mas não é aplicado pelo `k8s up`** (que coloca os tokens em `deile-secrets`). Duplicação confusa.
6. **Spread de configs** entre `.env` + Secrets + ConfigMaps + manifests `env:` + `settings.json` — sem ferramenta unificada de visualização ("onde está config X?").

Investigação detalhada em curso (sub-agents Sonnet auditando bot lifecycle / forge / Secrets / migração settings.json). Plano de correção a ser alinhado via questionário enterprise após sub-agents retornarem.

## claude-worker — dispatch pra Claude CLI dentro do cluster (issue #309 fase 2)

Além do `deile-worker` (que roda DEILE python via `wrapper.py worker`), o cluster ganha um pod paralelo `claude-worker` (Service `:8767`) que executa `claude -p` em worktrees isolados sob o PVC `claude-worker-home`. O pipeline despacha tasks per-stage:

- `classify`, `refine`, `implement`, `pr_review`, `follow_ups` — cada um pode apontar pra `deile-worker` OU `claude-worker`
- Resolver: `deile/orchestration/pipeline/dispatch_resolver.py`
  - env var per-stage: `DEILE_PIPELINE_DISPATCH_<STAGE>`
  - env var global: `DEILE_PIPELINE_DISPATCH_MODE`
  - default: `deile-worker`

### Setup inicial (cluster zerado pra claude-worker)

```bash
# Captura credenciais do host, cria Secret, aplica manifests, aguarda Ready:
python3 infra/k8s/deploy.py k8s claude-login

# Força nova OAuth (trocar de conta):
python3 infra/k8s/deploy.py k8s claude-login --switch

# CI-friendly (falha se sem creds):
python3 infra/k8s/deploy.py k8s claude-login --no-interactive
```

### Configurar per-stage no painel

- Tecla `[d]` no painel TUI → `DispatchMatrixView` (substitui `[d]` global da PR #330 + `[M]` per-stage do #305)
- Linha por stage: Worker (`deile-worker` / `claude-worker` / global) × Model (anthropic-only se `claude-worker`)
- Linha "Global default" no rodapé funciona como fallback (env vars `DEILE_PIPELINE_DISPATCH_MODE` + `DEILE_PIPELINE_MODEL`)
- `[L]` switch claude-worker login (`force_relogin`); `[I]` install se ausente
- `[enter]` em uma célula abre picker contextual; `[r]` reseta a célula
- **`[c]` cleanup on-demand** — mostra preview (count + bytes) dos leases/workdirs a remover; `[Y]` confirma via `kubectl exec`, `[N]`/`ESC` cancela sem efeito
- **`[p]` editar `max_parallel`** — abre prompt numérico; `[a]` seta sentinel `"auto"` (`DEILE_PIPELINE_MAX_PARALLEL=auto`); `[enter]` confirma, `[esc]` cancela

### Env vars do claude-worker (issue #408)

| Env var | Default | Uso |
|---|---|---|
| `DEILE_CLAUDE_CLEANUP_RETENTION_DAYS` | `7` | Dias de retenção dos workdirs (startup hook + CronJob). Workdirs sem sessão JSONL ou mais antigos que N dias são removidos. |
| `DEILE_PIPELINE_MAX_PARALLEL` | `2` | Número máximo de dispatches simultâneos no pipeline. Aceita inteiro ≥ 1. Muda via `[p]` no painel (rollout do `deile-pipeline` sem rebuild). |

**CronJob diário:** `claude-worker-cleanup` (03:00 UTC) — monta o PVC `claude-worker-home` e varre leases stale + workdirs abandonados. `kubectl get cronjobs -n deile` para verificar.

### Threat model resumido

Credentials residem em `/home/claude/.claude/credentials.json` mode `0600` (PVC writable, refresh in-pod). NetworkPolicy egress whitelist `api.anthropic.com:443` + `github.com:443` + `gitlab.com:443` (granularidade de repo via ConfigMap `claude-worker-allowed-repos`, enforcement no `wrapper.py`).

Gap conhecido (documentado em spec §7): prompt injection no claude pode exfiltrar credentials via canais legítimos (git push pra repo whitelisted no DNS mas com payload em commit message; data smuggling em headers HTTP legítimos). Mitigação V1 = audit logging do response do `/v1/dispatch` + pattern detection. FU prioritária: sidecar credential proxy (issue separada).

Ver spec completa em `docs/superpowers/specs/2026-05-26-claude-worker-design.md` seção 7.

## Forge — GitHub e GitLab

DEILE é **forge-agnóstico** (issue #297): o mesmo pipeline, briefs e tools operam sobre repos GitHub (cloud + GHES) e GitLab (cloud + self-hosted). A camada `deile/orchestration/forge/` esconde a diferença sob :class:`ForgeClient`; o pipeline nunca importa `gh`/`glab` direto.

Configuração (env vars resolvidas via `Settings` — mesmas regras de qualquer outra chave):

| Env var | Default | Quando usar |
|---|---|---|
| `DEILE_FORGE_KIND` | `auto` | `github`\|`gitlab`. `auto` detecta por host/path. Para GitLab self-hosted ou GHE, defina explicitamente. |
| `DEILE_FORGE_REPO` | — (cai pra `DEILE_PIPELINE_REPO`) | `owner/repo` (GH) ou `group/(subgroup/)*project` (GL). |
| `DEILE_GITHUB_HOST` | `github.com` | GHES — também aceita CSV (`ghe-a.x.com,ghe-b.x.com`). |
| `DEILE_GITLAB_HOST` | `gitlab.com` | GitLab self-hosted. |
| `GITHUB_TOKEN` | — | PAT GitHub. Pelo menos um token (GH OU GL) é exigido pela pipeline. |
| `GITLAB_TOKEN` / `GL_TOKEN` | — | PAT GitLab (escopo `api`, `read_repository`, `write_repository`). |

Pra rodar o pipeline contra um projeto GitLab piloto:

```bash
# 1. K8s Secret recebe o token (adicione ao deile-secrets existente).
GL_PAT=...
kubectl -n deile patch secret deile-secrets \
  -p "{\"stringData\":{\"GITLAB_TOKEN\":\"${GL_PAT}\"}}"

# 2. ConfigMap do pipeline declara forge=gitlab + repo.
kubectl -n deile set env deploy/deile-pipeline \
  DEILE_FORGE_KIND=gitlab DEILE_FORGE_REPO=group/sub/project
# (ou edite o Deployment manifest e re-aplique)

# 3. Restart.
python3 infra/k8s/deploy.py k8s restart
```

Vocabulário GitHub ↔ GitLab que o codigo mantém alinhado: PR↔MR, comment↔note, requested_reviewers↔reviewers, `gh`↔`glab`. URLs:

- GitHub: `https://<host>/<owner>/<repo>/{issues,pull}/<n>`
- GitLab: `https://<host>/<group>/(<subgroup>/)*<project>/-/{issues,merge_requests}/<n>`

Templates de issue: `.github/ISSUE_TEMPLATE/<f>.md` no GH; `.gitlab/issue_templates/<f>.md` no GL. O refinement gate sabe puxar do path certo conforme o forge.

> **Multi-forge na mesma sessão CLI**: o `deile-shell` interativo usa `ForgeRouter` (singleton) — você pode processar issues GH e MR GitLab na mesma conversa sem reconfigurar nada. O pipeline (`deile-pipeline`) ainda é per-repo: rode duas instâncias para servir GH e GL em paralelo (`MonitorIdentity` + shard, Decisão #18).

**Pipeline label state machine** (issues, com **portão de refinamento** — PR #275):

```
🆕 ~workflow:nova → 🔍 ~workflow:em_revisao → ┬─ CLARO → ✅ ~workflow:revisada
                                              │             ├─ intent  → 🧩 ~workflow:decomposta (architect abre N derivadas)
                                              │             └─ code    → 🚀 ~workflow:em_implementacao → 📬 ~workflow:em_pr
                                              └─ VAGO → 🏷️ refinar + (🧠 ~workflow:em_refinamento [intent/analyst]
                                                                        │  ou 🏛️ ~workflow:em_arquitetura [feat-bug-refactor/architect-debugger])
                                                       ↕ ⏸️ ~workflow:aguardando_stakeholder (humano remove p/ liftar)
                                                       → de volta p/ ~nova (até 5 voltas; estourou → ⛔ ~workflow:bloqueada)
```

PRs: `~review:pendente` → `~review:em_andamento` → `~review:concluida`. Locks: `~batch:<sha8>` (claim — **only applied when `shard_count>1`; a single monitor skips it**, so no add/remove churn), `~by:<id>` (owner). Markers: `~workflow:bloqueada` (hard block, excludes auto-resume), `~mention:processado` (mention/assignment already handled).

- **Refinement gate (PR #275, issue #257-bookkeeping):** every new issue is first **critiqued for scope** by a persona chosen by type (`intent`→`analyst`, `feature`/`refactor`→`architect`, `bug`→`debugger`). **VAGO** issues get `refinar` + a refine state (`em_refinamento` for intent, `em_arquitetura` for code-types) and are refined (rewrite body, read comments, fix the title's `[TIPO]` bracket) up to 5 rounds. A high-impact gap can pause in `~workflow:aguardando_stakeholder` with 2-3 suggested options assigned to the author; the human removes the label to resume. Intents that pass are **decomposed** into derived issues by `architect`; code-types implement in parallel via `asyncio.gather` (cap `max_parallel=2`, worker scaled to 2 replicas).
- **Resume (issue #254):** a parked `~workflow:em_implementacao` issue (or `~review:em_andamento` PR) is **auto-retried** on the next free tick (RESUME mode — reuses the per-channel PVC, no `reset --hard`), bounded by an attempt/budget ceiling. A real impediment moves it to `~workflow:bloqueada`, which **excludes it from auto-resume** — a human removes that label to unblock.
- **PR triage** only labels `~review:pendente` on PRs the monitor would actually review (owned `auto/issue-*` branch, or any branch with `enable_review_human_prs`). Foreign branches are left untouched.
- **Mention/assignment routing (issues #253/#261):** `process_mentions` is a router. **Issue + assignee/body-mention** → injects `~workflow:nova` (pipeline takes over, with resume + `auto/issue-N` branch). **PR + assignee** (+/– reviewer) → `work_merge` (review + resolve threads + merge; assignee>reviewer precedence). **PR + requested-reviewer only** → `review_only`: reviews and **assigns the author back, never merges**. **PR/issue + comment** → does what the comment asks. Sticky triggers marked `~mention:processado` (remove to re-handle). A comment on a gated issue (`~workflow:em_*`) does **not** pull it out of the gate — it lifts the `aguardando_stakeholder` pause if present, then is ignored by routing.
- **Quality-gate de review/merge (PR #276):** all 5 gate briefs (`review`, `review_resume`, `review_only`, `implement`, `implement_resume`) require **the FULL suite green** (`pytest deile/tests/ -q`, with the coverage gate — the real CI portal) before approve/merge/opening PR. Subset runs (`<files> -p no:cov`) are allowed only for fast iteration during fixes. `review_only` also confronts the delivery vs the request (issue body + comments) — testes verdes do not substitute a missing requirement. Briefs live at `deile/orchestration/pipeline/briefs.py`.

> **Label edits must use the REST issues endpoint, NOT `gh issue/pr edit --add-label`** — the latter runs a GraphQL `login` query that demands the `read:org` scope (which the pipeline token lacks). The pipeline already does this in `github_client.py`; for manual ops:
> ```bash
> gh api -X POST   "repos/elimarcavalli/deile/issues/<N>/labels" -f 'labels[]=~workflow:revisada'
> gh api -X DELETE "repos/elimarcavalli/deile/issues/<N>/labels/%7Eworkflow%3Arevisada"   # ~=%7E :=%3A
> ```

## Gotchas (not in the system design)

- **At least one provider API key is required at startup** — the agent exits if none are set. Configure any of: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, or `GOOGLE_API_KEY` in `.env` (loaded via `python-dotenv`). The `bootstrap_providers()` function in `deile/core/models/bootstrap.py` handles conditional registration.
- **`deilebot` lives in a separate repo** (`elimarcavalli/deilebot`). DEILE consumes it via two modalities — pick the one that fits:

  **Cloud / CI / fresh laptop (one-liner):** `pip install -e ".[bot]"` — the `bot` extra resolves `deilebot` directly from GitHub via git URL (no PyPI).

  **Dev local (recommended for hacking on the bot itself):**
  ```bash
  git clone https://github.com/elimarcavalli/deilebot.git
  pip install -e ./deilebot
  pip install -e .
  ```
  Pip detects `deilebot` already installed and ignores the extra. The `deilebot/` folder is gitignored.

  > **Migration note (issue #159):** the canonical clone path is `deilebot/` (no separator). If you have a legacy `deile_bot/` clone, run `mv deile_bot deilebot` and re-run `pip install -e ./deilebot`. Run the bot from the `deile/` repo root (`python3 -m deilebot run --provider discord`) — running from inside the clone shadows the parent `deile.config` package via the partial `deilebot/deile/` overrides.

  **Both cases:** configure `DEILE_BOT_ENDPOINT` + `DEILE_BOT_AUTH_TOKEN` (see `.env.example`). The `messaging.discord_*` tools auto-register only when `import deilebot` succeeds and both env vars are set.
- **If you're touching messaging tools**, open [`08-SEGURANCA.md`](docs/system_design/08-SEGURANCA.md) **before** writing code — DM and role-mention tools are gated by `ApprovalSystem` by design and changes to that gate are non-trivial.
- **Two `config/` directories**: `./config/` (runtime YAML/JSON) vs `./deile/config/` (package code + `settings.py` + YAML configs like `intent_patterns.yaml`). Don't conflate.
- **`deile/tests/` mixes two kinds of tests**:
  - *Pytest tests* (`test_*.py`) — collected automatically by `pytest`.
  - *Standalone scripts* (`*_test.py`, `smoke_test_*.py`, `proactive_final_test.py`) — run manually via `python deile/tests/<name>.py`. Pytest sees no `Test*` class / `test_*` function in them and silently skips, so they coexist safely. Use `python deile/tests/all.py` to run every standalone script in sequence; pass `--filter <substring>` to narrow the set.
- **`pytest.ini` uses `--strict-markers`** — register new markers there before using.
- **`asyncio_mode = auto`** — async tests don't need `@pytest.mark.asyncio`.
- **Settings is a singleton** — use `from deile.config.settings import get_settings`, never instantiate `Settings()` directly.
- **Personas are MD-driven** — instructions live in `deile/personas/instructions/*.md`; edit those to change behavior, no code change needed.
- **`.gitignore` has `*claude*`** — `CLAUDE.md` is explicitly negated (`!CLAUDE.md`). Don't remove that negation.

## Running DEILE for empirical testing

You are authorized to invoke `python3 deile.py` (or call the agent programmatically) to test behavior changes — persona rules, gates, tooling — against the real LLM. The user has approved modest token spend for this. Two distinct conventions for **where files go**, do not conflate:

| Folder | Owner | Purpose |
|---|---|---|
| `test-your-might/<nickname>/` | **DEILE writes here** | Sandbox for artifacts DEILE creates *during interactive intelligence-tests* the user runs against him (e.g. the calc-package test, the fib.py test). When the user prompts DEILE to "create a program in tmp/X/...", instruct DEILE to scope under `test-your-might/<nickname>/` so the project root stays clean. |
| `deile/tests/might/<nickname>/` | **You write here** | YOUR test scripts that make real LLM API requests (like `test_rule8.py`). Live alongside `deile/tests/` but isolated under `might/` because they cost real tokens and aren't part of the standard `pytest` suite. |
| `deile/tests/` (rest) | **You write here** | Regular pytest tests — no API calls, no token spend. |

Constraints when running:

- **Keep the budget proportional to the question** — a smoke test is 1–4 messages, not a 20-message marathon. The user covered ~38 requests ≈ $0.13; aim well below that per ad-hoc test.
- **Same DEILE process across multi-turn probes** so conversation history persists (e.g. probing S4 "summarize what you just said" requires history continuity).
- **To bootstrap programmatically**, mirror what `deile.py` (the CLI) does — `ConfigManager().load_config()` + `bootstrap_providers(router=get_model_router())`. Calling `bootstrap_providers()` alone registers 0 providers because the router is the singleton DeileAgent reads from.
- **Capture output, strip ANSI, report verbatim** what DEILE actually said + which tools it actually called. Don't paraphrase — that's exactly the kind of fabrication rule 8 was added to prevent.
- **If DEILE asks for an interactive confirmation you cannot answer**, kill the process and surface that as a finding rather than guessing.
- **ALWAYS** when asked to open an issue, follow the relevant `.github/ISSUE_TEMPLATE/*.md` (read all the templates and decide which is most appropriate). If user asks without specific details, use the `intent` template.

## SQL / database operations

All SQL scripts are the human operator's responsibility to run. If a DB error appears during a task, **stop and tell the operator which script to execute** — do not attempt to run migrations or schema changes yourself.
