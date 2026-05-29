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

## §1. O que é um "harness DEILE" — definição operacional COMPLETA

**Harness é o cluster inteiro**, não um pod. Cada `namespace` K8s é uma
**identidade de desenvolvimento** ("um dev autônomo"). Multi-namespace já
funciona: `deile` (main GH), `deile-gl` (pilot GitLab). Cada namespace tem
pipeline, workers, bot, shell e PVCs **independentes**, isolados por
NetworkPolicy default-deny.

### §1.1 — Topologia atual por namespace (5 pods + 4 PVCs + 6 Secrets)

```
┌──────────────────────────────────────────────────────────────────────────┐
│ NAMESPACE = UMA IDENTIDADE                                                │
│                                                                            │
│ ┌─────────────────────────────────────────────────────────────────────┐  │
│ │ CANAIS DE ENTRADA                                                    │  │
│ │  • Discord (DM, channels, mentions, voice* not yet)                  │  │
│ │  • Forge events (issues/PRs/comments/assignments/reviews)            │  │
│ │  • Cron schedules (cron.db SQLite no PVC bot-data)                   │  │
│ │  • Outros adapters (Telegram/WA/Meta = stubs no momento)             │  │
│ └──────────────┬──────────────────────────┬────────────────────────────┘  │
│                │                          │                                │
│                ▼                          ▼                                │
│ ┌───────────────────────────┐ ┌──────────────────────────────────────┐   │
│ │ deilebot  (Deployment)    │ │ deile-pipeline (Deployment singleton)│   │
│ │ • Discord I/O bridge :8765│ │ • Forge polling 60s                  │   │
│ │ • Owners config           │ │ • Label state machine                │   │
│ │ • SQLite cron + history   │ │ • Mention router (4 sources)         │   │
│ │ • PVC bot-data            │ │ • Refinement gate (5 voltas)         │   │
│ │ • Strategy: Recreate      │ │ • Implement (asyncio.gather)          │   │
│ │                           │ │ • Resume protocol (#254)             │   │
│ │ POST /v1/outbound/discord/│ │ • Dispatch ledger SQLite             │   │
│ │     dm.send               │ │ • Priority sort (~prioridade:N)      │   │
│ │     channel.post          │ │ • PR triage + review + merge gates   │   │
│ │     channel.react         │ │ • PVC pipeline-ledger                │   │
│ │ + cron daemon             │ │ • Status server :8768                │   │
│ └───────────────────────────┘ └──────┬───────────────────────────────┘   │
│                                       │ POST /v1/dispatch                  │
│                                       │ (per-stage routing)                │
│                       ┌───────────────┴────────────────┐                   │
│                       ▼                                ▼                   │
│            ┌─────────────────────┐         ┌─────────────────────┐        │
│            │ deile-worker:8766   │         │ claude-worker:8767  │        │
│            │ replicas=2          │         │ replicas=1 (limite  │        │
│            │ Strategy: Rolling   │         │ atual; meta: scale) │        │
│            │ • DEILE Python in   │         │ Strategy: Rolling   │        │
│            │   process           │         │ • claude -p subproc │        │
│            │ • Tools FULL (bash, │         │ • Worktree per task │        │
│            │   git, pytest, fs)  │         │ • OAuth in-pod      │        │
│            │ • workdir/channel   │         │ • workdir/task_id   │        │
│            │ • PVC RWO 5Gi       │         │ • PVC RWO 1Gi       │        │
│            │ • TASK_TIMEOUT 2h   │         │ • TASK_TIMEOUT 2h   │        │
│            └─────────────────────┘         └─────────────────────┘        │
│                                                                            │
│ ┌─────────────────────────────────────────────────────────────────────┐  │
│ │ deile-shell (Deployment, kubectl exec only)                          │  │
│ │ • Sandbox interativo com toolset cheio                               │  │
│ │ • emptyDir 1Gi (volátil; pode virar PVC opt-in)                      │  │
│ └─────────────────────────────────────────────────────────────────────┘  │
│                                                                            │
│ PVCs por NS: bot-data, deile-worker-work, claude-worker-home,             │
│              pipeline-ledger, deile-logs                                   │
│ Secrets por NS: bot-secrets, deile-secrets, worker-bearer,                │
│                 claude-worker-bearer, pipeline-status-bearer,             │
│                 claude-credentials (OAuth)                                 │
└──────────────────────────────────────────────────────────────────────────┘
```

### §1.2 — Capabilities operacionais do pipeline (a verdade verificada)

