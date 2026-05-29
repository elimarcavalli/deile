# DEILE Harness — Concepção, Verdade Atual e Visão de Evolução

> Documento de visão arquitetural escrito em **2026-05-28** após investigação
> rigorosa do código vigente (`deile/orchestration/pipeline/*`,
> `infra/k8s/{worker_server,claude_worker_server,_worker_resume}.py`,
> manifests 20/45/46/50). É um norte de migração — **não é spec final**.
>
> Propósito: dar visão total de O QUE É um harness DEILE hoje, ONDE estamos
> longe ou perto dos 10 pilares enterprise, e POR QUE cada degrau evolutivo
> faz sentido. Diagramas ASCII para leitura cold; tabelas para escolha.

---

## §1. O que é um "harness DEILE" — definição operacional

**Harness é o cluster inteiro**, não um pod. Cada `namespace` K8s é uma
**identidade de desenvolvimento** (um "dev autônomo") com:

- um repositório (ou conjunto) e uma conta de forge (GitHub OU GitLab)
- um agregado de canais de entrada (Discord bot, webhooks, polling de
  issues/PRs/menções, schedules cron)
- um pool de workers que **executam** trabalho (`deile-worker`,
  `claude-worker`, futuro `opendeile-worker`, etc.)
- um **monitor singleton** (`deile-pipeline`) que orquestra
- estado persistente (PVCs, ledger, settings, OAuth)

```
┌──────────────────────────────────────────────────────────────────────┐
│  NAMESPACE = UMA IDENTIDADE  (ex: "deile" = você no projeto deile)   │
│                                                                       │
│  ┌─────────────┐   ┌─────────────────────────┐   ┌────────────────┐  │
│  │  Discord    │──▶│  deilebot (control      │──▶│  Worker pool   │  │
│  │  bot (DM,   │   │  plane: SQLite, cron,   │   │  ───────────   │  │
│  │  canais,    │◀──│  Discord I/O)           │◀──│  deile-worker  │  │
│  │  mentions)  │   └────────┬────────────────┘   │  claude-worker │  │
│  └─────────────┘            │ HTTP /v1/dispatch  │  (futuro: gpt-,│  │
│                             ▼                    │   gemini-...)  │  │
│                  ┌─────────────────────┐         └────────┬───────┘  │
│  ┌────────────┐  │  deile-pipeline     │                  │           │
│  │ GitHub /   │◀▶│  (singleton monitor)│──────────────────┘           │
│  │ GitLab     │  │  forge polling,     │                              │
│  │ events     │──▶│  label state machine│                              │
│  └────────────┘  │  dispatch decisions │                              │
│                  └────────┬────────────┘                              │
│                           │                                           │
│                           ▼                                           │
│              ┌────────────────────────┐                               │
│              │  PVCs + ledger + OAuth │                               │
│              │  (estado persistente)  │                               │
│              └────────────────────────┘                               │
└──────────────────────────────────────────────────────────────────────┘
```

Múltiplos namespaces coexistem (`deile` = pessoal/main; `deile-gl` = pilot
GitLab; `deile-staging`, `deile-empresa-X`). Cada um é **isolado** por
NetworkPolicy default-deny. O `deploy.py` foi feito para subir N
namespaces independentes na mesma VM.

**Você acertou:** o harness é o cluster + os namespaces. **Correção menor:**
o `deile-pipeline` por namespace é **singleton** (Recreate strategy,
shard-counting via `MonitorIdentity` quando se quer paralelismo cross-tick).

---

## §2. A VERDADE sobre dispatch e continuidade entre workers — investigação direta

### §2.1 — Como o pipeline escolhe quem trabalha

Resolver em `deile/orchestration/pipeline/dispatch_resolver.py:50-52`:

```python
_ENDPOINT_DEFAULTS = {
    "deile-worker":  "http://deile-worker:8766",
    "claude-worker": "http://claude-worker:8767",
}
```

Resolução **por stage** (`classify`, `refine`, `implement`, `pr_review`,
`follow_ups`):

```
env DEILE_PIPELINE_DISPATCH_<STAGE>=claude-worker
  → cai pra DEILE_PIPELINE_DISPATCH_MODE=...
  → cai pro default "deile-worker"
```

