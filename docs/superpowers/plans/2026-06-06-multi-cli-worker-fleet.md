# Frota multi-CLI: workers plugáveis (OpenCode, Codex, Qwen, Aider, Goose, Antigravity) + Claude como um entre vários — Plano de Implementação

> **Para workers agênticos:** SUB-SKILL recomendada — `superpowers:subagent-driven-development` ou `executing-plans`. Passos usam checkbox (`- [ ]`).

**Goal:** Generalizar o padrão `claude-worker` num framework de **N workers CLI plugáveis**, cada um rodando um agente de coding headless (one-shot) num pod K8s isolado, despachável por-stage pelo pipeline. O `claude-worker` passa a ser **um entre vários**; o operador escolhe, por stage, qual CLI + qual modelo, com o grosso roteável a providers baratos (DeepSeek/Qwen/Gemini via OpenRouter), neutralizando o lock de preço.

**Architecture:** Um **servidor genérico** (`cli_worker_server.py`) parametrizado por um **adapter por CLI** (selecionado por env `DEILE_CLI_WORKER_KIND`). O servidor reaproveita TODA a maquinaria genérica do `claude_worker_server.py` (lease/heartbeat, session-metadata, cleanup, workspace isolado, contrato HTTP `/v1/dispatch`); o adapter especializa apenas **5 pontos**: montar argv headless, parsear saída, listar modelos, env de auth, dirs graváveis. Integra ao `dispatch_resolver` (cada CLI vira um dispatcher válido) e ao painel (`DispatchMatrixView`). Imagens **per-tool** via build-arg (runtimes divergem). Plano separado integra **OpenRouter** ao DEILE CLI (deile-worker in-process) e como provider unificador da frota.

**Tech Stack:** Python 3.11 (aiohttp server), K8s (Rancher Desktop/k3s), Docker multi-target por build-arg, CLIs: OpenCode (binário), OpenAI Codex (rust/npm), Qwen Code (node22/npm), Aider (pip), Goose (binário), Antigravity `agy` (go binário, **gated**).

---

## ⚠️ POR QUE AGORA — urgência da escalabilidade (contexto de cobrança jun/2026)