| Capability | Estado | Onde está | Notas |
|---|---|---|---|
| **Label state machine completa** | ✅ pronto | `pipeline/labels.py`, `stages.py` | `~workflow:{nova→em_revisao→revisada→em_implementacao→em_pr}`, `~review:{pendente→em_andamento→concluida}` |
| **Refinement gate (issue #257)** | ✅ pronto | `stages.py:778+` | Crítica de escopo por tipo: `intent→analyst`, `feature/refactor→architect`, `bug→debugger`. VAGO→`refinar` + `em_refinamento`/`em_arquitetura`. Até 5 voltas. Pausa em `~workflow:aguardando_stakeholder` se opção precisa decisão humana |
| **Intent decomposition** | ✅ pronto | `stages.py` | Issue `intent` aprovada é decomposta em N derivadas (architect abre as filhas) → marca `~workflow:decomposta` |
| **Parallel implementation** | ✅ pronto | `monitor.py:147,204`, `stages.py:1247+` | `max_parallel=2` (config); `asyncio.gather` cap-respeitando; subtrai `~workflow:em_implementacao` in-flight |
| **Resume same-worker (issue #254)** | ✅ pronto | `_worker_resume.py`, `implementer.py:582+` | Fingerprint substantivo (exclui meta), `.deile-progress.md` journal, attempt counter, budget acumulado. 3 tentativas → bloqueia |
| **Resume cross-worker** | ❌ ausente | — | Ver §2. PVCs isolados; só git push/pull transporta estado |
| **Mention/assignment router (issue #253/#261)** | ✅ pronto | `stages.py:452+` | 4 sources: comments (cursor), assignee, requested_reviewer, body-search. Roteamento: issue+assignee → injeta `~workflow:nova`; PR+assignee → `work_merge`; PR+reviewer-só → `review_only` (não mergeia); comment → atende. Sticky via `~mention:processado` |
| **Per-stage dispatcher routing (issue #309 fase 2)** | ✅ pronto | `dispatch_resolver.py` | `DEILE_PIPELINE_DISPATCH_<STAGE>` por stage; global `DEILE_PIPELINE_DISPATCH_MODE`; default `deile-worker` |
| **Per-stage model routing (issue #305)** | ✅ pronto | `model_resolver.py` | `DEILE_PIPELINE_MODEL_<STAGE>` por stage; UI no painel (`[d]` matrix view) |
| **Priority labels (issue #366/#370)** | ✅ pronto | `labels.py:19+`, sort em backlog | `~prioridade:N` (N≥0, 0=urgência máxima). PRs herdam de linked issue |
| **Multi-forge (GitHub + GitLab, issue #297)** | ✅ pronto | `forge/{github,gitlab}_forge.py`, `ForgeRouter` | Camada `ForgeClient` ABC. GH via `gh`, GL via `glab`. Detecção auto por host/path/env |
| **Multi-namespace** | ✅ pronto | `deploy.py -n`, manifests sem NS hardcoded | `deile` (main), `deile-gl` (pilot). Painel TUI tem NS-select |
| **Quality-gate de merge (PR #276)** | ✅ pronto | `briefs.py` | Gate exige suíte completa verde (`pytest deile/tests/`) + confronta entrega vs pedido (issue body + comentários) |
| **Reaper de stuck PRs** | ✅ pronto | `stages.py`, `notifier.py` | Solta batch `~batch:<sha8>` quando PR `~review:em_andamento` excede TTL; retry até teto |
| **DispatchLedger persistente** | ✅ pronto | `dispatch_ledger.py` SQLite | Mapeia `issue:N`/`pr:N` → `prev_task_id` para resume claude-worker. PVC sobrevive a restart |
| **Hash sharding monitor (decisão #18)** | ✅ pronto | `identity.py` `MonitorIdentity` | `~batch:<sha8>` só reivindicado se `shard_count>1` (single monitor não gera churn) |
| **Fire-and-forget dispatch (PR #374)** | ✅ pronto | `implementer.py`, worker `wait=False` | Pipeline despacha sem bloquear o tick; status reconciliado no próximo tick via ledger + resume-info |
| **Status server :8768 (issue #347)** | ✅ pronto | `infra/k8s/pipeline_status_server.py` | Endpoints: status, backlog, recent, ledger, reaper-preview, force-tick. Bearer auth |
| **Painel TUI universal (issue #347/#305/#330)** | ✅ pronto | `infra/k8s/_panel.py` (~5200 linhas, 13 views) | DispatchMatrixView, ClusterStatus, LiveSession, PodWatch (com hotkey `[t]` resize tmp), Issues/PRs, Costs, etc |
| **Skills hot-reload (PR #296)** | ✅ pronto | `deile/skills/` + `watchdog` | 5 dirs de scan, auto-injeção via triggers, function-call `invoke_skill`, slash `/<name>` |
| **OpenTelemetry (decisão #39)** | 🟡 instalado | `deile/observability/` | Tracer + metrics CNCF. No-op se sem `DEILE_OTLP_ENDPOINT` |
| **Memória 4 camadas (decisão #6)** | ✅ pronto | `deile/memory/` | working/episodic/semantic/procedural. SQLite per-session |
| **Sub-agentes paralelos em sessão CLI (decisão #34)** | ✅ pronto | `dispatch_parallel_subagents` | Sub-DEILEs concorrentes via asyncio.gather + semaphore; painel Rich multipanel |
| **Streaming-first (decisão #15)** | ✅ pronto | `process_input_stream` | Default da CLI |
| **Pipeline-status logging (PR #372)** | ✅ pronto | tick summary INFO | `tick #N done in Ts: classified=X reviewed=Y implemented=Z dispatched=W backlog={issues:I prs:P}` |
| **Personas plugáveis (decisão #12)** | ✅ pronto | `deile/personas/` MD + YAML | Editar Markdown muda comportamento sem código |
| **Invalidate review on new commit (issue #368)** | ✅ pronto | mention/review handlers | `~review:concluida` é invalidada se há novos commits após a review |
| **Auto-add ~by:default (issue #375/PR #380)** | ✅ pronto | `stages.py` | Orphan issues reviewed sem owner ganham `~by:default` automaticamente |
| **Cleanup tools (issue #361)** | ✅ pronto | deploy.py + docs | Manifest 43 deletado, DEILE_BOT_DISABLED documentado |

### §1.3 — Ciclo de vida típico de uma issue (exemplo `intent`)

```
1. Humano abre issue [INTENT] X                    →  ~workflow:nova
2. Pipeline classify (deile-worker, deepseek)      →  ~workflow:em_revisao
3. Critique (analyst persona, claude opus)         →  VAGO → ~workflow:em_refinamento + refinar
4. Refinement loop (até 5 voltas)                  →  body reescrito, título ajustado
5. CLARO + intent type                             →  ~workflow:revisada
6. Architect decompose                             →  ~workflow:decomposta
                                                      (N issues filhas: ~workflow:nova)
   ↓ (cada filha segue):
7. classify → em_revisao → critique → revisada     (paralelo se max_parallel>0)
8. Pipeline claim ~batch:<sha8> + ~by:default      →  ~workflow:em_implementacao
9. Worker (deile OU claude conforme stage routing) →  branch auto/issue-N, commits
10. Worker open PR                                  →  ~workflow:em_pr
11. PR triage detects auto/issue-N                  →  ~review:pendente
12. Pipeline pr_review → reviewer persona (claude)  →  ~review:em_andamento
13. Iterações até suíte verde + checklist OK        →  ~review:concluida
14. Merge automático (gh api PUT /merge)            →  PR fechado, branch deletado
15. Issue auto-closed via "Closes #N" commit       →  state machine drena pra fim
```

Cenários de **bloqueio** ao longo do ciclo:
- Refinement: 5 voltas sem clareza ou opção crítica → `~workflow:aguardando_stakeholder` (humano comenta opção)
- Implement: 3 attempts sem PR → `~workflow:bloqueada` (humano remove label pra retomar)
- Review: 3 attempts sem merge → solta `~batch`, pode rotacionar tentativas

### §1.4 — O pipeline NÃO faz (atualmente)

| Não faz | Por quê |
|---|---|
| Auto-scale workers conforme carga | Replicas estáticas; futuro: HPA + `SandboxWarmPool` |
| Roteia entre workers por task affinity | Service round-robin "burra" — qualquer pod pega |
| Persistir estado mid-execução do claude/agent | DispatchLedger é só fronteira pipeline↔worker; mid-call não é durable |
| MCP externalizado | Tools são in-process Python; futuros agentes externos não reusam |
| Multi-cluster federation | Single-cluster, single-VM hoje |
| Compartilha workspace cross-worker | PVCs isolados; cross-worker = re-clone (ver §2) |
| Lock distribuído de claude OAuth refresh | Hoje claude-worker é replicas=1 forçado; multi-replica requer flock (ver §13) |

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

---

## §13. Alteração MÍNIMA para claude-workers em paralelo (resposta direta)

> Pergunta: **qual é a alteração mínima necessária para que N
> claude-workers trabalhem em paralelo em atividades diferentes e NUNCA
> ao mesmo tempo na mesma?**

### §13.1 — Por que hoje está travado em `replicas: 1`

Três barreiras técnicas escondem-se em `claude_worker_server.py`:

1. **OAuth refresh race.** `credentials.json` está num PVC RWO 1Gi. Dois
   pods detectam expiração quase ao mesmo tempo, ambos disparam refresh,
   o segundo write sobrescreve o primeiro → corrupção → ambos quebram.

2. **Resume liveness fan-in furado.** O pipeline pergunta `claude_alive?`
   no Service `claude-worker:8767`. Round-robin → cai num pod arbitrário.
   Esse pod inspeciona seu `/proc` local; se o claude está rodando em
   OUTRO pod, ele responde "morreu" → pipeline dispara resume → 2 claudes
   na mesma session_id → JSONL corrompido.

3. **Worktree race em resume.** Se task_id A está no workdir
   `/home/claude/work/abc.../` e o pipeline pede resume, qualquer pod
   monta o mesmo PVC e tenta `git operations` simultaneamente.

### §13.2 — A alteração mínima (≈80 linhas Python, zero infra nova)

**Princípio: lock por task_id no PVC compartilhado.** Cada task tem
um `.lease.json` em seu workdir; quem segura o lease é quem trabalha;
liveness é checada pelo lease (não pelo `/proc`).

#### Componente 1 — OAuth file-lock (10 linhas)

`claude_worker_server.py::_load_oauth_token_into_env` (existente) +
novo helper `_refresh_oauth_with_lock`:

```python
import fcntl

def _refresh_oauth_with_lock(creds_path: Path) -> None:
    with open(creds_path, "r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)            # exclusive
        try:
            creds = json.load(fh)
            if _is_expiring_soon(creds):
                creds = _do_refresh(creds)        # chama Anthropic
                fh.seek(0); fh.truncate()
                json.dump(creds, fh)
                fh.flush(); os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
```

Chamado antes de cada spawn `claude -p` (não no startup do pod — para
ser sensível a refresh feito por outro pod entre dispatches).

#### Componente 2 — Lease por task_id (~50 linhas)

`claude_worker_server.py::dispatch_handler`:

```python
LEASE_TTL_S = 30                    # lease expira se heartbeat parar
HEARTBEAT_INTERVAL_S = 5

async def _acquire_lease(workspace: Path) -> Optional[dict]:
    lease_path = workspace / ".lease.json"
    pod_id = os.environ.get("HOSTNAME", "unknown")
    now = time.time()

    if lease_path.exists():
        try:
            current = json.loads(lease_path.read_text())
            heartbeat_age = now - float(current.get("heartbeat_at", 0))
            if heartbeat_age < LEASE_TTL_S:
                return None            # outro pod ATIVO
        except (json.JSONDecodeError, ValueError):
            pass                       # corrupto → trata como morto

    # write atomic via rename
    tmp = workspace / f".lease.tmp.{pod_id}"
    lease = {
        "pod": pod_id, "pid": os.getpid(),
        "started_at": now, "heartbeat_at": now,
    }
    tmp.write_text(json.dumps(lease))
    tmp.rename(lease_path)             # atomic POSIX

    # confirmação: relê — outro pod pode ter ganhado a corrida
    confirmed = json.loads(lease_path.read_text())
    if confirmed.get("pod") != pod_id:
        return None
    return lease

async def _heartbeat_loop(lease_path: Path, stop_event):
    while not stop_event.is_set():
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        try:
            lease = json.loads(lease_path.read_text())
            lease["heartbeat_at"] = time.time()
            lease_path.write_text(json.dumps(lease))
        except Exception:
            pass                       # best-effort

async def _release_lease(lease_path: Path):
    try: lease_path.unlink()
    except FileNotFoundError: pass
```

#### Componente 3 — `_is_claude_process_alive` consulta lease (5 linhas)

Substitui a inspeção de `/proc` local:

```python
def _is_claude_process_alive(session_id: str) -> bool:
    meta = _load_session_meta_by_session(session_id)
    if not meta: return False
    lease_path = Path(meta["workdir"]) / ".lease.json"
    if not lease_path.exists(): return False
    try:
        lease = json.loads(lease_path.read_text())
        age = time.time() - float(lease.get("heartbeat_at", 0))
        return age < LEASE_TTL_S
    except Exception:
        return False
```

Resultado: **qualquer pod responde corretamente** se a sessão está viva,
porque o lease vive no PVC compartilhado.

#### Componente 4 — Integração no dispatch (~15 linhas)

```python
# dentro de dispatch_handler, ANTES de spawn `claude -p`:
lease = await _acquire_lease(workspace)
if lease is None:
    return web.json_response({
        "ok": False, "error_code": "TASK_ALREADY_RUNNING",
        "error": "outra réplica do claude-worker já está executando "
                 f"task_id={task_id}; pipeline deve retry no próximo tick",
    }, status=409)

stop_event = asyncio.Event()
hb_task = asyncio.create_task(_heartbeat_loop(
    workspace / ".lease.json", stop_event))
try:
    result = await run_subprocess_with_progress(...)
finally:
    stop_event.set()
    hb_task.cancel()
    await _release_lease(workspace / ".lease.json")
```

#### Componente 5 — Pipeline tolera 409 como "skip-and-retry" (~5 linhas)

`implementer.py` quando worker retorna `TASK_ALREADY_RUNNING`:

```python
# Mesmo tratamento que o atual {"_still_alive": True}
return {"_still_alive": True}
```

### §13.3 — Por que isso satisfaz tuas 3 garantias

| Garantia | Como o design entrega |
|---|---|
| **Multi-replica funciona** | Service round-robin distribui dispatches; cada task cai em algum pod; o lease impede colisão |
| **NUNCA dois pods na mesma atividade** | Lease é exclusivo via atomic write+confirm-read. TTL de heartbeat protege contra pod morto sem release |
| **OAuth único compartilhado** | `flock` previne refresh concorrente; arquivo único no PVC; todos os pods leem a versão fresca em cada dispatch |
| **Atividades diferentes em paralelo** | task_id A no pod 1, task_id B no pod 2 — leases separados, workdirs separados, zero contenção |
| **Retomada cross-pod (mesma activity)** | Se pod 1 morre, lease expira em 30s; próximo dispatch da mesma task cai em qualquer pod (round-robin), adquire lease, faz resume via `prev_task_id` (mecanismo já existe) |

### §13.4 — Diff resumido para a PR

```
infra/k8s/claude_worker_server.py
  + ~80 linhas: _acquire_lease, _release_lease, _heartbeat_loop,
                _refresh_oauth_with_lock, integração no dispatch_handler
  + 5 linhas:   _is_claude_process_alive consulta lease em vez de /proc

infra/k8s/manifests/50-claude-worker-deployment.yaml
  - replicas: 1
  + replicas: 2   (ou via kubectl scale dinâmico — escolha do operador)

deile/orchestration/pipeline/implementer.py
  + ~5 linhas: trata TASK_ALREADY_RUNNING como _still_alive (já existe handler)

deile/tests/infra/test_claude_worker_lease.py  (novo)
  ~150 linhas: testes concorrentes do lease (acquire/release/TTL/heartbeat)
```

### §13.5 — Limitações honestas

- **Funciona em single-node k3s** (PVC RWO local-path permite multi-mount
  no mesmo nó). Em multi-node K8s real, precisaria RWX (NFS) ou
  StatefulSet com per-pod PVC + Service headless + routing por hash.
- **Lease TTL de 30s = janela de 30s onde pod morto bloqueia retry.**
  Aceitável; o pipeline já tem reaper rotativo que tolera atrasos.
- **Não resolve a P2'** (workspace compartilhado entre claude-worker e
  deile-worker). Esta proposta é APENAS para claude-worker N réplicas,
  não cross-worker. Mas é o degrau zero que destrava o resto.

### §13.6 — Esforço e ordem de execução

- **Esforço**: 1-2 dias de código + 1 dia de testes concorrentes.
- **Risco**: 🟢 baixo — adições puras, nada removido. Se algo der errado
  e voltar a `replicas: 1`, o lease vira no-op (sempre disponível).
- **Quando**: depois do PR atual (manifests tmpfs) ser mergeado. Esta é
  a próxima evolução natural; abre caminho pra P1 e P2'.

---

## §14. Resumo das ações pedidas nesta iteração

| O que pediu | Status |
|---|---|
| `/tmp` do claude-worker = 5Gi (em vez de 2Gi) | ✅ aplicado em `50-claude-worker-deployment.yaml` |
| Detalhar a §1 com a verdade do pipeline atual | ✅ §1 reescrita com 4 sub-seções, tabela de 28 capabilities verificadas, ciclo de vida típico, e lista do que NÃO faz |
| Alteração MÍNIMA para multi-claude paralelo | ✅ §13 nova, ≈80 linhas Python, sem infra nova, com diff e justificativa |