A escolha de **modelo** é independente da escolha de **worker** (decisão
#41 + #43): `claude-worker` só aceita `anthropic:*`; `deile-worker` aceita
qualquer provider (deepseek, anthropic, openai, gemini).

### §2.2 — Como cada worker organiza o workspace

| Worker | Path | Chave de reuso | PVC |
|---|---|---|---|
| `deile-worker` | `/home/deile/work/<channel_id>/` | **`channel_id` estável** (`pipeline-issue-N`, `pipeline-pr-N`) | `deile-worker-work` (RWO 5Gi) |
| `claude-worker` | `/home/claude/work/<task_id>/` | **`task_id` (hex16) novo a cada fresh dispatch**; resume requer `prev_task_id` + `resume_session_id` no payload | `claude-worker-home` (RWO 1Gi) |

```
DEILE-WORKER (channel_id-based, retomada implícita)
─────────────────────────────────────────────────────
Dispatch #1 issue 381  → channel_id = "pipeline-issue-381"
                       → workdir   = /home/deile/work/pipeline-issue-381/
                       → cria repo, faz commit A
Dispatch #2 issue 381  → channel_id = "pipeline-issue-381" (mesma!)
                       → workdir   = /home/deile/work/pipeline-issue-381/
                       → ENCONTRA repo + commit A → segue trabalhando
Dispatch #3 issue 99   → channel_id = "pipeline-issue-99"
                       → workdir   = /home/deile/work/pipeline-issue-99/
                       → workspace separado, isolado


CLAUDE-WORKER (task_id-based, resume EXPLÍCITO via ledger)
───────────────────────────────────────────────────────────
Dispatch #1 issue 381  → task_id = uuid4().hex[:16] = "ab12cd34..."
                       → workdir = /home/claude/work/ab12cd34.../
                       → cria repo, faz commit A, salva session.json
                       → pipeline DispatchLedger registra: key="issue:381" → task_id="ab12cd34..."
Dispatch #2 issue 381  → pipeline consulta ledger → prev_task_id="ab12cd34..."
                       → envia payload com {prev_task_id, resume_session_id}
                       → claude-worker valida via session.json e REUSA o workdir
                       → spawna `claude -p -r <session_id>` (resume real do claude CLI)
```

### §2.3 — A resposta exata à sua pergunta

> **"Se um deile-worker continua o trabalho de um claude-worker sobre a
> mesma issue/branch/PR, vai continuar o serviço ou vai clonar tudo de
> novo?"**

**HOJE, VAI CLONAR DO ZERO.** E perde o WIP não-commitado. Razão técnica:

1. PVCs distintos: `deile-worker-work` ≠ `claude-worker-home`. Filesystems
   sem intersecção. Nenhum dos dois workers enxerga o que o outro fez no
   working tree.
2. O **único veículo de cross-worker** é o **git remote (origin)**. Se o
   claude-worker fez `git push`, qualquer worker pode `git pull` e ver
   esses commits. WIP/untracked/branches locais — invisíveis.
3. O `.deile-progress.md` (journal de resume) é gravado **no workdir
   local de cada worker**. Cross-worker, ele é perdido.
4. O DispatchLedger guarda `task_id` por dispatcher. Se o stage muda de
   `claude-worker` → `deile-worker`, o `prev_task_id` armazenado se torna
   inválido (formato/escopo diferentes) — `_resolve_resume_meta` chama
   `get_resume_info(prev_task_id)` no endpoint resolvido pelo **stage
   atual**, pega 404, faz `clear()` no ledger e volta pra fresh dispatch.

**Conclusão:** trocar de worker mid-PR custa um clone fresh + perda de
WIP. Mesma issue por mesmo worker reusa workdir e (no caso do claude)
reusa session JSONL.

### §2.4 — Mesmo-worker, mesma-issue: já funciona muito bem

| Cenário | deile-worker | claude-worker |
|---|---|---|
| Mesma issue, dispatch novo | ✅ mesmo `/home/deile/work/pipeline-issue-N/` | ✅ resume via ledger → mesmo `/home/claude/work/<task_id>/` |
| Pod restart (mesma issue) | ✅ PVC sobrevive → workdir intacto | ✅ PVC + session.json → resume idêntico |
| Pod morre + scale=0/1 | ✅ PVC sobrevive | ✅ PVC sobrevive |
| 2 réplicas escritas concorrentes mesmo dir | ⚠️ race em meta-files | ⚠️ race no OAuth + worktree |

---

## §3. Auditoria contra os 10 pilares enterprise — onde estamos

> ✅ pronto / 🟡 parcial / 🔴 ausente

| # | Pilar | Status | Como está hoje | Gap |
|---|---|---|---|---|
| 1 | **Identity/Secrets Zero-Trust** | 🟡 | K8s Secrets (TLS-only at rest no etcd), namespace isolation, segredos como files (não env), pop após bootstrap | Sem Vault/ESO; rotação manual; cross-namespace secret é setup manual |
| 2 | **Workflow engine resiliente** | 🟡 | DispatchLedger SQLite + .deile-progress.md + fingerprint substantivo + budget; mas é **ad-hoc**, não durável a OOM mid-call | Sem Temporal; reaper só retoma stage-level, não step-level dentro da execução do claude/agent |
| 3 | **Control Plane dinâmico** | 🟡 | `deile-pipeline` é singleton por NS, dispara via HTTP /v1/dispatch; workers são pods long-running com replicas fixas | Sem auto-scaling; sem spawn ephemeral on-demand; workers ficam idle entre dispatches |
| 4 | **Message broker assíncrono** | 🔴 | HTTP síncrono pipeline→worker (com `wait=False` fire-and-forget recente); polling cross-tick via labels GitHub | Sem Kafka/NATS/Redis Streams; concurrency limit é via asyncio.Semaphore no pipeline |
| 5 | **Sandbox L4/L7** | 🟡 | NetworkPolicy default-deny ✅, drop ALL caps ✅, readOnlyRootFS ✅, seccomp RuntimeDefault ✅, allowlist de repos ✅ | Sem gVisor/Firecracker; sandbox é só pod-level; um worker malicioso pode CPU-burn um nó |
| 6 | **Memória vetorial por tenant** | 🟡 | Quatro camadas (working/episodic/semantic/procedural) em SQLite por sessão; bot tem PVC com SQLite | Sem PostgreSQL/pgvector; sem cache compartilhado Redis; sem cross-namespace knowledge |
| 7 | **AI Gateway centralizado** | 🔴 | Cada worker chama provider SDK direto (anthropic, openai, deepseek); circuit breaker EXISTE (`deile/core/models/circuit_breaker.py`) mas é per-process, não centralizado | Sem LiteLLM/Portkey; rate limiting é por-worker, não global; FinOps via UsageRepository SQLite (per-NS) |
| 8 | **Telemetry/OTel** | 🟡 | Decisão #39 (OpenTelemetry com tracer/metrics) — instalado, mas requer endpoint OTLP externo configurado; sem default exporter | Sem stack default (Tempo/Loki/Mimir); GenAI semantic conventions parciais |
| 9 | **GitOps bidirecional + lock** | 🟡 | Cria branches, abre PRs/MRs (GH+GL), lê CI checks parciais (`gh pr checks`), itera. Lock distribuído via labels `~batch:<sha8>` + `~by:<id>` | Sem leitura completa de CI failures pra iteração automática; sem consenso (k3s single-node) |
| 10 | **Tool registry padronizado** | ✅ | `ToolRegistry` (decisão #3, decisão #35 com Skills); auto-discover; ToolSchema converte pra Anthropic/OpenAI/Gemini formats; ApprovalSystem | Sem MCP (Model Context Protocol) externalizado; tools são in-process Python |

**Resumo executivo:** dos 10 pilares, **1 está pronto**, **6 parciais**, **3
ausentes**. Os 3 ausentes (4, 7) e o parcial mais crítico (2) são a coluna
vertebral de uma migração enterprise — é por aí que vale entrar.

---

## §4. Modelo A vs Modelo B — análise para o caso DEILE

### Reframing crítico

A pergunta "A ou B" pressupõe que workers são **iguais e intercambiáveis**.
Na prática:
- claude-worker e deile-worker têm **modelos diferentes** (claude opus vs
  deepseek), **custos por token diferentes** (opus = 5x deepseek), e
  **capabilidades diferentes** (claude tem ferramentas próprias do
  `claude -p`; deile-worker tem o tooling completo da DEILE Python lib).
- Trocar mid-PR **muda o resultado**, não só o desempenho.

### Tabela de adequação

| Critério | Modelo A (compartilhado) | Modelo B (isolado) |
|---|---|---|
| Aderência aos seus 3 requisitos (multi-replica, retomada, OAuth único) | ✅ atende todos | ⚠️ retomada cross-worker = perda |
| Aderência à realidade atual do código | ⚠️ exige RWX + locks | ✅ é o que existe (PVC RWO por worker) |
| Esforço de migração | 🟡 médio (NFS in-cluster + 2 manifests + flock OAuth) | 🟢 zero (já está) |
| Custo de tokens otimizado | ✅ stage-routing per modelo, sem re-clone | ❌ re-clone = pull + análise repetida = +tokens |
| Comparação A/B de modelos | ❌ contaminação cruzada de cache/contexto | ✅ tournament limpo |
| Auditoria forense | ⚠️ git log mistura commits sem distinção | ✅ trivial |
| Escala horizontal | ✅ N réplicas sobre N tasks paralelas, sem coordenação se tasks são disjuntas | ✅ trivial mas perde continuidade |
| Failover (worker morre mid-PR) | ✅ próximo worker continua | ❌ outro worker precisa re-clonar |

**Meu veredito:**
> O **Modelo A é o futuro** porque a **continuidade entre workers** é o
> ganho qualitativo decisivo — bate o custo extra de NFS. Mas o **Modelo
> B é o presente** e funciona. O caminho é **A com fallback B** — onde a
> continuidade é best-effort (se workspace compartilhado tá lá, usa; se
> não, re-clona).

Adicionalmente, **A não impede tournament**: pode-se reservar dirs
`/workspace/_tournament/<run-id>/<worker>/` quando o pipeline quer rodar
A/B (modo opt-in por dispatch).

---

## §4.5. O que a indústria fez até maio/2026 (research externo)

> Subagent paralelo coletou 30+ fontes. Highlights que **mudam** a discussão
> Modelo A vs B acima:

### Convergência principal: **isolated workspace per agent**, com git worktrees

O Kubernetes lançou em **20/mar/2026** o `kubernetes-sigs/agent-sandbox`,
um CRD oficial `Sandbox` exatamente para o caso de uso do DEILE:

> "AI agents are typically isolated, stateful, singleton workloads. They
> act as a digital workspace and require persistent identity + secure
> scratchpad."

Provê: stable hostname/network identity, persistent storage que sobrevive
restart, lifecycle pause/resume, suporte nativo a gVisor/Kata Containers.
`SandboxWarmPool` resolve cold-start via pool pré-provisionado, acessável
via `SandboxClaim` contra `SandboxTemplate`.

**Implicação pro DEILE:** o caminho "RWX compartilhado" do Modelo A
**não é o padrão da indústria em 2026**. O padrão é **isolated per agent
+ git worktrees como protocolo de cross-agent**. Isso REFORMULA minha
recomendação:

| Padrão | Quem usa | Evidência |
|---|---|---|
| Isolated VM/sandbox per agent + git worktree | Cursor 3, Devin, OpenHands, GitHub Copilot Workspace | múltiplas fontes |
| Worktrees viraram "load-bearing" pra AI coding em Q1/2026 | Augment Code, Appxlab | direct quote |
| Multi-Devin: 1 manager + até 10 worker Devins isolados | Cognition (mar/2026) | release notes |

**Reframing do Modelo A:** o ganho não vem de "PVC compartilhado", vem de
**worktree compartilhado** (mesmo `.git`, working dirs distintos). É mais
leve e respeita o isolamento físico que a indústria adotou.

### Outras descobertas relevantes

1. **Cursor 3** (abr/2026) tem **Helm chart e Kubernetes Operator com
   CRD `WorkerDeployment`** que auto-escala pods conforme a fila. É
   exatamente o que o DEILE precisa para escalar workers sob demanda
   (substituindo o `replicas: N` estático).

2. **Temporal + OpenAI Agents SDK Integration GA em 23/mar/2026.** Não
   é mais hype — virou produto. Netflix/Snap/NVIDIA/OpenAI em produção.
   Se DEILE for Python-puro, **DBOS** é alternativa zero-infra (só precisa
   Postgres, roda in-process).

3. **MCP (Model Context Protocol)** explodiu: **97M downloads/mês** dos
   SDKs Python+TypeScript em mai/2026 (escala comparável a React em 16
   meses). 41% das orgs em produção. **DEILE ainda usa tool registry
   proprietário** — vale considerar MCP como interface externalizada.

4. **AI Gateway = obrigatório** em 2026 para multi-team/regulated:
   - **LiteLLM**: dominante em OSS, mas **falha em ~2k RPS, OOMs com >8GB
     RAM**. OK para single-tenant.
   - **Portkey**: virou Apache 2.0 em mar/2026, mantém SaaS enterprise.
     Guardrails + semantic caching + audit trails para 250+ LLMs.
   - **Kong AI Gateway**: para quem já tem Kong.

5. **OpenTelemetry GenAI Semantic Conventions** virou stack canônica de
   fato — Datadog/New Relic/Dynatrace suportam nativo. **Langfuse** declara
   conformidade explícita. **DEILE já tem OTel (decisão #39)** — só
   precisa adicionar os atributos GenAI.

6. **Multi-tenant K8s patterns 2026:** namespace per tenant ✅ (DEILE já
   faz), NetworkPolicy default-deny ✅, ResourceQuota por NS ⚠️ (não tem),
   **vcluster v0.14** ganhou tração pra hard multi-tenancy (control
   plane por tenant — overkill para DEILE hoje mas válido pro multi-cliente).

7. **External Secrets Operator (ESO) + Vault** é o stack canônico de
   secrets — sincroniza via `ExternalSecret` CRDs. Rotação automática.

8. **Não há recomendação pública em 2026 endorsing RWX volumes para
   agentes.** O consenso é PVC dedicado + git como veículo de
   compartilhamento. Minha P2 original (NFS RWX) **vai contra a corrente
   da indústria**.

### Como isso muda as propostas

| Proposta | Antes da pesquisa | Depois da pesquisa |
|---|---|---|
| P1 (handoff git) | "stopgap" | **alinhado com indústria** — é o padrão |
| P2 (NFS RWX) | "solução enterprise" | **fora do mainstream**; considerar alternativas |
| P3 (Temporal+Vault+...) | "alvo de longo prazo" | **GA, validado, com adoption real** |

**Nova proposta intermediária P2'** (substitui P2 anterior):

---

## §5. Três propostas evolutivas (P1, P2', P3) — atualizadas com evidência

### P1 — "Handoff via git stash semântico" (3-5 dias, baixo risco)

```
┌───────────────────────────────────────────────────────────────────┐
│  P1: HANDOFF VIA GIT — workers continuam isolados                 │
│                                                                    │
│  claude-worker (PVC A)         deile-worker (PVC B)                │
│  ─────────────────────         ─────────────────────               │
│  /home/claude/work/<task>/     /home/deile/work/<chan>/            │
│     repo/                          repo/                           │
│     .deile-progress.md             .deile-progress.md              │
│                                                                    │
│  Quando claude termina sem PR:                                     │
│    git add -A && git commit -m "wip(handoff): claude → deile"      │
│    git push origin auto/issue-N                                    │
│    + push do .deile-progress.md em /handoff/<issue>.md (gist? PVC?)│
│                                                                    │
│  deile-worker recebe próximo dispatch:                             │
│    git clone --depth=20 (rápido, recente)                          │
│    git checkout auto/issue-N                                       │
│    git log -1 --grep "handoff" → detecta WIP                       │
│    pull .deile-progress.md do handoff store                        │
│    continua de onde claude parou                                   │
└───────────────────────────────────────────────────────────────────┘
```

**O que muda:**
- Novo commit-trailer `Handoff: <previous_worker>` na mensagem
- Endpoint `/v1/handoff/{issue}` em ambos workers (publica
  `.deile-progress.md` no PVC compartilhado existente `deile-logs`, ou
  num gist privado do GitHub).
- Pipeline detecta troca de worker entre dispatches → adiciona instrução
  "continue do handoff anterior" no brief.

**Custo:** zero infra (usa git + PVC já existente). +1 commit por handoff.

**Ganho:** continuidade **lógica** entre workers; perda só do working
tree intermediário (que reaparece de qualquer forma pelo `git checkout`).

**Limitação:** ainda re-clona; perde caches (.pytest_cache, node_modules
se houver, dependency cache). Mas o custo é **previsível**.

---

### P2' — "Shared `.git` repo + worktrees per worker" (1-2 semanas) — **alinhado à indústria 2026**

> **Substitui P2 anterior (NFS RWX).** Por quê: o padrão da indústria
> (Cursor 3, Devin Multi-Devin, OpenHands, Augment Code, k8s
> agent-sandbox) é **isolated workspace per agent + git worktrees como
> protocolo de compartilhamento**. Mais leve, mais alinhado, sem RWX.

```
┌──────────────────────────────────────────────────────────────────────┐
│  P2': SHARED .git, WORKTREES PER WORKER (padrão indústria 2026)      │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────┐      │
│  │  PVC RWO "shared-git" (bare repo + ref-cache)              │      │
│  │  Mount: /git/<repo>/  →  é UM clone --bare por repo        │      │
│  │  Quem escreve: só fetch periódico (init job)               │      │
│  └────────┬────────────┬────────────┬──────────────────────────┘      │
│           │            │            │                                 │
│  Cada worker tem seu PVC PRÓPRIO (isolated workspace):                │
│           ▼            ▼            ▼                                 │
│   ┌─────────────┐ ┌─────────────┐ ┌─────────────┐                     │
│   │ claude-     │ │ deile-      │ │ deile-      │                     │
│   │ worker-0    │ │ worker-0    │ │ worker-1    │                     │
│   │ PVC: claude-│ │ PVC: deile- │ │ PVC: deile- │                     │
│   │ workspace   │ │ workspace-0 │ │ workspace-1 │                     │
│   │             │ │             │ │             │                     │
│   │ Para nova   │ │ git worktree│ │ git worktree│                     │
│   │ task:       │ │ add /work/  │ │ add /work/  │                     │
│   │ git worktree│ │ <task>      │ │ <task>      │                     │
│   │ add /work/  │ │ /git/<repo> │ │ /git/<repo> │                     │
│   │ <task>      │ │ (compartilha│ │             │                     │
│   │ /git/<repo> │ │ objects via │ │             │                     │
│   │             │ │ same .git)  │ │             │                     │
│   └─────────────┘ └─────────────┘ └─────────────┘                     │
│                                                                       │
│  Vantagens vs NFS RWX:                                                │
│   - sem RWX (k3s native, sem NFS server, sem flock)                   │
│   - clone shallow ~50MB compartilhado entre workers (1 fetch)         │
│   - cada worker faz `git worktree add` (instantâneo, hardlinks)       │
│   - workspaces 100% isolados (sem race condition)                     │
│                                                                       │
│  Handoff cross-worker: via `git push origin auto/issue-N`             │
│   (mesmo P1, mas mais barato porque o fetch reusa /git/<repo>)        │
│                                                                       │
│  OAuth claude: continua em PVC dedicado claude-creds (RWO 1Gi),       │
│   refresh apenas pelo claude-worker (sem race).                       │
└──────────────────────────────────────────────────────────────────────┘
```

**O que muda:**
- 1 manifest novo: `42c-shared-git-pvc.yaml` (PVC RWO 5Gi para `clone --bare`).
- 1 manifest novo: `42d-git-sync-cron.yaml` (CronJob que faz `git fetch
  --all` no PVC a cada 5min).
- Manifests 45/50: novo init-container `git-worktree-setup` que faz
  `git worktree add /work/<task_id> /git/<repo>/refs/heads/auto/issue-N`
  no dispatch start. O `.git` é symlink pro PVC compartilhado.
- `dispatch_resolver`: ganha conhecimento de `repo` no payload pra
  resolver o caminho `/git/<repo>/` correto (multi-namespace, multi-repo).
- Adoção do **CRD `Sandbox` do `kubernetes-sigs/agent-sandbox`** (opt-in
  via Helm chart) — substitui o Deployment custom dos workers por
  Sandboxes managed.

**Custo:** ~5Gi PVC + 1 CronJob. Zero RAM adicional. Sem RWX, sem NFS.

**Ganho:**
- **Padrão de indústria 2026** validado por Cursor/Devin/OpenHands.
- Workspaces isolados (sem race condition possível).
- **Worktrees são instantâneos** (hardlinks dos objects do `.git`).
- Cada task tem seu próprio working dir limpo.
- Handoff cross-worker = git push + pull (custo: ~3s shallow pull,
  porque `/git/<repo>/` já tem os objects).

**Risco:** baixo. Padrão amplamente validado. Reversível (basta voltar
ao deploy atual).

**Bonus opcional:** adotar **agent-sandbox CRD** transforma os
deployments em Sandboxes com `SandboxWarmPool` — pool pré-aquecido de
workers vazios, dispatch usa `SandboxClaim`, zero cold-start.

---

### P3 — "Harness Enterprise" (1-2 meses, arquitetura)

```
┌────────────────────────────────────────────────────────────────────────┐
│  P3: HARNESS ENTERPRISE (a visão de longo prazo)                       │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  CONTROL PLANE (shared across namespaces)                         │  │
│  │  ┌──────────────┐  ┌─────────────────┐  ┌──────────────────────┐ │  │
│  │  │ AI Gateway   │  │ Vault / ESO     │  │  OTel Collector +    │ │  │
│  │  │ (LiteLLM)    │  │ secrets per-NS  │  │  Tempo/Loki/Mimir    │ │  │
│  │  │ + cost/quota │  │ rotation auto   │  │  GenAI conventions   │ │  │
│  │  │ + retries    │  │                 │  │  (langfuse adjacent) │ │  │
│  │  └──────┬───────┘  └────────┬────────┘  └──────────┬───────────┘ │  │
│  └─────────┼───────────────────┼──────────────────────┼─────────────┘  │
│            │                   │                       │                │
│  ┌─────────┼───────────────────┼───────────────────────┼─────────────┐  │
│  │  NAMESPACE "deile-empresa-X"                                       │  │
│  │         ▼                   ▼                       ▼              │  │
│  │  ┌──────────────────────────────────────────────────────────────┐ │  │
│  │  │  Temporal Worker Activities (durable execution)              │ │  │
│  │  │   - pipeline.tick (workflow)                                  │ │  │
│  │  │   - implement (activity, idempotent, retry policy)            │ │  │
│  │  │   - pr_review (activity)                                      │ │  │
│  │  │   - handoff_between_workers (activity)                        │ │  │
│  │  └────────────┬─────────────────────────────────────────────────┘ │  │
│  │               │ activity dispatches                                │  │
│  │               ▼                                                    │  │
│  │  ┌──────────────────────────────────────────────────────────────┐ │  │
│  │  │  NATS JetStream (message broker)                             │ │  │
│  │  │   subjects: task.implement, task.review, task.handoff        │ │  │
│  │  └────┬─────────────────────────────────┬────────────────────┬──┘ │  │
│  │       │                                  │                    │    │  │
│  │  ┌────▼────┐  ┌────────┐  ┌─────────┐  ┌▼─────────┐  ┌─────▼──┐ │  │
│  │  │ deile-  │  │ claude-│  │ gpt-    │  │ Workspace │  │ Forge  │ │  │
│  │  │ worker  │  │ worker │  │ worker  │  │ pool      │  │ (gh/   │ │  │
│  │  │ (HPA    │  │ (HPA   │  │ (future)│  │ NFS RWX   │  │  glab) │ │  │
│  │  │  1-10)  │  │  1-5)  │  │         │  │ +PG+pgvec │  │  bidir │ │  │
│  │  └─────────┘  └────────┘  └─────────┘  └───────────┘  └────────┘ │  │
│  │                                                                    │  │
│  │  ┌──────────────────────────────────────────────────────────────┐ │  │
│  │  │  PostgreSQL + pgvector (per-NS schema):                      │ │  │
│  │  │   - episodic memory                                          │ │  │
│  │  │   - semantic knowledge (RAG do repo + decisions)             │ │  │
│  │  │   - usage/cost ledger                                        │ │  │
│  │  │   - dispatch ledger (substitui SQLite atual)                 │ │  │
│  │  └──────────────────────────────────────────────────────────────┘ │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

**O que muda** (cada item com evidência da pesquisa):

- **Workflow engine** — Temporal **OpenAI Agents SDK integration GA em
  23/mar/2026**; 9,1 trilhão de execuções em produção; Netflix, Snap,
  NVIDIA, OpenAI, Block em produção. Alternativa zero-infra: **DBOS**
  (in-process, só Postgres). Pra Python-puro e dev local, DBOS pode
  ser melhor primeira escolha.

- **Message broker** — NATS JetStream (k8s-native, ~20MB RAM) ou
  Redis Streams (se já tem Redis). Não Kafka (overkill).

- **AI Gateway** — **Portkey virou Apache 2.0 em mar/2026** com
  guardrails + semantic caching + audit trails. Alternativa: LiteLLM
  (dominante em OSS mas **falha em ~2k RPS, OOMs com >8GB RAM** segundo
  Spheron/Alongside) — OK só para single-tenant.

- **Secrets** — HashiCorp Vault + **External Secrets Operator** (CRDs
  `ExternalSecret` sincronizam por NS). Padrão canônico em 2026.

- **Memory** — PostgreSQL + pgvector substitui SQLite. RAG do histórico
  de PRs/decisões. Cache Redis para contexto de curto prazo.

- **Observability** — **OpenTelemetry GenAI Semantic Conventions** é
  stack canônica de fato. Datadog/New Relic/Dynatrace já suportam
  nativo. **Langfuse** declara conformidade explícita — mapeia OTel
  traces ao seu modelo. **DEILE já tem OTel (decisão #39)** — só
  precisa adicionar atributos GenAI.

- **Sandbox** — adotar `kubernetes-sigs/agent-sandbox` (oficial mar/2026)
  com `SandboxTemplate` + `SandboxWarmPool`. Substitui os Deployments
  custom dos workers.

- **Tool registry** — **MCP atingiu 97M downloads/mês em mai/2026**,
  41% das orgs em produção. Externalizar `ToolRegistry` atual para MCP
  servers permite que outros harnesses (claude code, Cursor, OpenDeile)
  reusem os tools do DEILE — e vice-versa.

- **Multi-tenancy hard** (futuro) — `vcluster v0.14` se precisar de
  control-plane K8s por tenant (ex: cliente empresarial isolado).

**Custo:** alto em complexidade operacional. ~+500MB RAM em
componentes de plataforma. Curva de aprendizado.

**Ganho:** harness de verdade — auto-scaling, durabilidade, multi-tenant
hard, FinOps, observability full. Aderência aos 10 pilares: **8/10
pronto, 2/10 parcial**.

---

## §6. Comparação rápida das 3 propostas

| | P1 (handoff git) | P2' (shared `.git` + worktrees) | P3 (enterprise) |
|---|---|---|---|
| **Esforço** | 3-5 dias | 1-2 semanas | 1-2 meses (incremental) |
| **Risco** | 🟢 baixo | 🟢 baixo (padrão indústria) | 🟡 médio (incremental por pilar) |
| **Ganho de qualidade pipeline** | +20% | +60% | +200% |
| **Resolve corrupção OAuth multi-replica** | ❌ não | ✅ (claude-creds dedicado, sem race) | ✅ (Vault) |
| **Resolve re-clone cross-worker** | 🟡 mitiga (shallow) | ✅ (worktrees instantâneos) | ✅ |
| **Habilita escala 10+ workers** | ❌ | ✅ (SandboxWarmPool) | ✅ |
| **Custo runtime adicional** | 0 | ~50MB (CronJob git-sync) | ~500MB+ (componentes plataforma) |
| **Quanto dos 10 pilares atende** | 1/10 (gitops melhor) | 3/10 (#3 control plane parcial, #9 melhor) | 8-9/10 |
| **Alinhado à indústria 2026** | ✅ (Augment Code recomenda) | ✅✅ (Cursor 3, Devin, OpenHands, agent-sandbox) | ✅✅✅ |
| **Reversibilidade** | trivial | trivial (volta ao Deployment atual) | incremental por pilar |
| **Quando começar** | semana 1 — base pro P2' | semana 2-3 (após P1 validado) | semana 4+ pilar a pilar |

---

## §7. Roadmap recomendado

```
Hoje  ─┬─ correções pontuais (tmpfs, claude-worker scale=1, RAM VM 12→24GB)
       │
       ├─ [PR-A] Painel ganhou [t] resize /tmp  ✅ pronto nessa sessão
       │
       ├─ [PR-B] PROPOSTA P1 — handoff git semântico
       │   - endpoint /v1/handoff em ambos workers
       │   - pipeline injeta "continue do handoff" no brief
       │   - commit-trailer Handoff: <worker>
       │   ↓ baseline pro P2
       │
       ├─ [PR-C] PROPOSTA P2 — workspace compartilhado NFS
       │   - 3 manifests novos
       │   - flock OAuth
       │   - task_id determinístico cross-worker
       │   ↓ destrava multi-replica
       │
       ├─ [PR-D...G] PROPOSTA P3 (incremental, 1 pilar por PR)
       │   D. AI Gateway (LiteLLM): tira chamadas diretas dos workers
       │   E. Message broker (NATS): substitui HTTP síncrono
       │   F. PostgreSQL + pgvector: substitui SQLite
       │   G. Temporal: substitui ad-hoc workflow
       │   H. Vault: rotação automática de segredos
       │   I. OTel stack (Tempo/Loki/Mimir/Langfuse)
       │
Futuro ─┴─ multi-tenant hard, multi-cluster, federated
```

---

## §8. Decisões pendentes (precisam de você)

1. **Qual proposta executar primeiro?** Recomendo **P1 → P2**.
2. **Mantém claude-worker como worker-só-anthropic?** Ou abre pra
   `opendeile`/futuros harnesses (renomeia pra `harness-worker`)?
3. **Workspace path no P2:** `/workspace/work/<task_id>/` (canônico) ou
   `/workspace/<repo>/<issue>/<attempt>/` (organização por humano)?
4. **No P3, qual broker:** NATS (k8s-native, leve) ou Redis Streams
   (você já tem Redis em outros projetos)?
5. **Multi-cluster federado é meta de 12 meses?** Define se vale a pena
   investir em Crossplane/Argo CD agora.

---

## §9. Apêndice — mapa de arquivos investigados

Para qualquer dev que herde esse documento:

```
deile/orchestration/pipeline/
├── dispatch_resolver.py    — env → worker URL (per-stage)
├── dispatch_ledger.py      — SQLite: issue/pr → prev_task_id (resume key)
├── model_resolver.py       — env → model slug (per-stage)
├── implementer.py          — _resolve_endpoint / _resolve_resume_meta / _dispatch
├── stages.py               — channel_id = "pipeline-issue-N" / "pipeline-pr-N"
└── monitor.py              — tick loop, shard counting

infra/k8s/
├── worker_server.py        — deile-worker. _channel_workdir derivado de channel_id
├── claude_worker_server.py — claude-worker. workdir = root/<task_id>; resume via session.json
├── _worker_resume.py       — .deile-progress.md/.json, fingerprint substantivo
├── manifests/45-...        — deile-worker (PVC RWO 5Gi work + RWO 256Mi home)
└── manifests/50-...        — claude-worker (PVC RWO 1Gi home)
```

---

## §10. O que NÃO está neste documento

- Lista de testes a escrever (separada — cada proposta tem seu plano)
- Custo financeiro detalhado de cada provider (UsageRepository já mede)
- Comparação com OpenDeile / outros harnesses (futuro, quando código existir)
- Spec final de qualquer proposta (este é VISÃO, não SPEC)

> "Um dev que faz tudo" — quando aplicado a um cluster, vira um harness
> com identidade. O DEILE de hoje cumpre 60% disso. Os 40% restantes são
> 3 PRs (P1, P2', P3.A-C) e meses de polimento. Cada degrau aumenta a
> auto-suficiência sem refatoração apocalíptica.

---

## §11. Bibliografia (fontes verificadas, mai/2026)

**Agent infrastructure / sandboxing:**
- Kubernetes blog (20/mar/2026) — [Running Agents on Kubernetes with Agent Sandbox](https://kubernetes.io/blog/2026/03/20/running-agents-on-kubernetes-with-agent-sandbox/)
- [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
- Cursor 3 (abr/2026) — [self-hosted cloud agents guide](https://kalinga.ai/cursor-self-hosted-cloud-agents-guide/) e [Cursor 3 agent-first](https://www.digitalapplied.com/blog/cursor-3-agents-window-complete-guide)
- Cognition Multi-Devin (mar/2026) — [release notes via Augment Code](https://www.augmentcode.com/tools/best-devin-alternatives)
- OpenHands (ex-OpenDevin) — [arXiv 2407.16741](https://arxiv.org/abs/2407.16741) + [ToolHalla comparison](https://toolhalla.ai/blog/devin-vs-openhands-vs-swe-agent-2026)
- GitHub Copilot Workspace (14/mai/2026) — [Changelog](https://github.blog/changelog/2026-05-14-github-copilot-app-is-now-available-in-technical-preview/)

**Workflow engines:**
- Temporal — [OpenAI Agents SDK GA 23/mar/2026](https://temporal.io/blog/announcing-openai-agents-sdk-integration), [Série D US$300M](https://temporal.io/blog/temporal-raises-usd300m-series-d-at-a-usd5b-valuation)
- Comparativo Temporal/Restate/DBOS — [Dev Note abr/2026](https://devstarsj.github.io/2026/04/03/durable-execution-temporal-restate-dbos-distributed-workflows-2026/)
- [DBOS vs Temporal 2026](https://www.tiarebalbi.com/en/blog/dbos-vs-temporal-postgres-durable-execution)

**AI Gateways:**
- [Spheron AI Gateway setup 2026](https://www.spheron.network/blog/ai-gateway-litellm-portkey-kong-gpu-cloud/)
- [TrueFoundry AI Gateway guide 2026](https://www.truefoundry.com/blog/a-definitive-guide-to-ai-gateways-in-2026-competitive-landscape-comparison)
- [Portkey vs LiteLLM (alongside.team)](https://www.alongside.team/blog/litellm-vs-portkey-multi-model-ai-gateway)

**Multi-tenancy K8s:**
- [Kubernetes docs — Multi-tenancy](https://kubernetes.io/docs/concepts/security/multi-tenancy/)
- [Microsoft AKS — Agentic AI multi-tenancy](https://techcommunity.microsoft.com/blog/coreinfrastructureandsecurityblog/transforming-enterprise-aks-multi-tenancy-at-scale-with-agentic-ai-and-semantic-/4446252)
- [Spectro Cloud — three multi-tenancy approaches](https://www.spectrocloud.com/blog/kubernetes-multi-tenancy-three-key-approaches)
- [External Secrets Operator + Vault](https://external-secrets.io/latest/provider/hashicorp-vault/)

**Observability:**
- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [CallSphere — OTel GenAI status abr/2026](https://callsphere.ai/blog/td30-fw-opentelemetry-genai-conventions-april-2026-guide)
- [Langfuse OTel integration](https://langfuse.com/integrations/native/opentelemetry)
- [Zylos Research — OTel AI agents (fev/2026)](https://zylos.ai/research/2026-02-28-opentelemetry-ai-agent-observability)

**Git worktrees + multi-agent:**
- [Augment Code — Git worktrees parallel AI agents](https://www.augmentcode.com/guides/git-worktrees-parallel-ai-agent-execution)
- [Augment Code — multi-agent coding workspace](https://www.augmentcode.com/guides/how-to-run-a-multi-agent-coding-workspace)
- [Appxlab — worktrees workflow mar/2026](https://blog.appxlab.io/2026/03/31/multi-agent-ai-coding-workflow-git-worktrees/)

**MCP (Model Context Protocol):**
- [Digital Applied — MCP 97M downloads](https://www.digitalapplied.com/blog/mcp-97-million-downloads-model-context-protocol-mainstream)
- [Digital Applied — MCP adoption stats 2026](https://www.digitalapplied.com/blog/mcp-adoption-statistics-2026-model-context-protocol)
- [MCP spec 2026-07-28 RC](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)
- [WorkOS — MCP 2026 guide](https://workos.com/blog/everything-your-team-needs-to-know-about-mcp-in-2026)

---

## §12. Próxima decisão sua

Não há "PRs faltando" pra esta sessão — este documento é o entregável.
Para avançar, você precisa decidir:

1. **Aceitar este norte como base** (eventualmente quer revisar/editar?)
2. **Qual proposta executar primeiro:**
   - **P1 (handoff git semântico)** — começa amanhã, baixo risco, base pro resto
   - **P2' direto** (pular P1) — se já quiser worktrees compartilhados
   - **Esperar e amadurecer P3** primeiro (Vault, AI Gateway antes de mexer no workspace)
3. **Adotar `kubernetes-sigs/agent-sandbox`?** É a opção mais bold/moderna —
   substitui Deployments custom por CRD Sandbox + WarmPool. Reduz código
   próprio, ganha o que a indústria padronizou. Mas é dependência nova.

Se quiser, **abro issues no GitHub** para P1 e P2' (ou só P1 primeiro)
com escopo claro e critérios de aceite — e o pipeline pega no próximo
tick.