A partir de **15/jun/2026** a Anthropic separa a cobrança do uso **programático** (`claude -p` / Agent SDK — exatamente o que o `claude-worker` faz) do uso interativo: a frota deixa de consumir do pool da assinatura e passa a um **crédito mensal separado e menor** (Pro $20 / Max 5x $100 / Max 20x $200), e ao esgotar **para** ou cobra **preço de API cheio**. Fonte: [Anthropic Help — Use the Agent SDK with your plan](https://support.claude.com/en/articles/15036540).

Consequência direta para o DEILE: depender de **um único worker** (`claude-worker`) na assinatura vira gargalo de custo/quota da noite pro dia. **A escalabilidade da frota para múltiplos CLIs/providers (DeepSeek, Qwen, Gemini, GPT via OpenRouter) deixa de ser melhoria e vira requisito urgente de continuidade**: redistribuir o grosso do pipeline para workers/providers baratos e reservar o `claude-worker` (e o GPT/Codex) só para tarefas premium específicas. Este plano existe para tornar "adicionar/rotear worker" uma operação **trivial e barata**, neutralizando o lock.

**Princípio-guia deste plano:** o trabalho NÃO é "reescrever a frota" — é **"plugar novos workers numa moldura que já existe"**. Quase tudo (lease, heartbeat, dispatch por-stage, resolvers de modelo/reasoning, painel, manifests, cleanup) já está pronto no `claude-worker`. O esforço real é (a) extrair o genérico, (b) escrever **um adapter pequeno por CLI**. Se a moldura não estiver trivialmente escalável ao fim deste plano, o plano falhou. A meta de escalabilidade é explícita e verificável na **Seção 1.0**.

---

## PARTE 0 — Como a troca de modelo/worker funciona HOJE (estudo do código)

> Fonte: mapeamento do código em `infra/k8s/` e `deile/orchestration/pipeline/`. Trate file:line como aproximados (verificar no impl).

### 0.1 Eixos de configuração por-stage (já existem)
Três resolvers independentes, todos por-stage (`classify, refine, implement, pr_review, follow_ups`):

| Eixo | Resolver | Env per-stage | Env global | Settings.json | Default |
|---|---|---|---|---|---|
| **Worker** | `dispatch_resolver.resolve_stage_dispatcher()` | `DEILE_PIPELINE_DISPATCH_<STAGE>` | `DEILE_PIPELINE_DISPATCH_MODE` | `pipeline.dispatch.<stage>` | `deile-worker` |
| **Modelo** | `model_resolver.resolve_stage_model()` | `DEILE_PIPELINE_MODEL_<STAGE>` | `DEILE_PREFERRED_MODEL` | `pipeline.models.<stage>` | `None`→worker decide |
| **Reasoning** | `reasoning_resolver.resolve_stage_reasoning()` | `DEILE_PIPELINE_REASONING_<STAGE>` | `DEILE_REASONING_EFFORT` | `pipeline.reasoning.<stage>` | opinado por stage |

- `VALID_DISPATCHERS = {"deile-worker", "claude-worker"}` (`dispatch_resolver.py`). **Ponto de extensão #1.**
- Endpoints: `deile-worker→http://deile-worker:8766`, `claude-worker→http://claude-worker:8767` (override por `DEILE_*_WORKER_ENDPOINT`). **Ponto de extensão #2.**
- Os três valores viajam no **DispatchPayload** (`deile/infrastructure/deile_worker_client.py`): `preferred_model`, `preferred_reasoning`, `stage`, `branch`, `resume_session_id`, `prev_task_id`, `timeout_s`, `max_retries`, `brief`, `channel_id`. **Ponto de extensão #3** (relaxar validação `provider:model` para CLIs).

### 0.2 Quem executa o dispatch
`deile/orchestration/pipeline/implementer.py` — `WorkerImplementer` resolve `dispatcher = resolve_stage_dispatcher(stage)`, pega `endpoint = get_endpoint_for(dispatcher)`, faz `POST /v1/dispatch` (fire-and-forget `wait_for_result=False` + reconcile via `/v1/progress` no próximo tick, com `DispatchLedger`). **Genérico — funciona para qualquer worker que respeite o contrato HTTP.**

### 0.3 Maquinaria genérica do claude-worker (REAPROVEITAR)
`infra/k8s/claude_worker_server.py`: lease/heartbeat (`.lease.json`, TTL 30s, hb 5s), session-metadata (`~/.../tasks/<task_id>/session.json` para resume), workspace isolado (`$ROOT/<task_id>`), `startup_cleanup()` (GC de workdirs stale), contrato HTTP (`/v1/dispatch`, `/v1/health`, `/v1/progress`, `/v1/pod-status`). **Tudo isto é agnóstico de CLI** e vira o core compartilhado.

### 0.4 Específico-do-claude (NÃO reaproveitar — vira adapter)
argv mounting (`claude -p --permission-mode bypassPermissions --output-format json --model --effort`), parsing do `--output-format json`, OAuth handling (todo o `_claude_creds_refresh`, initContainer bootstrap-creds, CronJob renew), `_coerce_claude_effort`, `_ULTRACODE_PREAMBLE`.

### 0.5 Painel
`DispatchMatrixView` (tecla `[d]`): matriz stages × (Worker, Model, Reasoning); seta via `kubectl set env deploy/deile-pipeline DEILE_PIPELINE_*`. **Ponto de extensão #4** (incluir novos workers + buscar modelos via `/v1/models`).

### 0.6 deploy.py + manifests + Dockerfile
`K8S_DEPLOYMENTS` tuple lista os deployments; manifests numerados (`47–52` claude-worker); NetworkPolicy (`40`) com ingress-from-pipeline + egress-LLM-e-forges; Dockerfile single-image `deile-stack:local` com build-arg `WITH_BOT`. **Padrões a clonar por worker.**

---

## PARTE 1 — Arquitetura-alvo: framework de CLI workers

### 1.0 Escalabilidade — adicionar um worker novo deve ser trivial (requisito de 1ª classe)

**Meta verificável:** após este plano, adicionar um worker CLI novo = **3 artefatos + 1 linha de registro**, sem tocar no core nem no pipeline:

1. **1 arquivo adapter** `infra/k8s/cli_adapters/<kind>.py` (~80–150 linhas) implementando `CliAdapter` (5 métodos + metadados).
2. **1 bloco de install** no `Dockerfile.cli-worker` (gated por `WORKER_KIND=<kind>`).
3. **1 conjunto de manifests** gerado de **template** (`infra/k8s/manifests/templates/cli-worker.yaml.tmpl`) via `deploy.py k8s gen-worker <kind>` — preenche nome/porta/env/dirs a partir dos metadados do adapter. NÃO se escreve YAML à mão.
4. **1 linha** registrando o `kind` em `cli_adapters/__init__.py::ADAPTERS` (auto-discovery por import do pacote — idealmente nem isso: scan do diretório).

Tudo o mais (lease, heartbeat, dispatch, resolvers, painel, `/v1/models`, cleanup, NetworkPolicy base, gate pós-run, scale-to-zero) é **herdado** sem código novo. O `dispatch_resolver` deriva `VALID_DISPATCHERS` **do registro de adapters** (não de uma lista hardcoded) → registrar o adapter já o torna dispatcher válido. O painel deriva a lista de workers do mesmo registro. **Single source of truth: o registro de adapters.**

> **Auto-discovery (anti-hardcode):** `cli_adapters/` é escaneado em import; cada adapter declara `kind`, `default_port`, `auth_mode`, `supports_resume`, `supports_reasoning`, `egress_hosts`, `writable_dirs`, `auth_env_keys`. `dispatch_resolver`, painel, `deploy.py gen-worker` e a NetworkPolicy lêem desses metadados. Adicionar worker NÃO edita esses consumidores — eles iteram o registro.

**Checklist "novo worker em 1 PR" (Definition of Scalable):**
- [ ] criar `cli_adapters/<kind>.py`; [ ] `deploy.py k8s gen-worker <kind>` (gera manifests); [ ] add bloco no Dockerfile; [ ] `deploy.py k8s build-cli-workers --kind <kind>`; [ ] `k8s up`; [ ] aparece no painel + `/v1/models` responde; [ ] E2E 1 issue. **Zero edição** em `_worker_core.py`, `dispatch_resolver.py`, `implementer.py`, painel.

Um **teste de regressão de escalabilidade** (`test_worker_registry_drives_everything.py`) assegura que `VALID_DISPATCHERS`, a lista do painel e os endpoints são **derivados do registro** (falha se alguém re-hardcodar uma lista de workers em qualquer consumidor).

### 1.1 Servidor genérico `infra/k8s/cli_worker_server.py`
Extrai do `claude_worker_server.py` o **core agnóstico** (lease, heartbeat, session-meta, cleanup, HTTP, workspace) para um módulo compartilhado `infra/k8s/_worker_core.py`, e implementa o servidor genérico que delega ao **adapter** selecionado por `DEILE_CLI_WORKER_KIND`.

> **Decisão:** NÃO reescrever o `claude_worker_server.py` agora (risco). Em vez disso: (a) **extrair** o core para `_worker_core.py`, (b) o `claude_worker_server.py` passa a importar o core (refactor de baixo risco, testado), (c) o novo `cli_worker_server.py` usa o mesmo core. Claude vira "só mais um adapter" conceitualmente, mas mantém seu server dedicado por causa do OAuth (que os outros não têm). **Resultado:** core único, dois servers (claude-com-OAuth e cli-genérico), N adapters no cli-genérico.

### 1.2 Interface do adapter — `infra/k8s/cli_adapters/base.py`
```python
class CliAdapter(Protocol):
    # ---- metadados (dirigem registro/painel/manifests/netpol — single source of truth) ----
    kind: str                       # "opencode" | "codex" | ...
    default_port: int               # 8771..8776 (tabela 1.9)
    auth_mode: Literal["env", "oauth_file"]   # env=API key; oauth_file=cred mountado (claude/codex/antigravity)
    supports_resume: bool           # False → sempre fresh (aider/goose/opencode/antigravity)
    supports_reasoning: bool        # False → painel desabilita coluna Reasoning (aider/goose)
    git_strategy: Literal["cli_autocommit", "brief_driven"]
    auth_env_keys: list[str]        # API keys que o adapter consome (readiness/validação)
    egress_hosts: list[str]         # hosts p/ NetworkPolicy egress (gerado, não hardcoded)
    writable_dirs: list[str]        # p/ readOnlyRootFilesystem (mounts gerados)
    oauth: OAuthSpec | None         # quando auth_mode="oauth_file": cred_path, login_cmd, secret_name
    # ---- comportamento ----
    def build_argv(self, *, brief_path: str, model: str | None,
                   reasoning: str | None, workdir: str,
                   resume: ResumeCtx | None) -> list[str]: ...
    def env_overlay(self, *, home: str) -> dict[str, str]: ...   # HOME/XDG/CONFIG + config inline
    def parse_output(self, *, stdout: str, stderr: str, rc: int) -> WorkResult: ...
    def list_models(self) -> list[ModelInfo]: ...                # alimenta GET /v1/models (dinâmico ou catálogo)
```
- `WorkResult`: `ok, result_text, error_code, cost_usd|None`.
- `ResumeCtx`: `session_id, prev_task_id` (usado só se `supports_resume`; senão fresh).
- `OAuthSpec`: `cred_path` (ex. `~/.codex/auth.json`, `~/.claude/credentials.json`), `login_cmd` (ex. `["codex","login","--device-auth"]`), `secret_name` (K8s Secret que carrega a cred), `renewable: bool`.
- **Registro com auto-discovery**: `cli_adapters/__init__.py` escaneia o pacote e monta `ADAPTERS = {a.kind: a}`. `dispatch_resolver`, painel, `deploy.py gen-worker` e a geração de NetworkPolicy **iteram `ADAPTERS`** — nunca uma lista hardcoded. Server escolhe o adapter por `DEILE_CLI_WORKER_KIND`.

### 1.3 Modelo: como viaja e como o worker mostra os suportados
- **Viagem:** `DispatchPayload.preferred_model` deixa de exigir `provider:model` quando o destino é CLI worker. Para CLIs, o valor é o **model-id nativo do CLI** (ex.: `openrouter/anthropic/claude-3.7-sonnet`, `qwen3-coder-plus`, `gpt-5.5-codex`). **Mudança:** relaxar o validator do `preferred_model` (aceitar string livre quando `stage`/dispatcher é CLI) OU adicionar campo `cli_model: str|None` separado. **Escolha:** adicionar `cli_model` separado (não quebra o validator existente do deile-worker). O `model_resolver` ganha `resolve_stage_cli_model(stage)` lendo `DEILE_PIPELINE_MODEL_<STAGE>` como string livre.
- **"Cada worker mostra os modelos que suporta":** cada worker expõe **`GET /v1/models`** → `[{id, label, provider, context?, notes?}]`. Duas estratégias por adapter:
  - **Dinâmica** (preferida quando há comando): roda o list do CLI e parseia. `opencode models`, `aider --list-models ""`. Cacheado (TTL) porque pode tocar a rede (models.dev).
  - **Estática/catálogo**: lista curada no adapter (Codex, Qwen, Goose, Antigravity — não têm `list-models` confiável). Documentar fonte.
- O painel chama `GET /v1/models` do worker selecionado e popula o picker de modelo daquele stage. **Isto satisfaz o requisito explícito.**

### 1.4 Autonomia (sem TTY → qualquer prompt trava) — flags por CLI
| Worker | Flag/Env de autonomia total |
|---|---|
| opencode | `--dangerously-skip-permissions` **+** config `{"permission":"allow"}` via `OPENCODE_CONFIG_CONTENT` |
| codex | `--dangerously-bypass-approvals-and-sandbox` (alias `--yolo`); ou `--sandbox workspace-write` + `[sandbox_workspace_write] network_access=true` |
| qwen | `--yolo` (= `--approval-mode yolo`) + `QWEN_CODE_UNATTENDED_RETRY=1` |
| aider | `--yes-always` |
| goose | env `GOOSE_MODE=auto` |
| antigravity | `--dangerously-skip-permissions` (⚠️ não-confirmado oficialmente) |

### 1.5 Git/commit — estratégia por adapter (`git_strategy`)
- **aider** = `cli_autocommit`: roda com `--auto-commits`; o **wrapper faz `git push` + abre PR** (aider não faz push/PR). Mensagens Conventional Commits pelo weak-model. Usar `--no-attribute-author` conforme regra do projeto (sem "(aider)"/Co-Authored-By).
- **opencode/codex/qwen/goose/antigravity** = `brief_driven`: o brief instrui o agente a `git add/commit/push` via tool bash/shell (todos têm, sob auto-approve). O **wrapper valida** que houve commit+push (checa `git log`/branch remota) e, se não, faz fallback commit. **Identidade git** (`user.name`/`user.email`) + token (GITHUB_TOKEN/GITLAB_TOKEN) injetados no env.
- O **brief unificado** (`briefs.py`) ganha uma variante neutra-de-CLI (sem jargão claude) reusando o `cli_renderer` forge-agnóstico que já existe.

### 1.6 Exit code NÃO é confiável (vale p/ TODOS exceto parsing próprio)
Pesquisa confirmou: opencode/codex/qwen/goose/aider **não garantem exit-code 0/≠0 limpo** para gate. **Regra do core:** o `WorkResult.ok` é decidido pelo adapter via parse de saída **E** por um **gate pós-execução do wrapper** (git diff aplicado? push feito? `pytest` se o brief pediu?). Nunca confiar só em `$?`. (Espelha o que o pipeline já faz com o quality-gate.)

### 1.7 readOnlyRootFilesystem — dirs graváveis por CLI (todos non-root uid 10001)
| Worker | Dirs graváveis / env | Observações |
|---|---|---|
| opencode | `HOME`, `XDG_DATA_HOME=/data`; config via `OPENCODE_CONFIG_CONTENT` (inline, evita arquivo) | binário standalone; `opencode upgrade` proibido (pin) |
| codex | `CODEX_HOME=/data/codex`; workdir gravável | rust musl binary |
| qwen | `HOME` + `~/.qwen`; workdir | **node>=22** (conflito com node20 do claude → imagem própria) |
| aider | `HOME` (config/cache litellm) + workdir (history files); `--no-gitignore` | pip; precisa `git` |
| goose | `HOME`/`XDG_CONFIG_HOME` + `~/.config/goose`; **`GOOSE_DISABLE_KEYRING=1`** | binário; keyring/DBus quebra sem isso |
| antigravity | `HOME` + `~/.gemini/...`; keyring p/ OAuth (⚠️) | go binary |
Padrão K8s: montar `emptyDir`/PVC em `/home/<kind>` e `/data`; `HOME` aponta pra lá; resto read-only.

### 1.8 Estratégia de imagem — **per-tool via build-arg**
Runtimes divergem (node20 vs node22 vs pip vs binário) → **não cabe imagem única**. Criar `infra/k8s/Dockerfile.cli-worker` com `ARG WORKER_KIND` e blocos de install condicionais (mirror do `WITH_BOT`). Imagens: `deile-cli-worker-<kind>:local`. Camada-base comum (python server + git/gh/glab/kubectl + `_worker_core.py` + adapters) compartilhada; layer final instala só o CLI do kind. `deploy.py` ganha `k8s build-cli-workers [--kind <k>]`.

> **Tradeoff documentado:** 6 imagens novas (~500MB–1GB cada) em disco local. Aceitável no Rancher Desktop. Alternativa rejeitada: imagem-fat única (quebra por conflito node20/22 + bloat). Mitigação: base layer compartilhada via cache.

### 1.9 Dispatcher + endpoints + manifests por worker
- `dispatch_resolver.VALID_DISPATCHERS` += `{opencode-worker, codex-worker, qwen-worker, aider-worker, goose-worker, antigravity-worker}`.
- Endpoints: `<kind>-worker:<porta>` (porta única por worker, ex. 8771–8776). Override por `DEILE_<KIND>_WORKER_ENDPOINT`.
- Por worker, clonar o conjunto de manifests do claude-worker, **menos OAuth**: Deployment (cmd `python3 /app/cli_worker_server.py` + `DEILE_CLI_WORKER_KIND=<kind>`), Service, PVC (ou emptyDir se sem resume), allowed-repos ConfigMap (reusar o mesmo), cleanup CronJob (reusar core), bearer Secret, NetworkPolicy (ingress-from-pipeline + egress 443 LLM/forge — ajustar hosts por CLI: openrouter.ai, dashscope, api.openai.com, etc. + DNS).
- Secrets de auth: **um Secret compartilhado `cli-worker-keys`** com `OPENROUTER_API_KEY` (+ opcionalmente chaves diretas). Cada Deployment monta as env vars que seu adapter declara em `auth_env_keys()`.

### 1.10 Painel + deploy.py
- `DispatchMatrixView`: dropdown de Worker inclui os novos; ao escolher um worker num stage, busca `GET /v1/models` daquele worker p/ popular o picker de modelo; reasoning column só p/ workers que suportam (claude/codex/qwen têm; aider/goose não → desabilita).
- `deploy.py`: `K8S_DEPLOYMENTS` += novos; `k8s build-cli-workers`, `k8s up` aplica manifests novos; `k8s scale --<kind>-worker N`; `k8s gen-worker <kind>` (gera manifests do template).

### 1.11 Auth — dois modos, generalizando o padrão do claude-worker

Cada adapter declara `auth_mode`. O core/`deploy.py` tratam os dois de forma uniforme:

**(a) `auth_mode="env"` — API key (não expira; preferido para automação):** opencode, aider, goose, qwen, codex(API), antigravity(Vertex SA). O adapter declara `auth_env_keys`; o Deployment monta essas env vars do Secret compartilhado `cli-worker-keys` (ex. `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `DASHSCOPE_API_KEY`, `GITHUB_TOKEN`/`GITLAB_TOKEN`). Sem login, sem refresh. **É o caminho recomendado** — evita o inferno de refresh-token que já queimou o claude-worker.

**(b) `auth_mode="oauth_file"` — credencial OAuth montada (quando você QUER usar assinatura/OAuth):** claude, **codex (ChatGPT OAuth)**, **antigravity (Google OAuth)**. Generaliza o mecanismo do `claude-login` para **`deploy.py k8s <kind>-login`**:
1. Operador roda o login **no host** (`<adapter.oauth.login_cmd>`, ex.: `codex login --device-auth`, `claude setup-token`, `agy auth login`).
2. `deploy.py` captura o **cred file** do host (`adapter.oauth.cred_path`) → cria/atualiza o Secret `adapter.oauth.secret_name`.
3. InitContainer `bootstrap-creds` (template genérico, igual ao do claude) copia a cred do Secret → PVC writable em `cred_path` (mode 0600), permitindo refresh in-pod quando o CLI suportar.
4. Renovação: `deploy.py k8s <kind>-renew` (lightweight) onde aplicável; senão re-login.

> **Codex OAuth (confirmado na pesquisa):** `codex login` (browser) ou **`codex login --device-auth`** (device-code, headless-friendly) gravam `auth.json` sob `CODEX_HOME`. Logo, codex tem **dois modos**: `OPENAI_API_KEY` (env, não expira) **ou** OAuth ChatGPT (`auth.json` mountado via `codex-login`). Pega o mesmo problema de refresh do claude se usar OAuth → **default recomendado: API key**; OAuth é opt-in (`DEILE_CODEX_AUTH=oauth`).
>
> **Antigravity OAuth (parcial/gated):** o login padrão é **Google OAuth** (browser/device-flow) com cred no keyring — hostil a container `readOnlyRootFilesystem` sem keyring. O spike (Fase E) valida se o `agy` lê cred de **arquivo** (mountável via `antigravity-login`) ou se exige keyring/Vertex-SA. Três rotas possíveis, em ordem de preferência p/ pod: **Vertex service-account JSON** (env, mais robusta) > **OAuth file mountado** (se `agy` suportar arquivo) > OAuth keyring (inviável headless). O adapter declara a rota que o spike confirmar.

O **selector de auth por worker** é env: `DEILE_<KIND>_AUTH=env|oauth` (default `env`). `auth_mode` efetivo = o que o adapter suporta ∩ a escolha do operador.

### 1.12 Contrato HTTP unificado (o que o `implementer` envia / o que o worker responde)

Espelha o contrato do `claude-worker` (já implementado) + 1 campo. **O `cli_worker_server` aceita exatamente isto** (assim o `implementer.py` não precisa de caminho especial além de escolher o endpoint e o campo de modelo):

**`POST /v1/dispatch`** (Bearer auth) — request:
```jsonc
{
  "brief": "string (obrigatório, ≤ ~8 KiB; truncado/sentinela acima)",
  "stage": "classify|refine|implement|pr_review|follow_ups",
  "branch": "auto/issue-N | <branch> | null",
  "cli_model": "model-id NATIVO do CLI | null",   // NOVO: string livre (ex. openrouter/deepseek/deepseek-chat)
  "preferred_reasoning": "low|medium|high|... | null", // ignorado se adapter.supports_reasoning=False
  "resume_session_id": "uuid | null",            // usado só se adapter.supports_resume
  "prev_task_id": "hex16 | null",
  "wait_for_result": false,                       // fire-and-forget (default do pipeline) + reconcile
  "timeout_s": 1800,                              // resolve_stage_timeout_s
  "max_retries": 3,
  "channel_id": "string",
  "issue_number": 0
}
```
**Response** (igual ao claude-worker): `{ ok, task_id(hex16), session_id|null, returncode, stdout(≤50KiB), stderr(≤10KiB), duration_seconds, is_error, result, total_cost_usd|null, error_code|null }`.

- **Roteamento de campo de modelo no `implementer`:** se `dispatcher == "claude-worker"` → envia `preferred_model` (anthropic-only, como hoje); se `dispatcher` é um `*-worker` CLI → envia `cli_model = resolve_stage_cli_model(stage)` (string livre). **Única ramificação nova** no cliente HTTP. Isto evita relaxar/quebrar o validator `provider:model` do deile-worker.
- **`GET /v1/models`** → `{ "models": [{ "id": "string", "label": "string", "provider": "string|null", "context": int|null, "notes": "string|null" }], "source": "dynamic|catalog", "fetched_at": "iso8601" }`. Cacheado (TTL ~10min) quando `source=dynamic`.
- **`GET /v1/health`** → `{ ok, kind, auth_mode, ready }` (`ready=false` se faltar `auth_env_keys` ou cred OAuth).
- **`GET /v1/progress/{task_id}`** → snapshot mid-flight (tail do log/JSONL no PVC), igual ao claude-worker, para o reconcile do ledger.

### 1.13 Mapa de infra concreto (portas, egress, storage, réplicas)

**Portas** (sem colisão com o que já existe — deile-worker 8766, claude-worker 8767, pipeline-status 8768, monitor 8769):

| Worker | Service:porta | Endpoint env |
|---|---|---|
| opencode | `opencode-worker:8771` | `DEILE_OPENCODE_WORKER_ENDPOINT` |
| codex | `codex-worker:8772` | `DEILE_CODEX_WORKER_ENDPOINT` |
| qwen | `qwen-worker:8773` | `DEILE_QWEN_WORKER_ENDPOINT` |
| aider | `aider-worker:8774` | `DEILE_AIDER_WORKER_ENDPOINT` |
| goose | `goose-worker:8775` | `DEILE_GOOSE_WORKER_ENDPOINT` |
| antigravity | `antigravity-worker:8776` | `DEILE_ANTIGRAVITY_WORKER_ENDPOINT` |

**Egress da NetworkPolicy por worker** (gerado de `adapter.egress_hosts`; sempre + DNS:53 + forges github.com/gitlab.com:443; ingress só do `deile-pipeline`):

| Worker | Hosts LLM (443) |
|---|---|
| opencode | `openrouter.ai`, `models.dev` (catálogo; ou fixar modelos e omitir), + provider direto se usado |
| codex | `api.openai.com` (e/ou base_url do provider custom Responses-API) |
| qwen | `dashscope.aliyuncs.com` / `openrouter.ai` / base_url configurado |
| aider | `openrouter.ai` (+ `api.deepseek.com`/`generativelanguage.googleapis.com` se diretos) |
| goose | `openrouter.ai` / `api.openai.com` / host configurado |
| antigravity | Vertex (`*.googleapis.com`) ou Google OAuth hosts (gated) |

> **Regra:** preferir **OpenRouter** reduz o egress a `openrouter.ai:443` para quase todos → NetworkPolicy mínima e uniforme. Hosts diretos só quando o operador escolher provider direto.

**Storage por worker** (derivado de `auth_mode`+`supports_resume`):
- **PVC `<kind>-worker-home`** (RWO) quando `auth_mode=oauth_file` (precisa persistir cred + refresh in-pod) **ou** `supports_resume=True` (session JSONL). → claude, codex(oauth), qwen, antigravity.
- **`emptyDir`** (efêmero, mais barato) quando `auth_mode=env` **e** `supports_resume=False`. → opencode, aider, goose (e codex/qwen no modo env-only sem resume). O workdir do repo é sempre `emptyDir`/PVC gravável.
- Cleanup CronJob genérico (reusa `_worker_core.startup_cleanup`) só para workers com PVC.

**Réplicas / scale-to-zero (custo):** todo CLI worker novo nasce com **`replicas: 0`** no manifest. `k8s up` aplica todos, mas **só sobem quando o operador seleciona aquele worker num stage** (ou `k8s scale --<kind>-worker N`). Assim a frota inteira coexiste sem custo de CPU/RAM ociosa; só roda o que está em uso. O `dispatch_resolver` ao escolher um worker com 0 réplicas → o pipeline faz `scale 1` on-demand (ou avisa) — **task explícita** (ver D/F).

**Imagem (base compartilhada):** `Dockerfile.cli-worker` = stage `base` (python server + `_worker_core` + adapters + git/gh/glab/kubectl) reaproveitado via cache; stage final por `WORKER_KIND` instala só o runtime+CLI daquele kind (node22 só no qwen; binário no opencode/goose/antigravity; pip no aider; etc.). Tag `deile-cli-worker-<kind>:local`.

---

## PARTE 2 — Specs por worker (o "como" literal)

> **Auth (ver 1.11 para o mecanismo unificado):** cada worker suporta **`env` (API key, default, não expira)** e — onde o CLI permite — **`oauth_file` (opt-in via `DEILE_<KIND>_AUTH=oauth`)**. OAuth disponível em **claude** (setup-token/credentials), **codex** (`codex login --device-auth`) e **antigravity** (Google OAuth, gated). Recomendação geral: **API key via OpenRouter** (uma chave → DeepSeek/Qwen/Gemini/Claude/GPT) para o grosso barato; OAuth/assinatura reservado para claude/codex premium quando você quiser usar a assinatura em vez de pagar token.

### 2.1 opencode-worker  ⭐ (Tier 1 — primeiro a implementar)
- **Headless:** `opencode run --dir <workdir> -m <model> --dangerously-skip-permissions --format json -f <brief_path> "Implemente conforme o brief anexado."`
- **Modelo:** `-m provider/model` (ex. `openrouter/anthropic/claude-3.7-sonnet`). Config inline: `OPENCODE_CONFIG_CONTENT='{"$schema":"https://opencode.ai/config.json","permission":"allow"}'`.
- **list_models:** `opencode models` (dinâmico; cache TTL; `--refresh` toca models.dev → whitelist models.dev no egress OU catálogo estático fallback).
- **Auth:** `OPENROUTER_API_KEY` (ou `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`DEEPSEEK_API_KEY`). `{env:VAR}` na config.
- **Dirs:** `HOME`, `XDG_DATA_HOME`. **git:** brief_driven. **Pin** versão; nunca `upgrade`.
- **Gotcha:** confirmar `--dangerously-skip-permissions` existe na versão pinada (`opencode run --help`); fallback = só `permission:"allow"`. stdin não-oficial → usar `-f`.

### 2.2 codex-worker (Tier 2)
- **Headless:** `codex exec --sandbox workspace-write --json -o /work/out.json - < brief` (stdin) **ou** `codex exec --dangerously-bypass-approvals-and-sandbox --json "<brief>"`.
- **Modelo:** `-m gpt-5.5-codex` (ou via config.toml `model=`). Provider custom/OpenRouter: bloco `[model_providers.<id>]` com `base_url`+`env_key`, **mas `wire_api="responses"` é obrigatório** → **só providers que falam Responses API** (OpenRouter: validar por modelo; muitos só Chat Completions → **podem não funcionar**). ⚠️ Documentar: codex-worker é melhor com OpenAI direto.
- **list_models:** sem comando → **catálogo estático** no adapter (gpt-5.5, gpt-5.5-codex, …).
- **Auth (dois modos):** **(env, default)** `OPENAI_API_KEY` (não expira) — robusto. **(oauth, opt-in `DEILE_CODEX_AUTH=oauth`)** `codex login --device-auth` no host → `auth.json` sob `CODEX_HOME` → mountado via `deploy.py k8s codex-login` (mesmo mecanismo do claude; herda o risco de refresh). Permite usar a **assinatura ChatGPT** em vez de API key. **Dirs:** `CODEX_HOME` (gravável). **git:** brief_driven (não auto-commita).
- **Gotcha:** sempre `codex exec` (nunca `codex` puro — pode panicar sem TTY). Exit-code grosso → parse JSONL. OAuth ChatGPT auto-refresh antes de expirar (melhor que o claude), mas API key continua sendo o mais durável.

### 2.3 qwen-worker (Tier 2 — melhor custo)
- **Headless:** `qwen -p "<brief>" --yolo --output-format json` (+ `QWEN_CODE_UNATTENDED_RETRY=1`, capar com timeout do pod).
- **Modelo:** `OPENAI_MODEL=qwen3-coder-plus` + tríade `OPENAI_API_KEY`+`OPENAI_BASE_URL`. Via **OpenRouter**: `OPENAI_BASE_URL=https://openrouter.ai/api/v1`, `OPENAI_MODEL=qwen/qwen3-coder`. Multi-provider de fato via base_url.
- **list_models:** sem comando → catálogo estático (qwen3-coder-plus/next/480b + o que o base_url expõe).
- **Auth:** `OPENAI_API_KEY` (OAuth free tier **morto** desde 2026-04-15). **Dirs:** `HOME`+`~/.qwen`. **node>=22** → imagem própria. **git:** brief_driven.

### 2.4 aider-worker (Tier 1 — cirúrgico)
- **Headless:** `aider --model <prov/model> --message-file <brief> --yes-always --no-stream --no-pretty --analytics-disable --no-check-update --no-gitignore --auto-commits` (+ `--auto-test --test-cmd "pytest -q"` p/ loop de verificação).
- **Modelo:** `--model openrouter/anthropic/claude-3.7-sonnet` | `deepseek/deepseek-chat` | `gemini/...`. `--weak-model` p/ commit msgs barato.
- **list_models:** `aider --list-models ""` (dinâmico).
- **Auth:** `OPENROUTER_API_KEY`/`DEEPSEEK_API_KEY`/etc. **Dirs:** `HOME`+workdir. precisa `git`. **git:** `cli_autocommit` (auto-commit; wrapper faz push+PR; `--no-attribute-*` p/ regra do projeto).
- **Gotcha:** `--message --yes-always` é single-pass → **gate pós-run obrigatório** (build/test) porque pode commitar código quebrado.

### 2.5 goose-worker (Tier 1)
- **Headless:** `GOOSE_MODE=auto goose run --no-session -q --output-format json -t "<brief>"` (ou `-i -` stdin). `--max-turns <teto>` baixo p/ custo.
- **Modelo:** `GOOSE_PROVIDER` + `GOOSE_MODEL` (ou `--provider/--model` por invocação). OpenRouter: `GOOSE_PROVIDER=openrouter`+`OPENROUTER_API_KEY`+`GOOSE_MODEL=anthropic/claude-sonnet-4`. OpenAI-compat custom: `GOOSE_PROVIDER=openai`+`OPENAI_HOST`.
- **list_models:** sem comando → catálogo estático.
- **Auth:** chave do provider. **`GOOSE_DISABLE_KEYRING=1` obrigatório** (DBus quebra). **Dirs:** `~/.config/goose` gravável + `HOME`. **git:** brief_driven (Developer extension dá shell+text_editor).
- **Gotcha:** instalar com `CONFIGURE=false`; exit-code não-confiável; `GOOSE_MODE=auto` reportado falho com provider `claude-code` (#3386) — usar com OpenRouter/OpenAI.

### 2.6 antigravity-worker (Tier 3 — ⚠️ GATED por spike)
- **Status honesto:** closed-source, **auth headless por API key não suportada no consumer** (issue #78 aberta), Google-locked (modelos não-Google só via harness Google), `--print` sem conversation-ID por chamada (#7). Doc oficial JS-only (flags não-confirmadas literalmente).
- **GATE obrigatório (Task dedicada, ANTES de qualquer manifest):** spike num container — `agy --help`, `agy auth --help`, testar auth headless via **Vertex/Gemini Enterprise service-account** (`GOOGLE_APPLICATION_CREDENTIALS` + `GOOGLE_CLOUD_PROJECT`), e `agy -p "echo" --output-format json`. **Só prosseguir se o spike provar auth headless + one-shot determinístico.** Senão: **não implementar** — usar Gemini via OpenRouter/OpenCode ou via deile-worker (provider google) — registrar como "bloqueado, reavaliar quando #78 fechar".
- **Headless (SE spike passar):** `agy -p "<brief>" --dangerously-skip-permissions --output-format json --print-timeout <t>`. **Modelo:** `-m gemini-3.1-pro` (catálogo estático). **Dirs:** `HOME`+`~/.gemini`. **git:** brief_driven.
- **Auth (3 rotas, ordem de preferência p/ pod — o spike define qual):** **(1, preferida)** Vertex/Gemini Enterprise **service-account JSON** (`auth_mode=env`: `GOOGLE_APPLICATION_CREDENTIALS` + `GOOGLE_CLOUD_PROJECT` via Secret) — robusta, não expira como OAuth consumer. **(2)** **Google OAuth file** (`auth_mode=oauth_file` via `deploy.py k8s antigravity-login` → `agy auth login` device-flow no host → cred mountada) — **SÓ se o spike provar que o `agy` lê cred de arquivo** (não só keyring). **(3)** OAuth keyring — **inviável** em pod (sem DBus/keyring). Issue oficial #78 (API-key headless consumer) ainda aberta → não contar com `GEMINI_API_KEY` simples. **O adapter declara a rota que o spike confirmar; se nenhuma viável → worker não entra (usar Gemini via OpenRouter/deile-worker).**

### 2.7 claude-worker (já existe — agora "um entre vários")
- Mantém server dedicado (`claude_worker_server.py`) por causa do OAuth. Passa a importar `_worker_core.py`. No painel, aparece como mais um dispatcher. Usado pelo operador **pra tarefas específicas** (review crítico, arquitetura) — não default. Auth permanece o caminho oficial (setup-token quando migrar; ver plano OAuth separado já discutido).

---

## PARTE 3 — Plano SEPARADO: DEILE CLI + OpenRouter

> Independente da frota CLI; habilita o **deile-worker in-process** e o **DEILE CLI local** a falar OpenRouter (uma chave → todos os providers), e serve de provider unificador para os CLI workers.

### 3.1 Registrar OpenRouter no DEILE
- `deile/config/model_providers.yaml`: adicionar provider `openrouter` (OpenAI-compatible; `base_url=https://openrouter.ai/api/v1`; `api_key_env=OPENROUTER_API_KEY`; lista de modelos curada DeepSeek/Qwen/Gemini/Claude/GPT com tiers/custo).
- `deile/core/models/bootstrap.py` (`bootstrap_providers`): registrar o provider OpenRouter (reusar o adapter OpenAI-compatible existente; OpenRouter é OpenAI-compatible). Confirmar via context7/SDK que o provider OpenAI aceita `base_url` custom.
- Headers OpenRouter opcionais (`HTTP-Referer`, `X-Title`) — best-effort.

### 3.2 Secret + propagação
- `.env`: `OPENROUTER_API_KEY` (gitignored). `deploy.py k8s up`: propaga p/ Secret `deile-secrets` + monta no deile-worker e (se usado) no DEILE CLI. **É segredo do operador → só Secret, nunca no repo.**

### 3.3 Roteamento por-stage barato
- Per-stage models passam a aceitar `openrouter:deepseek/deepseek-chat` etc. Documentar tabela de custo (DeepSeek $0,78/M out vs Opus) no painel/doc p/ decisão consciente.

---

## PARTE 4 — Task breakdown (TDD, bite-sized, fases)

> **Regra de ouro p/ "funcionar de primeira" (a pesquisa mostrou que docs divergem dos binários):** ANTES de escrever cada adapter/manifest, rodar um **pré-flight de smoke** num container descartável com a **versão pinada** do CLI: `<cli> --help` + `<cli> <subcomando-headless> --help` + um one-shot trivial ("escreva hello.txt") confirmando os flags exatos (ex.: opencode `--dangerously-skip-permissions` existe? qwen aceita `-m` ou só `--model`/`OPENAI_MODEL`? antigravity `agy --help` real?). O adapter é escrito **contra o `--help` observado**, não contra a doc. Cada Fase C/D/E começa por esse pré-flight (sub-passo `.0`).

### Gate de sucesso pós-run (no core — vale p/ todos, exit-code não basta)
`WorkResult.ok = adapter.parse_output(...).ok AND wrapper_gate()`, onde `wrapper_gate()` checa, na ordem do `git_strategy`:
- **brief_driven:** houve **commit novo desde o início do dispatch** (`git rev-list <base>..HEAD` > 0) **E** o branch foi **pushado** (`git ls-remote` confirma) — senão `ok=false, error_code=NO_PUSH`. Fallback opcional: wrapper commita+pusha o working tree sujo e marca `error_code=WRAPPER_COMMITTED` (degradado mas não perdido).
- **cli_autocommit (aider):** há commit local (aider fez) → wrapper só **pusha** + (se brief pediu) roda `test_cmd`; gate falha se push falhar ou teste vermelho.
- Em ambos: se o brief exigiu suíte verde (implement/pr_review), roda o `test_cmd` e exige rc=0. (Espelha o quality-gate do pipeline.)

### Controle de custo (CLIs não têm `--max-budget-usd` como o claude)
Só o claude-worker tem cap nativo de orçamento. Para os CLIs, o controle de custo é: **(a)** `timeout_s` do pod (`DEILE_<KIND>_WORKER_TASK_TIMEOUT_S`), **(b)** teto de turns onde existe (`goose --max-turns`, `qwen` retry capado), **(c)** escolha de modelo barato por stage (DeepSeek/Qwen via OpenRouter), **(d)** OpenRouter dá teto/visibilidade de gasto por chave. Documentar que **não há cap por-task em USD** nesses CLIs → confiar em timeout + modelo barato. Task: `_worker_core` aplica `timeout_s` matando o subprocess (igual claude-worker rc=124).

### Fase A — Core compartilhado (fundação; sem isto nada funciona)
- [ ] **A1** Criar `infra/k8s/_worker_core.py` extraindo do `claude_worker_server.py`: lease/heartbeat, session-meta, workspace, `startup_cleanup`, helpers HTTP (auth bearer, health). **Teste:** mover/duplicar os testes existentes do claude-worker que cobrem essas funções, apontando p/ o core. Suíte verde.
- [ ] **A2** Refatorar `claude_worker_server.py` p/ importar do `_worker_core.py` (sem mudança de comportamento). **Teste:** suíte do claude-worker continua verde; smoke `/v1/health`.
- [ ] **A3** Definir `cli_adapters/base.py` (`CliAdapter` Protocol, `WorkResult`, `ResumeCtx`, `ModelInfo`) + registro. **Teste:** unit do registro + um FakeAdapter.
- [ ] **A4** `cli_worker_server.py`: server genérico que carrega adapter por `DEILE_CLI_WORKER_KIND`, expõe `/v1/dispatch`, `/v1/health`, `/v1/progress`, **`/v1/models`**. Gate pós-run (commit/push/test) no core. **Teste:** dispatch contra FakeAdapter (sem rede); `/v1/models` retorna lista do adapter; gate detecta "sem commit".
- [ ] **A5 (escalabilidade)** Auto-discovery em `cli_adapters/__init__.py` (scan do pacote → `ADAPTERS`). Template `infra/k8s/manifests/templates/cli-worker.yaml.tmpl` + `deploy.py k8s gen-worker <kind>` (preenche do metadado do adapter: porta, env, dirs graváveis, egress hosts, auth). **Teste de regressão `test_worker_registry_drives_everything.py`:** `dispatch_resolver.VALID_DISPATCHERS`, lista do painel e endpoints são derivados de `ADAPTERS` (falha se re-hardcodarem) + `gen-worker` produz YAML válido (kubeval/parse).
- [ ] **A6 (auth unificada)** No core: `auth_mode env|oauth_file` + `OAuthSpec`. `deploy.py` generaliza `claude-login`→`<kind>-login`/`<kind>-renew` (captura cred do host → Secret → initContainer→PVC), e monta `auth_env_keys` do Secret `cli-worker-keys` quando `env`. Selector `DEILE_<KIND>_AUTH`. **Teste:** unit do selector (env vs oauth) + mock do bootstrap de cred (sem tocar host real).

### Fase B — Integração de roteamento (pipeline enxerga os novos workers)
- [ ] **B1** `dispatch_resolver.py`: estender `VALID_DISPATCHERS` + aliases + endpoints (`<kind>-worker:<porta>`, env `DEILE_<KIND>_WORKER_ENDPOINT`). **Teste:** `resolve_stage_dispatcher` aceita cada novo kind; endpoint resolve.
- [ ] **B2** `model_resolver.py`: `resolve_stage_cli_model(stage)` (string livre). `DispatchPayload`: campo `cli_model: str|None` (não quebra validator `provider:model`). **Teste:** payload aceita cli_model; deile-worker ignora; cli-worker usa.
- [ ] **B3** `implementer.py`/`stages.py`: única ramificação nova no cliente HTTP — `claude-worker`→`preferred_model`; `*-worker` CLI→`cli_model=resolve_stage_cli_model(stage)`. Endpoint via `get_endpoint_for` (B1). **Teste:** mock HTTP confirma POST no endpoint certo com o campo de modelo certo (preferred_model vs cli_model) por dispatcher.
- [ ] **B4** Brief neutro-de-CLI: variante em `briefs.py` reusando `cli_renderer.render_brief_cmds(forge)` (forge-agnóstico já existe), sem jargão claude/ultracode, instruindo commit+push (git_strategy=brief_driven) ou deixando o aider auto-commitar. **Teste:** o brief renderiza p/ GH e GL; não contém termos claude-specíficos; contém instrução de push quando brief_driven.
- [ ] **B5 (scale-to-zero)** Workers nascem `replicas:0`. Antes de dispatchar, o `implementer` garante ≥1 réplica do worker-alvo (kubectl scale on-demand via SA do pipeline, OU health-probe + erro claro "worker escalado a 0; rode k8s scale"). Decidir entre auto-scale vs aviso — **default: auto-scale 1 com cooldown**, reusando RBAC do pipeline. **Teste:** mock — dispatcher com 0 réplicas → ação de scale disparada (ou erro instrutivo); cooldown evita flapping.

### Fase C — Imagem + um worker piloto (opencode) ponta-a-ponta
- [ ] **C1** `infra/k8s/Dockerfile.cli-worker` com `ARG WORKER_KIND` (base comum + install condicional). Implementar bloco `opencode` (binário pinado). **Teste:** build `--build-arg WORKER_KIND=opencode` verde; `opencode --version` no container.
- [ ] **C2** `cli_adapters/opencode.py` (build_argv, env_overlay, parse_output, list_models via `opencode models`, auth_env_keys, writable_dirs, git_strategy=brief_driven). **Teste:** unit de build_argv (flags exatas) + parse de saída JSON real capturada.
- [ ] **C3** Manifests `opencode-worker` (Deployment+Service+PVC+bearer+NetworkPolicy; reusar allowed-repos ConfigMap). `deploy.py` K8S_DEPLOYMENTS += ; `k8s build-cli-workers --kind opencode`. **Teste:** `k8s up` sobe pod Ready; `/v1/health` 200; `/v1/models` lista.
- [ ] **C4** **E2E piloto:** apontar `DEILE_PIPELINE_DISPATCH_IMPLEMENT=opencode-worker` + `DEILE_PIPELINE_MODEL_IMPLEMENT=openrouter/deepseek/deepseek-chat`, soltar uma issue simples, observar PR aberta pelo opencode-worker. **Prova real.** Reverter dispatch ao normal após.

### Fase D — Demais adapters (replicar o padrão C2+C3, um por vez, cada um com E2E)
- [ ] **D1** aider-worker (git_strategy=cli_autocommit; gate build/test; `--no-attribute-*`). E2E.
- [ ] **D2** goose-worker (`GOOSE_DISABLE_KEYRING=1`, `CONFIGURE=false`, `GOOSE_MODE=auto`). E2E.
- [ ] **D3** qwen-worker (imagem node22; tríade OPENAI_*; OpenRouter). E2E.
- [ ] **D4** codex-worker (OPENAI_API_KEY; `wire_api=responses` caveat documentado; preferir OpenAI direto). E2E.

### Fase E — Antigravity (GATED)
- [ ] **E1** **SPIKE** (não-código): instalar `agy` num container, validar auth headless (Vertex service-account) + `agy -p ... --output-format json`. **Decisão de prosseguir/abortar registrada no plano.**
- [ ] **E2** (só se E1 passar) adapter + manifests antigravity-worker (Vertex SA Secret). E2E. Senão: documentar bloqueio + usar Gemini via OpenRouter.

### Fase F — Painel + OpenRouter no DEILE (Parte 3) + docs
- [ ] **F1** `DispatchMatrixView`: incluir novos workers; picker de modelo via `GET /v1/models`; reasoning só onde suportado. **Teste:** test do painel (largura adaptativa + workers listados).
- [ ] **F2** Parte 3: OpenRouter provider no `model_providers.yaml` + `bootstrap_providers` + Secret propagation. **Teste:** bootstrap registra openrouter; smoke 1-msg via OpenRouter (custo mínimo).
- [ ] **F3** Docs: `docs/system_design/` nova seção "Frota multi-CLI" + atualizar CLAUDE.md (mapa de workers, env vars, portas). Decisão em `DECISOES.md`.

### Fase G — Validação final
- [ ] **G1** Suíte completa verde (`pytest deile/tests/ -q` com coverage gate). Multi-seed ordering (0/1/2/42/last).
- [ ] **G2** `k8s up` do zero sobe a frota inteira; cada worker `/v1/health` + `/v1/models`; matriz no painel troca worker×model×reasoning por stage e o pipeline respeita.
- [ ] **G3** Commit por fase (revisão + 100% verde antes de cada commit — protocolo multi-provider do operador). PRs pequenas por fase.

---

## PARTE 5 — Revisão cética (riscos, inconsistências, correções)

> Auto-crítica "onde isso quebra?" — feita após escrever o plano.

1. **Conflito de runtime Node (qwen22 vs claude20)** → RESOLVIDO por imagens per-tool (1.8). Não tentar imagem única.
2. **Exit code não-confiável em TODOS** → RESOLVIDO por gate pós-run no core (1.6); nunca confiar em `$?`. Risco residual: detectar "sucesso real" exige heurística (commit+push presente / testes verdes) — encapsular no core e cobrir por teste.
3. **Modelo `provider:model` vs CLI-native** → RESOLVIDO por campo `cli_model` separado (não quebra validator do deile-worker). Risco: painel precisa saber se um stage usa CLI worker (string livre) ou deile-worker (provider:model) → o picker decide pelo worker selecionado.
4. **Codex `wire_api=responses`** limita OpenRouter/OpenAI-compat → codex-worker é confiável **só com OpenAI direto**; documentado; não vender codex+OpenRouter sem validar por modelo. (Inconsistência com "tudo via OpenRouter" — corrigida: codex é exceção.)
5. **Antigravity auth headless** pode não existir no consumer → GATED por spike (Fase E); plano NÃO assume que funciona. Honesto.
6. **Goose `GOOSE_MODE=auto` + provider claude-code** falho (#3386) → goose-worker usa OpenRouter/OpenAI, não claude-code provider.
7. **opencode `--dangerously-skip-permissions`** divergência doc×issue → validar na versão pinada; fallback `permission:"allow"`. Encodado como gotcha + teste de smoke `--help`.
8. **readOnlyRootFilesystem** quebra cada CLI de um jeito (keyring goose, XDG opencode, CODEX_HOME, ~/.qwen) → tabela 1.7 + `writable_dirs()` por adapter + teste de container que escreve nos dirs.
9. **models.dev / list dinâmico toca rede** → NetworkPolicy egress precisa whitelist (models.dev p/ opencode; openrouter.ai/dashscope/api.openai.com por worker) + fallback catálogo estático se rede negar. Risco: `/v1/models` lento/falha → cache TTL + fallback.
10. **git push + token + identidade** em cada worker → env GITHUB_TOKEN/GITLAB_TOKEN + git config no core; allowed-repos ConfigMap reusado; NetworkPolicy egress p/ forge. Sem isso o brief_driven não consegue push.
11. **Custo de manutenção (6 imagens + 6 deployments)** → mitigar com base-layer compartilhada + `deploy.py` automatizando build/scale; workers escaláveis a 0 quando não usados (só sobe o que o operador escolher por stage). **Não precisa rodar todos sempre.**
12. **Segurança/threat-model** → cada CLI roda `--yolo`/bypass num pod isolado (mesmo modelo do claude-worker, Decisão #29). Prompt-injection→exfil via canais legítimos permanece (git push em repo whitelisted). Reusar NetworkPolicy + allowed-repos + audit. Não regride a postura atual.
13. **Resume/threading** → só claude/codex/qwen têm resume real; aider/goose/opencode/antigravity → **fresh sempre** (adapter declara `supports_resume=False`); o brief lê `.deile-progress.md` (já existe) p/ contexto natural. Antigravity #7 (sem conv-id) reforça fresh-only.
14. **DispatchPayload tem duas formas hoje** (deile-worker Pydantic vs claude-worker request) → o cli_worker adota o contrato claude-worker-style (brief/stage/branch/cli_model/...). Unificação total fica como FU; não bloquear.
15. **Reasoning** só p/ claude/codex(`model_reasoning_effort`)/qwen(parcial) → coluna do painel desabilita onde não há; `reasoning_resolver` retorna mas adapter ignora se não suporta.
16. **Doc ≠ binário** (confirmado: opencode flag, antigravity flags JS-only, qwen `-m`) → **pré-flight de smoke obrigatório** por CLI na versão pinada antes de escrever adapter (Regra de ouro, Parte 4). Adapter escrito contra `--help` real.
17. **Sem cap de custo por-task** nos CLIs (só claude tem `--max-budget-usd`) → mitigado por timeout do pod + max-turns + modelo barato + teto OpenRouter; documentado, não há solução nativa.
18. **Scale-to-zero race:** dispatcher escolhe worker com 0 réplicas → B5 garante scale 1 on-demand + cooldown; risco de cold-start (pull de imagem) na 1ª chamada → readiness probe + retry do reconcile cobre.
19. **Codex OAuth × `wire_api=responses`:** OAuth ChatGPT do codex fala Responses API (ok); mas provider custom/OpenRouter no codex só serve se falar Responses → codex+OpenRouter continua arriscado mesmo com OAuth. OAuth do codex resolve auth, não o limite de provider. Documentado em 2.2.
20. **`gen-worker` template precisa cobrir os 2 storage modes + 2 auth modes** → o template é condicional (PVC vs emptyDir; env vs oauth_file initContainer). Risco: template complexo → testar `gen-worker` p/ cada combinação (parse/kubeval) em A5.
21. **Dois servers (claude com OAuth + cli genérico) podem divergir** do core ao longo do tempo → A1/A2 extraem o core e fazem o claude IMPORTAR dele (não duplicar); teste garante claude-worker verde pós-extração.

**Inconsistência corrigida no texto:** a Parte 2 dizia "tudo via OpenRouter"; o item 4/2.2 deixa claro que **codex é exceção** (OpenAI direto). E **antigravity não é via OpenRouter** (Google-locked) — corrigido para Vertex-SA-gated.

---

## PARTE 6 — Ordem de implementação recomendada (resumo executável)
1. **Fase A** (core + base + server genérico) — fundação.
2. **Fase B** (resolver + payload) — pipeline enxerga workers.
3. **Fase C** (opencode piloto E2E) — prova o framework inteiro com 1 worker barato (OpenRouter→DeepSeek).
4. **Fase D** (aider, goose, qwen, codex) — replicar, 1 por vez, cada um com E2E.
5. **Fase E** (antigravity) — só após spike de auth passar; senão pular.
6. **Fase F** (painel + OpenRouter no DEILE + docs).
7. **Fase G** (validação full + commits por fase).

**Claude-worker:** intocado funcionalmente (só ganha o `_worker_core.py` por baixo); segue como worker premium para tarefas específicas.
